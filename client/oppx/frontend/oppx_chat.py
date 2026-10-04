"""OpenPhalanx chat: a terminal frontend over Aider's editing engine.

`oppx` embeds this file and runs it with the Python interpreter of the
installed Aider, so `aider`, `prompt_toolkit` and `rich` are importable. It
lets Aider's own `main()` build the coder (repo map, edit formats, model
settings) from the arguments oppx passes, but swaps in our InputOutput class
first, then drives `coder.run_one()` from our own prompt. Every line on screen,
every prompt and every confirmation goes through this file.

Keys and commands follow Claude Code, so people can switch without relearning.

Environment from oppx: OPENAI_API_BASE / OPENAI_API_KEY (the loopback proxy),
OPPX_SERVER, OPPX_CONTEXT, OPPX_WEB ("1"/"0"), OPPX_VERSION, OPPX_BIN,
OPPX_INITIAL (a first prompt) and OPPX_PRINT ("1": answer once and exit).
"""

import datetime
import difflib
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import aider
import aider.coders.base_coder as base_coder
import httpx
import aider.main as aider_main
from aider.commands import SwitchCoder
from aider.io import AutoCompleter, InputOutput
from aider.mdstream import MarkdownStream
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.enums import EditingMode
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
PRINT_MODE = os.environ.get("OPPX_PRINT") == "1"
MODEL_LABEL = "openphalanx-coder"
MEMORY_FILE = "OPENPHALANX.md"  # our CLAUDE.md
EXTRA_MEMORY = ("AGENTS.md",)  # read too when present

console = Console(highlight=False)


class UiState:
    """Prompt state shared with key bindings and the status bar."""

    plan_mode = False  # shift+tab: discuss only, no edits (Aider's ask mode)
    quiet = False  # suppress the model's text (whole-file retries show only the diff)
    before: dict = {}  # abs path -> content before this turn's first write
    interrupted = False
    queued: list = []  # whole lines typed while the model was working
    prefill = ""  # a partly typed line, put back at the next prompt
    last_ctrl_c = 0.0
    hint = ""
    hint_until = 0.0
    vi = False


UI = UiState()

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
        r"^\^C again to exit",
        r"is already in the chat",
        # Aider's context warnings; the context manager below handles this.
        r"Your estimated chat context",
        r"^To reduce the chat context",
        r"^- Use /drop",
        r"^- Use /clear",
        r"^- Break your code",
        r"probably safe to try",
        r"best to only add files that need changes",
        r"Unable to find a fencing strategy",
        # Aider's background summarizer; the context manager compacts itself.
        r"^Summarization failed",
        r"summarizer unexpectedly failed",
        r"^Applied edit to ",  # shown as a diff after the turn instead
    )
]


def out(markup: str) -> None:
    console.print(markup, soft_wrap=False)


def step(markup: str, color: str = MUTED) -> None:
    """An indented result line under the current step, Claude style."""
    out(f"  [{color}]{ELBOW}[/]  {markup}")


def headline(markup: str, color: str = ACCENT) -> None:
    out(f"\n[{color}]{BULLET}[/] {markup}")


# ---------------------------------------------------------------------------
# Context manager: keep every request inside the model's context window
# ---------------------------------------------------------------------------

OUTPUT_RESERVE = 4096  # room left for the model's answer
TOKEN_MARGIN = 0.10  # Aider counts with a generic tokenizer; Qwen's differs a little
MAP_MAX, MAP_MIN = 8192, 1024  # repo map share of the budget (Aider alone allows ~28k)
# File priorities: higher stays longer.
PRI_MODEL, PRI_USER, PRI_EDITED = 1, 2, 3


