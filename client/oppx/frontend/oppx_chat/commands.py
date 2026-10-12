"""Slash commands (Claude Code names first), project memory, and message translation."""

import datetime
import os
import re
import shlex
import subprocess
from pathlib import Path

from rich.markup import escape

from .config import ACCENT, CONTEXT, ENGINE, EXTRA_MEMORY, GREEN, MEMORY_FILE, MODEL_NAME, MUTED, OPPX_BIN, PRINT_MODE, RED, SERVER, VERSION, WEB, YELLOW
from .term import SPIN, Thinking, UI, headline, out, step
from .mcp import cost_warning
from . import browser
from .context import CONTEXT_MGR, MAP_MAX, PRI_EDITED, PRI_MODEL, PRI_USER, _orig_format_messages, _raw_tokens
from .oppx_io import show_shortcuts
from .routing import wants_edit
from .cache import ASK_NOTE
from .agent import ANSWER_RESERVE as AGENT_RESERVE


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
        ("/mcp", "connectors (MCP servers): list with what is wrong and how to fix it, add, remove, tools, reconnect [name]"),
        ("/browser", "the headless web browser for this session: start, stop, on/off for every session"),
        ("/permissions", "what runs without asking in this project: list, remove <rule>, clear"),
    ]),
    ("Session", [
        ("/status", "server, model, context and mode"),
        ("/model", "the model this server runs"),
        ("/doctor", "check the connection to the server"),
        ("/vim", "toggle vim key bindings"),
    ]),
]

NOT_AVAILABLE = {
    "/agents", "/login", "/logout", "/permissions", "/hooks", "/config", "/terminal-setup",
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
        ("connectors", (connector_names(UI.connectors) or "none (/mcp add …)") if UI.connectors is not None
         else "not used by --engine aider"),
        ("oppx", VERSION or "?"),
    ]
    for k, v in rows:
        step(f"[bold]{k:<14}[/] {escape(v)}")


def _show_agent_context(agent):
    """/context for the agent engine: it sends no repo map or whole files,
    only its instructions, the conversation and the tool output it asked for."""
    parts = agent.context_parts()
    total = sum(n for _, n in parts)
    budget = CONTEXT - AGENT_RESERVE
    width = 40
    filled = min(width, round(width * total / CONTEXT))
    mark = min(width - 1, round(width * budget / CONTEXT))
    bar = "".join("█" if i < filled else ("│" if i == mark else "░") for i in range(width))
    color = GREEN if total <= budget * 0.8 else (YELLOW if total <= budget else RED)
    headline(f"[bold]Context[/] [{MUTED}]· about {total:,} of {CONTEXT:,} tokens[/]")
    out(f"  [{color}]{bar}[/] [{MUTED}]{100 * total // CONTEXT}% used · │ = budget ({budget:,}), "
        f"the rest is kept for the answer[/]")
    for name, n in parts:
        out(f"    [{MUTED}]{name:<16}[/] {n:>7,}")
    if agent.last_prompt_tokens:
        out(f"  [{MUTED}]Last request, counted by the server: {agent.last_prompt_tokens:,} tokens.[/]")
    out(f"  [{MUTED}]Near the limit, OpenPhalanx cuts old tool output first (the latest few are kept), "
        f"then the oldest turns.[/]")


def show_context(coder):
    """Claude-style context view: what fills the window and how much room is left."""
    if UI.agent is not None:
        return _show_agent_context(UI.agent)
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
    if UI.agent is not None:  # the agent engine talks to the server itself
        a = UI.agent
        sent, recv = a.computed_tokens + a.cached_tokens, a.completion_tokens
    headline("[bold]Session usage[/]")
    step(f"{sent:,} tokens sent · {recv:,} received · runs on your own server, so $0.00")
    if UI.agent is not None and sent:
        step(f"[{MUTED}]{UI.agent.cached_tokens:,} of the tokens sent were served from the server's prefix cache "
             f"({100 * UI.agent.cached_tokens // sent}%)[/]")


# ---------------------------------------------------------------------------
# Connectors (MCP servers): /mcp
# ---------------------------------------------------------------------------

MCP_USAGE = ("/mcp add <name> -- <command> [args] (or --url <https://…>) · remove <name> · tools <name> · "
             "reconnect [<name>] (start again; with a name it waits for a login in the browser)")


