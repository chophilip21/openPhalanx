#!/usr/bin/env python3
"""Questions about this repository that name no file: can the agent find the
code on its own and answer from it? Each answer is checked for the fact and
the file it must mention. Also counts what must never happen: a reply that is
empty or raw JSON, a file changed for a question, a turn that got stuck.

Run with the oppx engine's Python against `oppx proxy --no-web`, in a scratch
clone (questions can't edit, but the clone keeps the commit fixed):

    OPENAI_API_BASE=… OPENAI_API_KEY=… ~/.local/share/oppx/engine/tools/aider-chat/bin/python \\
        scripts/eval_nav.py --repo /tmp/clone [--only 1,4] [--show]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client/oppx/frontend"))

from oppx_chat.agent import Agent, Hooks, Workspace, as_action  # noqa: E402

# (question, every regex the reply must match)
QUESTIONS = [
    ("What is the default context window, and where is that default set?",
     [r"32[_,.]?768|32\s?k", r"settings\.rs"]),
    ("How long is a pairing code valid, and how many wrong attempts burn it?",
     [r"10\s*min|600", r"\b5\b|five", r"gateway\.py"]),
    ("While a split model is serving, what happens when a member stops reporting, and after how long?",
     [r"12\s*s|twelve|12 seconds", r"cluster\.rs|lib\.rs|service\.rs", r"paus|stop"]),
    ("Where does the server decide that a downloaded model's files are broken, and what does it check?",
     [r"model\.rs", r"safetensors|size|header|marker"]),
    ("How does the chat client decide whether my message is a question or a request for a change?",
     [r"routing\.py", r"ask|edit"]),
    ("What rate limit applies to each paired device, and where is it enforced?",
     [r"120", r"gateway\.py"]),
    ("What stops the backend if the desktop app crashes, and after how many seconds?",
     [r"watchdog", r"\b60\b", r"gateway\.py|lib\.rs|docker\.rs"]),
    ("i am on qwen3.8.27b model at the moment. As far as I know, the context window is by default not 32k. "
     "Why is it fixed to 32k, not it's native context size?",
     [r"settings\.rs|context_len", r"default|slider"]),
    ("Which function shares a model's layers out between the servers of a cluster, and what is the share based on?",
     [r"split\.rs", r"free|VRAM|memory"]),
    ("Where is the web search query cleaned of dates the model invented?",
     [r"drop_invented_dates", r"gateway\.py"]),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--only")
    ap.add_argument("--context", type=int, default=32768)
    ap.add_argument("--show", action="store_true", help="print every reply")
    ap.add_argument("--json", help="write the results to this file")
    args = ap.parse_args()
    only = {int(x) for x in args.only.split(",")} if args.only else None
    try:
        from aider.io import InputOutput
        from aider.repomap import RepoMap
        repo_map = RepoMap(root=args.repo, io=InputOutput(pretty=False, yes=True))
    except Exception:  # noqa: BLE001
        repo_map = None
    rows = []
    print(f"{'#':>2} {'ok':>3} {'steps':>5} {'time':>5} {'peak':>6}  flags / question")
    for i, (question, wanted) in enumerate(QUESTIONS, 1):
        if only and i not in only:
            continue
        steps, notices, shown = [], [], []
        hooks = Hooks(step=lambda t, d: steps.append(t), notice=notices.append, confirm_run=lambda c: False,
                      stream=lambda text, final: shown.append(text) if final else None)
        agent = Agent(ws=Workspace(args.repo, repo_map), hooks=hooks, context=args.context, web=False,
                      reasoning=os.environ.get("OPPX_REASONING", "1") == "1")
        peak = [0]
        count = agent._count

        def counted(usage, count=count, agent=agent, peak=peak):
            count(usage)
            peak[0] = max(peak[0], agent.last_prompt_tokens)

        agent._count = counted
        t = time.time()
        try:
            # As the chat sends a question: edits possible, the message reads as a question.
            reply = agent.turn(question, can_edit=True, wants_change=False)
        except Exception as e:  # noqa: BLE001
            reply = f"(failed: {e})"
        took = time.time() - t
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=args.repo, capture_output=True, text=True).stdout.strip()
        if dirty:
            subprocess.run(["git", "checkout", "-q", "."], cwd=args.repo)
            subprocess.run(["git", "clean", "-fdq"], cwd=args.repo)
        missing = [w for w in wanted if not re.search(w, reply, re.I)]
        seen = shown[-1] if shown else ""
        flags = [f for f, on in (("STUCK", bool(agent.stuck)), ("EDITED", bool(dirty)),
                                 ("BAD-REPLY", not seen.strip() or as_action(seen) is not None)) if on]
        ok = not missing and "BAD-REPLY" not in flags and "EDITED" not in flags
        rows.append({"n": i, "ok": ok, "steps": len(steps), "time": round(took), "peak": peak[0], "flags": flags,
                     "missing": missing, "stuck": agent.stuck, "reply": reply, "actions": steps})
        print(f"{i:>2} {'✓' if ok else '✗':>3} {len(steps):>5} {took:>4.0f}s {peak[0]:>6}  "
              f"{' '.join(flags) + ' ' if flags else ''}{question[:70]}" + (f"   missing: {missing}" if missing else ""), flush=True)
        if args.show:
            print("   " + reply.strip().replace("\n", "\n   ")[:1500] + "\n")
    n = len(rows)
    if n:
        print(f"\n{sum(r['ok'] for r in rows)}/{n} answered · {sum(r['steps'] for r in rows) / n:.1f} steps and "
              f"{sum(r['time'] for r in rows) / n:.0f} s on average · stuck {sum('STUCK' in r['flags'] for r in rows)} · "
              f"bad replies {sum('BAD-REPLY' in r['flags'] for r in rows)} · edited {sum('EDITED' in r['flags'] for r in rows)}")
    if args.json:
        json.dump(rows, open(args.json, "w"), indent=1)


if __name__ == "__main__":
    main()
