#!/usr/bin/env python3
"""Unit tests for the gateway's search guard (docker/server/searchguard.py). No dependencies:

    python3 scripts/test_searchguard.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docker/server"))

import searchguard as G  # noqa: E402

HIT = [{"title": "t", "url": "https://example.com", "snippet": "s"}]
BLOCKED = [["brave", "Suspended: too many requests"], ["duckduckgo", "timeout"]]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.g = G.Guard(now=self.clock, wall=self.clock)

    def search(self, query, results=HIT, unresponsive=None):
        """One search as the gateway does it; True when it went to the engines."""
        cached = self.g.cached_results(query)
        if cached is not None:
            return False
        self.g.admit()
        self.g.record(query, results, unresponsive)
        return True

    def test_the_same_query_again_is_answered_from_memory(self):
        self.assertTrue(self.search("Vite  latest release"))
        self.assertFalse(self.search("vite latest RELEASE"))  # case and spacing don't matter
        self.assertEqual(self.g.cached_results("vite latest release"), HIT)
        self.assertEqual((self.g.sent, self.g.cached), (1, 2))
        self.clock.t += G.CACHE_S + 1
        self.assertIsNone(self.g.cached_results("vite latest release"))

    def test_an_empty_answer_from_working_engines_is_a_result(self):
        self.assertTrue(self.search("zxqv nothing matches", results=[]))
        self.assertEqual(self.g.cached_results("zxqv nothing matches"), [])
        # Some engines down, others answered: still a result, nothing trips.
        self.assertTrue(self.search("partly", results=HIT, unresponsive=BLOCKED))
        self.assertFalse(self.g.paused)

    def test_searches_leave_at_a_bounded_rate(self):
        for i in range(G.BURST):
            self.search(f"q{i}")
        with self.assertRaises(G.Refused) as e:
            self.search("one too many")
        self.assertIn("paced", str(e.exception))
        self.assertGreaterEqual(e.exception.retry_after, 1)
        self.assertLessEqual(e.exception.retry_after, G.REFILL_S)
        self.clock.t += G.REFILL_S
        self.assertTrue(self.search("one too many"))
        self.assertEqual(self.g.paced, 1)

    def test_engines_that_refuse_pause_searching(self):
        with self.assertRaises(G.Refused) as e:
            self.search("a", results=[], unresponsive=BLOCKED)
        self.assertIn("brave: Suspended: too many requests", str(e.exception))
        self.assertFalse(self.g.paused)  # one refusal can be a blip
        with self.assertRaises(G.Refused) as e:
            self.search("b", results=[], unresponsive=BLOCKED)
        self.assertTrue(self.g.paused)
        self.assertIn("paused for 5 min", str(e.exception))
        self.assertEqual(e.exception.retry_after, G.PAUSE_S)
        # While paused nothing goes out, and the refusal says why and for how long.
        sent = self.g.sent
        self.clock.t += 60
        with self.assertRaises(G.Refused) as e:
            self.search("c")
        self.assertEqual(self.g.sent, sent)
        self.assertIn("paused for 4 min", str(e.exception))
        self.assertIn("duckduckgo: timeout", str(e.exception))
        status = self.g.status()
        self.assertTrue(status["paused"])
        self.assertEqual(status["paused_for_s"], G.PAUSE_S - 60)
        # A refused search is not remembered as "no results".
        self.assertIsNone(self.g.cached_results("a"))

    def test_a_pause_doubles_while_they_keep_refusing_and_ends_when_they_answer(self):
        for q in ("a", "b"):
            with self.assertRaises(G.Refused):
                self.search(q, results=[], unresponsive=BLOCKED)
        self.clock.t += G.PAUSE_S + 1
        self.assertFalse(self.g.paused)
        with self.assertRaises(G.Refused) as e:  # the probe after the pause is refused too
            self.search("probe", results=[], unresponsive=BLOCKED)
        self.assertEqual(e.exception.retry_after, 2 * G.PAUSE_S)
        self.clock.t += 2 * G.PAUSE_S + 1
        self.assertTrue(self.search("probe"))  # the engines answer again
        self.assertEqual(self.g.status()["reason"], "")
        with self.assertRaises(G.Refused) as e:  # and a later refusal starts from one blip again
            self.search("later", results=[], unresponsive=BLOCKED)
        self.assertFalse(self.g.paused)

    def test_a_search_that_never_got_an_answer_costs_no_place(self):
        for i in range(G.BURST):
            self.g.admit()
            self.g.failed()
        self.g.admit()  # still room

    def test_refusals_reads_searxngs_list(self):
        self.assertEqual(G.refusals([["brave", "too many requests"], ["x"], "y", []]), ["brave: too many requests", "x", "y"])
        self.assertEqual(G.refusals(None), [])


if __name__ == "__main__":
    unittest.main()