def connector_names(hub) -> str:
    """One line: every connector with its tool count, or why it isn't running."""
    parts = []
    for name, c in hub.connectors.items():
        n = len(c.tools)
        parts.append(f"{name} ({n} tool{'s' if n != 1 else ''})" if c.state == "connected"
                     else f"{name} ({'not approved' if c.state == 'waiting' else 'failed'})")
    return ", ".join(parts)


def ask_about_connector(coder):
    """The question for a repository's own connector (its .mcp.json): it is a
    program that would run on this machine, so it starts only once the user
    agreed. In print mode nothing can be asked, so it waits."""
    def ask(d) -> bool:
        if PRINT_MODE:
            return False
        return coder.io.confirm_ask(
            "This repository's .mcp.json adds a connector: a program that runs on this machine. Start it?",
            subject=f"{d.name}: {d.summary()}", explicit_yes_required=True)
    return ask


def show_connector_problems(hub, adding: bool = False):
    """What the user should know about their connectors without asking: one
    that didn't start, and the room the tool lists take when that is a lot
    (or, right after adding one, whenever the model's window is small)."""
    if hub is None or PRINT_MODE:
        return
    if hub.error:
        step(f"Connectors: {escape(hub.error)}", YELLOW)
    for name, c in hub.connectors.items():
        if c.state != "connected":
            show_recovery(name, c)
    warning = cost_warning(hub.tokens(), CONTEXT, adding)
    if warning:
        step(f"⚠ {escape(warning)}", YELLOW)


def show_recovery(name: str, c):
    """A connector that isn't running: why, and the steps that bring it back."""
    why, steps = c.recovery()
    step(f"[{YELLOW}]Connector [bold]{escape(name)}[/] isn't running: {escape(why)}.[/] [{MUTED}]{escape(c.error[:160])}[/]")
    for i, line in enumerate(steps, 1):
        out(f"       [{ACCENT}]{i}.[/] {escape(line)}")


def show_connectors(hub):
    tokens = hub.tokens()
    cost = f" [{MUTED}]· about {tokens:,} tokens of every request ({100 * tokens / CONTEXT:.1f}% of {CONTEXT // 1024}k)[/]" if tokens else ""
    headline(f"[bold]Connectors[/]{cost}")
    if hub.error:
        step(escape(hub.error), YELLOW)
    if not hub.connectors:
        step("None yet. A connector is an MCP server whose tools the model can call.")
        step(f"[{MUTED}]{escape(MCP_USAGE)}[/]")
        return
    width = max(len(n) for n in hub.connectors)
    for name, c in hub.connectors.items():
        d = c.definition
        where = "yours" if d.scope == "user" else "this repo"
        if c.state == "connected":
            n = len(c.tools)
            state = f"[{GREEN}]✓[/] {name:<{width}}  {f'{n} tool' + ('s' if n != 1 else ''):<9}"
        else:
            state = f"[{RED if c.state == 'failed' else YELLOW}]{'✗' if c.state == 'failed' else '!'}[/] {name:<{width}}  {'failed' if c.state == 'failed' else 'waiting':<9}"
        step(f"{state}  [{MUTED}]{where:<9}[/]  {escape(d.summary()[:70])}")
        if c.state != "connected":
            why, steps = c.recovery()
            out(f"       [{YELLOW}]{escape(why)}[/] [{MUTED}]· {escape(c.error[:160])}[/]")
            for i, line in enumerate(steps, 1):
                out(f"       [{ACCENT}]{i}.[/] {escape(line)}")
    step(f"[{MUTED}]{escape(MCP_USAGE)}[/]")


def show_connector_tools(hub, name: str):
    c = hub.connectors.get(name)
    if c is None:
        step(f"No connector named {escape(name)}. /mcp lists them.", YELLOW)
        return
    if c.state != "connected":
        step(f"{escape(name)} isn't running: {escape(c.error)}", YELLOW)
        return
    headline(f"[bold]{escape(name)}[/] [{MUTED}]· {len(c.tools)} tools · {escape(c.definition.summary()[:70])}[/]")
    for t in c.tools:
        mark = f" [{MUTED}](read-only)[/]" if t.read_only else ""
        step(f"[{ACCENT}]{escape(t.qualified)}[/]{mark}  [{MUTED}]{escape(t.summary)}[/]")


