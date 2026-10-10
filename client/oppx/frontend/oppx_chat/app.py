"""Wires everything into Aider and runs the conversation loop."""

import contextlib
import os
import sys
from pathlib import Path

import aider
import aider.coders.base_coder as base_coder
import aider.main as aider_main
from aider.coders.ask_coder import AskCoder
from aider.commands import SwitchCoder
from aider.repomap import RepoMap
from rich.markup import escape
from rich.text import Text

from .config import (ACCENT, CONTEXT, ENGINE, EXTRA_MEMORY, GREEN, MEMORY_FILE, MODEL_NAME, MUTED, PRINT_MODE,
                     REASONING, RED, SERVER, TESTED_AIDER, WEB, YELLOW)
from .term import EscWatcher, SESSION, SPIN, Thinking, UI, out, step
from .render import BulletStream, hide_reasoning, show_diff, show_edits, snapshot
from .mcp import Hub
from .agent import Agent, Hooks, Workspace
from .routing import wants_edit
from .context import CONTEXT_MGR, PRI_EDITED, _check_tokens, _fitted_format_messages
from .oppx_io import OppxIO
from .cache import _ask_file_mentions, _ask_init, _coder_init, _dump_requests, _stable_ranked_map
from .commands import ask_about_connector, connector_names, load_memory, show_connector_problems, translate


def _interrupted(self):
    """Replaces Aider's Ctrl-C handler, which exits on a second press within
    2 s. Here an interrupt only stops the answer; quitting happens at the
    prompt (Ctrl-C twice or Ctrl-D), as in Claude Code. The message is
    printed after the answer's live view has closed (see run_turn)."""
    UI.interrupted = True


def build_coder(argv):
    aider_main.InputOutput = OppxIO  # main() constructs its IO from this name
    base_coder.WaitingSpinner = Thinking
    base_coder.Coder.keyboard_interrupt = _interrupted
    base_coder.Coder.format_messages = _fitted_format_messages
    base_coder.Coder.check_tokens = _check_tokens
    # Aider stops streaming (and its spinner) whenever it picks a non-```
    # fence, e.g. when a Markdown file with code blocks is in the chat; the
    # answer then appears all at once after a long silence. Our renderer
    # handles every fence, so always stream.
    base_coder.Coder.show_pretty = lambda self: True
    RepoMap.get_ranked_tags_map = _stable_ranked_map
    base_coder.Coder.__init__ = _coder_init
    AskCoder.__init__ = _ask_init
    AskCoder.check_for_file_mentions = _ask_file_mentions
    if os.environ.get("OPPX_DUMP_REQUESTS"):
        _dump_requests(os.environ["OPPX_DUMP_REQUESTS"])
    coder = aider_main.main(argv, return_coder=True)
    # main() installs the engine's crash reporter; errors are ours to report.
    sys.excepthook = sys.__excepthook__
    if not isinstance(coder, base_coder.Coder):
        sys.exit(coder if isinstance(coder, int) else 1)
    return coder


def welcome(coder):
    cwd = str(Path.cwd()).replace(str(Path.home()), "~", 1)
    web = f"[{GREEN}]on[/]" if WEB else f"[{MUTED}]off[/]"
    memory = [Path(f).name for f in coder.abs_read_only_fnames]
    lines = [
        f"[{ACCENT}]✻[/] [bold]Welcome to OpenPhalanx![/]",
        "",
        f"  [{MUTED}]/help for help, /status for your current setup[/]",
        "",
        f"  [{MUTED}]cwd:[/] {escape(cwd)}",
        f"  [{MUTED}]server:[/] {escape(SERVER)} · {MODEL_NAME} · {CONTEXT // 1024}k context · web search {web}",
    ]
    if memory:
        lines.append(f"  [{MUTED}]memory:[/] {escape(', '.join(memory))}")
    if UI.connectors is not None and UI.connectors.connectors:
        lines.append(f"  [{MUTED}]connectors:[/] {escape(connector_names(UI.connectors))}")
    width = max(Text.from_markup(line).cell_len for line in lines) + 2
    out(f"[{ACCENT}]╭{'─' * (width + 1)}╮[/]")
    for line in lines:
        pad = width - Text.from_markup(line).cell_len
        out(f"[{ACCENT}]│[/] {line}{' ' * pad}[{ACCENT}]│[/]")
    out(f"[{ACCENT}]╰{'─' * (width + 1)}╯[/]")
    if not memory:
        out(f"\n [{MUTED}]Tip: run /init to write {MEMORY_FILE}, a project summary loaded every session.[/]")
    print()


