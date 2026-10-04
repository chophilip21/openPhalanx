"""Slash commands (Claude Code names first), project memory, and message translation."""

import datetime
import os
import re
import shlex
import subprocess
from pathlib import Path

from rich.markup import escape

from .config import ACCENT, CONTEXT, EXTRA_MEMORY, GREEN, MEMORY_FILE, MODEL_NAME, MUTED, OPPX_BIN, RED, SERVER, VERSION, WEB, YELLOW
from .term import Thinking, UI, headline, out, step
from .context import CONTEXT_MGR, MAP_MAX, PRI_EDITED, PRI_MODEL, PRI_USER, _orig_format_messages, _raw_tokens
from .oppx_io import show_shortcuts
from .routing import wants_edit
from .cache import ASK_NOTE


# ---------------------------------------------------------------------------
# Commands (Claude Code names first)
# ---------------------------------------------------------------------------

HELP = [
    ("Conversation", [
        ("/clear", "start a new conversation (files and memory stay)"),
        ("/compact", "summarize the conversation to free context"),
        ("/context", "show how much of the context window is used"),
        ("/cost", "show tokens used this session"),
        ("/export [file]", "save this conversation as Markdown"),
        ("/exit", "quit (also ctrl-c twice, or ctrl-d)"),
    ]),
    ("Project", [
        ("/init", f"write {MEMORY_FILE}, a project summary loaded every session"),
        ("/memory", f"edit {MEMORY_FILE} (or type: # remember this)"),
        ("/review", "review your uncommitted changes"),
        ("/add, /drop <files>", "put files in or out of the chat (or type @file)"),
    ]),
    ("Tools", [
        ("/search <query>", "search the web and add the results"),
        ("/web <url>", "add a web page to the chat"),
        ("!<command>", "run a shell command and share its output"),
        ("/test, /lint", "run your tests or linter and fix failures"),
    ]),
    ("Session", [
        ("/status", "server, model, context and mode"),
        ("/model", "the model this server runs"),
        ("/doctor", "check the connection to the server"),
        ("/vim", "toggle vim key bindings"),
    ]),
]

NOT_AVAILABLE = {
    "/mcp", "/agents", "/login", "/logout", "/permissions", "/hooks", "/config", "/terminal-setup",
    "/bug", "/pr_comments", "/ide", "/install-github-app", "/upgrade", "/release-notes", "/add-dir",
    "/rewind", "/output-style", "/statusline", "/privacy-settings",
}


def show_help():
    headline("[bold]OpenPhalanx[/] [dim]· keys and commands follow Claude Code[/]")
    for section, rows in HELP:
        out(f"\n  [bold]{section}[/]")
        for cmd, what in rows:
            out(f"    [{ACCENT}]{cmd:<22}[/] [{MUTED}]{what}[/]")
    out("\n  [bold]Shortcuts[/]")
    show_shortcuts()
    out(f"\n  [{MUTED}]Edits are never committed: review with git diff, revert with git checkout -- <file>.[/]")
    out(f"  [{MUTED}]Later: oppx -c continues this conversation, oppx -r picks an older one.[/]\n")


def show_status(coder):
    headline("[bold]Status[/]")
    rows = [
        ("server", f"{SERVER}"),
        ("model", f"{MODEL_NAME} · {CONTEXT // 1024}k context"),
        ("web search", "on" if WEB else "off (oppx without --no-web turns it on)"),
        ("mode", "plan (no edits)" if UI.plan_mode else "default (edits applied, never committed)"),
        ("files in chat", ", ".join(coder.get_inchat_relative_files()) or "none"),
        ("memory", ", ".join(Path(f).name for f in coder.abs_read_only_fnames) or f"none (/init creates {MEMORY_FILE})"),
        ("oppx", VERSION or "?"),
    ]
    for k, v in rows:
        step(f"[bold]{k:<14}[/] {escape(v)}")


