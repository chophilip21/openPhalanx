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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

MODEL = "openphalanx-coder"
MAX_STEPS = 24  # actions per turn before the model must answer
READ_LINES = 250  # most lines one read returns
READ_PAD = 6  # extra lines shown around a requested range
WHOLE_FILE = 300  # files up to this many lines are always read whole
GREP_HITS = 60
LIST_FILES = 200
RESULT_CHARS = 12_000  # a tool result is cut beyond this
RUN_TIMEOUT = 120
ANSWER_RESERVE = 4096  # tokens kept free for the model's reply
FIT_LOW = 0.75  # once over budget, cut down to this share of it
KEEP_RECENT = 6  # tool results never cut: the latest few

TOOLS = ("list", "grep", "outline", "read", "edit", "create", "run", "web_search", "answer")
EDIT_TOOLS = ("edit", "create", "run")

SYSTEM = """You are a coding assistant working inside the user's repository. You cannot see any file until you read it, so find things with tools first, then answer or change the code.

Reply with exactly one JSON action per step:
- {"tool": "list", "path": "dir"}: files under a directory ("" for the whole repository)
- {"tool": "grep", "pattern": "regex", "path": "dir or file"}: matching lines as file:line (path optional)
- {"tool": "outline", "path": "file"}: the functions, classes and other definitions in a file, with line numbers
- {"tool": "read", "path": "file", "start": 1, "end": 120}: those lines of a file (at most 250 at a time)
- {"tool": "edit", "path": "file"}: change an existing file; you then write SEARCH/REPLACE blocks
- {"tool": "create", "path": "file"}: create a new file; you then write its whole content
- {"tool": "run", "command": "shell command"}: run a command in the repository (the user confirms it first); use it for tests and builds
- {"tool": "web_search", "query": "…"}: search the web, for things outside the repository
- {"tool": "answer"}: reply to the user; you then write the reply
Every action also has "why": a few words on what you are looking for.

How to work:
- Locate before reading: grep for names, outline big files, then read only the lines you need.
- grep shows where something is, not how it works: for how/why questions, read the code that does the work (follow the call to it) before answering.
- If a search finds nothing useful, try other names before giving up: constants in CAPS, the function that would do it, a word from an error message. Ask the user only when the repository really doesn't have it.
- If an edit fails, read the lines again and retry with text copied exactly; don't claim it's already done unless you've seen it.
- Base answers on code you have read. If what you read doesn't contain the answer, grep for the specific function, constant or message before answering; don't guess from nearby code.
- Read the exact lines you are going to change right before you edit them; never guess file contents.
- Do what was asked and no more. For a question, don't edit anything.
- To change a value or a default, change it where it is defined (the constant, setting or default argument), not where it's used.
- After editing, check your work if it's cheap (re-read the lines, or run the tests the user would run).
- Answer briefly when done: what you found, or what you changed and where. Don't repeat code you already edited."""

EDIT_PROMPT = """Now write the change to {path} as one or more SEARCH/REPLACE blocks, like this:

{path}
```
<<<<<<< SEARCH
lines copied exactly from the file, without line numbers
=======
the new lines
>>>>>>> REPLACE
```

Copy SEARCH lines character for character from what you read (keep the indentation), include enough lines to be unique, and keep each block small. Write only the blocks."""

CREATE_PROMPT = "Now write the whole content of the new file {path}, in one fenced code block and nothing else."
ANSWER_PROMPT = "Now write your reply to the user. Be brief and specific; refer to files as path:line."
# A small model answered five different messages with the same sentences (its own
# earlier replies made them the likeliest text): a repeat is redone once with this.
REPEAT_PROMPT = ("You just wrote a reply you already gave earlier, word for word, but the user's message is a "
                 "different one. Read their latest message again and answer exactly that, in different words, "
                 "without repeating earlier sentences. If they ask about a change or say it is missing, go by "
                 "the facts above and say plainly what was and wasn't changed.")
RECENT_REPLIES = 6  # how far back a repeat is looked for


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _sentences(text: str) -> list[str]:
    """Normalized sentences and lines, the units a repeat is made of."""
    return [n for n in (_norm(p) for p in re.split(r"(?<=[.!?:])\s+|\n+", text)) if n]


def is_repeat(reply: str, old: set[str]) -> bool:
    """Every sentence of the reply was already said in an earlier one."""
    parts = _sentences(reply)
    return bool(parts) and all(p in old for p in parts)


def action_schema(tools: tuple[str, ...]) -> dict:
    return {
        "type": "object",
        "properties": {
            "why": {"type": "string", "maxLength": 200},
            "tool": {"type": "string", "enum": list(tools)},
            "path": {"type": "string", "maxLength": 300},
            "pattern": {"type": "string", "maxLength": 200},
            "start": {"type": "integer", "minimum": 1},
            "end": {"type": "integer", "minimum": 1},
            "command": {"type": "string", "maxLength": 500},
            "query": {"type": "string", "maxLength": 200},
        },
        "required": ["why", "tool"],
        "additionalProperties": False,
    }


