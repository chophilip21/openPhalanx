#!/usr/bin/env python3
"""Multi-turn prefix-cache benchmark (milestone Step 5.2).

Drives a real `oppx` chat session in a pseudo-terminal through a fixed mix of
questions and edits, and reads SGLang's own log for each turn: how many prompt
tokens were computed versus served from the RadixAttention prefix cache.

Run on the server node (it reads `docker logs`), in a throwaway copy of a repo,
because the edit turns change files:

    cp -r . /tmp/bench-repo && cd /tmp/bench-repo
    scripts/bench_session.py --oppx target/debug/oppx [--turns turns.json]

`oppx` must already be paired (OPPX_CONFIG works). Web search is off so the
numbers measure the model, not the internet.

Per turn it reports: requests (prefill batches, including the one-token ask/edit
check), prompt tokens computed and cached, the cache hit rate, the time until
the answer starts on screen, and the turn's total time.
"""

import argparse
import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
from datetime import datetime, timezone

# Questions and edits on this repository; each turn ends at the idle prompt.
DEFAULT_TURNS = [
    "Explain what this repository does, in a short paragraph.",
    "How does device pairing work, and which files implement it?",
    "/add client/oppx/src/ui.rs",
    "What does the banner function in ui.rs do?",
    "In client/oppx/src/ui.rs, extend the doc comment of the `line` function to say it is indented by two spaces.",
    "What does client/oppx/src/tls.rs do?",
    "In client/oppx/src/ui.rs, add a unit test that `dim(\"x\")` returns plain \"x\" when colors are disabled.",
    "Summarize the changes we made so far in two sentences.",
    "/add docker/server/gateway.py",
    "How does gateway.py enforce the per-device rate limit?",
]

PREFILL = re.compile(r"^\[(?P<ts>[^\]]+)\] Prefill batch.*?#new-token: (?P<new>\d+), #cached-token: (?P<cached>\d+)")
IDLE = "for shortcuts"  # the prompt's hint line, drawn whenever oppx waits for input


def clean(b: bytes) -> str:
    t = b.decode("utf-8", "replace")
    t = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", t)
    t = re.sub(r"\x1b[\]P][^\x07\x1b]*(\x07|\x1b\\)", "", t)
    return t.replace("\r", "")


class Pty:
    def __init__(self, argv):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvp(argv[0], argv)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.buf = b""

    def send(self, text: str):
        os.write(self.fd, text.encode())

    def wait(self, pattern: str, timeout: float, since: int) -> float | None:
        """Seconds until `pattern` shows up after byte offset `since`, or None."""
        start = time.time()
        while time.time() - start < timeout:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    return None
                self.buf += chunk
                # Answer cursor-position requests like a terminal, or prompt_toolkit stalls.
                for _ in range(chunk.count(b"\x1b[6n")):
                    os.write(self.fd, b"\x1b[30;1R")
            if re.search(pattern, clean(self.buf[since:])):
                return time.time() - start
        return None


def prefills(container: str, since: datetime, until: datetime):
    out = subprocess.run(
        ["docker", "logs", "--since", since.isoformat(), "--until", until.isoformat(), container],
        capture_output=True, text=True,
    )
    rows = []
    for line in (out.stdout + out.stderr).splitlines():
        if m := PREFILL.match(line):
            rows.append((int(m["new"]), int(m["cached"])))
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--oppx", default="oppx")
    p.add_argument("--turns", help="JSON list of messages (default: built-in mix for this repo)")
    p.add_argument("--container", default="openphalanx-backend")
    p.add_argument("--timeout", type=float, default=600, help="per turn, seconds")
    p.add_argument("--json", help="also write the per-turn results here")
    args = p.parse_args()
    turns = json.load(open(args.turns)) if args.turns else DEFAULT_TURNS

    session = Pty([args.oppx, "--no-web"])
    if session.wait(IDLE, 180, 0) is None:
        sys.exit("oppx did not reach its prompt:\n" + clean(session.buf)[-2000:])
    time.sleep(1)

    results = []
    print(f"{'turn':>4}  {"batches":>7} {'computed':>9} {'cached':>8} {'hit':>5} {'first':>7} {'total':>7}  message")
    for i, text in enumerate(turns, 1):
        mark = len(session.buf)
        t0 = datetime.now(timezone.utc)
        session.send(text + "\r")
        started = time.time()
        first = None if text.startswith("/") else session.wait("⏺", args.timeout, mark)
        done = session.wait(IDLE, args.timeout, mark)
        total = None if done is None else time.time() - started
        time.sleep(1.5)  # let SGLang flush its log lines
        rows = prefills(args.container, t0, datetime.now(timezone.utc))
        new, cached = sum(r[0] for r in rows), sum(r[1] for r in rows)
        hit = cached / (new + cached) if new + cached else 0.0
        res = {"turn": i, "message": text, "requests": len(rows), "computed": new, "cached": cached,
               "hit": round(hit, 3), "first_s": first and round(first, 1), "total_s": total and round(total, 1)}
        results.append(res)
        fmt = lambda s: "   -   " if s is None else f"{s:6.1f}s"
        print(f"{i:>4}  {len(rows):>7} {new:>9,} {cached:>8,} {hit:>5.0%} {fmt(first)} {fmt(total)}  {text[:50]}")
        if total is None:
            print("      turn did not finish; stopping")
            break

    session.send("\x03")
    time.sleep(0.3)
    session.send("\x03")
    new = sum(r["computed"] for r in results)
    cached = sum(r["cached"] for r in results)
    print(f"\nall turns: {new:,} computed, {cached:,} cached, hit rate {cached / max(1, new + cached):.0%}")
    if args.json:
        json.dump(results, open(args.json, "w"), indent=1)
    with open(os.environ.get("BENCH_SCREEN", os.devnull), "wb") as f:
        f.write(session.buf)


if __name__ == "__main__":
    main()