class ContextManager:
    """Fits each request into the budget before it is sent, cheapest loss first:
    1. summarize older conversation (like Claude's auto-compact);
    2. set aside files the model asked for and never edited, least recently
       used first; they stay visible in the repo map as signatures;
    3. shrink the repo map (down to MAP_MIN);
    4. set aside older files you added, then older edited files.
    The current turn's files are never set aside."""

    def __init__(self):
        self.budget = int((CONTEXT - OUTPUT_RESERVE) / (1 + TOKEN_MARGIN))
        self.turn = 0
        self.files = {}  # abs path -> [priority, last turn used]
        self.used = 0  # tokens of the last request, for the status bar
        self.notes = []  # what was done to make room this turn

    def note(self, abs_path: str, priority: int):
        cur = self.files.get(abs_path)
        self.files[abs_path] = [max(priority, cur[0]) if cur else priority, self.turn]

    def start_turn(self, coder, user_text: str):
        self.turn += 1
        self.notes = []
        # Files already in the chat that we haven't seen came from /add or @.
        for f in coder.abs_fnames:
            if f not in self.files:
                self.note(f, PRI_USER)
        for f in coder.abs_fnames:
            if Path(f).name in user_text or coder.get_rel_fname(f) in user_text:
                self.note(f, max(self.files[f][0], PRI_USER))

    def end_turn(self, coder):
        # Model requests are recorded in accept_file; anything else new came
        # from /add or an @mention.
        for f in coder.abs_fnames:
            if f not in self.files:
                self.note(f, PRI_USER)
        for rel in coder.aider_edited_files or ():
            self.note(coder.abs_root_path(rel), PRI_EDITED)
        try:
            self.used = _raw_tokens(coder)
        except Exception:  # noqa: BLE001 - only feeds the status bar
            pass

    def file_tokens(self, coder, rel: str) -> int:
        try:
            text = Path(coder.abs_root_path(rel)).read_text(encoding=coder.io.encoding, errors="replace")
        except OSError:
            return 0
        return coder.main_model.token_count(text)

    def accept_file(self, coder, rel: str) -> bool:
        """Called when the model asks for a file: refuse ones that can't fit."""
        n = self.file_tokens(coder, rel)
        if n > self.budget * 0.6:
            step(f"Not loading [bold]{escape(rel)}[/]: {n:,} tokens is too large for the "
                 f"{CONTEXT // 1024}k context. Ask about specific functions instead.", YELLOW)
            return False
        self.note(coder.abs_root_path(rel), PRI_MODEL)
        return True

    def _droppable(self, coder, max_priority: int):
        cands = [
            (self.files.get(f, [PRI_MODEL, 0])[0], self.files.get(f, [PRI_MODEL, 0])[1], f)
            for f in coder.abs_fnames
            if self.files.get(f, [PRI_MODEL, 0])[1] < self.turn
            and self.files.get(f, [PRI_MODEL, 0])[0] <= max_priority
        ]
        return sorted(cands)  # lowest priority, then least recently used

    def fit(self, coder):
        rm = coder.repo_map
        if rm is not None:
            rm.map_mul_no_files = 1
            rm.max_map_tokens = MAP_MAX
        total = _raw_tokens(coder)
        compacted = False
        set_aside = []
        while total > self.budget:
            history = coder.main_model.token_count(coder.done_messages) if coder.done_messages else 0
            if not compacted and history > 1024:
                compacted = True
                before = history
                with Thinking():
                    coder.done_messages = coder.summarizer.summarize_all(coder.done_messages)
                after = coder.main_model.token_count(coder.done_messages)
                self.notes.append(f"summarized the conversation ({before:,} → {after:,} tokens)")
            elif cands := self._droppable(coder, PRI_MODEL):
                f = cands[0][2]
                coder.abs_fnames.discard(f)
                set_aside.append(coder.get_rel_fname(f))
            elif rm is not None and rm.max_map_tokens > MAP_MIN:
                rm.max_map_tokens = max(MAP_MIN, rm.max_map_tokens // 2)
            elif cands := self._droppable(coder, PRI_EDITED):
                f = cands[0][2]
                coder.abs_fnames.discard(f)
                set_aside.append(coder.get_rel_fname(f))
            else:
                break
            total = _raw_tokens(coder)
        if set_aside:
            self.notes.append("set aside " + ", ".join(set_aside) + " (still in the repo map; mention them to bring back)")
        for n in self.notes:
            step(f"Context: {escape(n)}", MUTED)
        self.notes = []
        self.used = total


CONTEXT_MGR = ContextManager()
_orig_format_messages = base_coder.Coder.format_messages


def _raw_tokens(coder) -> int:
    return coder.main_model.token_count(_orig_format_messages(coder).all_messages())


def _fitted_format_messages(self):
    """Every request Aider sends is built here: fit it first."""
    if not getattr(self, "_oppx_fitting", False):
        self._oppx_fitting = True
        try:
            CONTEXT_MGR.fit(self)
        finally:
            self._oppx_fitting = False
    return _orig_format_messages(self)


def _check_tokens(self, messages):
    """Final guard (replaces Aider's "proceed anyway?"): never send a request
    that can't fit; the server would reject it anyway."""
    n = self.main_model.token_count(messages)
    if n * (1 + TOKEN_MARGIN) <= CONTEXT - 512:
        return True
    step(f"This request needs about {n:,} tokens, more than the model's {CONTEXT:,}-token context "
         "even after making room. Drop files (/drop) or split the request.", RED)
    return False


# ---------------------------------------------------------------------------
# Esc interrupts a running answer (Ctrl-C does too)
# ---------------------------------------------------------------------------


class EscWatcher:
    """While the model works, reads the terminal in cbreak mode and turns a
    lone Esc into SIGINT, which Aider already handles as "interrupt". Paused
    whenever we need to ask the user something mid-turn."""

    def __init__(self):
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = None
        self._saved = None

    def _raw(self):
        import termios
        import tty

        if self.fd is not None and self._saved is None:
            self._saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)

    def _restore(self):
        import termios

        if self.fd is not None and self._saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            self._saved = None

    def _run(self):
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if not r or self._paused.is_set():
                continue
            data = os.read(self.fd, 64)
            # A lone ESC; arrow keys and the like arrive as ESC + more bytes.
            if data == b"\x1b":
                os.kill(os.getpid(), signal.SIGINT)
            elif not data.startswith(b"\x1b"):
                self._typed(data.decode("utf-8", "ignore"))

    def _typed(self, text: str):
        """Keeps what the user types while the model works (Claude-style
        type-ahead): finished lines are queued, the rest is pre-filled."""
        for ch in text:
            if ch in "\r\n":
                if UI.prefill.strip():
                    UI.queued.append(UI.prefill.strip())
                UI.prefill = ""
            elif ch in "\x7f\b":
                UI.prefill = UI.prefill[:-1]
            elif ch.isprintable():
                UI.prefill += ch

    def __enter__(self):
        if self.fd is not None:
            self._raw()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.5)
        self._restore()

    def pause(self):
        if self._thread:
            self._paused.set()
            time.sleep(0.12)  # let the reader leave select()
            self._restore()

    def resume(self):
        if self._thread:
            self._raw()
            self._paused.clear()