@dataclass
class Hooks:
    """What the UI provides. Every callback is optional."""

    step: Callable[[str, str], None] = lambda title, detail: None  # an action and its result
    stream: Callable[[str, bool], None] = lambda text, final: None  # the reply so far
    confirm_run: Callable[[str], bool] = lambda command: False
    waiting: Callable[[str | None], None] = lambda label: None  # the model is busy (None: stop)
    edited: Callable[[str, str | None, str], None] = lambda path, before, after: None
    interrupted: Callable[[], bool] = lambda: False


class ToolError(Exception):
    pass


class ContextOverflow(RuntimeError):
    """The server refused a request as longer than the model's window."""

    def __init__(self, message: str, input_tokens: int):
        super().__init__(message)
        self.input_tokens = input_tokens


# SGLang's refusals; the first number is the request's real input size.
OVERFLOW = re.compile(r"(\d+) tokens from the input messages|The input \((\d+) tokens\) is longer"
                      r"|Input length \((\d+) tokens\) exceeds")


def server_error(status: int, text: str) -> RuntimeError:
    m = OVERFLOW.search(text)
    if status == 400 and m:
        return ContextOverflow(f"server error {status}: {text[:300]}", int(next(g for g in m.groups() if g)))
    return RuntimeError(f"server error {status}: {text[:300]}")


class Workspace:
    """The repository the tools work in. Paths stay inside it."""

    def __init__(self, root: str, repo_map=None, encoding: str = "utf-8"):
        self.root = Path(root).resolve()
        self.repo_map = repo_map  # Aider's RepoMap, for outlines
        self.encoding = encoding

    def path(self, rel: str, must_exist: bool = True) -> Path:
        rel = (rel or "").strip().lstrip("./") if rel not in (".", "./") else ""
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"{rel} is outside the repository")
        if ".git" in p.relative_to(self.root).parts:
            raise ToolError("the .git folder is off limits")
        if must_exist and not p.exists():
            raise ToolError(f"{rel} doesn't exist (use list or grep to find the right path)")
        return p

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root))

    def files(self) -> list[str]:
        try:
            out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=self.root,
                                 capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                return [f for f in out.stdout.splitlines() if f]
        except (OSError, subprocess.TimeoutExpired):
            pass
        return [str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts]

    def read_text(self, p: Path) -> str:
        try:
            return p.read_text(encoding=self.encoding)
        except UnicodeDecodeError as e:
            raise ToolError(f"{self.rel(p)} isn't a text file") from e

    # ---- tools ------------------------------------------------------------
    def list(self, path: str) -> str:
        base = self.path(path)
        prefix = "" if base == self.root else self.rel(base).rstrip("/") + "/"
        files = [f for f in self.files() if f.startswith(prefix)]
        if not files:
            return f"No files under {path or 'the repository'}."
        if len(files) <= LIST_FILES:
            return "\n".join(files)
        # Too many: one line per subfolder with its file count.
        counts: dict[str, int] = {}
        for f in files:
            parts = f[len(prefix):].split("/")
            key = prefix + (parts[0] + "/" if len(parts) > 1 else parts[0])
            counts[key] = counts.get(key, 0) + 1
        return f"{len(files)} files; by folder (list one to see its files):\n" + "\n".join(
            f"{k}  ({n} file{'s' if n != 1 else ''})" if k.endswith("/") else k for k, n in sorted(counts.items()))

    def grep(self, pattern: str, path: str = "") -> str:
        if not pattern:
            raise ToolError("grep needs a pattern")
        base = self.path(path) if path else self.root
        args = ["git", "grep", "-n", "-I", "-E", "--untracked", "-e", pattern]
        if base != self.root:
            args += ["--", self.rel(base)]
        try:
            out = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as e:
            raise ToolError("grep took too long; narrow the path") from e
        if out.returncode not in (0, 1):
            raise ToolError(out.stderr.strip() or "grep failed")
        lines = out.stdout.splitlines()
        if not lines:
            return f"No matches for {pattern!r}."
        shown = [ln if len(ln) < 300 else ln[:300] + "…" for ln in lines[:GREP_HITS]]
        more = f"\n… {len(lines) - GREP_HITS} more matches (narrow the pattern or path)" if len(lines) > GREP_HITS else ""
        return "\n".join(shown) + more

    def outline(self, path: str) -> str:
        p = self.path(path)
        text = self.read_text(p)
        lines = text.splitlines()
        defs = []
        if self.repo_map is not None:
            try:
                for tag in self.repo_map.get_tags(str(p), self.rel(p)):
                    if tag.kind == "def":
                        defs.append(tag.line + 1)
            except Exception:  # noqa: BLE001 - fall back to the regex outline
                defs = []
        if not defs:
            pat = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:def|class|fn|struct|enum|trait|impl|type|interface|function|const|export\s+(?:default\s+)?(?:function|class|const))\b")
            defs = [i + 1 for i, ln in enumerate(lines) if pat.match(ln)]
        defs = sorted(set(defs))
        if not defs:
            return f"{self.rel(p)}: {len(lines)} lines, no definitions found; read it in parts."
        body = "\n".join(f"{n:>5}| {lines[n - 1].strip()[:160]}" for n in defs[:300])
        return f"{self.rel(p)}: {len(lines)} lines\n{body}"

    def read(self, path: str, start: int | None, end: int | None) -> str:
        p = self.path(path)
        if p.is_dir():
            raise ToolError(f"{path} is a folder; use list")
        lines = self.read_text(p).splitlines()
        n = len(lines)
        start = max(1, start or 1)
        end = min(n, end or start + READ_LINES - 1, start + READ_LINES - 1)
        # Ranges are often guessed from an outline: show the lines around
        # them too (a doc comment sits just above the line an outline names).
        start, end = max(1, start - READ_PAD), min(n, end + READ_PAD)
        if n <= WHOLE_FILE:
            # Small files whole: a model reading a narrow window missed the
            # function it was looking for, though the window held it.
            start, end = 1, n
        if n == 0:
            return f"{self.rel(p)} is empty."
        if start > n:
            raise ToolError(f"{self.rel(p)} has only {n} lines")
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
        rest = f"\n(lines {end + 1}-{n} not shown)" if end < n else ""
        return f"{self.rel(p)} lines {start}-{end} of {n}:\n{body}{rest}"

    def run(self, command: str) -> str:
        try:
            out = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True,
                                 timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            return f"Timed out after {RUN_TIMEOUT} s."
        text = (out.stdout + out.stderr).strip()
        if len(text) > RESULT_CHARS:
            text = "…" + text[-RESULT_CHARS:]
        return f"exit code {out.returncode}\n{text}"