INTERRUPT_NOTE = "I see that you interrupted my previous reply."
# Plan mode and /ask take the editing tools away; without being told, a model
# asked for a change "answers" that it made it.
READ_ONLY_NOTE = ("(Read-only request: you can't change files this time. If a change is asked for, "
                  "explain what you would change and where, and say that nothing was changed yet.)")


def _was_interrupted(coder) -> bool:
    # Aider records a mid-stream interrupt in the conversation instead of
    # calling keyboard_interrupt(); catch both.
    recent = (coder.done_messages + coder.cur_messages)[-2:]
    return UI.interrupted or any(m.get("content") == INTERRUPT_NOTE for m in recent)


def _whole_file_retry(coder, text: str, before: dict):
    """Model-agnostic fallback for a change request that produced no edit
    (e.g. the diff format's fences collided with fences in a Markdown file,
    which many models handle badly): ask once more in Aider's whole-file
    format, show only the resulting diff, then return to the usual format."""
    count = coder.main_model.token_count
    try:
        size = sum(count(Path(f).read_text(encoding=coder.io.encoding, errors="replace")) for f in coder.abs_fnames)
    except OSError:
        return coder, set()
    if not coder.abs_fnames or size > CONTEXT_MGR.budget // 3:
        return coder, set()
    files = ", ".join(coder.get_inchat_relative_files())
    step(f"No edit came through; retrying by rewriting {escape(files)}", YELLOW)
    whole = base_coder.Coder.create(io=coder.io, from_coder=coder, edit_format="whole", summarize_from_coder=False)
    whole.stream = False
    UI.quiet = True
    try:
        with EscWatcher() as w:
            SESSION.watcher = w
            with Thinking():
                whole.run_one(text, preproc=False)
    except KeyboardInterrupt:
        UI.interrupted = True
    finally:
        UI.quiet = False
        SESSION.watcher = None
    edited = set(whole.aider_edited_files or ())
    back = base_coder.Coder.create(io=coder.io, from_coder=whole, edit_format=coder.edit_format, summarize_from_coder=False)
    return back, edited


def _drop_refused(coder):
    """Removes the refused message and Aider's placeholder answer from the
    conversation, so a retry doesn't send them twice."""
    msgs = coder.cur_messages
    if msgs and msgs[-1]["role"] == "assistant" and "FinishReasonLength" in str(msgs[-1].get("content")):
        msgs.pop()
    if msgs and msgs[-1]["role"] == "user":
        msgs.pop()


# ---------------------------------------------------------------------------
# The agent engine: the model reads the repository through tools (agent.py)
# ---------------------------------------------------------------------------


def make_agent(coder) -> Agent:
    """One agent per session, over the coder's repository. Aider's coder still
    provides the prompt, commands, sessions and the outline parser."""
    stream = {"md": None}

    def on_step(title: str, detail: str):
        if title.startswith(("Update(", "Create(")) and not detail.startswith("Error"):
            return  # the diff is already on screen (on_edited)
        SPIN.stop()
        out(f"\n[{GREEN}]⏺[/] {escape(title)}")
        step(escape(detail))
        SPIN.start()

    def on_stream(text: str, final: bool):
        if stream["md"] is None:
            if not hide_reasoning(text).strip() and not final:
                return
            SPIN.stop()
            stream["md"] = BulletStream()
        stream["md"].update(text, final=final)
        if final:
            stream["md"] = None

    def on_edited(rel: str, before, after: str):
        key = str((Path(coder.root) / rel).resolve())
        UI.before.setdefault(key, before or "")
        UI.originals.setdefault(key, before)  # None: created this turn (for /undo)
        SPIN.stop()
        show_diff(rel, before or "", after)
        SPIN.start()

    def on_confirm(command: str) -> bool:
        SPIN.stop()
        if SESSION.watcher:
            SESSION.watcher.pause()
        try:
            return coder.io.confirm_ask("Run this command?", subject=command, explicit_yes_required=True)
        finally:
            if SESSION.watcher:
                SESSION.watcher.resume()
            SPIN.start()

    def on_confirm_tool(name: str, arguments: str, read_only: bool) -> bool:
        """A connector tool reaches outside the repository, so each call is
        the user's to allow: once, or for the rest of the session."""
        if name in UI.tools_allowed:
            return True
        SPIN.stop()
        note = f" [{MUTED}](its connector says it only reads)[/]" if read_only else ""
        out(f"\n[{YELLOW}]⏺[/] [bold]Call this connector tool?[/]{note}")
        for line in f"{name} {arguments}".splitlines()[:30]:
            out(f"  [{MUTED}]│[/] {escape(line)}")
        answer = coder.io._ask_line("  Allow? (y/N, a = always in this session) ").strip().lower()
        ok = answer in ("y", "yes", "a", "always")
        if answer in ("a", "always"):
            UI.tools_allowed.add(name)
        step(("Allowed for this session" if name in UI.tools_allowed else "Allowed") if ok else "Declined",
             GREEN if ok else MUTED)
        SPIN.start()
        return ok

    def on_notice(text: str):
        SPIN.stop()
        step(f"⚠ {escape(text)}", YELLOW)
        SPIN.start()

    hooks = Hooks(step=on_step, stream=on_stream, edited=on_edited, confirm_run=on_confirm, notice=on_notice,
                  confirm_tool=on_confirm_tool,
                  waiting=lambda label: SPIN.start(label), interrupted=lambda: UI.interrupted)
    agent = Agent(ws=Workspace(coder.root, getattr(coder, "repo_map", None), coder.io.encoding),
                  hooks=hooks, context=CONTEXT, reasoning=REASONING, web=WEB, memory=_memory(coder),
                  connectors=UI.connectors)
    # -c / -r: continue from the restored conversation (questions and answers).
    for m in coder.done_messages or []:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            agent.messages.append({"role": m["role"], "content": m["content"], "_user": m["role"] == "user"})
    return agent


