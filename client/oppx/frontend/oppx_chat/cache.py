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
ASK_NOTE = "(This is a question: answer it in prose. Don't write edit blocks or file listings; no files will be changed.)"


class _EditPrompts:
    prompts = None  # the latest editing coder's prompts


_orig_ask_init = AskCoder.__init__


def _ask_init(self, *args, **kwargs):
    _orig_ask_init(self, *args, **kwargs)
    if _EditPrompts.prompts is not None:
        self.gpt_prompts = _EditPrompts.prompts
    _stable_map_heading(self)


_orig_coder_init = base_coder.Coder.__init__


def _coder_init(self, *args, **kwargs):
    _orig_coder_init(self, *args, **kwargs)
    if not isinstance(self, AskCoder) and getattr(self, "edit_format", None) != "ask":
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