def show_context(coder):
    """Claude-style context view: what fills the window and how much room is left."""
    chunks = _orig_format_messages(coder)
    count = coder.main_model.token_count
    parts = [
        ("Instructions", count(chunks.system + chunks.examples + chunks.reminder)),
        ("Memory files", count(chunks.readonly_files) if chunks.readonly_files else 0),
        ("Repo map", count(chunks.repo) if chunks.repo else 0),
        ("Conversation", count(chunks.done + chunks.cur) if (chunks.done or chunks.cur) else 0),
    ]
    files = []
    for f in sorted(coder.abs_fnames):
        try:
            n = count(Path(f).read_text(encoding=coder.io.encoding, errors="replace"))
        except OSError:
            continue
        pri = CONTEXT_MGR.files.get(f, [PRI_MODEL, 0])[0]
        why = {PRI_EDITED: "edited", PRI_USER: "added by you", PRI_MODEL: "requested by the model"}[pri]
        files.append((coder.get_rel_fname(f), n, why))
    total = sum(n for _, n in parts) + sum(n for _, n, _ in files)
    budget = CONTEXT_MGR.budget
    width = 40
    filled = min(width, round(width * total / CONTEXT))
    mark = min(width - 1, round(width * budget / CONTEXT))
    bar = "".join("█" if i < filled else ("│" if i == mark else "░") for i in range(width))
    color = GREEN if total <= budget * 0.8 else (YELLOW if total <= budget else RED)
    headline(f"[bold]Context[/] [{MUTED}]· {total:,} of {CONTEXT:,} tokens[/]")
    out(f"  [{color}]{bar}[/] [{MUTED}]{100 * total // CONTEXT}% used · │ = budget ({budget:,}), "
        f"the rest is kept for the answer[/]")
    for name, n in parts:
        out(f"    [{MUTED}]{name:<16}[/] {n:>7,}")
    for name, n, why in files:
        out(f"    [{ACCENT}]{escape(name)[:44]:<44}[/] {n:>7,}  [{MUTED}]{why}[/]")
    out(f"  [{MUTED}]When a request would go over budget, OpenPhalanx summarizes older conversation, then sets aside "
        f"the least recently used files (they stay in the repo map), then shrinks the repo map.[/]")


def show_cost(coder):
    sent, recv = coder.total_tokens_sent, coder.total_tokens_received
    headline("[bold]Session usage[/]")
    step(f"{sent:,} tokens sent · {recv:,} received · runs on your own server, so $0.00")


def compact(coder, instructions: str):
    before = coder.main_model.token_count(coder.done_messages) if coder.done_messages else 0
    if not coder.done_messages:
        step("Nothing to compact yet.")
        return
    with Thinking(label="Compacting conversation"):
        coder.done_messages = coder.summarizer.summarize_all(coder.done_messages)
    after = coder.main_model.token_count(coder.done_messages)
    CONTEXT_MGR.used = _raw_tokens(coder)
    step(f"Conversation compacted: {before:,} → {after:,} tokens", GREEN)


def export(coder, target: str):
    path = Path(target or f"openphalanx-conversation-{datetime.datetime.now():%Y%m%d-%H%M%S}.md")
    lines = []
    for m in coder.done_messages + coder.cur_messages:
        role = {"user": "You", "assistant": "OpenPhalanx"}.get(m.get("role"), m.get("role"))
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        lines.append(f"## {role}\n\n{content}\n")
    path.write_text("\n".join(lines) or "(empty conversation)\n", encoding="utf-8")
    step(f"Saved the conversation to [bold]{escape(str(path))}[/]", GREEN)


def memory_path(coder) -> Path:
    return Path(coder.root) / MEMORY_FILE


def load_memory(coder):
    for name in (MEMORY_FILE, *EXTRA_MEMORY):
        p = Path(coder.root) / name
        if p.is_file():
            coder.abs_read_only_fnames.add(str(p.resolve()))


def remember(coder, note: str):
    path = memory_path(coder)
    exists = path.is_file()
    with path.open("a", encoding="utf-8") as f:
        if not exists:
            f.write(f"# {Path(coder.root).name}\n\nNotes for the OpenPhalanx coding assistant.\n\n")
        f.write(f"- {note.strip()}\n")
    coder.abs_read_only_fnames.add(str(path.resolve()))
    step(f"Saved to memory ([bold]{MEMORY_FILE}[/])", GREEN)


def edit_memory(coder):
    path = memory_path(coder)
    if not path.exists():
        path.write_text(f"# {Path(coder.root).name}\n\nNotes for the OpenPhalanx coding assistant.\n\n", encoding="utf-8")
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if subprocess.run(["which", "nano"], capture_output=True).returncode == 0 else "vi")
    subprocess.call([*shlex.split(editor), str(path)])
    coder.abs_read_only_fnames.add(str(path.resolve()))
    step(f"Memory updated ([bold]{MEMORY_FILE}[/])", GREEN)


