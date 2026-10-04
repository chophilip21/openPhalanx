"""Aider's InputOutput, restyled: the prompt, key bindings, completions and confirmations."""

import re
import time
from pathlib import Path

from aider.io import AutoCompleter
from aider.io import InputOutput
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer
from prompt_toolkit.completion import Completion
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.markup import escape

from .config import ACCENT, BULLET, CONTEXT, GREEN, MODEL_NAME, MUTED, PRINT_MODE, RED, SERVER, WEB, YELLOW
from .term import SESSION, SPIN, UI, console, out, step
from .render import BulletStream, hide_reasoning
from .context import CONTEXT_MGR


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
        r"^Retrying in [\d.]+ seconds",  # server busy or restarting: the spinner shows the wait
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


# ---------------------------------------------------------------------------
# Aider's IO, restyled
# ---------------------------------------------------------------------------


# What the user should do when the server refuses a request. Keys are what
# server_problem() returns; Aider's own follow-up hints map to "" (hidden).
SERVER_PROBLEMS = {
    "revoked": f"This device is no longer paired with {SERVER}: it was revoked on the server. "
    "Pair again with a new code from the app: oppx pair <server> <code> --force",
    "loading": f"{SERVER} is starting up or busy, so it can't answer yet. "
    "Try again in a minute; `oppx status` shows when the model is ready.",
    "unreachable": f"Can't reach {SERVER}. Check that the server is running and on the network; "
    "`oppx status` checks the connection.",
    "certificate": f"{SERVER}'s certificate changed, so nothing was sent. Run `oppx status` for details.",
}
_AIDER_HINTS = (
    "The API provider is not able to authenticate you",
    "Check your API key",
    "The API provider's servers are down or overloaded",
    "Retrying in ",
    "There was a problem with the API provider",
)


def server_problem(message: str) -> str | None:
    """Classifies an error Aider prints: a server problem kind, "" for one of
    Aider's generic follow-up hints, or None for anything else."""
    text = message or ""
    if "certificate changed" in text:
        return "certificate"
    if "cannot reach the OpenPhalanx server" in text or "Connection refused" in text or "ConnectError" in text:
        return "unreachable"
    code = re.search(r"Error code: (\d{3})", text)
    if "missing or invalid device token" in text or (code and code.group(1) == "401"):
        return "revoked"
    if (code and code.group(1) in ("502", "503")) or "ServiceUnavailableError" in text or "model is still loading" in text:
        return "loading" if not (code and code.group(1) == "502") else "unreachable"
    if any(h in text for h in _AIDER_HINTS):
        return ""
    return None


class OppxIO(InputOutput):
    """Aider's output and prompts, restyled and filtered."""


    def ai_output(self, content):
        """Answers are saved to the session file without their reasoning:
        Aider strips it only for the configured tag, and with SGLang's separate
        reasoning_content it opens with its own <thinking-content-…> tag, so the
        whole chain of thought was written to the file and resent on -c/-r."""
        super().ai_output(hide_reasoning(content or ""))
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
        if self._server_problem(message):
            return
        if message.strip():
            self.append_chat_history(message, linebreak=True, blockquote=True, strip=strip)
        self._route(message, YELLOW)

    def tool_error(self, message="", strip=True):
        self.num_error_outputs += 1
        if self._edit_retry(message):
            return
        if self._server_problem(message):
            return
        if message.strip():
            self.append_chat_history(message, linebreak=True, blockquote=True, strip=strip)
        self._route(message, RED)

    def _server_problem(self, message: str) -> bool:
        """Replaces litellm's errors and Aider's provider hints ("Check your API
        key") with what actually happened on the OpenPhalanx server, once per
        turn. Returns True when the message was handled."""
        kind = server_problem(message)
        if kind is None:
            return False
        if kind and not UI.server_error:
            UI.server_error = kind
            step(SERVER_PROBLEMS[kind], RED if kind == "revoked" else YELLOW)
        return True

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
                UI.originals[key] = UI.before[key]
            except (OSError, UnicodeDecodeError):
                UI.before[key] = ""
                UI.originals[key] = None if not Path(filename).exists() else ""
        return super().write_text(filename, content, *args, **kwargs)

    def get_assistant_mdstream(self):
        return BulletStream(
            mdargs=dict(style=self.assistant_output_color, code_theme=self.code_theme, inline_code_lexer="text")
        )

    def assistant_output(self, message, pretty=None):
        if UI.quiet:
            return
        SPIN.stop()
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
        if "add file" in q.lower() and subject and SESSION.coder is not None:
            return CONTEXT_MGR.accept_file(SESSION.coder, str(subject))
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
        SPIN.stop()
        if SESSION.watcher:
            SESSION.watcher.pause()
        try:
            # Plain line input: no prompt_toolkit, so no cursor-position
            # requests whose replies the paused Esc reader could swallow.
            return input(message)
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
        finally:
            if SESSION.watcher:
                SESSION.watcher.resume()

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
            f" {mode}{used}<b>{SERVER}</b> · {MODEL_NAME} · {CONTEXT // 1024}k context · {web} · "
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
