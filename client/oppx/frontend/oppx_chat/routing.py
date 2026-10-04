"""Decides whether a message asks for a change or only for information (one constrained token)."""

import os

import httpx

from .config import MODEL_LABEL


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


def wants_edit(text: str) -> bool:
    """One constrained token from the server's model (~40 ms): does this
    message ask for a change, or only for information? Questions then run
    without edits, as in Claude Code. Falls back to "edit" on any error."""
    base, key = os.environ.get("OPENAI_API_BASE"), os.environ.get("OPENAI_API_KEY")
    if not base or not key:
        return True
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": MODEL_LABEL,
                "messages": [{"role": "system", "content": INTENT_PROMPT}, {"role": "user", "content": text[-2000:]}],
                "regex": "(ask|edit)",
                "max_tokens": 2,
                "temperature": 0,
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip() != "ask"
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return True