def connect_connectors(coder) -> Hub:
    """Starts the user's connectors (MCP servers) before the first request, so
    their tools are in the system prompt from the start and stay there (a
    later change costs one uncached request)."""
    import atexit

    hub = Hub(coder.root)
    atexit.register(hub.close)
    hub.connect(ask_about_connector(coder), before=lambda: SPIN.start("Connecting to connectors"))
    SPIN.stop()
    return hub


def _memory(coder) -> str:
    parts = []
    for name in (MEMORY_FILE, *EXTRA_MEMORY):
        p = Path(coder.root) / name
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8"))
            except OSError:
                pass
    return "\n\n".join(parts)


def agent_turn(coder, text: str):
    """A message for the agent. It decides itself whether the message needs
    an answer or a change; nothing has to be typed for that. Edits are off
    only when the user switched them off (plan mode, /ask). Whether the
    message reads as a change request is passed along as a hint."""
    can_edit = not UI.plan_mode
    if text.startswith("/ask "):
        text, can_edit = text[5:].strip(), False
    read_only = not can_edit
    wants_change = can_edit and wants_edit(text)
    agent = UI.agent
    if agent.memory != _memory(coder):  # # notes and /init change it
        agent.memory = _memory(coder)
        agent.reset_system()
    UI.interrupted = False
    UI.server_error = ""
    UI.before = {}
    UI.originals = {}
    coder.io.user_input(text, log_only=True)  # the session file, for -c / -r
    # Files the user pointed at (/add, @file): the agent reads them as needed.
    pointed = sorted(coder.get_rel_fname(f) for f in coder.abs_fnames)
    if pointed:
        text += "\n\n(Files the user added to the chat: " + ", ".join(pointed) + ")"
    if read_only:
        text += "\n\n" + READ_ONLY_NOTE
    reply = ""
    try:
        with EscWatcher() as w:
            SESSION.watcher = w
            SPIN.start()
            reply = agent.turn(text, can_edit=can_edit, wants_change=wants_change)
    except KeyboardInterrupt:
        UI.interrupted = True
        agent.messages.append({"role": "assistant", "content": "(Interrupted by the user.)"})
    except Exception as e:  # noqa: BLE001 - shown, and the session goes on
        SPIN.stop()
        msg = str(e)
        if "401" in msg or "403" in msg:
            step("This device is no longer paired with the server (revoked or expired). Pair again: "
                 "oppx pair <server> <code> --force", RED)
        else:
            step(f"The request failed: {escape(msg[:300])}", RED)
    finally:
        SESSION.watcher = None
        SPIN.stop()
    if UI.interrupted:
        step("Interrupted by user", YELLOW)
    if reply:
        coder.io.ai_output(reply)
    if UI.originals:
        UI.undo = dict(UI.originals)
    CONTEXT_MGR.used = agent.last_prompt_tokens
    return coder


