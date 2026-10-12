#!/usr/bin/env python3
"""Scores the model-dependent classifiers against whatever model is loaded.

OpenPhalanx asks the served model a few one-token questions, each constrained
by a regex so any model can answer them:

* the gateway's web-search router (DECIDE_PROMPT in docker/server/gateway.py):
  does this message need a web search? (yes|no)
* the chat frontend's intent check (INTENT_PROMPT in
  client/oppx/frontend/oppx_chat/routing.py): does this message ask for a change? (ask|edit)
  The same check for a follow-up, read with the assistant's last reply and a
  change still owed ("yes, go ahead", "continue", "where is the edit?").
* the announcement check (ANNOUNCE_PROMPT, same file): does this reply say the
  assistant is about to make a change now? (yes|no)

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
import re
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


# (change asked for earlier and not made, the assistant's last reply, the message, expected)
FOLLOWUP_CASES = [
    ("", "I can add a sort dropdown next to the search box. Want me to go ahead?", "yes, go ahead.", "edit"),
    ("", "I've confirmed the date field is created_at. Now I'll implement the sort feature.", "yes, go ahead.", "edit"),
    ("add a way to sort the stories", "I got stuck in a loop and could not confirm the field name. If you tell me the "
     "field name I can implement it.", "where is the edit? I don't see any changes to the code.", "edit"),
    ("add a way to sort the stories", "I got stuck in a loop and could not confirm the field name.", "continue", "edit"),
    ("", "The fix is to guard against an empty list in parse(). Shall I apply it?", "sure", "edit"),
    ("", "Here is the plan: 1) add a sortBy state 2) add a dropdown 3) sort before rendering.", "looks good, do it", "edit"),
    ("", "I'd rename it to fetch_stories. Should I rename it everywhere or only in api.py?", "everywhere", "edit"),
    ("make the banner blue", "Nothing was changed: I could not find where the banner colour is set.", "it's in ui.rs", "edit"),
    ("", "I can add a retry around the request if you want.", "ok", "edit"),
    ("", "Voulez-vous que j'ajoute le test ?", "oui, vas-y", "edit"),
    ("", "I could split this function in two. Want that?", "please", "edit"),
    ("add input validation to the form", "I have read the form component. I'm ready to implement this.", "go", "edit"),
    ("", "I can add a sort dropdown next to the search box. Want me to go ahead?", "no, leave it as it is", "ask"),
    ("", "I can add a sort dropdown next to the search box. Want me to go ahead?", "not now", "ask"),
    ("", "I can refactor this module if you like.", "what would that break?", "ask"),
    ("", "The parse function returns a list of tokens, one per word.", "ok", "ask"),
    ("", "The parse function returns a list of tokens, one per word.", "thanks", "ask"),
    ("", "Done: the limit is now 4 in src/calc.py. I ran the tests and they pass.", "great, thanks", "ask"),
    ("", "The cache is filled in load() and read in get().", "and where is it cleared?", "ask"),
    ("", "I can add a sort dropdown next to the search box. Want me to go ahead?", "why a dropdown and not buttons?", "ask"),
    ("add a way to sort the stories", "I got stuck in a loop and could not confirm the field name.", "never mind, forget it", "ask"),
    ("", "Yes, the function is thread safe: it only reads.", "yes", "ask"),
    ("", "", "yes", "ask"),
    ("", "", "continue", "edit"),  # nothing before it: a resumed session, where it means the work
]

ANNOUNCE_CASES = [
    ("No, I was just stuck in a loop with a bad command. I've confirmed the date field is created_at (ISO string) and "
     "created_at_i (integer timestamp). Now I'll implement the sort feature.", "yes"),
    ("I'm ready to implement the sort feature. I'll add a sortBy state and a dropdown in the header to sort by date, "
     "points, or comments. Let me make the changes now.", "yes"),
    ("The bug is in the comparator: it compares strings. Let me fix that.", "yes"),
    ("I found the three places that use the old name. I will now rename them.", "yes"),
    ("Here are the changes I'm going to make: add the state, add the select, sort the list. Starting with App.jsx.", "yes"),
    ("Let me start by making the changes.", "yes"),
    ("The bug is in the comparator: it compares strings. Do you want me to fix it?", "no"),
    ("I can add a sort dropdown next to the search box if you like.", "no"),
    ("You can fix it by changing the comparator on line 12 to compare numbers.", "no"),
    ("The parse function returns a list of tokens, one per word.", "no"),
    ("Nothing was changed: I could not find where the banner colour is set. Tell me the file and I will change it.", "no"),
    ("I changed the limit to 4 in src/calc.py and ran the tests; they pass.", "no"),
    ("To add this I would need the API key. Could you provide it?", "no"),
    ("The cache is filled in load() and read in get(). It is never cleared.", "no"),
    ("Here is a plan: 1) add the state 2) add the dropdown 3) sort the list. Tell me if you want me to carry it out.", "no"),
    ("If you want it faster, the loop in parse() should be replaced by a dictionary lookup.", "no"),
]


def function(path: Path, name: str, **env):
    """A small pure function from a module, without importing the module (and its dependencies)."""
    source = path.read_text()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            scope = dict(env)
            exec(ast.get_source_segment(source, node), scope)  # noqa: S102 - our own source
            return scope[name]
    raise SystemExit(f"{name} not found in {path}")


def constant(path: Path, name: str) -> str:
    """Reads a module-level string constant without importing the module."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"{name} not found in {path}")


