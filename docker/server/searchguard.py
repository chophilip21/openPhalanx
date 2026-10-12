"""Keeps the gateway from wearing out the search engines behind SearXNG.

Public engines suspend an address that asks too often, for minutes to hours,
and SearXNG then answers with an empty result list: indistinguishable, for a
client, from "nothing found". An agent that rewords an empty search (41 times
in one turn, once) keeps the engines blocked for everyone on the server.

So every search asks this guard first, and tells it how the search went:

* **Cache:** the same query again within `CACHE_S` is answered from memory.
* **Pace:** searches leave at a bounded rate (a burst of `BURST`, then one
  every `REFILL_S` seconds, for the whole server). Over it, the search is
  refused with the wait, not queued.
* **Pause:** when engines refuse (every engine unresponsive and no results)
  `TRIP_AFTER` times in a row, searching stops for `PAUSE_S`, doubling up to
  `PAUSE_MAX_S` while they keep refusing. During a pause nothing is sent; the
  first search after it is the probe.

Nothing is logged or written to disk: the cache holds queries and results in
memory only, like the request trace. No dependencies, so it is unit-tested
without the gateway (scripts/test_searchguard.py).
"""

import os
import time
from collections import OrderedDict

CACHE_S = float(os.environ.get("SEARCH_CACHE_S", "900"))
CACHE_SIZE = int(os.environ.get("SEARCH_CACHE_SIZE", "256"))
BURST = int(os.environ.get("SEARCH_BURST", "6"))
REFILL_S = float(os.environ.get("SEARCH_REFILL_S", "20"))
TRIP_AFTER = int(os.environ.get("SEARCH_TRIP_AFTER", "2"))
PAUSE_S = float(os.environ.get("SEARCH_PAUSE_S", "300"))
PAUSE_MAX_S = float(os.environ.get("SEARCH_PAUSE_MAX_S", "3600"))


class Refused(Exception):
    """This search is not sent. `retry_after` is in seconds."""

    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = max(1, round(retry_after))


def _key(query: str) -> str:
    return " ".join(query.lower().split())


def _wait(seconds: float) -> str:
    seconds = max(1, round(seconds))
    return f"{seconds} s" if seconds < 90 else f"{round(seconds / 60)} min"


def refusals(unresponsive) -> list[str]:
    """SearXNG's `unresponsive_engines` ([[engine, reason], …]) as "engine: reason"."""
    out = []
    for item in unresponsive or []:
        if isinstance(item, (list, tuple)) and item:
            out.append(f"{item[0]}: {item[1]}" if len(item) > 1 and item[1] else str(item[0]))
        elif item:
            out.append(str(item))
    return out


class Guard:
    def __init__(self, now=time.monotonic, wall=time.time):
        self._now, self._wall = now, wall
        self._cache: OrderedDict[str, tuple[float, list]] = OrderedDict()
        self._tokens = float(BURST)
        self._filled = now()
        self._failures = 0  # refusals in a row
        self._trips = 0  # pauses in a row (each doubles the next)
        self._paused_until = 0.0
        self._paused_wall = 0.0
        self.reason = ""  # why searching is paused, or was refused last
        self.sent = self.cached = self.paced = self.refused = 0

    @property
    def paused(self) -> bool:
        return self._now() < self._paused_until

    # ---- before a search ----------------------------------------------------
    def cached_results(self, query: str) -> list | None:
        """The results of the same query a short while ago, if any."""
        hit = self._cache.get(_key(query))
        if hit is None:
            return None
        if self._now() - hit[0] > CACHE_S:
            del self._cache[_key(query)]
            return None
        self._cache.move_to_end(_key(query))
        self.cached += 1
        return hit[1]

    def admit(self) -> None:
        """Raises Refused when this search must not go out now: searching is
        paused, or searches are leaving too fast."""
        now = self._now()
        if now < self._paused_until:
            self.refused += 1
            raise Refused(f"web search is paused for {_wait(self._paused_until - now)}: the search engines are refusing "
                          f"requests ({self.reason})", self._paused_until - now)
        self._tokens = min(float(BURST), self._tokens + (now - self._filled) / REFILL_S)
        self._filled = now
        if self._tokens < 1:
            wait = (1 - self._tokens) * REFILL_S
            self.paced += 1
            raise Refused(f"too many web searches at once; the next one can go in {_wait(wait)} (searches are paced so "
                          "the search engines don't block this server)", wait)
        self._tokens -= 1
        self.sent += 1

    # ---- after a search -----------------------------------------------------
    def record(self, query: str, results: list, unresponsive=None) -> None:
        """How an admitted search went. Raises Refused when it failed because
        the engines refused (no results and engines unresponsive), so the
        caller reports that instead of "no results"."""
        failed = refusals(unresponsive)
        if results or not failed:
            # An answer, even an empty one from engines that did respond.
            self._failures = self._trips = 0
            self.reason = ""
            self._cache[_key(query)] = (self._now(), results)
            self._cache.move_to_end(_key(query))
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            return
        self.reason = "; ".join(failed)[:300]
        self._failures += 1
        if self._failures >= TRIP_AFTER:
            pause = min(PAUSE_MAX_S, PAUSE_S * (2 ** self._trips))
            self._trips += 1
            self._failures = TRIP_AFTER - 1  # after the pause, one more refusal pauses again
            self._paused_until = self._now() + pause
            self._paused_wall = self._wall() + pause
            raise Refused(f"the search engines are refusing requests ({self.reason}); web search is paused for "
                          f"{_wait(pause)}", pause)
        raise Refused(f"the search engines are refusing requests ({self.reason})", REFILL_S)

    def failed(self) -> None:
        """An admitted search that never got an answer (SearXNG down, timeout):
        give its place back, it cost the engines nothing we know of."""
        self._tokens = min(float(BURST), self._tokens + 1)

    # ---- for the admin API --------------------------------------------------
    def status(self) -> dict:
        left = self._paused_until - self._now()
        return {
            "paused": left > 0,
            "paused_for_s": max(0, round(left)),
            "paused_until": round(self._paused_wall) if left > 0 else None,
            "reason": self.reason,
            "sent": self.sent,
            "from_cache": self.cached,
            "paced": self.paced,
            "refused_while_paused": self.refused,
        }