def with_filename(reply: str, rel: str) -> str:
    """Adds the filename line Aider's parser needs above each SEARCH block
    that lacks one: the agent already knows the file, and Qwen3.6 dropped
    the line on every try, so a 25-step edit turn changed nothing."""
    out: list[str] = []
    for line in reply.splitlines(keepends=True):
        if line.strip().startswith("<<<<<<< SEARCH"):
            at = len(out) - 1 if out and out[-1].strip().startswith(("```", "~~~")) else len(out)
            named = at > 0 and out[at - 1].strip().strip("`*").endswith(rel)
            if not named:
                out.insert(at, rel + "\n")
        out.append(line)
    return "".join(out)


def apply_edit(ws: Workspace, path: str, reply: str) -> tuple[str, str, str]:
    """Applies the SEARCH/REPLACE blocks in `reply` to `path` with Aider's
    matcher (exact, then whitespace-tolerant). Returns (rel, before, after)."""
    from aider.coders.editblock_coder import DEFAULT_FENCE, do_replace, find_original_update_blocks

    p = ws.path(path)
    rel = ws.rel(p)
    before = ws.read_text(p)
    content = before
    blocks = 0
    try:
        found = list(find_original_update_blocks(with_filename(reply, rel), valid_fnames=[rel]))
    except ValueError as e:
        raise ToolError(f"couldn't parse the edit: {str(e)[:400]}") from e
    for fname, search, replace in found:
        if fname and fname.strip() not in (rel, path):
            raise ToolError(f"the edit names {fname}, but you are editing {rel}")
        # A SEARCH that matches several places would be applied to the first,
        # often the wrong one (seen with a bare closing brace).
        if search.strip() and content.count(search) > 1:
            at = [content[:i].count("\n") + 1 for i in _find_all(content, search)][:6]
            raise ToolError(f"the SEARCH text matches {content.count(search)} places in {rel} (lines "
                            f"{', '.join(map(str, at))}); include more surrounding lines so it matches only one")
        new = do_replace(str(p), content, search, replace, DEFAULT_FENCE)
        if new is None:
            raise ToolError(f"SEARCH text not found in {rel}.{_nearest(content, search)}")
        content = new
        blocks += 1
    if not blocks:
        if not reply.strip():
            raise ToolError("your edit was empty. If you need to see more of the file, read it first, then edit")
        raise ToolError("no SEARCH/REPLACE block found in your reply, which began: " + repr(reply.strip()[:300])
                        + ". Use exactly the <<<<<<< SEARCH / ======= / >>>>>>> REPLACE format")
    p.write_text(content, encoding=ws.encoding)
    return rel, before, content