def reconnect_one(coder, name: str):
    """/mcp reconnect <name>: starts one connector again and waits for it, long
    enough for a login in the browser (mcp-remote opens one)."""
    from .mcp import LOGIN_TIMEOUT, uses_mcp_remote
    hub = UI.connectors
    before = {n: (t.description, t.schema) for n, t in hub.tools().items()}
    known = hub.builtin.get(name) or next((d for d in hub.definitions() if d.name == name), None)
    if known is None:
        step(f"No connector named {escape(name)}. /mcp lists them.", YELLOW)
        return
    if uses_mcp_remote(known):
        step(f"Starting {escape(name)}. If a browser window opens, finish the login there; this waits up to "
             f"{LOGIN_TIMEOUT // 60} minutes.")
    SPIN.start(f"Connecting to {name}")
    c = hub.reconnect(name, ask_about_connector(coder))
    SPIN.stop()
    if c.state == "connected":
        n = len(c.tools)
        step(f"[{GREEN}]✓[/] {escape(name)} is connected again ({n} tool{'s' if n != 1 else ''}). Say \"continue\" to pick up "
             "what was waiting for it.")
    else:
        show_recovery(name, c)
    if {n: (t.description, t.schema) for n, t in hub.tools().items()} != before and UI.agent is not None:
        UI.agent.reset_system()


def reconnect_connectors(coder, adding: bool = False):
    """Starts the connectors again from the files as they are now (after
    /mcp add or remove, or to retry one that failed)."""
    hub = UI.connectors
    before = {n: (t.description, t.schema) for n, t in hub.tools().items()}
    hub.connect(ask_about_connector(coder), before=lambda: SPIN.start("Connecting to connectors"))
    SPIN.stop()
    show_connectors(hub)
    if {n: (t.description, t.schema) for n, t in hub.tools().items()} != before:
        if UI.agent is not None:
            UI.agent.reset_system()
        step(f"[{MUTED}]The tool list changed: the next request is sent whole once (no prefix cache).[/]")
    warning = cost_warning(hub.tokens(), CONTEXT, adding)
    if warning:
        step(f"⚠ {escape(warning)}", YELLOW)


# ---------------------------------------------------------------------------
# The headless browser: /browser, and the question when the model asks for it
# ---------------------------------------------------------------------------



def start_browser(coder, ask: bool) -> bool | str:
    """Starts the browser for this session, after asking when `ask`. True once
    it runs; otherwise a sentence for the model (the user said no, or why it
    couldn't start)."""
    agent, hub, perms = UI.agent, UI.connectors, UI.permissions
    if hub is None or agent is None:
        return "this session runs --engine aider"
    if agent.browser == "running":
        return True
    if ask:
        if PRINT_MODE:
            return "the user can't be asked in print mode (oppx browser on starts it with every session)"
        said = coder.io.ask_options("Start a headless browser (Playwright) for this session?", [
            "The model wants to open web pages (your local dev server, for example), click through them",
            "and read their console errors. It runs on this machine and stops when the chat ends.",
        ], [
            ("y", GREEN, "yes, for this session"),
            ("a", ACCENT, "always: start it with every session"),
            ("n", RED, "no  (Enter)"),
            ("…", MUTED, "or type what to do instead"),
        ]).strip()
        answer = said.lower()
        if answer in ("", "n", "no"):
            step("Not started", MUTED)
            SPIN.start()
            return "the user said no"
        if answer not in ("y", "yes", "a", "always"):
            step("Not started, with your note", MUTED)
            SPIN.start()
            return f"the user said no, and: {said}"
        if answer in ("a", "always"):
            done = subprocess.run([OPPX_BIN, "browser", "on"], capture_output=True, text=True)
            step("From now on it starts with every session (oppx browser off to be asked again)" if done.returncode == 0
                 else "Couldn't save that; starting it for this session only", GREEN if done.returncode == 0 else YELLOW)
    if not browser.available():
        if not browser.NODE:
            step(f"The browser can't start: {browser.why_unavailable()}", YELLOW)
            return f"it can't start on this machine: {browser.why_unavailable()}"
        SPIN.start("Installing the browser (one time)")
        ok, tail = browser.install()
        SPIN.stop()
        if not ok:
            step(f"Installing the browser failed: {escape(tail)}", YELLOW)
            return f"installing it failed: {tail}"
    SPIN.start("Starting the browser")
    c = hub.start(browser.definition(agent.session), hidden=browser.HIDDEN, postprocess=browser.inline_files,
                  timeout=browser.START_TIMEOUT, stateful=True)
    SPIN.stop()
    if c.state != "connected":
        step(f"The browser didn't start: {escape(c.error)}", YELLOW)
        return f"it didn't start: {c.error}"
    perms.allow_for_session(f"{browser.NAME}.*", unless=browser.EVERY_TIME)
    agent.browser = "running"
    agent.reset_system()
    step(f"Browser running (headless, {sum(n.startswith(browser.NAME + ".") for n in hub.names())} tools). Its tools "
         "run without asking this session, except running JavaScript in the page.", GREEN)
    SPIN.start()
    return True