def ask(base: str, key: str, model: str, system: str, text: str, regex: str, reasoning: bool) -> str:
    """The same call the gateway and the chat frontend make: one constrained
    token, or for reasoning models a short free answer at low effort whose
    first allowed word counts (SGLang constrains only after the reasoning)."""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
        "temperature": 0,
    }
    if reasoning:
        body.update(max_tokens=400, chat_template_kwargs={"reasoning_effort": "low", "enable_thinking": False})
    else:
        body.update(regex=regex, max_tokens=2)
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        answer = (json.load(r)["choices"][0]["message"]["content"] or "").lower()
    m = re.search(r"\b(" + regex.strip("()") + r")\b", answer)
    return m.group(1) if m else answer.strip()[:12]


def run_set(title, system, regex, cases, args) -> float:
    print(f"\n{title}")
    ok, times = 0, []
    for text, want in cases:
        t = time.perf_counter()
        try:
            got = ask(args.base, args.key, args.model, system, text, regex, args.reasoning)
        except Exception as e:  # noqa: BLE001 - report and keep going
            got = f"error: {e}"
        times.append((time.perf_counter() - t) * 1000)
        ok += got == want
        mark = "✓" if got == want else "✗"
        print(f"  {mark} {want:>4} {got:>4}  " + text.replace("\n", " | ")[-150:])
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
    p.add_argument("--reasoning", action="store_true",
                   help="the model reasons before answering (catalog reasoning_parser; /v1/info says so)")
    args = p.parse_args()
    if not args.base:
        p.error("set OPENAI_API_BASE or pass --base")

    decide = constant(ROOT / "docker/server/gateway.py", "DECIDE_PROMPT")
    routing = ROOT / "client/oppx/frontend/oppx_chat/routing.py"
    intent, announce = constant(routing, "INTENT_PROMPT"), constant(routing, "ANNOUNCE_PROMPT")
    chars = constant(routing, "CONTEXT_CHARS")
    intent_input = function(routing, "intent_input", CONTEXT_CHARS=chars)
    followups = [(intent_input(message, reply, pending), want) for pending, reply, message, want in FOLLOWUP_CASES]
    scores = [
        run_set("Web-search router (yes|no)", decide, "(yes|no)", SEARCH_CASES, args),
        run_set("Intent check (ask|edit)", intent, "(ask|edit)", INTENT_CASES, args),
        run_set("Intent check, a follow-up read with the last reply (ask|edit)", intent, "(ask|edit)", followups, args),
        run_set("Announcement check (yes|no)", announce, "(yes|no)", [("Reply: ..." + r, w) for r, w in ANNOUNCE_CASES], args),
    ]
    sys.exit(0 if min(scores) >= args.min else 1)


if __name__ == "__main__":
    main()