def _find_all(text: str, part: str):
    i = text.find(part)
    while i != -1:
        yield i
        i = text.find(part, i + 1)


def balance_warning(before: str, after: str) -> str:
    """A note when an edit changes the file's bracket balance, which usually
    means a block was cut or duplicated (language-agnostic, so a hint only)."""
    deltas = []
    for o, c in ("{}", "()", "[]"):
        d = (after.count(o) - after.count(c)) - (before.count(o) - before.count(c))
        if d:
            deltas.append(f"{o}{c} {'+' if d > 0 else ''}{d}")
    if not deltas:
        return ""
    return (f"\nWarning: this edit changed the bracket balance of the file ({', '.join(deltas)}); "
            "check the lines above for a missing or extra bracket and fix it.")


def _nearest(content: str, search: str) -> str:
    """The lines of the file that best match a SEARCH text that didn't, so
    the model can copy them exactly on its next try."""
    import difflib

    want = [ln.strip() for ln in search.splitlines() if ln.strip()]
    lines = content.splitlines()
    if not want or not lines:
        return ""
    best, best_i = 0.0, -1
    for i, ln in enumerate(lines):
        r = difflib.SequenceMatcher(None, ln.strip(), want[0]).ratio()
        if r > best:
            best, best_i = r, i
    if best < 0.5:
        return " Nothing like its first line is in the file; read the file to find the right place."
    lo, hi = max(0, best_i - 3), min(len(lines), best_i + len(want) + 3)
    body = "\n".join(f"{i + 1:>5}| {lines[i]}" for i in range(lo, hi))
    return f" The closest lines in the file are:\n{body}\nCopy SEARCH text exactly from the file (without the line numbers) and try again."


def changed_region(before: str, after: str, context: int = 3) -> str:
    """The edited lines with a little context, numbered, for the model to check."""
    import difflib

    a, b = before.splitlines(), after.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    spans = [(j1, j2) for tag, _, _, j1, j2 in sm.get_opcodes() if tag != "equal"]
    if not spans:
        return "(no change)"
    lo = max(0, spans[0][0] - context)
    hi = min(len(b), spans[-1][1] + context)
    if hi - lo > 80:
        hi = lo + 80
    return "\n".join(f"{i + 1:>5}| {b[i]}" for i in range(lo, hi))