INIT_PROMPT = (
    f"Create a file named {MEMORY_FILE} in the repository root that briefly orients a coding assistant to this "
    "project: what it does, how to build, test and run it, the main directories and what lives in them, and any "
    "conventions you can see. Use short Markdown sections and bullet points, at most 60 lines. Base it only on "
    "what is in this repository."
)

REVIEW_PROMPT = (
    "Review the following uncommitted changes as a careful senior engineer. List real problems first "
    "(bugs, missing error handling, security issues), then smaller suggestions. Be specific and brief. "
    "Do not rewrite the code.\n\n```diff\n{diff}\n```"
)


def git_diff(root: str) -> str:
    try:
        r = subprocess.run(["git", "diff", "HEAD"], cwd=root, capture_output=True, text=True, timeout=20)
        return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def translate(coder, text: str):
    """Maps Claude-style input to what Aider understands. Returns the text to
    run, or None when the input was fully handled here."""
    if text.startswith("!"):
        cmd = text[1:].strip()
        return f"/run {cmd}" if cmd else None
    if text.startswith("#") and not text.startswith("#!"):
        note = text.lstrip("#").strip()
        if note:
            remember(coder, note)
        return None
    if not text.startswith("/"):
        # @file mentions put the file in the chat.
        for path in re.findall(r"(?<!\S)@(\S+)", text):
            p = Path(coder.root) / path
            if p.is_file():
                coder.commands.cmd_add(path)
        text = re.sub(r"(?<!\S)@(\S+)", r"\1", text)
        if UI.plan_mode or not wants_edit(text):
            return f"/ask {text}\n\n{ASK_NOTE}"
        return text

    name, _, arg = text.partition(" ")
    arg = arg.strip()
    if name in ("/help", "/?"):
        show_help()
    elif name in ("/exit", "/quit"):
        raise EOFError
    elif name == "/clear":
        coder.commands.cmd_clear("")
        CONTEXT_MGR.map_tokens = MAP_MAX
        step("Started a new conversation (files and memory kept)", GREEN)
    elif name == "/compact":
        compact(coder, arg)
    elif name == "/cost":
        show_cost(coder)
    elif name in ("/context", "/tokens"):
        show_context(coder)
    elif name == "/status":
        show_status(coder)
    elif name == "/model":
        headline(f"[bold]{MODEL_NAME}[/] · {CONTEXT // 1024}k context on {escape(SERVER)}")
        step("The model is chosen on the server, in the OpenPhalanx app's Models page.")
    elif name == "/doctor":
        subprocess.call([OPPX_BIN, "status"])
    elif name == "/vim":
        UI.vi = not UI.vi
        step(f"Vim key bindings {'on' if UI.vi else 'off'}", GREEN)
    elif name == "/init":
        return INIT_PROMPT
    elif name == "/memory":
        edit_memory(coder)
    elif name == "/review":
        diff = git_diff(coder.root)
        if not diff.strip():
            step("No uncommitted changes to review.")
            return None
        if len(diff) > 24000:
            step(f"The diff is large; reviewing the first 24,000 of {len(diff):,} characters.", YELLOW)
        return "/ask " + REVIEW_PROMPT.format(diff=diff[:24000])
    elif name == "/export":
        export(coder, arg)
    elif name == "/search":
        if not arg:
            step("Usage: /search <query>", YELLOW)
            return None
        return f"/run {shlex.quote(OPPX_BIN)} search {shlex.quote(arg)}"
    elif name == "/resume":
        step("Exit (/exit), then run oppx -r to pick a past conversation, or oppx -c for the latest.")
    elif name in ("/undo", "/commit", "/git"):
        step("OpenPhalanx never commits. Use git yourself: git diff, git checkout -- <file>.", YELLOW)
    elif name in NOT_AVAILABLE:
        step(f"{name} isn't available in OpenPhalanx. /help lists what is.", YELLOW)
    else:
        return text  # Aider's own commands: /add, /drop, /ask, /code, /run, /test, /lint, /web, /tokens …
    return None
