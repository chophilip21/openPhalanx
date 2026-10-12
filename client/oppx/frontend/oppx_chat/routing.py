"""Decides whether a message asks for a change or only for information (a one-word model decision)."""

import os
import re

import httpx

from .config import MODEL_LABEL, REASONING, TRACE


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

A message may be a reply to what the assistant just said. Then it comes with the end of the assistant's last reply, and with a change the user asked for earlier that has not been made yet, when there is one. Read the message in that light:
- A go-ahead, a confirmation or "continue" (yes, ok, sure, go ahead, do it, please do, continue, in any language) is "edit" when the assistant offered, proposed, planned or announced a change, or when a requested change has not been made yet. Without either, it is "ask".
- When a requested change has not been made yet, whatever the reason the assistant gave (it got stuck, ran out of steps, could not find something), telling it to carry on ("continue", "keep going", "try again", "go on") is "edit", and so is pointing out that the change is missing ("where is the edit?", "nothing changed", "you didn't do it"): the user still wants it.
- An answer to a question the assistant needed for the change ("use created_at", "the second one") is "edit".
- A refusal or a stop (no, not now, never mind, wait) is "ask". So is thanks, and a question about something else.

Examples:
Assistant's last reply: ...I can add a sort dropdown to the header. Want me to?
Message: yes, go ahead. -> edit
Assistant's last reply: ...I've confirmed the field is created_at. Now I'll implement the sort feature.
Message: ok -> edit
Change asked for earlier, not made yet: add a sort dropdown
Assistant's last reply: ...I got stuck in a loop and could not confirm the field name.
Message: where is the edit? I don't see any changes to the code. -> edit
Change asked for earlier, not made yet: rename x to count
Assistant's last reply: ...I ran out of steps before I found every place that uses x.
Message: keep going -> edit
Assistant's last reply: ...Should I rename it everywhere, or only in main.rs?
Message: only in main.rs -> edit
Assistant's last reply: ...I can add a sort dropdown to the header. Want me to?
Message: no, leave it -> ask
Assistant's last reply: ...I can refactor this if you like.
Message: what would that break? -> ask
Assistant's last reply: ...The parse function returns a list of tokens.
Message: ok -> ask
Assistant's last reply: ...The parse function returns a list of tokens.
Message: thanks -> ask
Assistant's last reply: ...Done: the limit is now 4 in src/calc.py.
Message: yes -> ask

Reply with exactly one word: ask or edit."""

# Does a reply say the assistant is about to act, now, without waiting for the user?
ANNOUNCE_PROMPT = """You are given the end of a coding assistant's reply to a user. Nothing was changed in the code while that reply was written.

Answer "yes" when the reply says the assistant is now going to make a change itself, or is starting to: "I'll implement it now", "Let me make the changes", "Now I'll add the dropdown", "I'm ready to implement this. Let me make the changes now."
Answer "no" when it only answers or explains, when it offers or asks first ("Want me to add it?", "I can fix that if you like", "Should I proceed?"), when it says what the user could do, when it asks the user for something it needs, or when it says a change could not be made.

Examples:
Reply: ...I've confirmed the date field is created_at. Now I'll implement the sort feature. -> yes
Reply: ...I'll add a sortBy state and a dropdown in the header. Let me make the changes now. -> yes
Reply: ...The comparator is wrong. Let me fix that. -> yes
Reply: ...The comparator is wrong. Do you want me to fix it? -> no
Reply: ...You can fix it by changing the comparator on line 12. -> no
Reply: ...The parse function returns a list of tokens. -> no
Reply: ...Nothing was changed: I could not find the file. Tell me where it is and I will add it. -> no
Reply: ...I'll need the API key before I can add this. -> no

Reply with exactly one word: yes or no."""

CONTEXT_CHARS = 600  # of the assistant's last reply: its end is where an offer or an announcement is


def ask_choice(system: str, text: str, choices: tuple[str, ...], trace: dict | None = None) -> str | None:
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
    if trace and TRACE:
        body["oppx_trace"] = trace  # the server's Trace page: this call belongs to that turn
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


def intent_input(text: str, last_reply: str = "", pending: str = "") -> str:
    """What the intent check reads: the message, and what it may be an answer
    to. On its own, "yes, go ahead" is a question; after "Want me to add it?"
    it is the request."""
    lines = []
    if pending.strip():
        lines.append("Change asked for earlier, not made yet: " + " ".join(pending.split())[:300])
    if last_reply.strip():
        tail = " ".join(last_reply.split())
        lines.append("Assistant's last reply: " + ("..." + tail[-CONTEXT_CHARS:] if len(tail) > CONTEXT_CHARS else tail))
    return "\n".join([*lines, "Message: " + text[-2000:]]) if lines else text


def wants_edit(text: str, trace: dict | None = None, last_reply: str = "", pending: str = "") -> bool:
    """Does this message ask for a change, or only for information? It is
    read with the assistant's last reply and any change still owed, since a
    follow-up ("yes", "continue", "where is the edit?") means nothing alone.
    Falls back to "edit" when the model gives no answer."""
    return ask_choice(INTENT_PROMPT, intent_input(text, last_reply, pending), ("ask", "edit"), trace) != "ask"


def announces_change(reply: str, trace: dict | None = None) -> bool:
    """Does this reply say the assistant is about to make a change now (and
    then stop without making it)? False when the model gives no answer."""
    tail = " ".join(reply.split())
    return ask_choice(ANNOUNCE_PROMPT, "Reply: ..." + tail[-CONTEXT_CHARS:], ("yes", "no"), trace) == "yes"