WATCHER: EscWatcher | None = None
CODER = None  # the active coder, for confirmations that need it


def _interrupted(self):
    """Replaces Aider's Ctrl-C handler, which exits on a second press within
    2 s. Here an interrupt only stops the answer; quitting happens at the
    prompt (Ctrl-C twice or Ctrl-D), as in Claude Code. The message is
    printed after the answer's live view has closed (see run_turn)."""
    UI.interrupted = True


# ---------------------------------------------------------------------------
# Aider's IO, restyled
# ---------------------------------------------------------------------------


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
        if "reflections allowed, stopping" in m:
            step("Couldn't apply the edit after several tries; try rephrasing or @-mention the file", RED)
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

    def write_text(self, filename, content, *args, **kwargs):
        """Every edit format writes through here: remember the file's content
        before the turn's first write, so the diff is right even for files
        that entered the chat mid-turn."""
        key = str(Path(filename).resolve())
        if key not in UI.before:
            try:
                UI.before[key] = Path(filename).read_text(encoding=self.encoding)
            except (OSError, UnicodeDecodeError):
                UI.before[key] = ""
        return super().write_text(filename, content, *args, **kwargs)

    def get_assistant_mdstream(self):
        return BulletStream(
            mdargs=dict(style=self.assistant_output_color, code_theme=self.code_theme, inline_code_lexer="text")
        )

    def assistant_output(self, message, pretty=None):
        if UI.quiet:
            return
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
        # Files the model asks for: load them if they can fit.
        if "add file" in q.lower() and subject and CODER is not None:
            return CONTEXT_MGR.accept_file(CODER, str(subject))
        # Everything else (creating files, adding command output to the chat)
        # is routine: say yes quietly.
        return True

    def prompt_ask(self, question, default="", subject=None):
        if subject:
            out(f"  [{MUTED}]{escape(str(subject))}[/]")
        answer = self._ask_line(f"  {question.strip()} ")
        return answer or default

    def _ask_line(self, message: str) -> str:
        if PRINT_MODE:
            return ""  # never block a one-shot run on a question
        if WATCHER:
            WATCHER.pause()
        try:
            # Plain line input: no prompt_toolkit, so no cursor-position
            # requests whose replies the paused Esc reader could swallow.
            return input(message)
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
        finally:
            if WATCHER:
                WATCHER.resume()

    # ---- input --------------------------------------------------------------
    def get_input(self, root, rel_fnames, addable_rel_fnames, commands, abs_read_only_fnames=None, edit_format=None):
        completer = MentionCompleter(
            AutoCompleter(root, rel_fnames, addable_rel_fnames, commands, self.encoding, abs_read_only_fnames),
            sorted(set(rel_fnames) | set(addable_rel_fnames)),
        )
        if self._session is None:
            try:
                history = FileHistory(self.input_history_file) if self.input_history_file else InMemoryHistory()
            except OSError:
                history = InMemoryHistory()
            self._session = PromptSession(
                history=history, key_bindings=_keys(), multiline=True, enable_history_search=False
            )
        self._session.app.editing_mode = EditingMode.VI if UI.vi else EditingMode.EMACS
        width = console.size.width
        out(f"[{MUTED}]{'─' * width}[/]")
        text = self._session.prompt(
            lambda: HTML("<plan>⏸ </plan><prompt>&gt; </prompt>" if UI.plan_mode else "<prompt>&gt; </prompt>"),
            completer=completer,
            complete_while_typing=True,
            placeholder=HTML('<placeholder>Try "explain this repo", "fix the failing test", or /help</placeholder>'),
            bottom_toolbar=lambda: self._toolbar(rel_fnames),
            style=PROMPT_STYLE,
            prompt_continuation="  ",
            refresh_interval=0.5,
            default=UI.prefill,
        )
        UI.prefill = ""
        out(f"[{MUTED}]{'─' * width}[/]")
        text = text.strip()
        self.user_input(text)
        return text

    def _toolbar(self, rel_fnames):
        if UI.hint and time.time() < UI.hint_until:
            return HTML(f" <hint>{UI.hint}</hint>")
        files = len(rel_fnames)
        web = "web search on" if WEB else "web search off"
        used = f"context {min(99, round(100 * CONTEXT_MGR.used / CONTEXT))}% · " if CONTEXT_MGR.used else ""
        mode = "<plan>⏸ plan mode on</plan> (shift+tab to cycle) · " if UI.plan_mode else "? for shortcuts · "
        return HTML(
            f" {mode}{used}<b>{SERVER}</b> · {MODEL_LABEL} · {CONTEXT // 1024}k context · {web} · "
            f'{files} file{"s" if files != 1 else ""} in chat'
        )


