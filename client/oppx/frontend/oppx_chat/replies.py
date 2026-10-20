"""What the agent thinks and says (`Replying`, mixed into `Agent`): plans and
stock-takes, re-read checks, the facts given with a reply, the reply itself,
and the files a message names."""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from . import documents, writers
from .prompts import (
    ANSWER_PROMPT, ANSWER_RESERVE, PAGE_FILES, PLAIN_PROMPT, PLAIN_REGEX, READ_LINES, READ_PAD,
    RECENT_REPLIES, REPEAT_PROMPT, REVIEW_DIFF, STOPWORDS, STUCK_PROMPT, THINK_TOKENS,
)
from .actions import (
    ContextOverflow, ToolError, _norm, _sentences, as_action, cut_action_tail, strip_action, whole_sentences,
)


class Replying:
    """Plans, stock-takes and the reply; mixed into `Agent`."""

    def _named_files(self, text: str) -> list[str]:
        """Repository files the message names by path, or by a file name only
        one file has (at most 3)."""
        files = self.ws.files()
        paths, by_name = set(files), {}
        for f in files:
            by_name.setdefault(f.rsplit("/", 1)[-1], []).append(f)
        found = []
        for word in re.findall(r"(?<![\w./-])[\w./-]+\.\w+", text):
            w = word.strip("./`'\"")
            if w not in paths and len(by_name.get(w, [])) == 1:
                w = by_name[w][0]
            if w in paths and w not in found:
                found.append(w)
        return found[:3]

    def _documents_to_make(self, text: str) -> str:
        """A document or picture the request names to make: how. Asked for a
        PDF or a PNG, a 9B model wrote a Python script that makes it (and a
        command to run), or put the content in its reply, not in the file."""
        notes = []
        for word in dict.fromkeys(re.findall(r"(?<![\w./-])[\w./-]+\.\w+", text)):
            p = Path(word.strip("./`'\""))
            if not writers.writable(p):
                continue
            kind = documents.kind(p)
            how = (f"use create on {p} itself; you write {writers.SOURCE[p.suffix.lower()]} and it becomes the "
                   f"{kind}{' picture' if documents.is_image(p) else ''}. No script is needed")
            if (self.ws.root / p).exists():
                # Named to be read, as often as to be changed: "the first half is
                # the screenshot docs/phase2-part1.jpg" was told how to make it.
                notes.append(f"{p} exists. Only if the request asks you to change it: read it first, then {how}. "
                             "If it is there to be read, leave it as it is")
            else:
                notes.append(f"{p}: {how} to make it")
        return ("Files the request names that can be written:\n" + "\n".join(f"  {n}" for n in notes[:3])) if notes else ""

    def _documents_for(self, text: str) -> list[str]:
        """Documents and pictures a request is about though it names no file:
        named like its words ("the travel policy" for docs/travel-policy.pdf),
        or of a kind it mentions ("the PDF", "the screenshot"), when few are."""
        words = {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOPWORDS}
        files = [f for f in self.ws.files() if documents.kind(Path(f)) or documents.unsupported(Path(f))]
        kinds = {"pdf": (".pdf",), "word": (".docx", ".odt", ".rtf"), "docx": (".docx",), "document": tuple(documents.DOCUMENTS),
                 "excel": (".xlsx", ".xlsm"), "spreadsheet": (".xlsx", ".xlsm", ".csv"), "workbook": (".xlsx", ".xlsm"),
                 "epub": (".epub",), "book": (".epub",), "image": tuple(documents.IMAGES), "picture": tuple(documents.IMAGES),
                 "photo": tuple(documents.IMAGES), "screenshot": tuple(documents.IMAGES), "video": documents.VIDEOS,
                 "presentation": documents.PRESENTATIONS, "slides": documents.PRESENTATIONS, "powerpoint": documents.PRESENTATIONS,
                 "deck": documents.PRESENTATIONS}
        named = [f for f in files if words & set(re.findall(r"[a-z0-9]{3,}", f.lower().rsplit(".", 1)[0]))]
        wanted = tuple(ext for w, exts in kinds.items() if w in words or w + "s" in words for ext in exts)
        of_kind = [f for f in files if wanted and f.lower().endswith(wanted)]
        return list(dict.fromkeys(named + (of_kind if len(of_kind) <= 10 else [])))[:10]

    def _unseen(self) -> str:
        """Top-level folders no action of this turn has touched: where to look
        when the places tried so far have nothing."""
        top = sorted({f.split("/")[0] for f in self.ws.files() if "/" in f})
        trail = "\n".join(self._trail)
        unseen = [d for d in top if not re.search(rf"(?:\(| in ){re.escape(d)}(?:[/):]|$)", trail, re.M)]
        return (" Folders you haven't looked in this turn: " + ", ".join(d + "/" for d in unseen[:20]) + ".") if unseen else ""

    def _diff(self) -> str:
        """Everything this request changed so far, as one diff."""
        out, size = [], 0
        for rel, before in self._before.items():
            try:
                after = self.ws.view(self.ws.path(rel, must_exist=False))
            except (OSError, ToolError):
                after = ""
            if size > REVIEW_DIFF:
                out.append(f"(also changed: {rel}; not shown)")
                continue
            lines = list(difflib.unified_diff((before or "").splitlines(), after.splitlines(), f"a/{rel}" if before is not None
                                              else "/dev/null", f"b/{rel}", lineterm="", n=2))
            text = "\n".join(lines)
            if size + len(text) > REVIEW_DIFF:
                text = text[:max(0, REVIEW_DIFF - size)] + f"\n(the rest of {rel}'s changes is not shown)"
            size += len(text)
            out.append(text)
        return "\n".join(out) or "(nothing)"

    def _think(self, prompt: str, title: str, tokens: int = THINK_TOKENS) -> str:
        """A free-text step (a plan, a rethink): written by the model, kept in
        the conversation and shown to the user as one step."""
        kind = {"Rethink": "rethink", "Taking stock": "stock", "Review": "review"}.get(title, "plan")
        self._call = {"kind": kind}
        self._event(kind)
        self.messages.append({"role": "user", "content": prompt})
        self.hooks.waiting("Planning")
        try:
            plan = strip_action(self._text(tokens, stream=False))
            if not plan or as_action(plan) is not None:
                # The action again instead of the text (the conversation is
                # full of them): once more, the first character constrained.
                self._event("retry")
                try:
                    plan = strip_action(self._text(tokens, stream=False, regex=r"[^{\s`][\s\S]*"))
                except ContextOverflow:
                    raise
                except RuntimeError:  # a server without regex constraints
                    plan = ""
        finally:
            self.messages.pop()
        if not plan or as_action(plan) is not None:
            plan = "(no plan written)"
        plan = whole_sentences(plan, tokens)
        last = self.messages[-1] if self.messages else None
        if last and last["role"] == "assistant" and last["content"].startswith("{"):
            last["content"] = plan  # in place of the action that asked for it
        else:
            self.messages.append({"role": "assistant", "content": plan})
        self._trail.append(f"- {title}")
        self.hooks.step(title, plan)
        return plan

    def _unseen_page(self, changed_files: list[str], called: list[str]) -> bool:
        """Files that show in a web page were changed, and no browser tool looked at the result."""
        return (any(f.lower().endswith(PAGE_FILES) for f in changed_files)
                and not any(t.startswith("playwright.") for t in called))

    def _facts(self, changed_files: list[str], failed_edits: int, can_edit: bool, called: list[str] | None = None,
               check: str | None = None, unseen_page: bool = False) -> str:
        """What really changed, given with every reply: small models claim edits
        they never made, and repeat the claim when asked about it."""
        now = list(dict.fromkeys(changed_files))
        before = [f for f in self.changed_session if f not in now]
        parts = ["Files you changed in this request: " + (", ".join(now) if now else "none") + "."]
        if failed_edits and not now:
            parts.append("An edit you tried was refused, so that change was NOT made: say so, don't describe it as done.")
        elif can_edit and not now and not called:
            # Asked for a change, it read some code and answered as if the code already did it.
            parts.append("The user asked for a change and you made none: begin your reply by saying that nothing "
                         "was changed, then say why, or what is missing.")
        if now and check is None:
            parts.append("Nothing was run to check these changes: say in one sentence that they are not verified.")
        elif now and check != "exit code 0":
            parts.append(f"The last command you ran after editing ended with {check}: say that the check failed; don't call the work done.")
        elif now:
            parts.append("The last command you ran after editing succeeded (exit code 0): you may say what you ran and that it passed.")
        if now and unseen_page:
            # The user may not know the browser exists: say what wasn't seen, and that it can be.
            parts.append("You changed what a web page shows and did not look at the page: say in one sentence that it was "
                         "not checked in a browser" + (
                             ", and that you can open it in a headless browser and try it if they want (they will be asked "
                             "to allow it)." if self.browser == "offer" else
                             ", and that you can do so with the browser that is running if they want." if self.browser == "running"
                             else "."))
        parts.append("Files you changed earlier in this conversation: " + (", ".join(before) if before else "none") + ".")
        if called:
            # What a request like "open an issue" was about; not a file change.
            parts.append("Connector tools that ran in this request: " + ", ".join(dict.fromkeys(called))
                         + " (their results are above; report what they returned, not more).")
            # Asked to mark an issue done, Qwen3.6 read it (linear.get_issue)
            # and reported "status changed to Done".
            known = self.connectors.tools() if self.connectors is not None else {}
            if all(n in known and known[n].read_only for n in called):
                parts.append("All of them only read: nothing was created, updated, sent or marked through a connector in this "
                             "request. If the user asked for that, say it has not been done yet.")
        for name, (why, steps) in self.down.items():
            # The reply is where the user reads how to get it back.
            parts.append(f"The {name} connector stopped working during this request ({why}), so whatever needed it was NOT "
                         f"done. Begin your reply by saying so, list what was left undone, then give the user these steps to "
                         f"get it back, as they are: " + " ".join(f"({i}) {s}" for i, s in enumerate(steps, 1))
                         + " End by saying that once it is back, they can ask you to continue.")
        parts.append("Never say you added, changed or fixed something in a file that is not in these lists; "
                     "mention the lists only if the user asks about changes.")
        return "\n\nFacts: " + " ".join(parts)

    @staticmethod
    def _already_read(act: dict, shown: list) -> int | None:
        """The step that already showed every line of this read, if any."""
        if not shown:
            return None
        start = max(1, (act.get("start") or 1) - READ_PAD)
        end = (act.get("end") or start + READ_LINES - 1) + READ_PAD
        # Only the lines a read asked for count: a small file is shown whole
        # for a read of its first 100 lines, and the lines it didn't ask for
        # may be read once (refused, Qwen3.6 asked for them twice more and the
        # turn ended stuck, with its tests failing).
        covered = set()
        for a, b, _, asked_a, asked_b in shown:
            covered.update(range(max(a, asked_a - READ_PAD), min(b, asked_b + READ_PAD) + 1))
        last_line = max(r[1] for r in shown)
        wanted = set(range(start, min(end, last_line) + 1))
        if wanted and wanted <= covered and end <= last_line + READ_PAD:
            return shown[-1][2]
        return None

    @staticmethod
    def _note_read(act: dict, result: str, reads: dict, step_no: int) -> str:
        """Records which lines a read showed; a second read of a big file gets
        a hint to grep instead of paging through it."""
        m = re.match(r"\S+ lines (\d+)-(\d+) of (\d+):", result)
        if not m:
            return result
        first, last, total = (int(x) for x in m.groups())
        path = act.get("path") or ""
        reads.setdefault(path, []).append((first, last, step_no, act.get("start") or first, act.get("end") or last))
        if total > 2 * READ_LINES and len(reads[path]) >= 2:
            result += (f"\n\n[{path} has {total} lines; paging through it is slow. "
                       f"grep with path {path!r} for the name you need instead.]")
        return result

    def _answer(self, user_text: str = "", facts: str = "", stuck: str = "") -> str:
        """The reply, as free text, streamed. It is never an action, empty, or
        a copy of an earlier reply: those are held back and written again once,
        and if that fails too the harness says what happened itself."""
        asked = _norm(user_text)
        # What it said to other messages; asking the same thing again may get the same reply.
        old = {s for q, r in self._replies if _norm(q) != asked for s in _sentences(r)}
        self._call = {"kind": "reply"}
        self._prev = {"prev_tool": "answer"}
        if stuck:
            self._event("stuck")
            self.hooks.notice(f"Stuck: {stuck}. The reply below is from what was found so far.")
        prompt = ANSWER_PROMPT + (STUCK_PROMPT.format(reason=stuck) if stuck else "") + facts
        # The action that chose to answer would be the last thing the model
        # sees before writing; as JSON it invites more JSON.
        chose = self.messages[-1] if self.messages and self.messages[-1]["role"] == "assistant" \
            and self.messages[-1]["content"].startswith("{") else None
        hidden: list = [(chose, chose["content"])] if chose else []
        if chose:
            chose["content"] = "I'm ready to reply."
        self.messages.append({"role": "user", "content": prompt})
        try:
            reply, shown = self._held_back(old)
            if not shown and (not reply.strip() or as_action(reply) is not None):
                what = "empty" if not reply.strip() else "a JSON action"
                self._event("retry")
                self.messages[-1] = {"role": "user", "content": prompt + "\n\n" + PLAIN_PROMPT.format(what=what)}
                try:
                    reply, shown = self._held_back(old, regex=PLAIN_REGEX)
                except RuntimeError:  # a server without regex constraints
                    reply, shown = self._held_back(old)
            if not shown and reply.strip() and as_action(reply) is None:
                self.messages[-1] = {"role": "user", "content": prompt + "\n\n" + REPEAT_PROMPT}
                # The earlier copies are what it copies from (with them in view
                # the redo came out the same, even with randomness): for this
                # one request they are replaced by a note.
                said = set(_sentences(reply))
                for m in self.messages:
                    if m["role"] == "assistant" and not m["content"].startswith("{") and said & set(_sentences(m["content"])):
                        hidden.append((m, m["content"]))
                        m["content"] = "(An earlier reply of yours, which didn't answer what the user asks now.)"
                reply = self._text(ANSWER_RESERVE, stream=True, temperature=0.7)
                shown = True
            if not shown or not reply.strip() or as_action(reply) is not None:
                # Still nothing a person can read: say so, with what was done.
                reply = self._gave_up(stuck)
                self.hooks.stream(reply, True)
        finally:
            for m, content in hidden:
                m["content"] = content
            self.messages.pop()  # the instruction isn't part of the conversation
        reply = cut_action_tail(reply) or reply  # kept (and saved) as it was shown
        self._replies = (self._replies + [(user_text, reply)])[-RECENT_REPLIES:]
        if chose and self.messages and self.messages[-1] is chose:
            self.messages[-1] = {"role": "assistant", "content": reply}
        else:
            self.messages.append({"role": "assistant", "content": reply})
        return reply

    def _gave_up(self, stuck: str) -> str:
        """The harness's own reply when the model produced none: what happened
        and what was looked at, never an empty line or raw JSON."""
        self.stuck = self.stuck or "the model returned no readable reply, twice"
        trail = "\n".join(self._trail[-15:]) or "- nothing yet"
        why = f" It had stopped early: {stuck}." if stuck else ""
        return ("I couldn't produce an answer: the model returned no readable text when asked for the reply." + why
                + f"\n\nWhat I did:\n{trail}\n\nAsk again, or point me at a file or a name to start from.")
