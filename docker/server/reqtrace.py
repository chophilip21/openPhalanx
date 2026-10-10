"""Request tracing for the gateway: what each chat request cost and how it
relates to the ones before it, kept in memory for the server app's Trace page.

Nothing here is written to disk, and by default nothing a client sent is kept:
a record holds timings, token counts and the labels a client attached (turn,
step, kind of call, tool name). The shape of a conversation is kept as a chain
of hashes, one per message prefix, which is enough to rebuild the tree of
shared prefixes that SGLang's RadixAttention reuses, without the text. Content
capture (the latest message, the reply, tool titles) is a separate switch, off
by default, and switching it off drops what was captured.

No dependencies: the gateway imports it, and the tests run it alone.
"""

from __future__ import annotations

import collections
import hashlib
import time

MAX_RECORDS = 2000
MAX_SEEN = 200_000  # prefix hashes remembered, to tell what SGLang could have had cached
CONTENT_CHARS = 6000  # of the latest message and of the reply, when capture is on
TREE_REQUESTS = 300  # the tree is built from this many of the latest requests
# What a client may say about a request (`oppx_trace` in the body). Short
# labels and numbers only; anything else is dropped.
KINDS = ("action", "reply", "edit", "create", "plan", "rethink", "stock", "arguments", "route", "other")
EVENTS = ("cut", "dropped", "repeat", "rethink", "stock", "stuck", "overflow", "retry", "nudge", "plan")