PROMPT_STYLE = Style.from_dict(
    {
        "prompt": f"{ACCENT} bold",
        "plan": f"{YELLOW} bold",
        "hint": f"{YELLOW}",
        "placeholder": f"{MUTED} italic",
        "bottom-toolbar": f"bg:#111725 {MUTED}",
        "bottom-toolbar.text": f"{MUTED}",
    }
)

SHORTCUTS = [
    ("! for bash mode", "@ to mention a file", "# to save to memory"),
    ("/ for commands", "shift+tab plan mode", "esc to interrupt"),
    ("\\⏎ for a new line", "ctrl-r history search", "ctrl-c ×2 / ctrl-d to exit"),
]


def _flash(text: str, seconds: float = 2.0) -> None:
    UI.hint, UI.hint_until = text, time.time() + seconds


def _keys() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        buf = event.current_buffer
        if buf.complete_state:
            buf.complete_state = None  # accept the completion, don't send yet
            return
        if buf.document.text_before_cursor.endswith("\\"):
            buf.delete_before_cursor(1)
            buf.insert_text("\n")  # backslash + Enter: new line, as in Claude Code
            return
        if buf.text.strip() == "?":
            buf.reset()
            show_shortcuts()
            return
        buf.validate_and_handle()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("c-c")
    def _(event):
        buf = event.current_buffer
        if buf.text:
            buf.reset()  # clear the line first
            return
        now = time.time()
        if now - UI.last_ctrl_c < 2.0:
            event.app.exit(exception=EOFError)
            return
        UI.last_ctrl_c = now
        _flash("Press Ctrl-C again to exit")

    @kb.add("s-tab")
    def _(event):
        UI.plan_mode = not UI.plan_mode
        _flash("⏸ plan mode on: the model discusses and plans, no edits" if UI.plan_mode else "plan mode off: edits allowed")

    return kb