@dataclass
class Agent:
    ws: Workspace
    hooks: Hooks = field(default_factory=Hooks)
    base: str = field(default_factory=lambda: os.environ.get("OPENAI_API_BASE", "").rstrip("/"))
    key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    context: int = 32768
    reasoning: bool = False
    web: bool = True
    memory: str = ""  # OPENPHALANX.md / AGENTS.md
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
    _replies: list = field(default_factory=list)  # (user message, reply), the latest few

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
        self._replies.clear()

    def undone(self, names: list[str]):
        """/undo restored these files: they no longer count as changed."""
        self.changed_session[:] = [f for f in self.changed_session if Path(f).name not in names]
        self.messages.append({"role": "user", "_result": True, "content":
                              "(The user undid your last edit: " + ", ".join(names) + " is back to how it was.)"})

    def reset_system(self):
        """The project notes changed: rebuild the system prompt (this costs
        one uncached request)."""
        self._system_text = None

    def _build_system(self) -> str:
        top = sorted({f.split("/")[0] + ("/" if "/" in f else "") for f in self.ws.files()})
        overview = "Top level of the repository: " + ", ".join(top[:80])
        memory = f"\n\nProject notes from the user (follow them):\n{self.memory.strip()[:8000]}" if self.memory.strip() else ""
        return f"{SYSTEM}\n\n{overview}{memory}"

    def _body(self, extra: dict) -> dict:
        # oppx_utility: the agent has its own web_search tool, so the gateway
        # must not route these calls through its automatic search.
        body = {"model": MODEL, "messages": [{"role": "system", "content": self._system()}] + self._clean(),
                "temperature": 0, "oppx_utility": True, **extra}
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
            self._fit()
            return send()

    def _action(self, tools: tuple[str, ...]) -> dict:
        schema = action_schema(tools)
        data = self._sized(lambda: self._post(self._body({
            "max_tokens": 400,
            "response_format": {"type": "json_schema", "json_schema": {"name": "action", "schema": schema}},
        })))
        msg = data["choices"][0]["message"]
        # Reasoning models may think first, or put the action in their reasoning.
        text = msg.get("content") or msg.get("reasoning_content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            act = json.loads(m.group(0) if m else text)
        except json.JSONDecodeError:
            act = {"tool": "answer", "why": "the action couldn't be read"}
        if act.get("tool") not in tools:
            act["tool"] = "answer"
        return act

    def _room(self, want: int) -> int:
        """Output tokens to ask for: `want`, or what's left of the window."""
        used = self._estimate()
        return max(256, min(want, self.context - used - 256))

    def _held_back(self, old: set[str]) -> tuple[str, bool]:
        """Streams the reply, but shows nothing while it only repeats sentences
        of earlier replies. Returns the reply and whether it was shown: a
        reply that is a repeat from start to end never is."""
        show, shown = self.hooks.stream, False

        def gate(text: str, final: bool):
            nonlocal shown
            if not shown:
                parts = _sentences(text)
                last = parts.pop() if parts and not final else ""  # still being written
                # Something new: a whole sentence, or one that has gone its own way.
                shown = any(p not in old for p in parts) or (
                    len(last) >= 40 and not any(o.startswith(last) for o in old))
            if shown:
                show(text, final)

        self.hooks.stream = gate
        try:
            reply = self._text(ANSWER_RESERVE, stream=True)
        finally:
            self.hooks.stream = show
        if not shown and not is_repeat(reply, old):
            show(reply, True)  # short, and new after all
            shown = True
        return reply, shown

    def _text(self, max_tokens: int, stream: bool, temperature: float | None = None) -> str:
        return self._sized(lambda: self._text_once(max_tokens, stream, temperature))

    def _text_once(self, max_tokens: int, stream: bool, temperature: float | None = None) -> str:
        self._fit()
        body = self._body({"max_tokens": self._room(max_tokens), "stream": stream})
        if temperature is not None:
            body["temperature"] = temperature
        if not stream:
            msg = self._post(body)["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content.strip():
                # Reasoning models sometimes write the whole answer inside
                # their reasoning (SGLang returns it as reasoning_content)
                # and leave the content empty: use what follows a fence or
                # SEARCH marker there.
                thought = msg.get("reasoning_content") or ""
                m = re.search(r"(?s)(\S*\n?```.*|<<<<<<< SEARCH.*)", thought)
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
            saved = int(len(m["content"]) // 3 * self.ratio)
            m["content"] = m["content"].split("\n", 1)[0] + "\n[output removed to save space; run the tool again if you need it]"
            m["_cut"] = True
            est -= saved
            if est <= budget:
                return
        # Still too long: drop whole early turns (up to the next user message).
        while est > budget and len(self.messages) > 8:
            drop = 1
            while drop < len(self.messages) and not self.messages[drop].get("_user"):
                drop += 1
            est -= int(sum(len(m["content"]) for m in self.messages[:drop]) // 3 * self.ratio)
            del self.messages[:drop]

    def _raw_estimate(self) -> int:
        """The system prompt and every message, at ~3 characters a token."""
        return (len(self._system()) + sum(len(m["content"]) + 8 for m in self.messages)) // 3

    def _estimate(self) -> int:
        """Prompt tokens of the next request, corrected by what the server counted."""
        return int(self._raw_estimate() * self.ratio)

    def context_parts(self) -> list[tuple[str, int]]:
        """What the next request holds, estimated like `_fit` does (/context)."""
        tool = sum(len(m["content"]) + 8 for m in self.messages if m.get("_result"))
        talk = sum(len(m["content"]) + 8 for m in self.messages if not m.get("_result"))
        return [(name, int(n // 3 * self.ratio))
                for name, n in (("Instructions", len(self._system())), ("Conversation", talk), ("Tool output", tool))]

    def _clean(self) -> list:
        return [{"role": m["role"], "content": m["content"]} for m in self.messages]

    # ---- the turn ---------------------------------------------------------
    def turn(self, text: str, can_edit: bool = True) -> str:
        try:
            return self._turn(text, can_edit)
        except Exception as e:
            # Leave a trace, so a later turn doesn't assume this one worked.
            self.messages.append({"role": "assistant", "content": f"(This request failed: {str(e)[:200]}. Nothing more was done.)"})
            raise

    def _named_files(self, text: str) -> list[str]:
        """Repository files the message names by path (at most 3)."""
        files = set(self.ws.files())
        found = []
        for word in re.findall(r"[\w./-]+\.\w+", text):
            w = word.strip("./`'\"")
            if w in files and w not in found:
                found.append(w)
        return found[:3]

    def _turn(self, text: str, can_edit: bool) -> str:
        self.messages.append({"role": "user", "content": text, "_user": True})
        # Files the message names are opened up front (as an @-mention would):
        # small ones whole, big ones as an outline to grep or read from.
        for path in self._named_files(text):
            try:
                lines = len(self.ws.read_text(self.ws.path(path)).splitlines())
            except ToolError:
                continue
            act = ({"why": "the file the user named", "tool": "read", "path": path} if lines <= WHOLE_FILE
                   else {"why": "the file the user named (outline; it is long)", "tool": "outline", "path": path})
            self.messages.append({"role": "assistant", "content": json.dumps(act)})
            self.messages.append({"role": "user", "_result": True, "content":
                                  f"Result of step 0 ({act['tool']}):\n{self._do(act)}"})
        tools = tuple(t for t in TOOLS if (can_edit or t not in EDIT_TOOLS) and (self.web or t != "web_search"))
        done: dict[str, int] = {}  # action -> step it was taken, to stop loops
        reads: dict[str, list] = {}  # path -> [(first, last, step)] shown this turn
        repeats = search_errors = failed_edits = 0
        searched = opened = changed = 0  # grep/list, read/outline, edit/create steps this turn
        nudged = edit_nudged = checked = False
        changed_files: list[str] = []
        for n in range(1, MAX_STEPS + 1):
            if self.hooks.interrupted():
                return ""
            self._fit()
            self.hooks.waiting("Thinking")
            # Small models can get stuck repeating an action: after two
            # repeats only the reply is left to choose.
            act = self._action(tools if repeats < 2 else ("answer",))
            tool = act["tool"]
            self.messages.append({"role": "assistant", "content": json.dumps(act, ensure_ascii=False)})
            if tool == "answer":
                if searched and not opened and not nudged:
                    # Small models answer straight from grep hits; one check
                    # that the answer doesn't need the code itself.
                    nudged = True
                    repeats = 0
                    self.messages.append({"role": "user", "_result": True, "content":
                                          "You found where things are but haven't opened any code yet. If the "
                                          "answer depends on how the code works, read the key lines first "
                                          "(outline, then read). If it doesn't, choose answer again."})
                    continue
                if can_edit and not changed and not edit_nudged:
                    # A change was asked for and nothing changed yet: small
                    # models give up after one empty search.
                    edit_nudged = True
                    repeats = 0  # let it act again
                    self.messages.append({"role": "user", "_result": True, "content":
                                          "You haven't changed anything yet, and the user asked for a change. Keep "
                                          "going: list or outline the file the user named, read the right lines, then "
                                          "edit. Choose answer only if it truly can't be done, and say why."})
                    continue
                if can_edit and changed and not checked:
                    # Before claiming the work is done: what actually changed.
                    checked = True
                    repeats = 0
                    self.messages.append({"role": "user", "_result": True, "content":
                                          "Files you changed in this request: " + ", ".join(dict.fromkeys(changed_files))
                                          + ". If the task needs more (other files, callers of something renamed, "
                                          "a test you meant to add), do it now with edit; grep for an old name to check. "
                                          "Otherwise choose answer, and describe only these changes."})
                    continue
                return self._answer(text, self._facts(changed_files, failed_edits, can_edit))
            if tool in ("grep", "list"):
                searched += 1
            elif tool in ("read", "outline"):
                opened += 1
            sig = _signature(act)
            seen_at = self._already_read(act, reads) if tool == "read" else None
            if seen_at:
                repeats += 1
                result = (f"Those lines were already shown at step {seen_at}; they're above. "
                          "Use them, grep for something specific, or answer.")
            elif sig in done and tool not in ("edit", "create", "run"):
                repeats += 1
                result = (f"You already did exactly this at step {done[sig]}; its result is above. "
                          "Use it: answer now, or take a different action.")
            else:
                done[sig] = n
                result = self._do(act)
                if tool == "web_search" and result.startswith("Error:"):
                    # A failing search fails the same way rephrased; small
                    # models kept rewording the query until the steps ran out.
                    search_errors += 1
                    if search_errors >= 2:
                        tools = tuple(t for t in tools if t != "web_search")
                        result += "\n\nWeb search isn't working right now; carry on without it."
                if tool == "read":
                    result = self._note_read(act, result, reads, n)
                if tool in ("edit", "create"):
                    # Only an edit that applied is a change. Counting the attempt
                    # told the model it had changed a file when its edit was refused.
                    if result.startswith("Error:"):
                        failed_edits += 1
                        result += "\n\nNothing was changed. Read the exact lines again and retry, or say that it failed."
                    else:
                        changed += 1
                        path = act.get("path") or ""
                        changed_files.append(path)
                        if path not in self.changed_session:
                            self.changed_session.append(path)
            left = MAX_STEPS - n
            self.messages.append({"role": "user", "_result": True, "content":
                                  f"Result of step {n} ({tool}):\n{result}\n\n[{left} steps left]"})
        self.messages.append({"role": "user", "content": "You have used all your steps."})
        return self._answer(text, self._facts(changed_files, failed_edits, can_edit))

    def _facts(self, changed_files: list[str], failed_edits: int, can_edit: bool) -> str:
        """What really changed, given with every reply: small models claim edits
        they never made, and repeat the claim when asked about it."""
        now = list(dict.fromkeys(changed_files))
        before = [f for f in self.changed_session if f not in now]
        parts = ["Files you changed in this request: " + (", ".join(now) if now else "none") + "."]
        if failed_edits and not now:
            parts.append("An edit you tried was refused, so that change was NOT made: say so, don't describe it as done.")
        elif can_edit and not now:
            # Asked for a change, it read some code and answered as if the code already did it.
            parts.append("The user asked for a change and you made none: begin your reply by saying that nothing "
                         "was changed, then say why, or what is missing.")
        parts.append("Files you changed earlier in this conversation: " + (", ".join(before) if before else "none") + ".")
        parts.append("Never say you added, changed or fixed something in a file that is not in these lists; "
                     "mention the lists only if the user asks about changes.")
        return "\n\nFacts: " + " ".join(parts)

    @staticmethod
    def _already_read(act: dict, reads: dict) -> int | None:
        """The step that already showed every line of this read, if any."""
        shown = reads.get(act.get("path") or "")
        if not shown:
            return None
        start = max(1, (act.get("start") or 1) - READ_PAD)
        end = (act.get("end") or start + READ_LINES - 1) + READ_PAD
        covered = set()
        for a, b, _ in shown:
            covered.update(range(a, b + 1))
        last_line = max(b for _, b, _ in shown)
        wanted = set(range(start, min(end, last_line) + 1))
        if wanted and wanted <= covered and end <= last_line + READ_PAD:
            return shown[-1][2]
        return None

    @staticmethod
    def _note_read(act: dict, result: str, reads: dict, step_no: int) -> str:
        """Records which lines a read showed; a second read of a big file gets
        a hint to grep instead of paging through it."""
        m = re.match(r"\S+ lines (\d+)-(\d+) of (\d+):", result)
        if not m:
            return result
        first, last, total = (int(x) for x in m.groups())
        path = act.get("path") or ""
        reads.setdefault(path, []).append((first, last, step_no))
        if total > 2 * READ_LINES and len(reads[path]) >= 2:
            result += (f"\n\n[{path} has {total} lines; paging through it is slow. "
                       f"grep with path {path!r} for the name you need instead.]")
        return result

    def _answer(self, user_text: str = "", facts: str = "") -> str:
        """The reply, as free text, streamed. A reply that repeats the previous
        one (to a different message) is held back and written again once."""
        asked = _norm(user_text)
        # What it said to other messages; asking the same thing again may get the same reply.
        old = {s for q, r in self._replies if _norm(q) != asked for s in _sentences(r)}
        self.messages.append({"role": "user", "content": ANSWER_PROMPT + facts})
        hidden: list = []
        try:
            reply, shown = self._held_back(old) if old else (self._text(ANSWER_RESERVE, stream=True), True)
            if not shown:
                self.messages[-1] = {"role": "user", "content": ANSWER_PROMPT + facts + "\n\n" + REPEAT_PROMPT}
                # The earlier copies are what it copies from (with them in view
                # the redo came out the same, even with randomness): for this
                # one request they are replaced by a note.
                said = set(_sentences(reply))
                for m in self.messages:
                    if m["role"] == "assistant" and not m["content"].startswith("{") and said & set(_sentences(m["content"])):
                        hidden.append((m, m["content"]))
                        m["content"] = "(An earlier reply of yours, which didn't answer what the user asks now.)"
                reply = self._text(ANSWER_RESERVE, stream=True, temperature=0.7)
        finally:
            for m, content in hidden:
                m["content"] = content
            self.messages.pop()  # the instruction isn't part of the conversation
        self._replies = (self._replies + [(user_text, reply)])[-RECENT_REPLIES:]
        if self.messages and self.messages[-1]["role"] == "assistant" and self.messages[-1]["content"].startswith("{"):
            self.messages[-1] = {"role": "assistant", "content": reply}
        else:
            self.messages.append({"role": "assistant", "content": reply})
        return reply

    def _do(self, act: dict) -> str:
        tool, path = act["tool"], act.get("path") or ""
        title = {
            "list": f"List({path or '.'})",
            "grep": f"Grep({act.get('pattern', '')}{' in ' + path if path else ''})",
            "outline": f"Outline({path})",
            "read": f"Read({path}:{act.get('start') or 1}-{act.get('end') or ''})",
            "edit": f"Update({path})",
            "create": f"Create({path})",
            "run": f"Bash({act.get('command', '')})",
            "web_search": f"Search({act.get('query', '')})",
        }.get(tool, tool)
        try:
            if tool == "list":
                res = self.ws.list(path)
            elif tool == "grep":
                res = self.ws.grep(act.get("pattern", ""), path)
            elif tool == "outline":
                res = self.ws.outline(path)
            elif tool == "read":
                res = self.ws.read(path, act.get("start"), act.get("end"))
            elif tool == "edit":
                res = self._edit(path)
            elif tool == "create":
                res = self._create(path)
            elif tool == "run":
                cmd = act.get("command", "").strip()
                if not cmd:
                    raise ToolError("run needs a command")
                if not self.hooks.confirm_run(cmd):
                    res = "The user declined to run this command."
                else:
                    self.hooks.waiting("Running")
                    res = self.ws.run(cmd)
            elif tool == "web_search":
                res = self._search(act.get("query", ""))
            else:
                raise ToolError(f"unknown tool {tool}")
        except ToolError as e:
            res = f"Error: {e}"
        if len(res) > RESULT_CHARS:
            res = res[:RESULT_CHARS] + "\n… (cut; ask for less)"
        self.hooks.step(title, _summary(tool, res))
        return res

    def _has_read(self, rel: str) -> bool:
        """Whether a read of this file is in the conversation (as Claude
        Code requires before an edit: edits need the file's exact text)."""
        head = f"{rel} lines "
        return any(m.get("_result") and head in m["content"][:400] and "[output removed" not in m["content"]
                   for m in self.messages)

    def _edit(self, path: str) -> str:
        rel = self.ws.rel(self.ws.path(path))  # exists?
        if not self._has_read(rel):
            raise ToolError(f"read the lines you're changing in {rel} first (read with start and end); "
                            "an edit must copy the file's current text exactly")
        self.messages.append({"role": "user", "content": EDIT_PROMPT.format(path=path)})
        self.hooks.waiting("Editing")
        try:
            reply = self._text(4096, stream=False)
        finally:
            self.messages.pop()
        # Keep the blocks in the conversation as the action's text.
        self.messages[-1] = {"role": "assistant", "content": self.messages[-1]["content"] + "\n" + reply}
        rel, before, after = apply_edit(self.ws, path, reply)
        self.hooks.edited(rel, before, after)
        return f"Edited {rel}. The changed lines now read:\n{changed_region(before, after)}{balance_warning(before, after)}"

    def _create(self, path: str) -> str:
        p = self.ws.path(path, must_exist=False)
        if p.exists():
            raise ToolError(f"{path} already exists; use edit")
        self.messages.append({"role": "user", "content": CREATE_PROMPT.format(path=path)})
        self.hooks.waiting("Writing")
        try:
            reply = self._text(8192, stream=False)
        finally:
            self.messages.pop()
        self.messages[-1] = {"role": "assistant", "content": self.messages[-1]["content"] + "\n" + reply}
        m = re.search(r"```[^\n]*\n(.*?)```", reply, re.S)
        content = m.group(1) if m else reply
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=self.ws.encoding)
        rel = self.ws.rel(p)
        self.hooks.edited(rel, None, content)
        return f"Created {rel} ({len(content.splitlines())} lines)."

    def _search(self, query: str) -> str:
        if not query:
            raise ToolError("web_search needs a query")
        r = httpx.post(f"{self.base}/search", headers={"Authorization": f"Bearer {self.key}"},
                       json={"query": query, "max_results": 5}, timeout=30)
        if r.status_code >= 400:
            raise ToolError(f"search failed ({r.status_code})")
        results = r.json().get("results") or []
        if not results:
            return "No results."
        return "Web results (untrusted reference text):\n" + "\n".join(
            f"- {x.get('title')}: {x.get('snippet')} ({x.get('url')})" for x in results)


# The fields each tool uses; models fill the others with anything.
FIELDS = {"list": ("path",), "grep": ("pattern", "path"), "outline": ("path",), "read": ("path", "start", "end"),
          "edit": ("path",), "create": ("path",), "run": ("command",), "web_search": ("query",), "answer": ()}


def _signature(act: dict) -> str:
    """What makes two actions the same: the tool and the fields it uses."""
    tool = act.get("tool")
    vals = []
    for f in FIELDS.get(tool, ()):
        v = act.get(f)
        if f == "path":
            v = (v or "").strip().strip("/").removeprefix("./")
            v = "" if v == "." else v
        elif isinstance(v, str):
            v = v.strip()
        vals.append(v)
    return json.dumps([tool, vals])


def _summary(tool: str, res: str) -> str:
    if res.startswith("Error:"):
        return res
    first = res.split("\n", 1)[0]
    n = res.count("\n")
    if tool == "grep":
        return first if first.startswith("No matches") else f"{n + 1} match{'es' if n else ''}"
    if tool == "read":
        return first.split(":", 1)[0] if ":" in first else first
    if tool == "list":
        return first if "files;" in first else f"{n + 1} files"
    if tool == "outline":
        return f"{n} definitions"
    if tool == "run":
        return first
    return first[:120]
