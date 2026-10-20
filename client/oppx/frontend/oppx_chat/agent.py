"""The agent loop: the model reads the repository on demand through tools,
instead of being sent whole files and a repo map with every request.

Each step the model picks one action as JSON, constrained by a JSON schema
(model-agnostic: no per-model tool-call parser). Actions that carry text —
an edit, a new file, the reply — are then written as plain text in a second,
unconstrained call, in the formats models already know (Aider's
SEARCH/REPLACE blocks for edits), and edits are applied with Aider's own
fuzzy matcher. The conversation only grows by appending, which keeps SGLang's
prefix cache warm; when it gets long, old tool output is cut first.

This module has no terminal code: the UI passes callbacks (see `Hooks`)."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import documents
from .browser import hint as browser_hint
from .prompts import (
    ANSWER_RESERVE, ASK_EDIT_CHECK, FAR_BACK, BROWSER_LINE, CHECKPOINT_PROMPT, CHECK_EVERY, CONNECTOR_DOWN,
    CONNECTOR_HEAD, CONNECTOR_SAME, EDIT_PLAN_PROMPT, EDIT_TOOLS, FIT_LOW, FIX_CHECKPOINT_PROMPT, IMAGE_BYTES,
    KEEP_RECENT, LOOKUPS, MAX_PLANS, MAX_SEARCHES, MAX_STEPS, MAX_STEPS_CHANGING, MAX_THEN, MODEL,
    PENDING_TURNS, PLAN_PROMPT, PLAN_TOKENS_LONG, RETHINK_PROMPT, RETRY_TEMPERATURE, REVIEW_PROMPT,
    REVIEW_TOKENS, SWEEP, SYSTEM, THINK_TOKENS, TOOLS, WHOLE_FILE, WROTE,
)
from .actions import (
    ContextOverflow, Hooks, ToolError, _outer_braces, _sentences, _signature, _title, action_schema,
    as_action, cut_action_tail, is_repeat, maybe_json, server_error, several_parts,
)
from .workspace import Workspace, _dump, _image_tokens
from .replies import Replying
from .tools import Doing
# Kept importable from here: the app, the scripts and the tests use them as agent.<name>.
from .prompts import CONNECTOR_DECLINED, PLAIN_REGEX, STATEFUL_REPEATS  # noqa: F401
from .actions import TOOL_FIELDS, _fenced, _summary, clean_arguments, degenerate  # noqa: F401
from .actions import misplaced_lookup, strip_action, whole_sentences  # noqa: F401
from .workspace import apply_edit  # noqa: F401


@dataclass
class Agent(Replying, Doing):
    ws: Workspace
    hooks: Hooks = field(default_factory=Hooks)
    base: str = field(default_factory=lambda: os.environ.get("OPENAI_API_BASE", "").rstrip("/"))
    key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    context: int = 32768
    reasoning: bool = False
    web: bool = True
    memory: str = ""  # OPENPHALANX.md / OPPX.md / AGENTS.md
    connectors: object = None  # mcp.Hub: the user's MCP connectors, as extra tools (None: none)
    # The headless browser: "none" (this session can't have one), "offer" (the
    # browser action is offered; the user is asked), "running", "declined".
    browser: str = "none"
    messages: list = field(default_factory=list)  # the conversation after the system prompt
    last_prompt_tokens: int = 0
    cached_tokens: int = 0
    computed_tokens: int = 0
    completion_tokens: int = 0
    # Real tokens per estimated token: the largest the server has shown this
    # session (characters / 3 ran ~5% short on Qwen3.6 for code).
    ratio: float = 1.0
    _sent_raw: int = 0
    # Files really changed in this conversation (an edit that applied), for the
    # facts each reply is given; and the last reply, to catch a repeat.
    changed_session: list = field(default_factory=list)
    _wrote: dict = field(default_factory=dict)  # file -> the whole text the model wrote into it
    _replies: list = field(default_factory=list)  # (user message, reply), the latest few
    # A change the user asked for that no turn has made yet (the turn got
    # stuck, was interrupted, or ended on a promise). The next message is
    # read in its light: "continue" and "where is the edit?" then mean it.
    pending: str = ""
    _pending_age: int = 0
    asking: str = ""  # the tool a command is being confirmed for ("run", "start"): the question differs
    _trail: list = field(default_factory=list)  # this turn's actions and results, one line each
    _before: dict = field(default_factory=dict)  # path -> its text before this request first changed it
    _search_off_until: float = 0.0  # the server paused or paced web search until then (monotonic)
    _ran: bool = False  # the last run/start really ran (not declined, not refused)
    down: dict = field(default_factory=dict)  # connector -> (why, steps): stopped working in this request
    _searched: list = field(default_factory=list)  # (words, query, results) of this session's web searches
    stuck: str = ""  # why the last turn stopped early ("" when it didn't)
    # Labels sent with every request (`oppx_trace`), for the server's Trace
    # page: which conversation, turn and step a request belongs to and what
    # kind of call it is. No text; a tool's title only shows there while the
    # server's owner has switched content capture on.
    trace: bool = False  # the server reads the labels (/v1/info says so); never sent otherwise
    session: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    turns: int = 0
    _step: int = 0
    _call: dict = field(default_factory=lambda: {"kind": "other"})  # the call being made now
    _events: dict = field(default_factory=dict)  # what happened since the last request (cuts, repeats…)
    _prev: dict = field(default_factory=dict)  # the last action as it turned out (tool, how long it ran here)
    _called: dict = field(default_factory=dict)  # this turn's connector calls -> the step's title
    # A stateful connector (the browser): how many calls changed its state,
    # and the last such call with how many times in a row it was made.
    _state_version: int = 0
    _state_last: tuple = ("", 0)

    # ---- model calls ------------------------------------------------------
    def _system(self) -> str:
        # Built once per session: it must stay byte-identical for the prefix cache.
        if getattr(self, "_system_text", None) is None:
            self._system_text = self._build_system()
        return self._system_text

    def reset(self):
        """/clear: a fresh conversation (the project notes are re-read)."""
        self.messages.clear()
        self._system_text = None
        self.changed_session.clear()
        self._wrote.clear()
        self._replies.clear()
        self.pending, self._pending_age = "", 0
        self.session, self.turns = uuid.uuid4().hex[:12], 0

    def undone(self, names: list[str]):
        """/undo restored these files: they no longer count as changed."""
        self.changed_session[:] = [f for f in self.changed_session if Path(f).name not in names]
        self.messages.append({"role": "user", "_result": True, "content":
                              "(The user undid your last edit: " + ", ".join(names) + " is back to how it was.)"})

    def reset_system(self):
        """The project notes or the connectors changed: rebuild the system
        prompt (this costs one uncached request)."""
        self._system_text = None

    def _connector_text(self) -> str:
        section = self.connectors.prompt_section() if self.connectors is not None else ""
        return f"\n\n{section}" if section else ""

    def _build_system(self) -> str:
        top = sorted({f.split("/")[0] + ("/" if "/" in f else "") for f in self.ws.files()})
        overview = "Top level of the repository: " + ", ".join(top[:80])
        memory = f"\n\nProject notes from the user (follow them):\n{self.memory.strip()[:8000]}" if self.memory.strip() else ""
        system = SYSTEM.replace("{BROWSER}", BROWSER_LINE if self.browser == "offer" else "")
        if not self.web:
            # Listed but never on offer, it was typed as a shell command instead.
            system = "\n".join(line for line in system.split("\n") if not line.startswith('- {"tool": "web_search"'))
            system += "\n\nWeb search is switched off in this session: if the user asks you to search the web, say that you can't here, and go on from what you know and the files."
        return f"{system}{self._connector_text()}\n\n{overview}{memory}"

    def _event(self, name: str, count: int = 1):
        self._events[name] = self._events.get(name, 0) + count

    def _trace_labels(self) -> dict:
        labels = {"session": self.session, "turn": self.turns, "step": self._step, "window": self.context,
                  "estimate": self._estimate(), **self._call, **self._prev}
        if self._events:
            labels["events"] = dict(self._events)
        self._events.clear()
        self._prev = {}
        return labels

    def _body(self, extra: dict) -> dict:
        # oppx_utility: the agent has its own web_search tool, so the gateway
        # must not route these calls through its automatic search.
        body = {"model": MODEL, "messages": [{"role": "system", "content": self._system()}] + self._clean(),
                "temperature": 0, "oppx_utility": True, **extra}
        if self.trace:
            body["oppx_trace"] = self._trace_labels()
        if self.reasoning:
            # Both spellings, like the router: gpt-oss reads the first, Qwen3
            # the second. With only the first, Qwen3.6 wrote its whole action
            # into reasoning_content and left the content empty.
            body.setdefault("chat_template_kwargs", {"reasoning_effort": "low", "enable_thinking": False})
        self._sent_raw = self._raw_estimate()
        return body

    def _post(self, body: dict) -> dict:
        r = httpx.post(f"{self.base}/chat/completions", headers={"Authorization": f"Bearer {self.key}"},
                       json=body, timeout=600)
        if r.status_code >= 400:
            raise server_error(r.status_code, r.text)
        data = r.json()
        self._count(data.get("usage") or {})
        _dump(body, (data.get("choices") or [{}])[0].get("message"), data.get("usage"))
        return data

    def _count(self, usage: dict):
        prompt = usage.get("prompt_tokens") or 0
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        self.last_prompt_tokens = prompt
        if prompt and self._sent_raw:
            self.ratio = max(self.ratio, prompt / self._sent_raw * 1.02)
        self.cached_tokens += cached
        self.computed_tokens += max(0, prompt - cached)
        self.completion_tokens += usage.get("completion_tokens") or 0

    def _sized(self, send):
        """Sends; if the server says the request doesn't fit, learns its real
        size from the refusal, fits the conversation again and resends once."""
        try:
            return send()
        except ContextOverflow as e:
            self.ratio = max(self.ratio, e.input_tokens / max(1, self._sent_raw) * 1.05)
            self._event("overflow")
            self._fit()
            return send()

    def _action(self, tools: tuple[str, ...]) -> dict:
        self._call = {"kind": "action"}

        def ask(flat: bool, **more):
            schema = action_schema(tools, flat)
            # A JSON action has no blank line in it. The grammar allows any
            # whitespace between fields, and Qwen3.6 once filled the 400
            # tokens with newlines after "missing" (11 s, no tool): stop there.
            return self._sized(lambda: self._post(self._body({
                "max_tokens": 400, "stop": ["\n\n"], **more,
                "response_format": {"type": "json_schema", "json_schema": {"name": "action", "schema": schema}},
            })))

        def parse(data: dict) -> dict | None:
            msg = data["choices"][0]["message"]
            # Reasoning models may think first, or put the action in their reasoning.
            text = msg.get("content") or msg.get("reasoning_content") or ""
            braces = _outer_braces(text)
            try:
                act = json.loads(braces or text)
            except json.JSONDecodeError:
                return None
            return act if isinstance(act, dict) and act.get("tool") else None

        # After a refused repeat the next action is sampled: at temperature 0
        # the same conversation gave the very same command again, four times,
        # each time after the model had written out the fix in plain text.
        warm, self._sample = ({"temperature": RETRY_TEMPERATURE} if getattr(self, "_sample", False) else {}), False
        try:
            data = ask(getattr(self, "_flat_schema", False), **warm)
        except ContextOverflow:
            raise
        except RuntimeError as e:
            if getattr(self, "_flat_schema", False) or "400" not in str(e):
                raise
            self._flat_schema = True  # this server's grammar can't do anyOf
            data = ask(True, **warm)
        act = parse(data)
        if act is None:
            # Cut off before it named a tool. That used to count as "answer",
            # which ended a turn in the middle of the work; ask once more,
            # sampled, since the same prompt would be cut at the same place.
            self._event("retry")
            act = parse(ask(getattr(self, "_flat_schema", False), temperature=0.7)) or {"enough": True, "tool": "answer"}
        if act.get("tool") not in tools:
            act["tool"] = "answer"
        return act

    def _room(self, want: int) -> int:
        """Output tokens to ask for: `want`, or what's left of the window."""
        used = self._estimate()
        return max(256, min(want, self.context - used - 256))

    def _held_back(self, old: set[str], **kw) -> tuple[str, bool]:
        """Streams the reply, but shows nothing while it could still turn out
        unusable: it opens like a JSON action, or it only repeats sentences of
        earlier replies. Returns the reply and whether it was shown; an empty
        reply, an action or a repeat from start to end never is."""
        show, shown = self.hooks.stream, False

        def gate(text: str, final: bool):
            nonlocal shown
            if not shown and not maybe_json(text):
                parts = _sentences(text)
                last = parts.pop() if parts and not final else ""  # still being written
                # Something new: a whole sentence, or one that has gone its own way.
                shown = not old or any(p not in old for p in parts) or (
                    len(last) >= 40 and not any(o.startswith(last) for o in old))
            if shown:
                # A reply may end with the next action ("Let me start." and
                # then {"enough": false, …}): not part of what the user reads.
                show(cut_action_tail(text), final)

        self.hooks.stream = gate
        try:
            reply = self._text(ANSWER_RESERVE, stream=True, **kw)
        finally:
            self.hooks.stream = show
        if not shown and reply.strip() and as_action(reply) is None and not is_repeat(reply, old):
            show(reply, True)  # short and new, or JSON the user asked for
            shown = True
        return reply, shown

    def _text(self, max_tokens: int, stream: bool, temperature: float | None = None, regex: str | None = None) -> str:
        return self._sized(lambda: self._text_once(max_tokens, stream, temperature, regex))

    def _text_once(self, max_tokens: int, stream: bool, temperature: float | None = None,
                   regex: str | None = None) -> str:
        self._fit()
        body = self._body({"max_tokens": self._room(max_tokens), "stream": stream})
        if temperature is not None:
            body["temperature"] = temperature
        if regex:
            body["regex"] = regex
        if not stream:
            msg = self._post(body)["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content.strip():
                # Reasoning models sometimes write the whole answer inside
                # their reasoning (SGLang returns it as reasoning_content)
                # and leave the content empty: use what follows a fence or
                # SEARCH marker there.
                thought = msg.get("reasoning_content") or ""
                m = re.search(r"(?s)((?<!\S)\S*\n?```.*|<<<<<<< SEARCH.*)", thought)
                content = m.group(1) if m else ""
            return content
        body["stream_options"] = {"include_usage": True}
        out = ""
        with httpx.stream("POST", f"{self.base}/chat/completions", headers={"Authorization": f"Bearer {self.key}"},
                          json=body, timeout=600) as r:
            if r.status_code >= 400:
                r.read()
                raise server_error(r.status_code, r.text)
            for line in r.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                chunk = json.loads(line[6:])
                if chunk.get("usage"):
                    self._count(chunk["usage"])
                for ch in chunk.get("choices") or []:
                    delta = (ch.get("delta") or {}).get("content") or ""
                    if delta:
                        out += delta
                        self.hooks.stream(out, False)
                if self.hooks.interrupted():
                    break
        self.hooks.stream(out, True)
        _dump(body, {"content": out}, None)
        return out

    # ---- context ----------------------------------------------------------
    def _fit(self):
        """Keeps the next request inside the window: cut old tool output first
        (oldest first, never the latest few), then drop the oldest turns.

        Each cut changes an early message, so the prefix cache misses from
        there on; it cuts down to FIT_LOW of the budget, so the next steps only
        append. Cutting just enough recomputed the whole ~28k-token prompt on
        every step near the limit."""
        budget = self.context - ANSWER_RESERVE
        est = self._estimate()
        if est <= budget:
            return
        budget = int(budget * FIT_LOW)
        results = [i for i, m in enumerate(self.messages) if m.get("_result") and not m.get("_cut")]
        for i in results[:-KEEP_RECENT]:
            m = self.messages[i]
            saved = int(len(m["content"]) // 3 * self.ratio) + _image_tokens(m)
            m["content"] = m["content"].split("\n", 1)[0] + "\n[output removed to save space; run the tool again if you need it]"
            m["_cut"] = True
            self._event("cut")
            est -= saved
            if est <= budget:
                return
        # Still too long: drop whole early turns (up to the next user message).
        while est > budget and len(self.messages) > 8:
            drop = 1
            while drop < len(self.messages) and not self.messages[drop].get("_user"):
                drop += 1
            est -= int(sum(len(m["content"]) for m in self.messages[:drop]) // 3 * self.ratio) + sum(map(_image_tokens, self.messages[:drop]))
            del self.messages[:drop]
            self._event("dropped")

    def _raw_estimate(self) -> int:
        """The system prompt and every message, at ~3 characters a token."""
        return (len(self._system()) + sum(len(m["content"]) + 8 for m in self.messages)) // 3

    def _estimate(self) -> int:
        """Prompt tokens of the next request, corrected by what the server
        counted (pictures are counted apart: their size is known)."""
        return int(self._raw_estimate() * self.ratio) + sum(map(_image_tokens, self.messages))

    def context_parts(self) -> list[tuple[str, int]]:
        """What the next request holds, estimated like `_fit` does (/context)."""
        tool = sum(len(m["content"]) + 8 for m in self.messages if m.get("_result"))
        talk = sum(len(m["content"]) + 8 for m in self.messages if not m.get("_result"))
        pictures = sum(map(_image_tokens, self.messages))
        system = len(self._system())
        # As sent: connectors added since the prompt was built aren't in it yet.
        conn = len(self._connector_text()) if self._connector_text() in self._system() else 0
        parts = [("Instructions", system - conn), ("Connectors", conn), ("Conversation", talk), ("Tool output", tool)]
        out = [(name, int(n // 3 * self.ratio)) for name, n in parts if n or name != "Connectors"]
        return out + [("Pictures", pictures)] if pictures else out

    def _clean(self) -> list:
        # Pictures go as image parts, the newest first up to IMAGE_BYTES: the
        # whole conversation is sent each time, and the gateway takes 4 MiB.
        room, sent = IMAGE_BYTES, {}
        for i in range(len(self.messages) - 1, -1, -1):
            for img in reversed(self.messages[i].get("_images") or [] if not self.messages[i].get("_cut") else []):
                if len(img["url"]) <= room:
                    room -= len(img["url"])
                    sent.setdefault(i, []).insert(0, img)
        out = []
        for i, m in enumerate(self.messages):
            images = m.get("_images") if not m.get("_cut") else None
            if not images:
                out.append({"role": m["role"], "content": m["content"]})
                continue
            parts = [{"type": "text", "text": m["content"]}]
            parts += [{"type": "image_url", "image_url": {"url": img["url"]}} for img in sent.get(i, [])]
            if len(sent.get(i, [])) < len(images):
                parts.append({"type": "text", "text": "[some pictures above are no longer attached, to save space; read them again to see them]"})
            out.append({"role": m["role"], "content": parts})
        return out

    # ---- the turn ---------------------------------------------------------
    def turn(self, text: str, can_edit: bool = True, wants_change: "bool | None | Future" = None) -> str:
        """One user message. `can_edit` is False only when the user switched
        edits off (plan mode). `wants_change` is a hint, not a gate: whether
        the message reads as a request for a change (None: assume it does when
        edits are on). The model decides what the message needs. It may be a
        Future (the router still deciding): it is waited for only when the
        turn's own setup is done, so the two overlap."""
        self._trail, self.stuck, self._called = [], "", {}
        self.turns += 1
        self._step, self._prev = 0, {}
        if not isinstance(wants_change, Future):
            wants_change = can_edit if wants_change is None else (wants_change and can_edit)
        # Until the turn says otherwise: a change request that fails or is interrupted is still owed.
        self._wanted, self._did = can_edit if isinstance(wants_change, Future) else wants_change, False
        try:
            return self._turn(text, can_edit, wants_change)
        except Exception as e:
            # Leave a trace, so a later turn doesn't assume this one worked.
            self.messages.append({"role": "assistant", "content": f"(This request failed: {str(e)[:200]}. Nothing more was done.)"})
            raise
        finally:
            if self._did:
                self.pending, self._pending_age = "", 0
            elif self._wanted:
                # Still owed. A follow-up ("yes, go ahead") keeps the request it was about.
                self.pending, self._pending_age = self.pending or text.split("\n\n(Files the user added", 1)[0][:400], 0
            elif self.pending:
                self._pending_age += 1
                if self._pending_age >= PENDING_TURNS:
                    self.pending = ""  # they moved on

    def last_reply(self) -> str:
        """The assistant's latest reply, for reading the next message in its light."""
        if self._replies:
            return self._replies[-1][1]
        # A resumed session has the messages only: the last thing said that isn't an action.
        return next((m["content"] for m in reversed(self.messages)
                     if m["role"] == "assistant" and not m["content"].lstrip().startswith("{")), "")

    def _turn(self, text: str, can_edit: bool, wants_change: "bool | Future") -> str:
        self.messages.append({"role": "user", "content": text, "_user": True})
        # Files the message names are opened up front (as an @-mention would):
        # small ones whole, big ones as an outline to grep or read from.
        named = self._named_files(text)
        for path in named:
            try:
                lines = len(self.ws.text_of(self.ws.path(path)).splitlines())
            except ToolError as e:
                p = self.ws.path(path)
                if not (documents.is_image(p) or documents.unsupported(p) or isinstance(e.__cause__, documents.NoText)):
                    continue
                lines = 0  # a picture, a scan, a video or slides: read shows it, or says why it can't
            act = ({"enough": False, "missing": "the file the user named", "tool": "read", "path": path} if lines <= WHOLE_FILE
                   else {"enough": False, "missing": "the file the user named (outline; it is long)", "tool": "outline", "path": path})
            self.messages.append({"role": "assistant", "content": json.dumps(act)})
            self.messages.append({"role": "user", "_result": True, "content":
                                  f"Result of step 0 ({act['tool']}):\n{self._do(act)}"})
            if images := self.ws.take_images():
                self.messages[-1]["_images"] = images
        if made := self._documents_to_make(text):
            self.messages.append({"role": "user", "_result": True, "content": made})
        if not named:
            # Where to start: definitions named like the words of the message.
            # Without it a "pairing codes" request cost ten greps in the wrong
            # folders before the file that holds PAIRING_TTL_S was found.
            docs = self._documents_for(text)
            if docs:
                self.messages.append({"role": "user", "_result": True, "content":
                                      "Documents and pictures in the repository that the request may be about "
                                      "(read one to see what it holds):\n" + "\n".join(f"  {d}" for d in docs)})
            related = self.ws.related(text)
            if related:
                self.messages.append({"role": "user", "_result": True, "content":
                                      "Definitions named like words of the request (a starting point; read before "
                                      f"relying on one):\n{related}"})
        # Connector tools sit before "answer". With edits off, only those
        # their connector marks as read-only are offered.
        if isinstance(wants_change, Future):
            # The router ran while the setup above did; its answer is needed now.
            routed = wants_change.result()
            wants_change = can_edit if routed is None else (bool(routed) and can_edit)
            self._wanted = wants_change

        def searching() -> bool:
            # Not while the server says its search engines are refusing: asking
            # again only keeps them blocked.
            return self.web and time.monotonic() >= self._search_off_until

        def offered() -> tuple[tuple, tuple]:
            extra = self.connectors.names(read_only=not can_edit) if self.connectors is not None else ()
            tools = tuple(t for t in TOOLS if (can_edit or t not in EDIT_TOOLS) and (searching() or t != "web_search")
                          and (self.browser == "offer" or t != "browser") and t != "answer") + extra + ("answer",)
            return extra, tools

        extra, tools = offered()
        if self.browser == "offer":
            hint = browser_hint(text)
            if hint:
                self.messages.append({"role": "user", "_result": True, "content": hint})
        called: list[str] = []  # connector tools that ran in this request
        done: dict[str, int] = {}  # action -> step it was taken, to stop loops
        outputs: dict[str, int] = {}  # a search's output -> the step that first returned it
        results: dict[int, dict] = {}  # step -> its result message (cut from the window later, maybe)
        reads: dict[str, list] = {}  # path -> [(first, last, step)] shown this turn
        reread: set[str] = set()  # files read again because their earlier read was far back
        repeats = search_errors = searches = search_empty = failed_edits = plans = 0
        searched = opened = changed = 0  # find/grep/list, read/outline, edit/create steps this turn
        act_forced = must_act = False  # a change request that keeps choosing answer gets one step without it
        announced = False  # a reply that said "I'll make the change now" was held to it, once
        planned_at = -1  # the model call that last chose `plan`
        sampled_at = 0  # the repeat count the last sampled action answered
        nudged = edit_nudged = checked = reviewed = rethought = edit_checked = edit_planned = False
        changed_files: list[str] = []
        self._before = {}  # path -> its text before this request's first change to it
        declined: set[str] = set()  # commands the user declined, or that were refused, in this request
        self.down = {}
        failed_runs: dict[str, int] = {}  # command -> the step it failed at, until something changes
        failed_times: dict[str, int] = {}  # command -> how often it failed in this request
        run_results: dict[str, tuple[str, int]] = {}  # command -> its last output and step, until an edit
        last_repeat = ""
        failed_title = None  # the title of a failing command that was sent again unchanged

        def close():
            self._wanted, self._did = wants_change, bool(changed or called)
        check: str | None = None  # the first line of the last command run after the latest edit ("exit code 0")
        queued: list[dict] = []  # lookups asked for in one step, still to run
        calls = 0  # actions the model chose (a queued lookup costs no call)
        sweep: tuple[str, int] = ("", 0)  # a grep pattern and how many places in a row it was tried on
        checked_at = 0
        changed_at = 0  # the model call of the latest change

        def visible(step_no: int) -> bool:
            # A result cut from the window to save space may be fetched again.
            return not results.get(step_no, {}).get("_cut")

        def note(content: str):
            self.messages.append({"role": "user", "_result": True, "content": content})
            if images := self.ws.take_images():
                self.messages[-1]["_images"] = images

        limit = MAX_STEPS
        for n in range(1, MAX_STEPS_CHANGING + 1):
            limit = MAX_STEPS_CHANGING if changed else MAX_STEPS
            if n > limit:
                self.stuck = f"it used all {limit} steps without finishing"
                break
            self._step = n
            if self.hooks.interrupted():
                return ""
            self._fit()
            self.hooks.waiting("Thinking")
            if repeats >= 2:
                if rethought:
                    # Twice in circles: stop, and say so.
                    self.stuck = (f"it kept sending the same failing command without changing it (last: {last_repeat}). "
                                  "No corrected command was ever sent, so none was rejected" if last_repeat == failed_title else
                                  f"it kept repeating actions it had already taken (last: {last_repeat})")
                    break
                # Going in circles (small models, and any model whose search
                # words don't match the code): one forced look at what is
                # already known, in free text, before anything else.
                rethought, repeats, checked_at, queued = True, 0, calls, []
                self._think(RETHINK_PROMPT.format(trail="\n".join(self._trail[-12:]), unseen=self._unseen()), "Rethink")
                note("Now act on that: answer if you have enough, or take the new action you named.")
                continue
            if calls - checked_at >= CHECK_EVERY and not changed and not queued:
                checked_at = calls
                self._think(CHECKPOINT_PROMPT.format(n=n - 1), "Taking stock")  # counted in model calls, not lookups
                note("Now act on that: answer if it is enough, otherwise take that one action.")
                continue
            if changed and calls - max(checked_at, changed_at) >= CHECK_EVERY and not queued:
                # Files written, then call after call of runs and reads with no
                # further edit: the iridium prefilter turn spent sixty steps
                # re-running its failing tests and never changed the code.
                checked_at = calls
                self._think(FIX_CHECKPOINT_PROMPT.format(k=calls - changed_at), "Taking stock")
                note("Now act on that: make that edit, or answer.")
                continue
            if queued:
                # A lookup asked for along with the previous one: no model call.
                act = queued.pop(0)
            else:
                calls += 1
                # A change was asked for: the first move is never "answer"
                # (with a list of likely definitions in view, the model
                # declared it had enough and offered to make the change).
                first = wants_change and calls == 1 and not changed
                doing = tuple(t for t in tools if t in (*EDIT_TOOLS, "run", "start")) if must_act else ()
                must_act = False
                self._sample, sampled_at = bool(repeats) and repeats != sampled_at, repeats
                act = self._action(doing or (tuple(t for t in tools if t != "answer") if first else tools))
                then = act.pop("then", None) if act.get("tool") in LOOKUPS else None
                queued = [{"enough": False, "missing": "(asked for with the step before)", **x}
                          for x in (then or [])[:MAX_THEN] if isinstance(x, dict) and x.get("tool") in LOOKUPS and x.get("tool") in tools]
            tool = act["tool"]
            self.messages.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False)})
            if tool == "answer":
                if searched and not opened and not nudged:
                    # Small models answer straight from search hits; one check
                    # that the answer doesn't need the code itself.
                    nudged = True
                    note("You found where things are but haven't opened any code yet. If the "
                         "answer depends on how the code works, read the key lines first "
                         "(outline, then read). If it doesn't, choose answer again.")
                    continue
                if wants_change and not changed and not called and not edit_nudged:
                    # A change was asked for and nothing changed yet: small
                    # models give up after one empty search.
                    edit_nudged = True
                    note("You haven't changed anything yet, and the user asked for a change. Keep "
                         "going: find the code that does it (find by name, or outline the file), read the "
                         "right lines, then edit. Choose answer only if it truly can't be done, and say why.")
                    continue
                if wants_change and not changed and not called and can_edit and not act_forced and not repeats:
                    # Answer again, still with nothing changed: after reading
                    # every file of a small app, Qwen3.6 "answered" with the
                    # list of changes and "Let me start by making the changes."
                    # ("enough": true read as "enough to start", and the only
                    # tool that goes with it is answer.) One step with nothing
                    # but the doing tools on offer; given lookups and plan
                    # too, it planned twice more and answered again.
                    act_forced = must_act = True
                    note("Nothing has been changed yet, so there is nothing to report. You have read enough to start: "
                         "make the first change now (edit or create, with the file's path).")
                    continue
                if changed and not checked:
                    # Before claiming the work is done: what actually changed,
                    # and whether anything was run to check it ("Done" came
                    # back for an edit nothing had been run against).
                    checked = True
                    verify = "" if check is not None or "run" not in tools else (
                        " You haven't run anything to check the change: if the project has a quick check for what "
                        "you touched (its type check, its tests, a build), run it now; if nothing can check it, "
                        "say in your reply that it is unverified.")
                    if self._unseen_page(changed_files, called) and self.browser in ("offer", "running"):
                        # A lint passed, and "did you check it in the browser?"
                        # was the user's next message: they didn't know it was
                        # on offer, and the model never asked for it.
                        verify += (" What you changed shows in a web page, and a lint or a build doesn't show whether it "
                                   "works there. If the page can be opened (a dev server that runs, or one you can start), "
                                   "look at it now: " + ("use the playwright.* tools" if self.browser == "running" else
                                                         "choose the browser action (the user is asked once)")
                                   + ", open the page, try the change, read the console.")
                    note("Files you changed in this request: " + ", ".join(dict.fromkeys(changed_files))
                         + ". If the task needs more (other files, callers of something renamed, "
                         "a test you meant to add), do it now with edit; grep for an old name to check. "
                         "Otherwise choose answer, and describe only these changes." + verify)
                    continue
                if changed and not reviewed and several_parts(text):
                    # The work is done as far as the model is concerned: now
                    # each part of the request against the diff, once.
                    reviewed = True
                    self._think(REVIEW_PROMPT.format(request=text.strip(), diff=self._diff()), "Review", REVIEW_TOKENS)
                    note("If a line of your review says NOT DONE, do that part now (look up what you need first), then "
                         "choose answer. Otherwise choose answer. Either way the reply must not call a part done "
                         "that your review couldn't show.")
                    continue
                close()
                reply = self._answer(text, self._facts(changed_files, failed_edits, wants_change, called, check, self._unseen_page(changed_files, called)))
                if (can_edit and not changed and not called and not announced and not act_forced and not self.stuck
                        and reply and self.hooks.announces(reply)):
                    # "Now I'll implement the sort feature." and the turn ended:
                    # the message read as a question, so nothing held the model
                    # to it. A reply that announces a change is followed by the
                    # change, as it is in Claude Code; an offer ("want me to?")
                    # is not, and waits for the user.
                    announced = act_forced = must_act = wants_change = True
                    self._event("announced")
                    note("You just told the user you would make the change now, and nothing has been changed. Do it: "
                         "make the first change (edit or create, with the file's path).")
                    continue
                return reply
            if tool == "plan":
                again = planned_at == calls - 1
                plans += not again  # one refused for coming right after a plan isn't a plan
                if plans > MAX_PLANS or again:
                    # More than enough plans, or a plan right after a plan:
                    # nothing was learnt in between that a new one could use.
                    repeats += 1
                    self._event("repeat")
                    last_repeat = "plan"
                    planned_at = calls
                    note("You just wrote a plan and nothing has happened since: act on the plan above (its first step), or answer."
                         if again else "You have planned enough; act on the plan above, or answer.")
                    continue
                planned_at = checked_at = calls  # a plan is a stock-take too: not one right after the other
                self._think(PLAN_PROMPT, "Plan", PLAN_TOKENS_LONG if several_parts(text) else THINK_TOKENS)
                note("Carry out the plan, one action at a time.")
                continue
            if tool in ("edit", "create"):
                if not wants_change and not edit_checked:
                    # The message read as a question. The model may still be
                    # right that a change is wanted; it has to choose it twice.
                    edit_checked = True
                    note(ASK_EDIT_CHECK)
                    continue
                wants_change = True
                if not edit_planned and not changed and not named and not plans:
                    # A change the user didn't point at a file for: decide the
                    # whole of it before the first edit. Without this, models
                    # edit the first plausible place and stop.
                    edit_planned = True
                    self._think(EDIT_PLAN_PROMPT, "Plan")
                    # The plan is this stretch's stock-take; and an edit that was
                    # forced stays forced. Otherwise the stock-take came next,
                    # asked whether that answered the request, and the turn
                    # ended on "Let me create the files now." with none written.
                    checked_at = calls
                    if act_forced:
                        must_act = True
                        note("Now carry it out: make the first change of the plan (edit or create, with the file's path).")
                    else:
                        note("Now carry it out: read anything the plan says is still unread, then make "
                             "each edit in order.")
                    continue
            if tool in ("find", "grep", "list"):
                searched += 1
            elif tool in ("read", "outline"):
                opened += 1
            sig = _signature(act)
            title = _title(act)
            seen_at = self._already_read(act, [r for r in reads.get(act.get("path") or "", []) if visible(r[2])]) \
                if tool == "read" else None
            if tool == "read" and (seen_at or done.get(sig)) and n - (seen_at or done[sig]) > FAR_BACK \
                    and (act.get("path") or "") not in reread:
                # Read long ago, and asked for again: once, it is read. After a
                # 33-step read-through, the plan said to edit store.py first; it
                # was read at step 3, and the read before the edit was refused
                # twice, so the turn ended stuck with nothing changed.
                reread.add(act.get("path") or "")
                seen_at = None
                done.pop(sig, None)
            if seen_at:
                repeats += 1
                self._event("repeat")
                last_repeat = title
                result = (f"Those lines were already shown at step {seen_at}; they're above. "
                          "Use them, look somewhere else, or answer.")
            elif tool in ("run", "start") and (act.get("command") or "").strip() in declined:
                # Running a command again is fine (tests after a fix); asking
                # again for one that was just declined or refused is not.
                repeats += 1
                self._event("repeat")
                last_repeat = title
                result = ("This command was already declined or refused in this request, and nothing has changed: "
                          "don't ask for it again. Do the task another way, or answer and say what couldn't be run.")
            elif tool == "run" and (act.get("command") or "").strip() in failed_runs:
                # The same command, after it failed, with no edit and no other
                # command that worked in between: it fails the same way (and
                # the user is asked about it again).
                repeats += 1
                self._event("repeat")
                last_repeat = failed_title = title
                result = (f"This exact command already failed at step {failed_runs[(act.get('command') or '').strip()]} and nothing "
                          "has changed since; its output is above. The command you just sent is, character for character, "
                          "the one that failed: if you worked out a fix, it is not in it. Write the corrected command "
                          "out in full, or fix the cause.")
            elif sig in done and visible(done[sig]) and tool not in ("edit", "create", "run", "start") and tool not in extra:
                repeats += 1
                self._event("repeat")
                last_repeat = title
                result = (f"You already did exactly this at step {done[sig]}; its result is above. "
                          "Use it: answer now, or take a different action.")
            else:
                done[sig] = n
                result = self._do(act)
                if tool in ("run", "start") and not self._ran:
                    declined.add((act.get("command") or "").strip())
                if tool == "run" and self._ran and re.match(r"exit code [1-9]", result):
                    command = (act.get("command") or "").strip()
                    failed_runs[command] = n
                    failed_times[command] = failed_times.get(command, 0) + 1
                elif tool in EDIT_TOOLS and result.startswith(WROTE):
                    failed_runs.clear()  # the files changed: a command that failed may work now
                    failed_times.clear()
                    run_results.clear()
                elif tool in ("run", "start") and self._ran:
                    # Another command worked (an install, say), so one that
                    # failed once may work now. One that failed twice the same
                    # way won't: a `print(keys)` in between let a broken
                    # one-liner run, and fail, again and again.
                    failed_runs = {c: at for c, at in failed_runs.items() if failed_times.get(c, 0) >= 2}
                if tool == "run" and self._ran and result.startswith("exit code 0"):
                    # The same command printing the very same thing, with no
                    # edit in between: `pytest … | grep FAILED` exits 0, and was
                    # run twelve times in a row until the steps ran out.
                    command = (act.get("command") or "").strip()
                    if run_results.get(command, ("", 0))[0] == result:
                        repeats += 1
                        self._event("repeat")
                        last_repeat = title
                        result += (f"\n\n[This is exactly what this command printed at step {run_results[command][1]}, "
                                   "and no file was edited since: running it again won't change it. Fix the cause, "
                                   "or answer.]")
                    else:
                        run_results[command] = (result, n)
                if (tool in ("run", "start") and self._ran) or (
                        tool in EDIT_TOOLS and result.startswith(("Edited ", "Created ", "Replaced "))):
                    # The files may differ now, so what was looked up before
                    # can be looked up again. After an edit that half applied,
                    # every read of the file came back as "you already did
                    # this at step 1" and the turn ended stuck, the model
                    # sure the reads were cached.
                    done.clear()
                    reads.clear()
                    outputs.clear()
                if tool == "grep":
                    # One pattern tried on file after file: a sweep finds
                    # nothing a single search of the folder wouldn't.
                    pattern = (act.get("pattern") or "").strip()
                    sweep = (pattern, sweep[1] + 1) if pattern == sweep[0] and act.get("path") else (pattern, 1)
                    if sweep[1] >= SWEEP:
                        repeats += 1
                        self._event("repeat")
                        last_repeat = title
                        result += ("\n\n[You are trying the same pattern on one file after another. Search once "
                                   "without a path (or with a folder), or use find for the name.]")
                if tool == "web_search" and result.startswith("Error:"):
                    # A failing search fails the same way rephrased; small
                    # models kept rewording the query until the steps ran out.
                    search_errors += 1
                    if search_errors >= 2 or not searching():
                        tools = tuple(t for t in tools if t != "web_search")
                        result += "\n\nWeb search isn't working right now; carry on without it, and say so if the answer needed it."
                elif tool == "web_search":
                    # Nor does a search that finds nothing get better reworded:
                    # 41 in one turn, the last ones for "a", "b", "c"…
                    searches += 1
                    search_empty = search_empty + 1 if result == "No results." else 0
                    if search_empty >= 3 or searches >= MAX_SEARCHES:
                        tools = tuple(t for t in tools if t != "web_search")
                        result += ("\n\nWeb search is returning nothing right now; carry on without it." if search_empty >= 3 else
                                   f"\n\nThat was web search number {searches} in this request, the last one: work with what you found.")
                if tool in ("find", "grep", "list") and not result.startswith("Error:"):
                    # A reworded search that returns the very same lines is a repeat too.
                    first = outputs.setdefault(result, n)
                    if first != n and visible(first):
                        repeats += 1
                        self._event("repeat")
                        last_repeat = title
                        result = (f"Same result as step {first} (it's above): this search adds nothing. "
                                  "Read one of the files it names, or look for something else.")
                if tool == "read":
                    result = self._note_read(act, result, reads, n)
                if tool == "run" and changed and result.startswith("exit code"):
                    check = result.split("\n", 1)[0]
                if tool == "browser":
                    extra, tools = offered()  # its tools in, the browser action out
                if tool in extra:
                    # Its arguments are only known once written (in _do).
                    if result.startswith(CONNECTOR_SAME):
                        repeats += 1
                        self._event("repeat")
                        last_repeat = tool
                    elif result.startswith(CONNECTOR_HEAD):
                        called.append(tool)
                    elif result.startswith(CONNECTOR_DOWN):
                        extra, tools = offered()  # without that connector's tools
                if tool in ("edit", "create"):
                    # Only an edit that applied is a change. Counting the attempt
                    # told the model it had changed a file when its edit was refused.
                    if result.startswith("Error:"):
                        failed_edits += 1
                        result += "\n\nNothing was changed. Read the exact lines again and retry, or say that it failed."
                        # ...and that read must not be refused as a repeat: told to
                        # read again, Qwen3.6 did, got "you already did exactly
                        # this" three times and the turn ended stuck.
                        done.clear()
                        reads.clear()
                        outputs.clear()
                    elif result.startswith(WROTE):
                        changed += 1
                        changed_at = calls
                        check = None  # a new edit: whatever ran before says nothing about it
                        path = act.get("path") or ""
                        changed_files.append(path)
                        if path not in self.changed_session:
                            self.changed_session.append(path)
            left = (MAX_STEPS_CHANGING if changed else MAX_STEPS) - n
            if left <= 5:
                foot = f"\n\n[{left} steps left: wrap up]"
            elif n % 6 == 0 and not changed:
                foot = (f"\n\n[{n} steps so far. If what you have read answers the request, answer now; "
                        "otherwise go for the one thing still missing.]")
            else:
                foot = ""
            note(f"Result of step {n} ({tool}):\n{result}{foot}")
            results[n] = self.messages[-1]
        else:
            self.stuck = f"it used all {limit} steps without finishing"
        close()
        return self._answer(text, self._facts(changed_files, failed_edits, wants_change, called, check,
                                              self._unseen_page(changed_files, called)), self.stuck)