def show_shortcuts():
    for row in SHORTCUTS:
        out("  " + "".join(f"[{MUTED}]{c:<28}[/]" for c in row))


class MentionCompleter(Completer):
    """Aider's completions, plus `@path` file mentions."""

    def __init__(self, inner, files):
        self.inner = inner
        self.files = files

    def get_completions(self, document, complete_event):
        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("@"):
            stem = word[1:].lower()
            for f in self.files:
                if stem in f.lower():
                    yield Completion("@" + f, start_position=-len(word), display=f)
            return
        yield from self.inner.get_completions(document, complete_event)


class BulletStream(MarkdownStream):
    """Assistant replies start with a bullet, like Claude's CLI. Raw edit
    blocks are hidden while they stream; the turn ends with a real diff."""

    def update(self, text, final=False):
        if UI.quiet:
            return
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
            sys.stdout.write(f"\r\x1b[2K\x1b[38;2;94;234;212m{frame}\x1b[0m {word}… \x1b[2m({secs}s · esc to interrupt)\x1b[0m")
            sys.stdout.flush()
            i += 1
            self._stop.wait(self.delay)
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    def start(self):
        if not PRINT_MODE:
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
    out(f"\n  [bold]Shortcuts[/]")
    show_shortcuts()
    out(f"\n  [{MUTED}]Edits are never committed: review with git diff, revert with git checkout -- <file>.[/]")
    out(f"  [{MUTED}]Later: oppx -c continues this conversation, oppx -r picks an older one.[/]\n")


