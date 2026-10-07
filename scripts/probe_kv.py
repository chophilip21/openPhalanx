#!/usr/bin/env python3
"""Quality check for a server setting that may cost accuracy (e.g. an FP8 KV
cache): long-context recall and small coding tasks, scored automatically.

Run it against the same model with and without the setting and compare:

    OPENAI_API_BASE=… OPENAI_API_KEY=… scripts/probe_kv.py [--sizes 8000,24000] [--model NAME]

(the base and key come from `oppx proxy --no-web`).

* Recall: a file of N tokens of small functions, each returning a random
  constant; the model is asked for one function's value, with the function
  near the start, the middle and the end. Exact answers count.
* Coding: short functions are written by the model, then run against tests.
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request

BASE = os.environ.get("OPENAI_API_BASE", "").rstrip("/")
KEY = os.environ.get("OPENAI_API_KEY", "")

CODING = [
    ("Write a Python function `is_prime(n: int) -> bool`.",
     "assert [x for x in range(30) if is_prime(x)] == [2,3,5,7,11,13,17,19,23,29]; assert not is_prime(1) and is_prime(7919)"),
    ("Write a Python function `merge_intervals(xs: list[tuple[int, int]]) -> list[tuple[int, int]]` that merges overlapping closed intervals and returns them sorted.",
     "assert merge_intervals([(1,3),(2,6),(8,10),(15,18)]) == [(1,6),(8,10),(15,18)]; assert merge_intervals([(1,4),(4,5)]) == [(1,5)]; assert merge_intervals([]) == []"),
    ("Write a Python function `roman(n: int) -> str` that converts 1..3999 to Roman numerals.",
     "assert roman(1994) == 'MCMXCIV' and roman(3999) == 'MMMCMXCIX' and roman(4) == 'IV'"),
    ("Write a Python function `camel_to_snake(s: str) -> str`, e.g. 'parseHTTPResponse' -> 'parse_http_response'.",
     "assert camel_to_snake('parseHTTPResponse') == 'parse_http_response' and camel_to_snake('simple') == 'simple' and camel_to_snake('getX') == 'get_x'"),
    ("Write a Python function `top_k(words: list[str], k: int) -> list[str]` returning the k most frequent words, ties broken alphabetically.",
     "assert top_k(['b','a','b','c','a','b'], 2) == ['b','a'] and top_k(['x','y'], 1) == ['x']"),
]


def chat(model: str, content: str, max_tokens: int = 1200) -> str:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "low"},
    }
    req = urllib.request.Request(f"{BASE}/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)["choices"][0]["message"]["content"] or ""


def recall(model: str, size: int, rng: random.Random) -> list[bool]:
    per_fn = 22  # tokens per function, roughly
    n = max(30, size // per_fn)
    values = [rng.randrange(10_000, 99_999) for _ in range(n)]
    src = "\n".join(f"def f{i}():\n    return {v}" for i, v in enumerate(values))
    out = []
    for where in (0.05, 0.5, 0.95):
        i = int(n * where)
        a = chat(model, f"{src}\n\nWhat does f{i}() return? Reply with the number only.", 40)
        out.append(str(values[i]) in a)
    return out


def coding(model: str) -> list[bool]:
    out = []
    for task, test in CODING:
        a = chat(model, task + " Reply with only the code in one ```python block.")
        m = re.search(r"```(?:python)?\n(.*?)```", a, re.S)
        code = m.group(1) if m else a
        try:
            ns: dict = {}
            exec(code, ns)  # noqa: S102 - the model's own answer, run in this throwaway process
            exec(test, ns)  # noqa: S102
            out.append(True)
        except Exception:  # noqa: BLE001
            out.append(False)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="8000,24000", help="recall prompt sizes in tokens")
    ap.add_argument("--model", default="openphalanx-coder")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    if not BASE:
        sys.exit("set OPENAI_API_BASE and OPENAI_API_KEY (from `oppx proxy --no-web`)")
    rng = random.Random(args.seed)
    t = time.time()
    total = right = 0
    for size in (int(s) for s in args.sizes.split(",")):
        r = recall(args.model, size, rng)
        print(f"recall {size:>7,} tokens: {''.join('✓' if x else '✗' for x in r)}  (start, middle, end)")
        total, right = total + len(r), right + sum(r)
    c = coding(args.model)
    print(f"coding: {''.join('✓' if x else '✗' for x in c)}")
    total, right = total + len(c), right + sum(c)
    print(f"score: {right}/{total} in {time.time() - t:.0f} s")


if __name__ == "__main__":
    main()
