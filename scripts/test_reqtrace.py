#!/usr/bin/env python3
"""Unit tests for the gateway's request trace (docker/server/reqtrace.py). No dependencies:

    python3 scripts/test_reqtrace.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docker/server"))

import reqtrace  # noqa: E402

DEVICE = {"id": "dev1", "name": "laptop"}


def msgs(*texts):
    return [{"role": "system", "content": "rules"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(texts)]


class TraceTests(unittest.TestCase):
    def run_request(self, trace, messages, labels=None, prompt=None, cached=0, out=5, status=200):
        rec = trace.begin(DEVICE, {"messages": messages, "max_tokens": 100}, labels, stream=True)
        trace.mark(rec, "queue_ms")
        trace.mark(rec, "ttft_ms")
        if prompt is not None:
            trace.usage(rec, {"prompt_tokens": prompt, "completion_tokens": out, "prompt_tokens_details": {"cached_tokens": cached}})
        trace.finish(rec, status)
        return rec

    def test_a_record_holds_no_text_unless_content_is_on(self):
        trace = reqtrace.Trace()
        labels = {"session": "s1", "turn": 1, "step": 2, "kind": "action", "tool": "grep", "title": "Grep(secret_name)"}
        rec = self.run_request(trace, msgs("where is the secret key?"), labels, prompt=40)
        public = trace.public(rec)
        self.assertNotIn("secret", repr(public))
        self.assertEqual((public["session"], public["turn"], public["step"], public["kind"], public["tool"]), ("s1", 1, 2, "action", "grep"))
        self.assertEqual(public["state"], "done")
        self.assertNotIn("_chain", public)

        trace.set_content(True)
        rec = self.run_request(trace, msgs("where is the secret key?"), labels, prompt=40)
        trace.output(rec, "in vault.rs")
        self.assertEqual(rec["title"], "Grep(secret_name)")
        self.assertIn("secret key", rec["input_tail"])
        self.assertEqual(rec["output"], "in vault.rs")
        trace.set_content(False)  # off: what was captured is dropped
        self.assertNotIn("secret", repr([trace.public(r) for r in trace.records]))

    def test_an_action_learns_its_tool_from_the_next_request(self):
        trace = reqtrace.Trace()
        act = self.run_request(trace, msgs("q"), {"session": "s1", "turn": 1, "step": 1, "kind": "action"}, prompt=10)
        other = self.run_request(trace, msgs("z"), {"session": "s2", "turn": 1, "step": 1, "kind": "action"}, prompt=10)
        self.run_request(trace, msgs("q", "a", "r"), {"session": "s1", "turn": 1, "step": 2, "kind": "action",
                                                     "prev_tool": "grep", "prev_title": "Grep(x)", "prev_ms": 12}, prompt=20)
        self.assertEqual((act["tool"], act["tool_ms"]), ("grep", 12))
        self.assertNotIn("title", act)  # titles only while content is kept
        self.assertNotIn("tool", other)
        self.assertFalse(any(k.startswith("prev_") for r in trace.records for k in r))

    def test_labels_are_cleaned(self):
        labels = reqtrace.client_labels({"session": "x" * 100, "turn": -1, "step": True, "kind": "weird", "tool": 7,
                                         "events": {"cut": 3, "bogus": 1, "stuck": "yes"}, "extra": "dropped"})
        self.assertEqual(labels, {"session": "x" * 40, "kind": "other", "events": {"cut": 3}})
        self.assertEqual(reqtrace.client_labels("not a dict"), {})

    def test_expected_cache_follows_the_shared_prefix(self):
        trace = reqtrace.Trace()
        first = self.run_request(trace, msgs("q1"), prompt=100)
        self.assertEqual(first["known_chars"], 0)  # nothing sent before
        grown = self.run_request(trace, msgs("q1", "a1", "q2"), prompt=200, cached=96)
        self.assertEqual(grown["known_chars"], first["chars"])
        self.assertEqual(grown["expected_cached"], round(200 * first["chars"] / grown["chars"]))
        # A changed early message: only the system prompt is shared.
        changed = self.run_request(trace, msgs("q1 edited", "a1", "q2"), prompt=200, cached=10)
        self.assertEqual(changed["known_chars"], len("rules") + 8)

    def test_tree_branches_where_conversations_part(self):
        trace = reqtrace.Trace()
        a1 = self.run_request(trace, msgs("q1"), prompt=30)
        a2 = self.run_request(trace, msgs("q1", "a1", "q2"), prompt=60)
        b1 = self.run_request(trace, msgs("other question"), prompt=40)
        tree = trace.tree()
        nodes = {n["id"]: n for n in tree["nodes"]}
        roots = [n for n in tree["nodes"] if n["parent"] == ""]
        self.assertEqual(len(roots), 1, "one system prompt, one root")
        root = roots[0]
        self.assertEqual(root["requests"], 3)
        kids = [n for n in tree["nodes"] if n["parent"] == root["id"]]
        self.assertEqual(len(kids), 2, "the two conversations part after the system prompt")
        a_branch = next(n for n in kids if a1["seq"] in n["ends"])
        self.assertEqual(a_branch["requests"], 2)
        leaf = next(n for n in tree["nodes"] if n["parent"] == a_branch["id"])
        self.assertEqual(leaf["ends"], [a2["seq"]])
        self.assertGreater(leaf["end"], leaf["start"])
        self.assertTrue(any(b1["seq"] in n["ends"] for n in kids))
        self.assertEqual(len(nodes), 4)

    def test_a_long_chain_does_not_recurse(self):
        trace = reqtrace.Trace()
        texts = []
        for i in range(1500):
            texts.append(f"m{i}")
            if i % 5 == 0:
                self.run_request(trace, msgs(*texts), prompt=10 + i)
        self.assertGreater(len(trace.tree()["nodes"]), 200)

    def test_since_returns_new_and_unfinished(self):
        trace = reqtrace.Trace(max_records=5)
        open_rec = trace.begin(DEVICE, {"messages": msgs("q")}, None, stream=False)
        for i in range(3):
            self.run_request(trace, msgs(f"q{i}"), prompt=10)
        got = trace.since(after=3)
        self.assertEqual([r["seq"] for r in got["records"]], [open_rec["seq"], 4])
        self.assertEqual(got["latest"], 4)
        for i in range(10):
            self.run_request(trace, msgs(f"r{i}"), prompt=10)
        self.assertEqual(len(trace.records), 5)
        failed = self.run_request(trace, msgs("x"), status=400)
        self.assertEqual(failed["state"], "failed")


if __name__ == "__main__":
    unittest.main()
