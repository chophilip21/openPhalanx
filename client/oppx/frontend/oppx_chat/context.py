"""Fits every request Aider builds into the model's context window, cheapest loss first."""

import os
import re
import threading
from pathlib import Path

import aider.coders.base_coder as base_coder
import aider.exceptions as aider_exceptions
import httpx
from rich.markup import escape

from .config import CONTEXT, MUTED, RED, YELLOW
from .term import SPIN, Thinking, step


# ---------------------------------------------------------------------------
# Context manager: keep every request inside the model's context window
# ---------------------------------------------------------------------------

OUTPUT_RESERVE = 4096  # room left for the model's answer
# Aider counts tokens with a generic tokenizer, so its estimates are scaled by
# a factor: a safe guess until the server's own tokenizer has measured the
# real ratio (after the first answer), then that ratio plus a little slack.
UNCALIBRATED_FACTOR = 1.10
CALIBRATED_SLACK = 1.03
# Uncalibrated and above this share of the budget: measure before sending.
# The 1.10 guess was 20% short on Qwen3.6, whose first request then failed
# again and again, so no answer ever came to calibrate from.
CALIBRATE_FIRST_AT = 0.6
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
        self.ratio = None  # server tokens / Aider's estimate, measured
        # Repo map size for the session. It only shrinks (each change makes
        # the server reprocess everything after the map), until /clear.
        self.map_tokens = MAP_MAX
        self.turn = 0
        self.files = {}  # abs path -> [priority, last turn used]
        self.used = 0  # tokens of the last request, for the status bar
        self.notes = []  # what was done to make room this turn
        # The server's own input limit, once a refusal has told us (it can be
        # below the context window when its KV cache is smaller).
        self.limit = None
        self.overflow = None  # (tokens sent, limit) of this turn's refusal

    @property
    def factor(self) -> float:
        if self.ratio is None:
            return UNCALIBRATED_FACTOR
        return self.ratio * CALIBRATED_SLACK

    @property
    def budget(self) -> int:
        room = CONTEXT - OUTPUT_RESERVE
        if self.limit:
            room = min(room, self.limit - 256)
        return int(room / self.factor)

    def overflowed(self, sent: int, limit: int):
        """The server refused a request of ``sent`` tokens over its ``limit``:
        learn the real tokenizer ratio from it, so the refit is right."""
        self.overflow = (sent, limit)
        self.limit = limit
        if self.used > 500:
            ratio = min(max(sent / self.used, 0.7), 1.5)
            self.ratio = max(self.ratio or 0, ratio)

    def calibrate(self, coder, wait: bool = False):
        """Measures the request with the server's tokenizer, in the background
        (or at once with ``wait``), so the budget matches whatever model is
        loaded."""
        base, key = os.environ.get("OPENAI_API_BASE"), os.environ.get("OPENAI_API_KEY")
        if not base or not key:
            return
        try:
            text = "\n".join(str(m.get("content") or "") for m in _orig_format_messages(coder).all_messages())
            text = text[-1_000_000:]
            estimate = coder.main_model.token_count(text)
        except Exception:  # noqa: BLE001 - calibration is best effort
            return
        if estimate < 500:
            return  # too little text for a stable ratio

        def run():
            try:
                r = httpx.post(f"{base}/tokenize", headers={"Authorization": f"Bearer {key}"},
                               json={"text": text}, timeout=20)
                count = r.json()["count"] if r.status_code == 200 else None
            except Exception:  # noqa: BLE001 - older server or network: keep the guess
                return
            if not count:
                return
            ratio = min(max(count / estimate, 0.7), 1.5)
            self.ratio = ratio if self.ratio is None else 0.5 * self.ratio + 0.5 * ratio

        if wait:
            run()
        else:
            threading.Thread(target=run, daemon=True).start()

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
        self.calibrate(coder)

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
            rm.max_map_tokens = self.map_tokens
        total = _raw_tokens(coder)
        if self.ratio is None and total * UNCALIBRATED_FACTOR > CALIBRATE_FIRST_AT * (CONTEXT - OUTPUT_RESERVE):
            self.calibrate(coder, wait=True)  # a large first request: measure, don't guess
        compacted = False
        set_aside = []
        while total > self.budget:
            history = coder.main_model.token_count(coder.done_messages) if coder.done_messages else 0
            if not compacted and history > 1024:
                compacted = True
                before = history
                with Thinking(label="Compacting conversation"):
                    coder.done_messages = coder.summarizer.summarize_all(coder.done_messages)
                SPIN.start()  # keep showing progress until the answer streams
                after = coder.main_model.token_count(coder.done_messages)
                self.notes.append(f"summarized the conversation ({before:,} → {after:,} tokens)")
            elif cands := self._droppable(coder, PRI_MODEL):
                f = cands[0][2]
                coder.abs_fnames.discard(f)
                set_aside.append(coder.get_rel_fname(f))
            elif rm is not None and rm.max_map_tokens > MAP_MIN:
                rm.max_map_tokens = self.map_tokens = max(MAP_MIN, rm.max_map_tokens // 2)
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
    if n * CONTEXT_MGR.factor <= CONTEXT - 512:
        return True
    step(f"This request needs about {n:,} tokens, more than the model's {CONTEXT:,}-token context "
         "even after making room. Drop files (/drop) or split the request.", RED)
    return False


# SGLang's refusals of over-long input (both wordings; model-agnostic).
_TOO_LONG = re.compile(
    r"Input length \((\d+) tokens\) exceeds the maximum allowed length \((\d+) tokens\)"
    r"|The input \((\d+) tokens\) is longer than the model's context length \((\d+) tokens\)"
)
_orig_get_ex_info = aider_exceptions.LiteLLMExceptions.get_ex_info


def _get_ex_info(self, ex):
    """An over-long request is a context overflow, not a connection problem:
    Aider would otherwise retry the identical request with growing delays for
    about a minute, with nothing on screen."""
    m = _TOO_LONG.search(str(ex))
    if m:
        nums = [int(g) for g in m.groups() if g]
        CONTEXT_MGR.overflowed(nums[0], nums[1])
        return aider_exceptions.ExInfo("ContextWindowExceededError", False, None)
    return _orig_get_ex_info(self, ex)


aider_exceptions.LiteLLMExceptions.get_ex_info = _get_ex_info
_orig_show_exhausted = base_coder.Coder.show_exhausted_error


def _show_exhausted(self):
    """Our own refusal handling (run_turn retries once) replaces Aider's
    'context window exhausted' advice."""
    if CONTEXT_MGR.overflow is None:
        _orig_show_exhausted(self)


base_coder.Coder.show_exhausted_error = _show_exhausted