def run_turn(coder, text: str):
    """Runs one message; returns the coder to continue with (a new one after
    a mode switch such as /ask, which runs in its own temporary coder)."""
    if UI.agent is not None and (not text.startswith("/") or text.startswith("/ask ")):
        return agent_turn(coder, text)
    coder.io._retry_shown = False
    UI.interrupted = False
    UI.server_error = ""
    UI.before = {}
    UI.originals = {}
    SESSION.coder = coder
    CONTEXT_MGR.start_turn(coder, text)
    before = snapshot(coder)
    result = coder
    CONTEXT_MGR.overflow = None
    # A shell command owns the terminal (a pager, a prompt): the Esc watcher
    # would read its keystrokes. Ctrl-C still reaches the command.
    shell = text.split(" ", 1)[0] in ("/run", "/test", "/lint")
    try:
        with contextlib.nullcontext() if shell else EscWatcher() as w:
            SESSION.watcher = w
            coder.run_one(text, preproc=True)
            if CONTEXT_MGR.overflow is not None:
                # The server refused it as too long; the budget now uses the
                # measured ratio and the server's limit, so refit and resend once.
                sent, limit = CONTEXT_MGR.overflow
                step(f"Context: the request was {sent:,} tokens, over the server's limit of {limit:,}; "
                     "fitting it again", MUTED)
                _drop_refused(coder)
                CONTEXT_MGR.overflow = None
                coder.run_one(text, preproc=True)
                if CONTEXT_MGR.overflow is not None:
                    _drop_refused(coder)
                    step("Still too long for the model. Try /clear, /drop some files, or a shorter message.", YELLOW)
    except SwitchCoder as switch:
        if getattr(switch, "placeholder", None) is not None:
            coder.io.placeholder = switch.placeholder
        kwargs = dict(io=coder.io, from_coder=coder)
        kwargs.update(switch.kwargs)
        kwargs.pop("show_announcements", None)
        result = base_coder.Coder.create(**kwargs)
    except KeyboardInterrupt:
        UI.interrupted = True
    finally:
        SESSION.watcher = None
    interrupted = _was_interrupted(result)
    if interrupted:
        step("Interrupted by user", YELLOW)
    edited = set(coder.aider_edited_files or ())
    wanted_edit = not text.startswith("/") and result is coder and result.edit_format not in ("ask", "whole")
    if wanted_edit and not edited and not interrupted and not UI.server_error:
        result, edited = _whole_file_retry(result, text, before)
        if not edited and not UI.interrupted:
            step("No changes were made. Name the exact place to change, or @-mention the file, and try again.", YELLOW)
    show_edits(coder, before, edited=edited)
    if edited and UI.originals:
        UI.undo = dict(UI.originals)
    for rel in edited:
        CONTEXT_MGR.note(coder.abs_root_path(rel), PRI_EDITED)
    CONTEXT_MGR.end_turn(result)
    SESSION.coder = result
    return result


def run(argv) -> int:
    if not aider.__version__.startswith(TESTED_AIDER):
        out(f"[{YELLOW}]warning:[/] this frontend was tested with engine {TESTED_AIDER}.x; found {aider.__version__}.")
    coder = build_coder(argv)
    load_memory(coder)
    if ENGINE == "agent":
        UI.connectors = connect_connectors(coder)
        UI.agent = make_agent(coder)
    initial = os.environ.get("OPPX_INITIAL", "").strip()

    if PRINT_MODE:
        text = translate(coder, initial) if initial else None
        if text:
            run_turn(coder, text)
        return 0

    welcome(coder)
    show_connector_problems(UI.connectors)
    pending = initial
    while True:
        if not pending and UI.queued:
            pending = UI.queued.pop(0)
        if pending:
            text, pending = pending, ""
            out(f"[{ACCENT} bold]>[/] {escape(text)}")
        else:
            try:
                text = coder.get_input()
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
        if not text:
            continue
        SPIN.begin_turn()  # visible from Enter on (classification, repo map, context, model)
        try:
            text = translate(coder, text)
        except EOFError:
            SPIN.stop()
            break
        if text and text.split(" ", 1)[0] in ("/run", "/test", "/lint"):
            # Shell commands print straight to the terminal: no spinner over them.
            SPIN.stop()
        if text:
            coder = run_turn(coder, text)
            load_memory(coder)  # picks up OPENPHALANX.md right after /init
        SPIN.stop()
        print()
    out(f"[{MUTED}]Bye. Your changes are in the working tree; review them with git diff.[/]")
    return 0


def main() -> int:
    # `!git log` and friends: the output goes into the chat, so no pager
    # (less would take over the terminal and wait for keys).
    os.environ["PAGER"] = os.environ["GIT_PAGER"] = "cat"
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
