"""OpenPhalanx chat: a terminal frontend over Aider's editing engine.

`oppx` embeds this file and runs it with the Python interpreter of the
installed Aider, so `aider`, `prompt_toolkit` and `rich` are importable. It
lets Aider's own `main()` build the coder (repo map, edit formats, model
settings) from the arguments oppx passes, but swaps in our InputOutput class
first, then drives `coder.run_one()` from our own prompt. Every line on screen,
every prompt and every confirmation goes through this file.

Environment from oppx: OPENAI_API_BASE / OPENAI_API_KEY (the loopback proxy),
OPPX_SERVER, OPPX_CONTEXT, OPPX_WEB ("1"/"0"), OPPX_VERSION, OPPX_BIN.
"""

import difflib
import os
import re
import shlex
import sys
import threading
import time
from pathlib import Path

import aider
import aider.coders.base_coder as base_coder
import aider.main as aider_main
from aider.commands import SwitchCoder
from aider.io import AutoCompleter, InputOutput
from aider.mdstream import MarkdownStream
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markup import escape
from rich.text import Text

TESTED_AIDER = "0.86"  # internals this file relies on; other versions get a warning

ACCENT = "#5EEAD4"
MUTED = "#8A95AB"
GREEN = "#34D399"
RED = "#F87171"
YELLOW = "#FBBF24"
BULLET = "⏺"
ELBOW = "⎿"

SERVER = os.environ.get("OPPX_SERVER", "server")
CONTEXT = int(os.environ.get("OPPX_CONTEXT", "32768"))
WEB = os.environ.get("OPPX_WEB", "1") == "1"
VERSION = os.environ.get("OPPX_VERSION", "")
OPPX_BIN = os.environ.get("OPPX_BIN", "oppx")
MODEL_LABEL = "openphalanx-coder"

console = Console(highlight=False)

# Aider status lines that mean nothing to an OpenPhalanx user.
SUPPRESS = [
    re.compile(p)
    for p in (
        r"^Aider v",
        r"^(Main|Weak|Editor) model:",
        r"^Model:",
        r"^Git repo:",
        r"^Repo-map:",
        r"^Tokens:",
        r"^Cost:",
        r"aider\.chat",
        r"^Use /help",
        r"Input is not a terminal",
        r"^You can use /undo",
        r"^Added \.aider",
        r"^Restored previous conversation",
        r"^Update git (name|email)",
        r"^\^C KeyboardInterrupt",
        r"^Applied edit to ",  # shown as a diff after the turn instead
    )
]


def out(markup: str) -> None:
    console.print(markup, soft_wrap=False)


def step(markup: str, color: str = MUTED) -> None:
    """An indented result line under the current step, Claude style."""
    out(f"  [{color}]{ELBOW}[/]  {markup}")


