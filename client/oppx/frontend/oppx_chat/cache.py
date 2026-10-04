"""Keeps requests prefix-cache friendly, so the server reuses work between turns."""

import aider.coders.base_coder as base_coder
from aider.coders.ask_coder import AskCoder
from aider.repomap import RepoMap


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Prefix-cache friendliness. SGLang reuses the longest prompt prefix it has
# seen, so anything that changes near the start of a request makes the server
# reprocess everything after it (measured: 3-7% cache hits over 10 turns,
# scripts/bench_session.py). Two things changed on every turn:
# ---------------------------------------------------------------------------

# 1. The repo map, which comes right after the system prompt. Aider re-ranks
#    it around the files in the chat and the names each message mentions, so
#    it changed on nearly every turn. We rank the whole repo once instead and
#    reuse that map until the repo's file list or the map size changes. Files
#    in the chat are sent in full later in the request anyway, and files the
#    user mentions are still added to the chat as usual.
_MAP_CACHE = {}
_orig_ranked_map = RepoMap.get_ranked_tags_map


def _stable_ranked_map(self, chat_fnames, other_fnames=None, max_map_tokens=None,
                       mentioned_fnames=None, mentioned_idents=None, force_refresh=False):
    files = sorted(set(chat_fnames or ()) | set(other_fnames or ()))
    key = (tuple(files), max_map_tokens)
    if key not in _MAP_CACHE:
        if len(_MAP_CACHE) > 16:
            _MAP_CACHE.clear()
        _MAP_CACHE[key] = _orig_ranked_map(self, [], files, max_map_tokens, None, None, True)
    return _MAP_CACHE[key]


def _stable_map_heading(coder):
    """The map's heading says "other files" once files are in the chat; keep
    it constant (and the same for questions and edits)."""
    if getattr(coder, "repo_map", None) is not None:
        coder.repo_map.repo_content_prefix = (coder.gpt_prompts.repo_content_prefix or "").replace("{other}", "")


# 2. The system prompt, which differed between questions (Aider's ask mode)
#    and edits. Questions now use the edit prompts too, plus a note in the
#    message; ask mode never applies edits, so a stray edit block is harmless.
ASK_NOTE = "(This is not a change request: just reply in plain text, as in a normal chat. Don't use SEARCH/REPLACE blocks; no files will be changed.)"


# Models that rank the system prompt strictly above the user (gpt-oss) obey
# the edit prompts' "ONLY EVER RETURN CODE IN A SEARCH/REPLACE BLOCK" over
# ASK_NOTE, and answered a question with an edit block instead of prose. The
# exception therefore lives in the system prompt itself. It is identical for
# questions and edits, so the shared prefix (and the cache) is unchanged.
QUESTION_RULE = (
    "\n\nException: when the user's latest message ends with a note saying it is not a change "
    "request, answer it in plain prose as in a normal chat. Don't write SEARCH/REPLACE blocks or "
    "file listings for it; nothing will be applied. Every other message follows the rules above."
)


def _with_question_rule(prompts):
    """The coder's prompts with QUESTION_RULE appended to the system prompt
    and to the final reminder (which repeats the edit-block rule)."""
    cls = type(prompts)
    if getattr(cls, "_oppx_question_rule", False):
        return prompts
    sub = type(
        "Oppx" + cls.__name__,
        (cls,),
        {
            "main_system": (cls.main_system or "") + QUESTION_RULE,
            "system_reminder": (cls.system_reminder or "") + QUESTION_RULE,
            "_oppx_question_rule": True,
        },
    )
    return sub()


class _EditPrompts:
    prompts = None  # the latest editing coder's prompts


_orig_ask_init = AskCoder.__init__


def _ask_init(self, *args, **kwargs):
    _orig_ask_init(self, *args, **kwargs)
    if _EditPrompts.prompts is not None:
        self.gpt_prompts = _EditPrompts.prompts
    _stable_map_heading(self)


def _ask_file_mentions(self, content):
    """A question's answer that names a file adds it to the chat (for the next
    question) but doesn't trigger Aider's "I added these files" follow-up
    request, which made the model answer a second time with filler."""
    super(AskCoder, self).check_for_file_mentions(content)
    return None


_orig_coder_init = base_coder.Coder.__init__


def _coder_init(self, *args, **kwargs):
    _orig_coder_init(self, *args, **kwargs)
    if not isinstance(self, AskCoder) and getattr(self, "edit_format", None) != "ask":
        self.gpt_prompts = _with_question_rule(self.gpt_prompts)
        _EditPrompts.prompts = self.gpt_prompts
        _stable_map_heading(self)


def _dump_requests(path: str):
    """Debugging aid (OPPX_DUMP_REQUESTS=file): appends every request's
    messages as one JSON line, to see what changes between turns and breaks
    the server's prefix cache (scripts/bench_session.py)."""
    import json

    orig = base_coder.Coder.send

    def send(self, messages, model=None, functions=None):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"coder": type(self).__name__, "messages": messages}) + "\n")
        return orig(self, messages, model, functions)

    base_coder.Coder.send = send