def stop_browser():
    hub, agent = UI.connectors, UI.agent
    c = hub.connectors.pop(browser.NAME, None) if hub is not None else None
    if c is not None:
        c.close()
    if hub is not None:
        hub.builtin.pop(browser.NAME, None)
    if agent is not None and agent.browser == "running":
        agent.browser = "offer"
        agent.reset_system()


def browser_command(coder, arg: str):
    sub = arg.strip().lower()
    agent = UI.agent
    if agent is None:
        step("The browser is used by the default engine; this session runs --engine aider.", YELLOW)
        return
    if sub in ("on", "off"):
        SPIN.stop()
        subprocess.run([OPPX_BIN, "browser", sub])
        if sub == "on" and agent.browser != "running":
            start_browser(coder, ask=False)
        SPIN.stop()
    elif sub == "start":
        start_browser(coder, ask=False)
        SPIN.stop()
    elif sub == "stop":
        stop_browser()
        step("Browser stopped. The model can ask for it again.")
    elif sub in ("", "status"):
        state = {"running": "running (headless) for this session", "offer": "not started; the model asks when it needs it",
                 "declined": "not started; you said no this session (/browser start starts it)",
                 "none": "not available in this session"}.get(agent.browser, agent.browser)
        step(f"Browser: {state}")
        step(f"[{MUTED}]At session start: {'starts without asking (oppx browser on)' if browser.AUTO else 'the chat asks when needed (oppx browser off)'}[/]")
        if not browser.available():
            step(f"[{MUTED}]{escape(browser.why_unavailable() or '')}; installed on first use[/]")
    else:
        step("Usage: /browser [start | stop | on | off]", YELLOW)


def mcp_command(coder, arg: str):
    sub, _, rest = arg.partition(" ")
    rest = rest.strip()
    hub = UI.connectors
    if sub in ("add", "remove", "rm"):
        try:
            words = shlex.split(rest)
        except ValueError as e:
            step(f"Couldn't read that: {escape(str(e))}", YELLOW)
            return
        SPIN.stop()
        # oppx owns the connector files; told it runs inside the chat, it
        # leaves the check and the context warning to us (below).
        done = subprocess.run([OPPX_BIN, "mcp", "remove" if sub == "rm" else sub, *words],
                              env={**os.environ, "OPPX_IN_CHAT": "1"})
        if done.returncode == 0 and hub is not None:
            reconnect_connectors(coder, adding=sub == "add")
        elif done.returncode == 0:
            step("Saved. Connectors are used by the default engine; this session runs --engine aider.", YELLOW)
        return
    if hub is None:
        SPIN.stop()
        subprocess.run([OPPX_BIN, "mcp", "list"])
        step("Connectors are used by the default engine; this session runs --engine aider.", YELLOW)
    elif sub in ("", "list"):
        show_connectors(hub)
    elif sub == "tools" and rest:
        show_connector_tools(hub, rest)
    elif sub in ("reconnect", "reload", "auth", "login") and rest:
        reconnect_one(coder, rest.split()[0])
    elif sub in ("reconnect", "reload"):
        reconnect_connectors(coder)
    else:
        step(f"Usage: /mcp  ·  {escape(MCP_USAGE)}", YELLOW)


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


