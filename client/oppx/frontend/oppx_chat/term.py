"""Terminal output: the console, status lines, the turn spinner and Esc-to-interrupt."""

import codecs
import os
import select
import signal
import sys
import threading
import time

from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape

from .config import ACCENT, BULLET, ELBOW, MUTED, PRINT_MODE


console = Console(highlight=False)


class UiState:
    """Prompt state shared with key bindings and the status bar."""

    plan_mode = False  # shift+tab: discuss only, no edits (Aider's ask mode)
    quiet = False  # suppress the model's text (whole-file retries show only the diff)
    before: dict = {}  # abs path -> content before this turn's first write
    originals: dict = {}  # same, None for a file the turn created (for /undo)
    undo: dict = {}  # originals of the last turn that edited files
    interrupted = False
    server_error = ""  # this turn's request was refused by the server (see oppx_io.server_problem)
    queued: list = []  # whole lines typed while the model was working
    prefill = ""  # a partly typed line, put back at the next prompt
    unannounced: list = []  # queued lines not yet shown as "Queued: …" (an answer was streaming)
    last_ctrl_c = 0.0
    hint = ""
    hint_until = 0.0
    vi = False
    agent = None  # the agent (agent.Agent) when the agent engine runs
    connectors = None  # the session's MCP connectors (mcp.Hub), with the agent engine
    permissions = None  # permissions.Permissions: what runs without asking in this project
    read_only_turn = False  # plan mode or /ask: only commands that read may run


UI = UiState()


OUT_LOCK = threading.RLock()


def out(markup: str) -> None:
    with OUT_LOCK:
        SPIN.clear_line()
        console.print(markup, soft_wrap=False)


def step(markup: str, color: str = MUTED) -> None:
    """An indented result line under the current step, Claude style."""
    out(f"  [{color}]{ELBOW}[/]  {markup}")


def headline(markup: str, color: str = ACCENT) -> None:
    out(f"\n[{color}]{BULLET}[/] {markup}")


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
        self._decode = codecs.getincrementaldecoder("utf-8")(errors="ignore").decode

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
            data = os.read(self.fd, 1024)
            # A lone ESC; arrow keys and the like arrive as ESC + more bytes.
            if data == b"\x1b":
                os.kill(os.getpid(), signal.SIGINT)
            elif not data.startswith(b"\x1b"):
                # A character cut in two by the read is finished by the next one.
                self._typed(self._decode(data))

    def _typed(self, text: str):
        """Keeps what the user types while the model works (Claude-style
        type-ahead): finished lines are queued and sent when the turn ends,
        the rest is pre-filled at the next prompt. The spinner's line shows
        it (see `ahead`), since the terminal doesn't echo during a turn:
        typing used to work and show nothing, which read as "can't type"."""
        for ch in text:
            if ch in "\r\n":
                if UI.prefill.strip():
                    UI.queued.append(UI.prefill.strip())
                    UI.unannounced.append(UI.prefill.strip())
                UI.prefill = ""
            elif ch in "\x7f\b":
                UI.prefill = UI.prefill[:-1]
            elif ch == "\x15":  # Ctrl-U: the line
                UI.prefill = ""
            elif ch == "\x17":  # Ctrl-W: the last word
                UI.prefill = UI.prefill.rstrip().rpartition(" ")[0] + (" " if " " in UI.prefill.rstrip() else "")
            elif ch == "\t":
                UI.prefill += " "
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


class Session:
    """The running turn, for key handlers and confirmations."""

    coder = None  # the active coder
    watcher: EscWatcher | None = None


SESSION = Session()


def announce_queued():
    """Shows each newly queued line once, as part of the transcript. Called
    where printing is safe (the spinner's tick, the end of a turn): not while
    an answer is streaming."""
    while UI.unannounced:
        text = UI.unannounced.pop(0)
        console.print(f"  [{MUTED}]{ELBOW}  Queued for when this is done:[/] {escape(text)}", soft_wrap=False)


def fit(text: str, cells: int) -> str:
    """The end of a text that fits in so many terminal cells (what is being typed is at the end)."""
    if cell_len(text) <= cells:
        return text
    while text and cell_len(text) > cells - 1:
        text = text[1:]
    return "…" + text


def ahead(status: str, typed: str, queued: int, width: int) -> tuple[str, str]:
    """The spinner line's two parts: what the turn is doing, and what the
    user is typing ahead. Both together fit the terminal's width (the line
    is redrawn in place, so it must never wrap)."""
    note = f"{queued} queued" if queued else "" if typed else "type to queue a message"
    status = f"{status} · {note})" if note else f"{status})"
    room = width - 3 - cell_len(status)  # the spinner's own two cells, and one spare
    if room < 0:
        return fit(status, max(width - 3, 1)), ""
    return status, (fit(typed, room - 4) if typed and room >= 12 else "")


class TurnSpinner:
    """One Claude-style "✻ Thinking… (12s · esc to interrupt)" line for the
    whole turn: from Enter until the answer starts streaming, and again
    whenever we wait (edits being applied, a follow-up request, compaction).
    Anything printed clears the line first (see out()); the spinner redraws
    on its next tick."""

    # No ✳: it has an emoji form, which many terminals draw two cells wide.
    FRAMES = "·✢*✶✻✽✻✶*✢"
    WORDS = ("Thinking", "Reading", "Working", "Considering")

    def __init__(self, delay: float = 0.12):
        self.delay = delay
        self.label = None  # fixed label, or None for the rotating words
        self._active = threading.Event()
        self._drawn = False
        self._turn_start = time.time()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def begin_turn(self):
        self._turn_start = time.time()
        self.start()

    def start(self, label: str | None = None):
        if PRINT_MODE or not sys.stdout.isatty():
            return
        self.label = label
        self._active.set()

    def stop(self):
        self._active.clear()
        with OUT_LOCK:
            self.clear_line()

    def clear_line(self):
        if self._drawn:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
            self._drawn = False

    def _spin(self):
        i = 0
        while True:
            self._active.wait()
            with OUT_LOCK:
                if self._active.is_set():
                    secs = int(time.time() - self._turn_start)
                    word = self.label or self.WORDS[(secs // 6) % len(self.WORDS)]
                    frame = self.FRAMES[i % len(self.FRAMES)]
                    if UI.unannounced:
                        self.clear_line()
                        announce_queued()
                    status, typed = ahead(f"({secs}s · esc to interrupt", UI.prefill, len(UI.queued),
                                          console.size.width - cell_len(word) - 2)
                    # The text starts at a fixed column (CSI 3G), so a frame
                    # the terminal draws wider or narrower can't shift it.
                    # What is typed ahead follows, with a block for the cursor.
                    sys.stdout.write(
                        f"\r\x1b[2K\x1b[38;2;94;234;212m{frame}\x1b[0m\x1b[3G{word}… "
                        f"\x1b[2m{status}\x1b[0m" + (f"  > {typed}\x1b[7m \x1b[0m" if typed else "")
                    )
                    sys.stdout.flush()
                    self._drawn = True
            i += 1
            time.sleep(self.delay)


SPIN = TurnSpinner()


class Thinking:
    """Stands in for Aider's "Waiting for <model>" spinner (and wraps our own
    waits): start/stop drive the shared turn spinner."""

    def __init__(self, text: str = "", label: str | None = None, **_):
        self.label = label

    def start(self):
        SPIN.start(self.label)

    def stop(self):
        SPIN.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
