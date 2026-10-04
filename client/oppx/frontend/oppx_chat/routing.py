"""Decides whether a message asks for a change or only for information (a one-word model decision)."""

import os
import re

import httpx

from .config import MODEL_LABEL, REASONING


INTENT_PROMPT = """Classify the programmer's message to a coding assistant.

Answer "edit" when the message asks to change the code or files in any way: add, write, create, implement, fix, refactor, rename, remove, delete, update, optimize, or "make it ..." — including polite questions such as "can you add tests?".
Answer "ask" when the message only wants information or text in the reply: a question, an explanation, a review, an opinion, a plan, an essay, a joke, a translation, or a general-knowledge request, without asking for any file or code to be changed.

Examples:
Message: What does the parse function return? -> ask
Message: Explain how the cache works in this repo. -> ask
Message: Why does this test fail? -> ask
Message: Is this function thread safe? -> ask
Message: Review my changes. -> ask
Message: Write a short essay about the history of computing. -> ask
Message: Tell me a joke. -> ask
Message: Add a function sub(a, b) to calc.py. -> edit
Message: Can you add unit tests for config.rs? -> edit
Message: Fix the failing test. -> edit
Message: Why does this test fail? Please fix it. -> edit
Message: Rename x to count. -> edit
Message: Make the parser faster. -> edit

Reply with exactly one word: ask or edit."""


def ask_choice(system: str, text: str, choices: tuple[str, ...]) -> str | None:
    """A one-word decision from the server's model, through the proxy.

    Non-reasoning models answer in one regex-constrained token (~40 ms).
    Reasoning models (gpt-oss, Qwen3 thinking…) can't: SGLang enforces the
    regex only after the reasoning ends, so a 2-token budget ends mid-thought
    with an empty answer. They think briefly at low effort and answer freely;
    the first allowed word counts (16/16 on gpt-oss-20b, ~260 ms)."""
    base, key = os.environ.get("OPENAI_API_BASE"), os.environ.get("OPENAI_API_KEY")
    if not base or not key:
        return None
    body = {
        "model": MODEL_LABEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": text[-2000:]}],
        "temperature": 0,
    }
    if REASONING:
        # oppx_utility: the gateway never routes or searches these (servers
        # that report `reasoning` understand the flag).
        body.update(max_tokens=400, oppx_utility=True,
                    chat_template_kwargs={"reasoning_effort": "low", "enable_thinking": False})
    else:
        body.update(regex="(" + "|".join(choices) + ")", max_tokens=2)
    try:
        r = httpx.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body, timeout=20)
        r.raise_for_status()
        answer = (r.json()["choices"][0]["message"]["content"] or "").lower()
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None
    m = re.search(r"\b(" + "|".join(choices) + r")\b", answer)
    return m.group(1) if m else None


def wants_edit(text: str) -> bool:
    """Does this message ask for a change, or only for information? Questions
    then run without edits, as in Claude Code. Falls back to "edit" when the
    model gives no answer."""
    return ask_choice(INTENT_PROMPT, text, ("ask", "edit")) != "ask"