class OppxIO(InputOutput):
    """Aider's output and prompts, restyled and filtered."""

    def __init__(self, *args, **kwargs):
        # Aider passes `pretty` as the first positional argument.
        if args:
            args = (True,) + tuple(args[1:])
        else:
            kwargs["pretty"] = True
        kwargs["fancy_input"] = False  # we run our own prompt_toolkit session
        super().__init__(*args, **kwargs)
        self._session = None
        self.coder = None  # set by the main loop, for the status bar

    # ---- output -----------------------------------------------------------
    def _route(self, message: str, color: str) -> None:
        for raw in str(message).splitlines() or [""]:
            line = raw.rstrip()
            if not line.strip() or any(p.search(line) for p in SUPPRESS):
                continue
            m = re.match(r"^Added (.+?) to the chat\.?$", line)
            if m:
                step(f"Read [bold]{escape(m.group(1))}[/]")
                continue
            m = re.match(r"^Creating empty file (.+)$", line)
            if m:
                step(f"Created [bold]{escape(m.group(1))}[/]", GREEN)
                continue
            if line.strip() == "^C again to exit":
                step("Interrupted · press Ctrl-C again to exit", YELLOW)
                continue
            out(f"  [{color}]{escape(line)}[/]")

    def tool_output(self, *messages, log_only=False, bold=False):
        if messages:
            self.append_chat_history(" ".join(messages).strip(), linebreak=True, blockquote=True)
        if self._edit_retry(" ".join(str(m) for m in messages)):
            return
        if not log_only:
            self._route(" ".join(str(m) for m in messages), MUTED)

    def _edit_retry(self, message: str) -> bool:
        """Aider's "edit didn't match" reflection dumps raw edit blocks; show one line."""
        m = str(message)
        if "did not conform to the edit format" in m or "SEARCH/REPLACE block failed to match" in m or "SearchReplaceNoExactMatch" in m:
            if not getattr(self, "_retry_shown", False):
                step("First edit didn't match the file exactly; retrying", YELLOW)
                self._retry_shown = True
            return True
        if "Only 3 reflections allowed" in m or "reflections allowed, stopping" in m:
            step("Couldn't apply the edit after several tries; try rephrasing or /add the file", RED)
            return True
        return False

    def tool_warning(self, message="", strip=True):
        if self._edit_retry(message):
            return
        if message.strip():
            self.append_chat_history(message, linebreak=True, blockquote=True, strip=strip)
        self._route(message, YELLOW)

    def tool_error(self, message="", strip=True):
        self.num_error_outputs += 1
        if self._edit_retry(message):
            return
        if message.strip():
            self.append_chat_history(message, linebreak=True, blockquote=True, strip=strip)
        self._route(message, RED)

    def rule(self):
        pass  # the prompt draws its own separator

    def get_assistant_mdstream(self):
        return BulletStream(
            mdargs=dict(style=self.assistant_output_color, code_theme=self.code_theme, inline_code_lexer="text")
        )

    def assistant_output(self, message, pretty=None):
        if not message:
            step("The model returned an empty response.", YELLOW)
            return
        stream = self.get_assistant_mdstream()
        stream.update(message, final=True)

    # ---- confirmations ----------------------------------------------------
    def confirm_ask(self, question, default="y", subject=None, explicit_yes_required=False, group=None, allow_never=False):
        self.num_user_asks += 1
        q = question.strip()
        # Things only a person should decide: running commands the model
        # proposed, and going over the context window.
        if explicit_yes_required or "proceed anyway" in q.lower():
            out(f"\n[{YELLOW}]{BULLET}[/] [bold]{escape(q)}[/]")
            if subject:
                for line in str(subject).splitlines():
                    out(f"  [{MUTED}]│[/] {escape(line)}")
            answer = self._ask_line("  Allow? (y/N) ").strip().lower()
            ok = answer in ("y", "yes")
            step("Allowed" if ok else "Declined", GREEN if ok else MUTED)
            return ok
        # Everything else (adding files the model needs, creating files,
        # adding command output to the chat) is routine: say yes quietly.
        return True

    def prompt_ask(self, question, default="", subject=None):
        if subject:
            out(f"  [{MUTED}]{escape(str(subject))}[/]")
        answer = self._ask_line(f"  {question.strip()} ")
        return answer or default

    def _ask_line(self, message: str) -> str:
        try:
            return PromptSession().prompt(message)
        except (EOFError, KeyboardInterrupt):
            return ""

    # ---- input --------------------------------------------------------------
    def get_input(self, root, rel_fnames, addable_rel_fnames, commands, abs_read_only_fnames=None, edit_format=None):
        completer = AutoCompleter(root, rel_fnames, addable_rel_fnames, commands, self.encoding, abs_read_only_fnames)
        if self._session is None:
            try:
                history = FileHistory(self.input_history_file) if self.input_history_file else InMemoryHistory()
            except OSError:
                history = InMemoryHistory()
            self._session = PromptSession(history=history, key_bindings=_keys(), multiline=True)
        # Only conversational modes mean something to the user (not "diff"/"whole").
        mode = f" {edit_format}" if edit_format in ("ask", "architect", "help", "context") else ""
        width = console.size.width
        out(f"[{MUTED}]{'─' * width}[/]")
        text = self._session.prompt(
            HTML(f'<prompt>{mode.strip() + " " if mode else ""}&gt; </prompt>'),
            completer=completer,
            complete_while_typing=True,
            placeholder=HTML('<placeholder>Ask about the code, or describe a change… (/help)</placeholder>'),
            bottom_toolbar=lambda: self._toolbar(rel_fnames),
            style=PROMPT_STYLE,
            prompt_continuation="  ",
        )
        out(f"[{MUTED}]{'─' * width}[/]")
        text = text.strip()
        self.user_input(text)
        return text

    def _toolbar(self, rel_fnames):
        files = len(rel_fnames)
        web = "web search on" if WEB else "web search off"
        return HTML(
            f' <b>{SERVER}</b> · {MODEL_LABEL} · {CONTEXT // 1024}k context · {web} · '
            f'{files} file{"s" if files != 1 else ""} in chat · <i>esc⏎ newline · ⏎ send</i>'
        )