def show_status(coder):
    headline("[bold]Status[/]")
    rows = [
        ("server", f"{SERVER}"),
        ("model", f"{MODEL_LABEL} · {CONTEXT // 1024}k context"),
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
    with Thinking():
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


INTENT_PROMPT = """Classify the programmer's message to a coding assistant.

Answer "edit" when the message asks to change the code or files in any way: add, write, create, implement, fix, refactor, rename, remove, delete, update, optimize, or "make it ..." — including polite questions such as "can you add tests?".
Answer "ask" when the message only wants information: a question, an explanation, a review, an opinion, or a plan, without asking for any change to be made.

Examples:
Message: What does the parse function return? -> ask
Message: Explain how the cache works in this repo. -> ask
Message: Why does this test fail? -> ask
Message: Is this function thread safe? -> ask
Message: Review my changes. -> ask
Message: Add a function sub(a, b) to calc.py. -> edit
Message: Can you add unit tests for config.rs? -> edit
Message: Fix the failing test. -> edit
Message: Why does this test fail? Please fix it. -> edit
Message: Rename x to count. -> edit
Message: Make the parser faster. -> edit

Reply with exactly one word: ask or edit."""


def wants_edit(text: str) -> bool:
    """One constrained token from the server's model (~40 ms): does this
    message ask for a change, or only for information? Questions then run
    without edits, as in Claude Code. Falls back to "edit" on any error."""
    base, key = os.environ.get("OPENAI_API_BASE"), os.environ.get("OPENAI_API_KEY")
    if not base or not key:
        return True
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": MODEL_LABEL,
                "messages": [{"role": "system", "content": INTENT_PROMPT}, {"role": "user", "content": text[-2000:]}],
                "regex": "(ask|edit)",
                "max_tokens": 2,
                "temperature": 0,
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip() != "ask"
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return True


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
            return f"/ask {text}"
        return text

    name, _, arg = text.partition(" ")
    arg = arg.strip()
    if name in ("/help", "/?"):
        show_help()
    elif name in ("/exit", "/quit"):
        raise EOFError
    elif name == "/clear":
        coder.commands.cmd_clear("")
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
        headline(f"[bold]{MODEL_LABEL}[/] · {CONTEXT // 1024}k context on {escape(SERVER)}")
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


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def build_coder(argv):
    aider_main.InputOutput = OppxIO  # main() constructs its IO from this name
    base_coder.WaitingSpinner = Thinking
    base_coder.Coder.keyboard_interrupt = _interrupted
    base_coder.Coder.format_messages = _fitted_format_messages
    base_coder.Coder.check_tokens = _check_tokens
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
        f"  [{MUTED}]server:[/] {escape(SERVER)} · {MODEL_LABEL} · {CONTEXT // 1024}k context · web search {web}",
    ]
    if memory:
        lines.append(f"  [{MUTED}]memory:[/] {escape(', '.join(memory))}")
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
            globals()["WATCHER"] = w
            with Thinking():
                whole.run_one(text, preproc=False)
    except KeyboardInterrupt:
        UI.interrupted = True
    finally:
        UI.quiet = False
        globals()["WATCHER"] = None
    edited = set(whole.aider_edited_files or ())
    back = base_coder.Coder.create(io=coder.io, from_coder=whole, edit_format=coder.edit_format, summarize_from_coder=False)
    return back, edited


def run_turn(coder, text: str):
    """Runs one message; returns the coder to continue with (a new one after
    a mode switch such as /ask, which runs in its own temporary coder)."""
    global WATCHER
    global CODER
    coder.io._retry_shown = False
    UI.interrupted = False
    UI.before = {}
    CODER = coder
    CONTEXT_MGR.start_turn(coder, text)
    before = snapshot(coder)
    result = coder
    try:
        with EscWatcher() as WATCHER:
            coder.run_one(text, preproc=True)
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
        WATCHER = None
    interrupted = _was_interrupted(result)
    if interrupted:
        step("Interrupted by user", YELLOW)
    edited = set(coder.aider_edited_files or ())
    wanted_edit = not text.startswith("/") and result is coder and result.edit_format not in ("ask", "whole")
    if wanted_edit and not edited and not interrupted:
        result, edited = _whole_file_retry(result, text, before)
        if not edited and not UI.interrupted:
            step("No changes were made. Name the exact place to change, or @-mention the file, and try again.", YELLOW)
    show_edits(coder, before, edited=edited)
    for rel in edited:
        CONTEXT_MGR.note(coder.abs_root_path(rel), PRI_EDITED)
    CONTEXT_MGR.end_turn(result)
    CODER = result
    return result


def run(argv) -> int:
    if not aider.__version__.startswith(TESTED_AIDER):
        out(f"[{YELLOW}]warning:[/] this frontend was tested with engine {TESTED_AIDER}.x; found {aider.__version__}.")
    coder = build_coder(argv)
    load_memory(coder)
    initial = os.environ.get("OPPX_INITIAL", "").strip()

    if PRINT_MODE:
        text = translate(coder, initial) if initial else None
        if text:
            run_turn(coder, text)
        return 0

    welcome(coder)
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
        try:
            text = translate(coder, text)
        except EOFError:
            break
        if text:
            coder = run_turn(coder, text)
            load_memory(coder)  # picks up OPENPHALANX.md right after /init
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
