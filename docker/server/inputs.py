"""What the loaded model accepts besides text, and the check on each request.

SGLang knows whether the model it loaded has a vision or an audio encoder and
says so in /model_info. The gateway reads that once, as soon as SGLang is up,
and refuses a request carrying a kind of input the model can't take, before
it reaches SGLang. Video is refused for every model: not supported here.
No dependencies, so it is tested on its own (scripts/test_inputs.py).
"""

from __future__ import annotations

# OpenAI content part type -> the kind of input it carries.
PART_KINDS = {
    "text": "text",
    "image_url": "image",
    "input_image": "image",
    "input_audio": "audio",
    "audio_url": "audio",
    "video_url": "video",
}
NAMES = {"image": "pictures", "audio": "audio", "video": "video", "text": "text"}
NEVER = frozenset({"video"})  # refused whatever the model is


def accepted(model_info: dict) -> frozenset[str]:
    """The kinds of input a model takes, from SGLang's /model_info."""
    kinds = {"text"}
    if model_info.get("has_image_understanding"):
        kinds.add("image")
    if model_info.get("has_audio_understanding"):
        kinds.add("audio")
    return frozenset(kinds)


def kinds_in(messages: list) -> set[str]:
    """The kinds of input a request's messages carry (an unknown part type is
    its own kind, so it is refused rather than passed on)."""
    found = set()
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    t = str(part.get("type") or "text")
                    found.add(PART_KINDS.get(t, t))
    return found


def refusal(messages: list, accepts: frozenset[str] | None, model: str) -> str | None:
    """Why the request can't be served, or None. `accepts` None: not known yet
    (SGLang still loading); only what is never served is refused then."""
    carried = kinds_in(messages) - {"text"}
    never = carried & NEVER
    if never:
        return f"{_list(never).capitalize()} input isn't supported by this server. Send text, documents or pictures instead."
    if accepts is None:
        return None
    missing = carried - accepts
    if not missing:
        return None
    takes = _list(accepts)
    return (f"The loaded model ({model}) can't take {_list(missing)}: it takes {takes} only. "
            "Load a model that can (for pictures, one with a vision encoder), or send text.")


def _list(kinds) -> str:
    names = [NAMES.get(k, f"'{k}' parts") for k in sorted(kinds, key=lambda k: (k != "text", k))]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