def _text(content) -> str:
    """A message's text (OpenAI content may be a list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def prefix_chain(messages: list) -> list[tuple[str, int]]:
    """One (hash, characters so far) per message: the hash covers the
    conversation up to and including that message, so two requests share a
    hash exactly where they share a prefix."""
    chain, h, chars = [], hashlib.blake2b(digest_size=8), 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        text = _text(m.get("content"))
        h.update(str(m.get("role", "")).encode())
        h.update(b"\0")
        h.update(text.encode("utf-8", "replace"))
        h.update(b"\1")
        chars += len(text) + 8
        chain.append((h.copy().hexdigest(), chars))
    return chain


def client_labels(raw) -> dict:
    """The labels a client sent, cleaned: known keys, short strings, small numbers."""
    if not isinstance(raw, dict):
        return {}

    def short(key: str, limit: int) -> str | None:
        v = raw.get(key)
        return v[:limit] if isinstance(v, str) and v else None

    def number(key: str, top: int = 10_000_000) -> int | None:
        v = raw.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= top else None

    out = {
        "session": short("session", 40),
        "turn": number("turn"),
        "step": number("step"),
        "kind": raw.get("kind") if raw.get("kind") in KINDS else ("other" if raw.get("kind") else None),
        "tool": short("tool", 60),
        "title": short("title", 200),  # kept only while content capture is on: it names files and patterns
        # What the previous action of this session turned out to be, known only
        # once the client had run it: its tool, its title, and how long the
        # client took to run it (the model isn't involved in that time).
        "prev_tool": short("prev_tool", 60),
        "prev_title": short("prev_title", 200),
        "prev_ms": number("prev_ms"),
        "window": number("window"),
        "estimate": number("estimate"),
    }
    events = raw.get("events")
    if isinstance(events, dict):
        clean = {k: v for k, v in events.items() if k in EVENTS and isinstance(v, int) and not isinstance(v, bool) and 0 < v < 100_000}
        if clean:
            out["events"] = clean
    return {k: v for k, v in out.items() if v is not None}


class Trace:
    """The latest requests, newest last, and the prefixes seen so far."""

    def __init__(self, max_records: int = MAX_RECORDS):
        self.records: collections.deque[dict] = collections.deque(maxlen=max_records)
        self.seq = 0
        self.content = False
        self.started_at = time.time()
        # prefix hash -> the request that first sent it (oldest dropped first)
        self._seen: collections.OrderedDict[str, int] = collections.OrderedDict()

    # ---- recording ----------------------------------------------------------
    def begin(self, device: dict, body: dict, labels, *, stream: bool) -> dict:
        """A request has arrived. Returns its record; the caller fills in what
        happens to it and calls `finish`."""
        self.seq += 1
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        chain = prefix_chain(messages)
        total = chain[-1][1] if chain else 0
        # The longest prefix some earlier request already sent: what the
        # prefix cache could hold of this one, if nothing was evicted.
        known = 0
        for digest, chars in chain:
            if digest not in self._seen:
                break
            known = chars
        for digest, _ in chain:
            if digest not in self._seen:
                self._seen[digest] = self.seq
                if len(self._seen) > MAX_SEEN:
                    self._seen.popitem(last=False)
        labels = client_labels(labels)
        if labels.get("session") and (labels.get("prev_tool") or labels.get("prev_ms") is not None):
            # Attach to the action that chose it: the latest one of this session.
            for old in reversed(self.records):
                if old.get("session") == labels["session"] and old.get("kind") == "action":
                    if "tool" not in old and labels.get("prev_tool"):
                        old["tool"] = labels["prev_tool"]
                    if self.content and labels.get("prev_title"):
                        old["title"] = labels["prev_title"]
                    if labels.get("prev_ms") is not None:
                        old["tool_ms"] = labels["prev_ms"]
                    break
        rec = {
            "seq": self.seq,
            "at": time.time(),
            "device": str(device.get("id", ""))[:16],
            "device_name": str(device.get("name", ""))[:60],
            **{k: v for k, v in labels.items() if k != "title" and not k.startswith("prev_")},
            "messages": len(chain),
            "chars": total,
            "known_chars": known,
            "stream": stream,
            "constrained": "json" if body.get("response_format") else ("regex" if body.get("regex") else None),
            "max_tokens": body.get("max_tokens") if isinstance(body.get("max_tokens"), int) else None,
            "state": "queued",
            "_chain": chain,
            "_t0": time.monotonic(),
        }
        if self.content:
            if labels.get("title"):
                rec["title"] = labels["title"]
            if messages and isinstance(messages[-1], dict):
                rec["input_tail"] = _text(messages[-1].get("content"))[-CONTENT_CHARS:]
            rec["output"] = ""
        self.records.append(rec)
        return rec

    @staticmethod
    def mark(rec: dict, key: str) -> None:
        """Milliseconds since arrival, once: `queue_ms` (got a slot),
        `ttft_ms` (first generated token)."""
        if key not in rec:
            rec[key] = round((time.monotonic() - rec["_t0"]) * 1000)

    @staticmethod
    def usage(rec: dict, usage) -> None:
        if not isinstance(usage, dict):
            return
        prompt = int(usage.get("prompt_tokens") or 0)
        details = usage.get("prompt_tokens_details")
        rec["prompt_tokens"] = prompt
        rec["cached_tokens"] = int((details or {}).get("cached_tokens") or 0) if isinstance(details, dict) else 0
        rec["completion_tokens"] = int(usage.get("completion_tokens") or 0)
        # Characters to tokens with this request's own ratio.
        if rec.get("chars"):
            rec["expected_cached"] = round(prompt * rec["known_chars"] / rec["chars"])

    def output(self, rec: dict, text: str) -> None:
        if self.content and "output" in rec and text and len(rec["output"]) < CONTENT_CHARS:
            rec["output"] = (rec["output"] + text)[:CONTENT_CHARS]

    @staticmethod
    def finish(rec: dict, status: int, error: str | None = None) -> None:
        if rec.get("state") in ("done", "failed"):
            return
        rec["total_ms"] = round((time.monotonic() - rec["_t0"]) * 1000)
        rec["status"] = status
        rec["state"] = "failed" if status >= 400 or error else "done"
        if error:
            rec["error"] = error[:120]

    def set_content(self, on: bool) -> None:
        self.content = bool(on)
        if not self.content:
            for rec in self.records:
                for key in ("title", "input_tail", "output"):
                    rec.pop(key, None)

    def clear(self) -> None:
        self.records.clear()
        self._seen.clear()

    # ---- reading ------------------------------------------------------------
    @staticmethod
    def public(rec: dict) -> dict:
        return {k: v for k, v in rec.items() if not k.startswith("_")}

    def since(self, after: int = 0, limit: int = 500) -> dict:
        """Records newer than `after`, and the unfinished ones before it (they
        change until they finish)."""
        recs = [self.public(r) for r in self.records if r["seq"] > after or r.get("state") not in ("done", "failed")]
        return {"latest": self.seq, "content": self.content, "started_at": self.started_at, "records": recs[-limit:]}

    def tree(self, last: int = TREE_REQUESTS) -> dict:
        """The tree of shared prefixes among the latest requests. A node is a
        run of messages every request through it has in common; it ends where
        requests part ways or one of them ends. Sizes are in tokens, from each
        request's own characters-per-token ratio."""
        recs = [r for r in list(self.records)[-last:] if r.get("_chain")]
        children: dict[str, dict[str, None]] = {"": {}}
        info: dict[str, dict] = {}
        for r in recs:
            ratio = (r["prompt_tokens"] / r["chars"]) if r.get("prompt_tokens") and r.get("chars") else 1 / 3.2
            parent = ""
            for digest, chars in r["_chain"]:
                node = info.get(digest)
                if node is None:
                    node = info[digest] = {"parent": parent, "tokens": round(chars * ratio), "requests": 0, "ends": [],
                                           "first": r["seq"], "last": r["seq"], "last_at": r["at"]}
                    children.setdefault(parent, {})[digest] = None
                    children.setdefault(digest, {})
                node["requests"] += 1
                node["last"], node["last_at"] = r["seq"], r["at"]
                parent = digest
            if r["_chain"]:
                info[r["_chain"][-1][0]]["ends"].append(r["seq"])

        # Collapse runs: a node with one child and no request ending at it is
        # the same segment as its child.
        nodes = []

        todo = [(root, "", 0) for root in reversed(list(children[""]))]
        while todo:  # depth first, without recursion: a long session is a deep chain
            digest, parent_id, start_tokens = todo.pop()
            node = info[digest]
            while len(children[digest]) == 1 and not node["ends"]:
                digest = next(iter(children[digest]))
                node = info[digest]
            nodes.append({
                "id": digest, "parent": parent_id, "start": start_tokens, "end": max(start_tokens, node["tokens"]),
                "requests": node["requests"], "ends": node["ends"][-8:], "ended": len(node["ends"]),
                "first": node["first"], "last": node["last"], "last_at": node["last_at"],
            })
            todo.extend((child, digest, max(start_tokens, node["tokens"])) for child in reversed(list(children[digest])))
        return {"nodes": nodes, "requests": len(recs), "latest": self.seq}