PROMPT_STYLE = Style.from_dict(
    {
        "prompt": f"{ACCENT} bold",
        "placeholder": f"{MUTED} italic",
        "bottom-toolbar": f"bg:#111725 {MUTED}",
        "bottom-toolbar.text": f"{MUTED}",
    }
)


def _keys() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        buf = event.current_buffer
        if buf.complete_state:
            buf.complete_state = None  # accept the completion, don't send yet
            return
        buf.validate_and_handle()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    return kb


class BulletStream(MarkdownStream):
    """Assistant replies start with a bullet, like Claude's CLI. Raw edit
    blocks are hidden while they stream; the turn ends with a real diff."""

    def update(self, text, final=False):
        text = re.sub(r"\*?SEARCH/REPLACE\*? blocks?", "edit", hide_edit_blocks(text))
        super().update(_bullet(text), final)


_FENCE_BLOCK = re.compile(
    r"(?ms)^[^\n`]*\n?```[^\n]*\n<<<<<<< SEARCH\n.*?^>>>>>>> REPLACE[^\n]*\n```[^\n]*\n?"
)
_MARKER = "<<<<<<< SEARCH"


def hide_edit_blocks(text: str) -> str:
    """Removes complete SEARCH/REPLACE blocks (with the filename line before
    them), and hides a trailing block that is still streaming in."""
    text = _FENCE_BLOCK.sub("", text)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("```"):
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


