#!/usr/bin/env python3
"""Scores the model-dependent classifiers against whatever model is loaded.

OpenPhalanx asks the served model two one-token questions, each constrained
by a regex so any model can answer them:

* the gateway's web-search router (DECIDE_PROMPT in docker/server/gateway.py):
  does this message need a web search? (yes|no)
* the chat frontend's intent check (INTENT_PROMPT in
  client/oppx/frontend/oppx_chat/routing.py): does this message ask for a change? (ask|edit)

The prompts are read from those files, so this always tests what ships. Run
it whenever the model changes; a model that scores badly needs a prompt fix
that is checked against every model, never a parser for that one model.

Usage, through a running `oppx proxy --no-web` (prints the base and key):

    OPENAI_API_BASE=http://127.0.0.1:PORT/v1 OPENAI_API_KEY=KEY scripts/probe_routing.py

Any OpenAI-compatible endpoint that accepts SGLang's `regex` works. Exits
non-zero when a probe set scores below --min (default 0.9).
"""

import argparse
import ast
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SEARCH_CASES = [
    ("What's the latest stable version of Rust?", "yes"),
    ("Is there a newer release of axum than 0.7? What changed?", "yes"),
    ("What does the error 'E0502 cannot borrow as mutable' mean in the current Rust docs?", "yes"),
    ("Look up how to configure SGLang's --mem-fraction-static", "yes"),
    ("Search the web for the FastAPI BackgroundTask docs", "yes"),
    ("Has CVE-2024-3094 in xz been fixed in Ubuntu 24.04?", "yes"),
    ("What are today's top Hacker News stories about Rust?", "yes"),
    ("Which Python versions does aider-chat 0.86 support?", "yes"),
    ("Rename the function parse_args to parse_cli in main.rs", "no"),
    ("Add a unit test for name_for", "no"),
    ("Why does this function return None when the list is empty?", "no"),
    ("Explain what the proxy module does", "no"),
    ("Fix the typo in README.md", "no"),
    ("Refactor ContextManager.fit to be shorter", "no"),
    ("What does `git rebase --onto` do?", "no"),
    ("Write a function that reverses a linked list", "no"),
]

INTENT_CASES = [
    ("What does the mul function do?", "ask"),
    ("Explain how pairing works in this repo", "ask"),
    ("Why is the test failing?", "ask"),
    ("Where is the TLS fingerprint checked?", "ask"),
    ("Is there any dead code in proxy.rs?", "ask"),
    ("How would you approach adding rate limits? Don't change anything yet.", "ask"),
    ("Can you summarize README.md?", "ask"),
    ("Add a docstring to mul", "edit"),
    ("Fix the failing test", "edit"),
    ("Rename `foo` to `bar` everywhere", "edit"),
    ("Can you add error handling to the download function?", "edit"),
    ("Delete the unused imports in main.rs", "edit"),
    ("Make the banner blue", "edit"),
    ("Write a short essay about the history of computing", "ask"),
    ("Tell me a joke about programmers", "ask"),
    ("The spinner flickers, please fix it", "edit"),
]


def constant(path: Path, name: str) -> str:
    """Reads a module-level string constant without importing the module."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"{name} not found in {path}")


def ask(base: str, key: str, model: str, system: str, text: str, regex: str) -> str:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
        "regex": regex,
        "max_tokens": 2,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def run_set(title, system, regex, cases, args) -> float:
    print(f"\n{title}")
    ok, times = 0, []
    for text, want in cases:
        t = time.perf_counter()
        try:
            got = ask(args.base, args.key, args.model, system, text, regex)
        except Exception as e:  # noqa: BLE001 - report and keep going
            got = f"error: {e}"
        times.append((time.perf_counter() - t) * 1000)
        ok += got == want
        mark = "✓" if got == want else "✗"
        print(f"  {mark} {want:>4} {got:>4}  {text}")
    times.sort()
    score = ok / len(cases)
    print(f"  {ok}/{len(cases)} correct · median {times[len(times) // 2]:.0f} ms")
    return score


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base", default=os.environ.get("OPENAI_API_BASE"), help="OpenAI-compatible base URL (…/v1)")
    p.add_argument("--key", default=os.environ.get("OPENAI_API_KEY", ""))
    p.add_argument("--model", default="openphalanx-coder")
    p.add_argument("--min", type=float, default=0.9, help="minimum score per probe set")
    args = p.parse_args()
    if not args.base:
        p.error("set OPENAI_API_BASE or pass --base")

    decide = constant(ROOT / "docker/server/gateway.py", "DECIDE_PROMPT")
    intent = constant(ROOT / "client/oppx/frontend/oppx_chat/routing.py", "INTENT_PROMPT")
    scores = [
        run_set("Web-search router (yes|no)", decide, "(yes|no)", SEARCH_CASES, args),
        run_set("Intent check (ask|edit)", intent, "(ask|edit)", INTENT_CASES, args),
    ]
    sys.exit(0 if min(scores) >= args.min else 1)


if __name__ == "__main__":
    main()