def memory_files(root) -> list[Path]:
    """The memory files in the repository root, in reading order."""
    try:
        found = {p.name.lower(): p for p in sorted(Path(root).iterdir()) if p.is_file()}
    except OSError:
        return []
    return [found[n.lower()] for n in (MEMORY_FILE, *EXTRA_MEMORY) if n.lower() in found]


def load_memory(coder):
    for p in memory_files(coder.root):
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


def permissions_command(arg: str):
    """/permissions: this project's allow and deny rules (as in Claude Code)."""
    perms = UI.permissions
    if perms is None:
        step("Permissions apply to the agent engine.", YELLOW)
        return
    word, _, rest = arg.partition(" ")
    rule = " ".join(rest.strip().strip("`").split())
    # So `allow linear.save_issue` is a rule for that tool, not for a command of that name.
    perms.connectors = set(getattr(UI.connectors, "connectors", {}) or ())
    if word == "clear":
        perms.clear()
        step("Cleared. Commands that change things are asked about again.")
    elif word in ("remove", "rm") and rule:
        step(f"Removed {rule}." if perms.remove(rule) else f"No rule {rule}.", MUTED)
    elif word == "allow" and rule:
        if perms.add("run", rule):
            step(f"Allowed without asking from now on: {escape(rule)}", GREEN)
        else:
            step(f"{escape(rule)} is asked every time and can't be allowed in advance.", YELLOW)
    elif word == "deny" and rule:
        perms.add("deny", rule)
        step(f"Denied from now on: {escape(rule)} (refused without asking; it wins over allow rules)", YELLOW)
    else:
        headline("Permissions in this project")
        step("never asked: commands that only read, inside the project (git status/log/diff/show, ls, cat, rg, grep, find, "
             "sed -n, a version check …)")
        for r in perms.run:
            step(f"[{GREEN}]allow[/] {escape(perms.label(('run', r)))}")
        for d in perms.dirs:
            step(f"[{GREEN}]allow[/] {escape(perms.label(('dirs', d)))} [dim](outside the project; remove with its path)[/]")
        for t in perms.tools:
            step(f"[{GREEN}]allow[/] {escape(t)} [dim](connector tool)[/]")
        for r in perms.deny:
            step(f"[{RED}]deny[/]  {escape(r)}")
        if not (perms.run or perms.tools or perms.deny or perms.dirs):
            step("no rules yet: answer [bold]a[/] to a question, or /permissions allow <command>")
        step("asked every time: rm, sudo, chmod, kill, git push/reset/clean/rebase, git checkout -- <file>, publishing "
             "(npm publish, gh pr merge), bash -c, eval, python -c, and anything outside the project that holds credentials")
        step(f"/permissions allow|deny|remove <rule> · /permissions clear · saved in {escape(str(perms.path))}")


def undo_edits() -> None:
    """Restores the files the last editing turn changed (nothing is committed,
    so this is a file restore, not a git revert). Works once per turn."""
    if not UI.undo:
        step("Nothing to undo: no edits were made in this session yet.", YELLOW)
        return
    names = []
    for path, content in UI.undo.items():
        p = Path(path)
        try:
            if content is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(content, encoding="utf-8")
        except OSError as e:
            step(f"Couldn't restore {escape(p.name)}: {escape(str(e))}", YELLOW)
            continue
        names.append(p.name)
    UI.undo = {}
    if names and UI.agent is not None:
        UI.agent.undone(names)  # the agent stops counting them as changed
    if names:
        step(f"Undid the last edit: restored {escape(', '.join(names))}", GREEN)


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
        if ENGINE == "agent":
            return text  # the agent turn decides ask or edit (app.agent_turn)
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
        if UI.agent is not None:
            UI.agent.reset()
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
    elif name == "/undo":
        undo_edits()
    elif name == "/mcp":
        mcp_command(coder, arg)
    elif name == "/browser":
        browser_command(coder, arg)
    elif name in ("/permissions", "/allowed-tools"):
        permissions_command(arg)
    elif name in ("/commit", "/git"):
        step("OpenPhalanx never commits. Use git yourself: git diff, git commit.", YELLOW)
    elif name in NOT_AVAILABLE:
        step(f"{name} isn't available in OpenPhalanx. /help lists what is.", YELLOW)
    else:
        return text  # Aider's own commands: /add, /drop, /ask, /code, /run, /test, /lint, /web, /tokens …
    return None