class Thinking:
    """Replaces Aider's "Waiting for <model>" spinner."""

    FRAMES = "·✢✳✶✻✽✻✶✳✢"
    WORDS = ("Thinking", "Reading", "Working", "Considering")

    def __init__(self, text: str = "", delay: float = 0.12):
        self.delay = delay
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._start = time.time()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            secs = int(time.time() - self._start)
            word = self.WORDS[(secs // 6) % len(self.WORDS)]
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r\x1b[2K\x1b[38;2;94;234;212m{frame}\x1b[0m {word}… \x1b[2m({secs}s · ctrl-c to interrupt)\x1b[0m")
            sys.stdout.flush()
            i += 1
            self._stop.wait(self.delay)
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


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


def show_edits(coder, before: dict, max_lines: int = 40) -> None:
    for rel in sorted(coder.aider_edited_files or ()):
        abs_path = coder.abs_root_path(rel)
        old = before.get(abs_path) or ""
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


# ---------------------------------------------------------------------------
# Commands of our own
# ---------------------------------------------------------------------------

HELP = [
    ("/add <files>", "put files in the chat so they can be edited"),
    ("/drop <files>", "remove files from the chat"),
    ("/ask <question>", "ask without changing any files"),
    ("/code <request>", "make a change (the default)"),
    ("/search <query>", "search the web and add the results to the chat"),
    ("/web <url>", "add a web page to the chat"),
    ("/run <command>", "run a shell command and share its output"),
    ("/test, /lint", "run your tests or linter and fix failures"),
    ("/clear", "forget the conversation (keep files)"),
    ("/reset", "forget the conversation and drop all files"),
    ("/tokens", "show how much of the context is used"),
    ("/exit", "quit"),
]


def show_help():
    out(f"\n[{ACCENT}]{BULLET}[/] [bold]OpenPhalanx commands[/]")
    for cmd, what in HELP:
        out(f"  [{ACCENT}]{cmd:<18}[/] [{MUTED}]{what}[/]")
    out(f"\n  [{MUTED}]Edits are never committed. Review with git diff; revert with git checkout -- <file>.[/]")
    out(f"  [{MUTED}]Enter sends · Esc then Enter adds a new line · Ctrl-C interrupts · Ctrl-D quits[/]\n")


def welcome(coder):
    cwd = str(Path.cwd()).replace(str(Path.home()), "~", 1)
    web = f"[{GREEN}]on[/]" if WEB else f"[{MUTED}]off[/]"
    lines = [
        f"[{ACCENT}]✻[/] [bold]Welcome to OpenPhalanx[/]",
        "",
        f"  [{MUTED}]/help for commands · /exit to quit[/]",
        f"  [{MUTED}]cwd:[/] {escape(cwd)}",
        f"  [{MUTED}]server:[/] {escape(SERVER)} · {MODEL_LABEL} · {CONTEXT // 1024}k context · web search {web}",
    ]
    width = max(Text.from_markup(line).cell_len for line in lines) + 2
    out(f"[{ACCENT}]╭{'─' * (width + 1)}╮[/]")
    for line in lines:
        pad = width - Text.from_markup(line).cell_len
        out(f"[{ACCENT}]│[/] {line}{' ' * pad}[{ACCENT}]│[/]")
    out(f"[{ACCENT}]╰{'─' * (width + 1)}╯[/]")
    files = coder.get_inchat_relative_files()
    if files:
        step("In chat: " + ", ".join(escape(f) for f in files))
    print()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def build_coder(argv):
    aider_main.InputOutput = OppxIO  # main() constructs its IO from this name
    base_coder.WaitingSpinner = Thinking
    coder = aider_main.main(argv, return_coder=True)
    # main() installs the engine's crash reporter; errors are ours to report.
    sys.excepthook = sys.__excepthook__
    if not isinstance(coder, base_coder.Coder):
        sys.exit(coder if isinstance(coder, int) else 1)
    return coder


def run(argv) -> int:
    if not aider.__version__.startswith(TESTED_AIDER):
        out(f"[{YELLOW}]warning:[/] this frontend was tested with engine {TESTED_AIDER}.x; found {aider.__version__}.")
    coder = build_coder(argv)
    io = coder.io
    welcome(coder)
    while True:
        try:
            text = coder.get_input()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not text:
            continue
        cmd = text.split(maxsplit=1)
        if cmd[0] in ("/exit", "/quit"):
            break
        if cmd[0] == "/help":
            show_help()
            continue
        if cmd[0] == "/search":
            if len(cmd) < 2:
                step("Usage: /search <query>", YELLOW)
                continue
            text = f"/run {shlex.quote(OPPX_BIN)} search {shlex.quote(cmd[1])}"
        io._retry_shown = False
        before = snapshot(coder)
        try:
            coder.run_one(text, preproc=True)
            show_edits(coder, before)
        except SwitchCoder as switch:
            if getattr(switch, "placeholder", None) is not None:
                io.placeholder = switch.placeholder
            kwargs = dict(io=io, from_coder=coder)
            kwargs.update(switch.kwargs)
            kwargs.pop("show_announcements", None)
            coder = base_coder.Coder.create(**kwargs)
        except KeyboardInterrupt:
            step("Interrupted", YELLOW)
        print()
    out(f"[{MUTED}]Bye. Your changes are in the working tree; review them with git diff.[/]")
    return 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except (KeyboardInterrupt, EOFError):
        return 130
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    except Exception as e:  # noqa: BLE001 - last-resort, user-facing
        import traceback

        crash = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "oppx" / "last-crash.txt"
        try:
            crash.parent.mkdir(parents=True, exist_ok=True)
            crash.write_text(traceback.format_exc())
        except OSError:
            pass
        out(f"\n[{RED}]error:[/] the OpenPhalanx chat stopped unexpectedly: {escape(type(e).__name__)}: {escape(str(e))}")
        out(f"  [{MUTED}]Details: {escape(str(crash))}[/]")
        out(f"  [{MUTED}]You can keep working with the classic interface: oppx --classic[/]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
