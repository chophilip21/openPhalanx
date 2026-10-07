#!/usr/bin/env python3
"""Runs a multi-turn session through the agent loop (oppx_chat/agent.py)
without the terminal UI, and reports per turn: steps, the actions taken,
prompt tokens computed and served from the prefix cache, the largest request,
and time. Compare with scripts/bench_session.py (the Aider path).

Run with the oppx engine's Python (it has Aider and httpx), in a throwaway
copy of a repo, since edit turns change files:

    OPENAI_API_BASE=… OPENAI_API_KEY=… ~/.local/share/oppx/engine/tools/aider-chat/bin/python \\
        scripts/bench_agent.py --repo /tmp/bench-repo [--turns turns.json] [--context 32768]
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client/oppx/frontend"))

from oppx_chat.agent import Agent, Hooks, Workspace  # noqa: E402

DEFAULT_TURNS = [
    "Explain what this repository does, in a short paragraph.",
    "How does device pairing work, and which files implement it?",
    "What does the banner function in client/oppx/src/ui.rs do?",
    "In client/oppx/src/ui.rs, extend the doc comment of the `line` function to say it is indented by two spaces.",
    "What does client/oppx/src/tls.rs do?",
    "In client/oppx/src/ui.rs, add a unit test that `dim(\"x\")` returns plain \"x\" when colors are disabled.",
    "Summarize the changes we made so far in two sentences.",
    "How does docker/server/gateway.py enforce the per-device rate limit?",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--turns")
    ap.add_argument("--context", type=int, default=32768)
    ap.add_argument("--show-answers", action="store_true")
    args = ap.parse_args()
    turns = json.load(open(args.turns)) if args.turns else DEFAULT_TURNS

    steps: list[str] = []
    edits: list[str] = []
    hooks = Hooks(step=lambda t, d: steps.append(f"{t} → {d}"),
                  edited=lambda p, b, a: edits.append(p),
                  confirm_run=lambda c: True)
    try:
        from aider.io import InputOutput
        from aider.repomap import RepoMap
        repo_map = RepoMap(root=args.repo, io=InputOutput(pretty=False, yes=True))
    except Exception:  # noqa: BLE001
        repo_map = None
    agent = Agent(ws=Workspace(args.repo, repo_map), hooks=hooks, context=args.context, web=False)

    print(f"{'turn':>4} {'steps':>5} {'computed':>9} {'cached':>8} {'hit':>4} {'max req':>8} {'time':>6}  message")
    tot_c = tot_k = 0
    for i, text in enumerate(turns, 1):
        steps.clear()
        edits.clear()
        c0, k0 = agent.computed_tokens, agent.cached_tokens
        biggest = 0
        orig = agent._count

        def count(usage, orig=orig):
            nonlocal biggest
            biggest = max(biggest, usage.get("prompt_tokens") or 0)
            orig(usage)

        agent._count = count
        t = time.time()
        try:
            reply = agent.turn(text)
        except Exception as e:  # noqa: BLE001
            reply = f"[failed: {e}]"
        agent._count = orig
        c, k = agent.computed_tokens - c0, agent.cached_tokens - k0
        tot_c, tot_k = tot_c + c, tot_k + k
        hit = 100 * k / (c + k) if c + k else 0
        print(f"{i:>4} {len(steps):>5} {c:>9,} {k:>8,} {hit:>3.0f}% {biggest:>8,} {time.time() - t:>5.1f}s  {text[:60]}")
        for s in steps:
            print(f"        {s[:150]}")
        if edits:
            print(f"        edited: {', '.join(edits)}")
        if args.show_answers:
            print("        ── " + reply.strip().replace("\n", "\n           ")[:1500])
    hit = 100 * tot_k / (tot_c + tot_k) if tot_c + tot_k else 0
    print(f"\nall turns: {tot_c:,} computed, {tot_k:,} cached, hit rate {hit:.0f}%")


if __name__ == "__main__":
    main()
