#!/usr/bin/env python3
"""Edit tasks on this repository, checked automatically, for comparing the
chat engines (`oppx --engine agent` and `--engine aider`) on the same model.

Each task runs in a fresh clone at a fixed commit, through `oppx -p` (print
mode: one request, then exit), and passes when its check command succeeds.
It reports pass/fail, time, and the prompt tokens the server computed and
served from cache (from SGLang's log; run it on the server node).

    scripts/eval_edits.py --oppx target/debug/oppx --engine agent [--commit 9e8840e] [--only 1,3]

`oppx` must be paired (OPPX_CONFIG works). Needs cargo for the Rust checks.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARGO = f"PATH={os.path.expanduser('~/.cargo/bin')}:$PATH CARGO_TARGET_DIR={ROOT}/target/eval"

TASKS = [
    ("In client/oppx/src/ui.rs, add a unit test that `dim(\"x\")` returns plain \"x\" when colors are disabled.",
     f"{CARGO} cargo test -q -p oppx ui:: 2>&1 | grep -q 'test result: ok' && grep -q 'dim(\"x\")' client/oppx/src/ui.rs"),
    ("In scripts/bump_version.py, make a [hotfix] PR title prefix bump the patch version, like [fix].",
     "python3 -c \"import importlib.util as u;s=u.spec_from_file_location('b','scripts/bump_version.py');b=u.module_from_spec(s);"
     "s.loader.exec_module(b);assert b.parse_title('[hotfix] x')[0]=='patch';assert b.parse_title('[fix] x')[0]=='patch'\""),
    ("In crates/openphalanx-core/src/vram.rs, add a unit test checking that runtime_overhead(0) equals RUNTIME_BASE.",
     f"{CARGO} cargo test -q -p openphalanx-core vram:: 2>&1 | grep -q 'test result: ok' && grep -q 'runtime_overhead(0)' crates/openphalanx-core/src/vram.rs"),
    ("Rename the function `short_fingerprint` in crates/openphalanx-core/src/cluster.rs to `fingerprint_prefix`, and update every caller.",
     f"! git grep -q short_fingerprint && git grep -q 'fn fingerprint_prefix' && {CARGO} cargo check -q --workspace"),
    ("In docker/server/gateway.py, change the default per-device rate limit from 120 to 180 requests a minute.",
     "grep -Eq 'RATE_PER_MINUTE.*180' docker/server/gateway.py && ! grep -Eq 'RATE_PER_MINUTE.*\"120\"' docker/server/gateway.py && python3 -m py_compile docker/server/gateway.py"),
    ("Add a --version flag to scripts/probe_routing.py that prints \"probe_routing 1.0\" and exits.",
     "python3 scripts/probe_routing.py --version 2>&1 | grep -q 'probe_routing 1.0'"),
]

PREFILL = re.compile(r"^\[(?P<ts>[^\]]+)\] Prefill batch.*?#new-token: (?P<new>\d+), #cached-token: (?P<cached>\d+)")


def tokens_since(since: str) -> tuple[int, int, int]:
    out = subprocess.run(["docker", "logs", "--since", since, "openphalanx-backend"], capture_output=True, text=True)
    new = cached = batches = 0
    for line in (out.stdout + out.stderr).splitlines():
        m = PREFILL.match(line)
        if m:
            new, cached, batches = new + int(m["new"]), cached + int(m["cached"]), batches + 1
    return new, cached, batches


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oppx", required=True)
    ap.add_argument("--engine", default="agent", choices=["agent", "aider"])
    ap.add_argument("--commit", default="9e8840e")
    ap.add_argument("--only", help="task numbers, e.g. 1,3")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    oppx = os.path.abspath(args.oppx)
    only = {int(x) for x in args.only.split(",")} if args.only else None

    print(f"engine: {args.engine}")
    print(f"{'#':>2} {'pass':>4} {'time':>6} {'batches':>7} {'computed':>9} {'cached':>8}  task")
    passed = 0
    for i, (task, check) in enumerate(TASKS, 1):
        if only and i not in only:
            continue
        with tempfile.TemporaryDirectory(prefix="oppx-eval-") as tmp:
            repo = os.path.join(tmp, "repo")
            subprocess.run(["git", "clone", "-q", "--local", ROOT, repo], check=True)
            subprocess.run(["git", "checkout", "-q", args.commit], cwd=repo, check=True)
            since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            t = time.time()
            env = dict(os.environ, OPPX_ENGINE=args.engine, NO_COLOR="1")
            try:
                run = subprocess.run([oppx, "--no-web", "-p", task], cwd=repo, env=env, capture_output=True,
                                     text=True, timeout=args.timeout)
                log = run.stdout + run.stderr
            except subprocess.TimeoutExpired:
                log = "[timed out]"
            secs = time.time() - t
            ok = subprocess.run(["bash", "-c", check], cwd=repo, capture_output=True).returncode == 0
            new, cached, batches = tokens_since(since)
            passed += ok
            print(f"{i:>2} {'✓' if ok else '✗':>4} {secs:>5.0f}s {batches:>7} {new:>9,} {cached:>8,}  {task[:70]}")
            if not ok:
                diff = subprocess.run(["git", "diff", "--stat"], cwd=repo, capture_output=True, text=True).stdout
                print("     changed: " + (diff.strip().replace("\n", "\n              ") or "nothing"))
                tail = "\n".join(log.strip().splitlines()[-6:])
                print("     output:  " + tail.replace("\n", "\n              "))
            sys.stdout.flush()
    total = len(only) if only else len(TASKS)
    print(f"\n{passed}/{total} passed")


if __name__ == "__main__":
    main()
