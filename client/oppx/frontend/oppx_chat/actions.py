"""Reading the model's replies as actions: the action schema, parsing and
cleaning an action, telling text from an action, and the titles shown for each
step. Also the callbacks the UI passes (`Hooks`) and the agent's errors."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .prompts import CONNECTOR_HEAD, LOOKUPS, MAX_THEN


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _sentences(text: str) -> list[str]:
    """Normalized sentences and lines, the units a repeat is made of."""
    return [n for n in (_norm(p) for p in re.split(r"(?<=[.!?:])\s+|\n+", text)) if n]


def is_repeat(reply: str, old: set[str]) -> bool:
    """Every sentence of the reply was already said in an earlier one."""
    parts = _sentences(reply)
    return bool(parts) and all(p in old for p in parts)


def _unfenced(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0] if t.rstrip().endswith("```") else t
    return t.strip()


def maybe_json(text: str) -> bool:
    """While a reply streams: it has nothing readable yet, or it opens like a JSON object."""
    t = text.lstrip()
    if t.startswith("```"):
        if "\n" not in t:
            return True
        t = t.split("\n", 1)[1].lstrip()
    return t[:1] in ("", "{")


def as_action(text: str) -> dict | None:
    """The reply parsed as one of our JSON actions (or an empty object), if that
    is all it is: a model that keeps writing actions when asked for the reply."""
    t = _unfenced(text)
    if not t.startswith("{"):
        return None
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        # Cut off mid-object, or several objects: still not a reply.
        return {} if re.match(r'\{\s*"(?:enough|missing|why|tool)"\s*:', t) else None
    if isinstance(obj, dict) and (not obj or "tool" in obj or set(obj) <= {"enough", "missing", "why", "arguments", *FIELD_TYPES}):
        return obj
    return None


# What each tool takes (required first, marked with *). One schema per tool:
# with a single flat schema models filled fields of other tools (a path in
# "query", "end" on a grep), and the tool then ran without them.
TOOL_FIELDS = {
    "find": ("*query",), "grep": ("*pattern", "path"), "list": ("path",), "outline": ("*path",),
    "read": ("*path", "start", "end"), "plan": (), "edit": ("*path",), "create": ("*path",),
    "run": ("*command",), "start": ("*command",), "web_search": ("*query",), "browser": (), "answer": (),
}
FIELD_TYPES = {
    "path": {"type": "string", "maxLength": 300}, "pattern": {"type": "string", "maxLength": 200},
    "query": {"type": "string", "maxLength": 200}, "command": {"type": "string", "maxLength": 500},
    "start": {"type": "integer", "minimum": 1}, "end": {"type": "integer", "minimum": 1},
}


ACTION_KEYS = {"enough", "missing", "tool", "why", "then"}


def clean_arguments(args: dict, schema: dict) -> dict:
    """A tool's arguments without our action's own fields: unconstrained (a
    schema the server's grammar refused), a model wrote its action with the
    arguments inside it ({"enough": false, "tool": …, "arguments": {…}})."""
    props = schema.get("properties") or {}
    inner = args.get("arguments")
    if isinstance(inner, dict) and "arguments" not in props and set(args) - {"arguments"} <= ACTION_KEYS:
        args = inner
    return {k: v for k, v in args.items() if k in props or k not in ACTION_KEYS}


# Starts at a line break followed by spaces only: with `\n\s*`, a run of blank
# lines was tried from each of its line breaks (quadratic; scripts/check_regexes.py).
_ACTION_TAIL = re.compile(r'\n[ \t]*(?:```(?:json)?\s*)?\{\s*"(?:enough|missing|tool)"[\s\S]*$')


def cut_action_tail(text: str) -> str:
    """Text without a JSON action at its end, nor the blank lines before it."""
    m = _ACTION_TAIL.search(text)
    return text[:m.start()].rstrip() if m else text


def _outer_braces(text: str) -> str | None:
    """From the first "{" to the last "}": where a JSON object is in a reply.
    String search, not the regex `{.*}` (quadratic on a reply of many "{")."""
    i, j = text.find("{"), text.rfind("}")
    return text[i:j + 1] if 0 <= i < j else None


def _fenced(reply: str, path: str = "") -> str | None:
    """The body of the first fenced block (```lang ... ```), or None. With a
    path, a block holding only that file's name is skipped: asked again for
    a new file, Qwen3.6 fenced its name, and the name became the file."""
    names = {path, path.removeprefix("./"), Path(path).name} - {""} if path else set()
    pos = 0
    while True:
        start = reply.find("```", pos)
        if start < 0:
            return None
        body = reply.find("\n", start)
        end = reply.find("```", body + 1) if body >= 0 else -1
        if end < 0:
            return None
        text = reply[body + 1:end]
        if text.strip() not in names:
            return text
        pos = end + 3


def _query_words(query: str) -> frozenset:
    """A search query as its words: lower case, no punctuation, no filler."""
    found = re.findall(r"[a-z0-9]+", query.lower())
    filler = {"a", "an", "the", "of", "for", "in", "on", "to", "and", "or", "by", "with", "how", "what", "is", "are", "do", "does", "from"}
    return frozenset(w for w in found if w not in filler) or frozenset(found)


def mcp_kind(kind: str) -> str:
    from .mcp import KINDS
    return KINDS.get(kind, KINDS["other"])


def whole_sentences(text: str, tokens: int) -> str:
    """A text that ran into its token limit, cut back to where its last
    complete line or sentence ends, so a plan doesn't stop in the middle of a
    word. Left as it is when it ended by itself, or when little would remain."""
    body = text.rstrip()
    if len(body) < tokens * 2.5 or body.endswith((".", "!", "?", ":", ")", "`", "]", '"')):
        return text  # well under the limit (a token is 3 to 4 characters), or it ends cleanly
    # The end of a line, or of a sentence (a full stop after a word, not the "3." of a list).
    ends = [m.end() for m in re.finditer(r"(?<=[^\W\d])[.!?](?=\s)|\n", body)]
    cut = ends[-1] if ends else 0
    return body[:cut].rstrip() + " …" if cut >= len(body) // 2 else text


def several_parts(text: str) -> bool:
    """A request that asks for several things: numbered or bulleted items, or
    simply a long one. Those get a review of each part before the reply."""
    items = re.findall(r"(?:^|[\s:;])(?:\d{1,2}[.)]|[-*•])\s+\S", text)
    return len(items) >= 2 or len(text) >= 400


def misplaced_lookup(text: str) -> dict | None:
    """Asked for the text of an edit, a model sometimes writes that it has to
    look at something first, and the action for that ("Let me read the current
    state of the file first." then a read as JSON). That action, when it is a
    lookup; None for anything else."""
    lines = text.rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith(("{", "```")):
            act = as_action("\n".join(lines[i:]))
            if act is not None:
                return act if act.get("tool") in LOOKUPS else None
    return None


def degenerate(reply: str) -> bool:
    """A reply that is one or two lines over and over (Qwen3.6, asked for an
    edit: `<tool_call>` until the 4096-token limit, two minutes of GPU)."""
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    return len(lines) >= 20 and len(set(lines)) <= 2


def _has_payload(reply: str, path: str = "") -> bool:
    """An edit's block, or a code block that is neither an action nor the file's
    name, is there to use whatever came with it: Qwen3.6 wrote its create action
    and then the whole 200-line file, and asking again cost a second full file."""
    if "<<<<<<< SEARCH" in reply:
        return True
    body = _fenced(reply, path)
    return body is not None and bool(body.strip()) and as_action(body) is None


def _regex_literal(text: str) -> str:
    """Text as a regex that matches only itself (for a server's regex constraint)."""
    return "".join("\\" + c if c in ".^$*+?()[]{}|\\" else c for c in text)


def _action_not_text(reply: str, path: str = "") -> bool:
    """Asked for a file's content or an edit's blocks, the reply is words and
    then an action that isn't a lookup (those are done in place, see
    misplaced_lookup): no code block or edit in it, so nothing to write."""
    if "<<<<<<< SEARCH" in reply:
        return False
    body = _fenced(reply, path)
    if body is not None and as_action(body) is None:
        return False
    return strip_action(reply) != reply.strip() and misplaced_lookup(reply) is None


def strip_action(text: str) -> str:
    """Free text without a JSON action tacked on at its end: asked for a plan,
    a model writes the plan and then its next action, and that line was shown
    to the user as part of the plan."""
    lines = text.rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith(("{", "```")) and as_action("\n".join(lines[i:])) is not None:
            kept = lines[:i]
            while kept and (not kept[-1].strip() or kept[-1].strip().startswith("```")):
                kept.pop()  # the fence the action sat in
            return "\n".join(kept).rstrip()
    return text.strip()


def action_schema(tools: tuple[str, ...], flat: bool = False) -> dict:
    missing = {"type": "string", "maxLength": 90}  # every step pays for these tokens
    if flat:  # for a server whose grammar can't do anyOf
        return {"type": "object", "additionalProperties": False, "required": ["enough", "tool"],
                "properties": {"enough": {"type": "boolean"}, "missing": missing,
                               "tool": {"type": "string", "enum": list(tools)}, **FIELD_TYPES}}
    def branch(t: str, head: dict, then: dict | None = None) -> dict:
        fields = TOOL_FIELDS.get(t, ())  # a connector tool: its arguments are written in a second call
        props = {**head, "tool": {"type": "string", "enum": [t]}}
        props.update({f.lstrip("*"): FIELD_TYPES[f.lstrip("*")] for f in fields})
        if then:
            props["then"] = then
        return {"type": "object", "properties": props, "additionalProperties": False,
                "required": [*head, "tool"] + [f[1:] for f in fields if f.startswith("*")]}

    # A lookup may bring up to two more lookups along ("then"): each model
    # call costs a second or two whatever it asks for.
    lookups = [branch(t, {}) for t in LOOKUPS if t in tools]
    then = {"type": "array", "maxItems": MAX_THEN, "items": lookups[0] if len(lookups) == 1 else {"anyOf": lookups}} if lookups else None
    one = []
    for t in tools:
        if t == "answer":
            one.append(branch(t, {"enough": {"type": "boolean", "enum": [True]}}))
        else:
            one.append(branch(t, {"enough": {"type": "boolean", "enum": [False]}, "missing": missing},
                              then if t in LOOKUPS else None))
    return one[0] if len(one) == 1 else {"anyOf": one}


@dataclass
class Hooks:
    """What the UI provides. Every callback is optional."""

    step: Callable[[str, str], None] = lambda title, detail: None  # an action and its result
    stream: Callable[[str, bool], None] = lambda text, final: None  # the reply so far
    # True to run it; False, or a sentence for the model (what the user said instead, why it is refused).
    confirm_run: Callable[[str], bool | str] = lambda command: False
    # A connector tool is about to be called: its name, the arguments as JSON, and whether it says it only reads.
    confirm_tool: Callable[[str, str, bool], bool] = lambda name, arguments, read_only: False
    waiting: Callable[[str | None], None] = lambda label: None  # the model is busy (None: stop)
    edited: Callable[[str, str | None, str], None] = lambda path, before, after: None
    # A document or picture about to be replaced, as bytes: what /undo restores
    # (`edited` then gets its text, for the diff).
    original: Callable[[str, bytes], None] = lambda path, data: None
    interrupted: Callable[[], bool] = lambda: False
    notice: Callable[[str], None] = lambda text: None  # a warning from the harness (stuck, gave up)
    # The model asked for the browser: True once it runs, or a sentence for
    # the model (the user said no, or it couldn't start).
    start_browser: Callable[[], bool | str] = lambda: "the user's setup has no browser"
    # Does this reply say the assistant is about to make a change now? (A
    # one-word model decision the app supplies; the engine has no UI imports.)
    announces: Callable[[str], bool] = lambda reply: False


class ToolError(Exception):
    pass


class ContextOverflow(RuntimeError):
    """The server refused a request as longer than the model's window."""

    def __init__(self, message: str, input_tokens: int):
        super().__init__(message)
        self.input_tokens = input_tokens


# SGLang's refusals; the first number is the request's real input size.
OVERFLOW = re.compile(r"(?<!\d)(\d+) tokens from the input messages|The input \((\d+) tokens\) is longer"
                      r"|Input length \((\d+) tokens\) exceeds")


def server_error(status: int, text: str) -> RuntimeError:
    m = OVERFLOW.search(text)
    if status == 400 and m:
        return ContextOverflow(f"server error {status}: {text[:300]}", int(next(g for g in m.groups() if g)))
    return RuntimeError(f"server error {status}: {text[:300]}")


# The fields each tool uses; models fill the others with anything.
FIELDS = {"list": ("path",), "find": ("query",), "plan": (), "grep": ("pattern", "path"), "outline": ("path",), "read": ("path", "start", "end"),
          "edit": ("path",), "create": ("path",), "run": ("command",), "start": ("command",),
          "web_search": ("query",), "browser": (), "answer": ()}


def _title(act: dict) -> str:
    """An action as the user sees it."""
    tool, path = act.get("tool"), act.get("path") or ""
    return {
        "list": f"List({path or '.'})",
        "find": f"Find({act.get('query') or act.get('pattern') or ''})",
        "grep": f"Grep({act.get('pattern', '')}{' in ' + path if path else ''})",
        "outline": f"Outline({path})",
        "read": f"Read({path}:{act.get('start') or 1}-{act.get('end') or ''})",
        "edit": f"Update({path})",
        "create": f"Create({path})",
        "run": f"Bash({act.get('command', '')})",
        "start": f"Start({act.get('command', '')})",
        "web_search": f"Search({act.get('query', '')})",
        "browser": "Browser(start)",
    }.get(tool, str(tool))


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
    if res.startswith("Not edited yet:"):
        return "Not edited yet: it looked at that first"
    if res.startswith(CONNECTOR_HEAD):
        body = res[len(CONNECTOR_HEAD):].rsplit("\n(Called ", 1)[0].strip()  # the arguments are in the title
        # The first line that says something (Playwright opens with "### Page").
        first = next((ln.strip(" -") for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith(("#", "```"))),
                     body.split("\n", 1)[0])[:100]
        return first + (f" … ({len(body):,} characters)" if len(body) > len(first) else "")
    first = res.split("\n", 1)[0]
    n = res.count("\n")
    if tool == "grep":
        return first.rstrip(":")
    if tool == "find":
        m = re.findall(r"(?m)^(?:Files named like it|Definitions) \((\d+)\)", res)
        return first if first.startswith("No ") else f"{sum(int(x) for x in m)} found"
    if tool == "read":
        return first.split(":", 1)[0] if ":" in first else first
    if tool == "list":
        return first if "files;" in first else f"{n + 1} files"
    if tool == "outline":
        return f"{n} definitions"
    if tool in ("run", "start"):
        return first[:120]
    if tool == "web_search":
        # Not the heading ("Web results (untrusted reference text):"): how many, and the first.
        hits = re.findall(r"(?m)^- (.*)$", res)
        if not hits:
            return first[:120]
        count = f"{len(hits)} result{'s' if len(hits) != 1 else ''}"
        again = " (from your earlier search, not searched again)" if res.startswith("(Not searched again") else ""
        return f"{count}{again}: {hits[0].split(': ', 1)[0][:80]}" + (" …" if len(hits) > 1 else "")
    if tool == "browser":
        return "Running (headless)" if res.startswith("The browser is running") else first[:120]
    return first[:120]
