"""How answers appear: streamed Markdown with reasoning and edit blocks hidden, then diffs."""

import difflib
import re
from pathlib import Path

from aider.mdstream import MarkdownStream
from rich.markup import escape
from rich.text import Text

from .config import BULLET, GREEN, MUTED, RED
from .term import SPIN, UI, console, out, step


class BulletStream(MarkdownStream):
    """Assistant replies start with a bullet, like Claude's CLI. Raw edit
    blocks are hidden while they stream; the turn ends with a real diff."""

    def update(self, text, final=False):
        if UI.quiet:
            return
        text = hide_reasoning(text)
        if not text.strip() and not final:
            return  # still reasoning: keep the spinner going
        SPIN.stop()
        text = re.sub(r"\*?SEARCH/REPLACE\*? blocks?", "edit", hide_edit_blocks(text))
        super().update(_bullet(text), final)


# Reasoning models think before answering. Aider rewrites the model's
# reasoning tag (from the catalog, `think` by default) into these markers;
# raw tags are handled too, in case a model's template opens the tag itself.
# Opening: Aider's THINKING marker, a raw <think>/<thinking> tag, or Aider's
# internal <thinking-content-…> tag (Aider 0.86 opens with that one but closes
# with the configured tag, so the opening is never converted; seen with
# gpt-oss, whose reasoning arrives as SGLang's separate reasoning_content).
# Closing: the ANSWER marker, a raw closing tag, or nothing yet (streaming).
_TAG = r"(?:think(?:ing)?|thinking-content-[0-9a-f]+)"
_REASONING = re.compile(
    rf"(?s)(?:-+\n► \*\*THINKING\*\*|<{_TAG}>).*?(?:-+\n► \*\*ANSWER\*\*\s*|</{_TAG}>\s*|$)"
)


def hide_reasoning(text: str) -> str:
    if "</think>" in text and "<think>" not in text:
        text = text.split("</think>", 1)[1]
    return _REASONING.sub("", text).lstrip("\n")


# Aider picks ```, ````, <source>, <code>, <pre>, <codeblock> or <sourcecode>
# fences depending on what the files in the chat contain.
_OPEN = r"(?:`{3,}[^\n]*|<(?:source|code|pre|codeblock|sourcecode)>[^\n]*)"
_CLOSE = r"(?:`{3,}|</(?:source|code|pre|codeblock|sourcecode)>)"
_FENCE_BLOCK = re.compile(
    rf"(?ms)^[^\n`<]*\n?{_OPEN}\n<<<<<<< SEARCH\n.*?^>>>>>>> REPLACE[^\n]*\n{_CLOSE}[^\n]*\n?"
)
_OPEN_LINE = re.compile(rf"^{_OPEN}$")
_MARKER = "<<<<<<< SEARCH"


def hide_edit_blocks(text: str) -> str:
    """Removes complete SEARCH/REPLACE blocks (with the filename line before
    them), and hides a trailing block that is still streaming in."""
    text = _FENCE_BLOCK.sub("", text)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not _OPEN_LINE.match(line):
            continue
        rest = "\n".join(lines[i + 1 :])
        if rest.startswith(_MARKER) or (rest and _MARKER.startswith(rest)) or (not rest and i == len(lines) - 1):
            # Unfinished edit block: drop it and its filename line.
            start = i - 1 if i > 0 and lines[i - 1].strip() and "`" not in lines[i - 1] else i
            return "\n".join(lines[:start]).rstrip() + "\n"
    return text


def _bullet(text: str) -> str:
    t = text.lstrip("\n")
    if not t:
        return t
    # Markdown blocks (code fences, headings, lists, tables) can't share a line.
    if t[0] in "#`|>-*" or re.match(r"^\d+[.)] ", t):
        return f"{BULLET}\n\n{t}"
    return f"{BULLET} {t}"


# ---------------------------------------------------------------------------
# Diffs of what the agent changed this turn
# ---------------------------------------------------------------------------


def snapshot(coder) -> dict:
    files = {}
    for abs_path in coder.abs_fnames:
        try:
            files[abs_path] = Path(abs_path).read_text(encoding=coder.io.encoding)
        except (OSError, UnicodeDecodeError):
            files[abs_path] = None
    return files


def show_edits(coder, before: dict, max_lines: int = 40, edited=None) -> None:
    for rel in sorted(edited if edited is not None else (coder.aider_edited_files or ())):
        abs_path = coder.abs_root_path(rel)
        old = UI.before.get(str(Path(abs_path).resolve()), before.get(abs_path)) or ""
        try:
            new = Path(abs_path).read_text(encoding=coder.io.encoding)
        except (OSError, UnicodeDecodeError):
            continue
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1))[2:]
        adds = sum(1 for d in diff if d.startswith("+"))
        dels = sum(1 for d in diff if d.startswith("-"))
        verb = "Create" if not old else "Update"
        out(f"\n[{GREEN}]{BULLET}[/] [bold]{verb}[/]({escape(rel)})")
        step(f"{'Created' if not old else 'Updated'} [bold]{escape(rel)}[/] with "
             f"[{GREEN}]{adds} addition{'s' if adds != 1 else ''}[/] and "
             f"[{RED}]{dels} removal{'s' if dels != 1 else ''}[/]")
        shown = 0
        for d in diff:
            if shown >= max_lines:
                out(f"       [{MUTED}]… {len(diff) - shown} more lines (git diff {escape(rel)})[/]")
                break
            if d.startswith("@@"):
                out(f"       [{MUTED}]{escape(d)}[/]")
            elif d.startswith("+"):
                console.print(Text("       " + d, style=f"{GREEN} on #0f2a20"))
            elif d.startswith("-"):
                console.print(Text("       " + d, style=f"{RED} on #2a1215"))
            else:
                console.print(Text("       " + d, style=MUTED))
            shown += 1
