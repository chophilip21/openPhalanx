#!/usr/bin/env python3
"""Unit tests for the agent loop (client/oppx/frontend/oppx_chat/agent.py) with
a scripted model: no server needed. Run with any Python that has httpx:

    ~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/test_agent.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client/oppx/frontend"))

from oppx_chat import agent as A  # noqa: E402


try:  # edits are applied with Aider's matcher; CI runs without it
    import aider  # noqa: F401
    HAVE_AIDER = True
except ImportError:
    HAVE_AIDER = False
needs_aider = unittest.skipUnless(HAVE_AIDER, "needs aider-chat (run with the oppx engine's Python)")
try:  # the terminal module draws with rich, which CI doesn't install either
    import rich  # noqa: F401
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


class Scripted:
    """An Agent whose model calls are played from lists."""

    def __init__(self, repo: str):
        self.shown, self.steps, self.notices, self.prompts = [], [], [], []
        self.actions, self.texts = [], []
        hooks = A.Hooks(step=lambda t, d: self.steps.append((t, d)), notice=self.notices.append,
                        stream=lambda text, final: self.shown.append(text) if final else None)
        self.agent = ag = A.Agent(ws=A.Workspace(repo), hooks=hooks)
        ag._fit = lambda: None

        def action(tools):
            act = dict(self.actions.pop(0)) if self.actions else {"tool": "answer"}
            if act["tool"] not in tools:
                act = {"tool": "answer"}
            return act

        def text_once(max_tokens, stream, temperature=None, regex=None):
            self.prompts.append((ag.messages[-1]["content"], regex))
            out = self.texts.pop(0) if self.texts else "Done."
            if stream:
                acc = ""
                for word in out.split(" "):
                    acc += word + " "
                    ag.hooks.stream(acc.strip(), False)
                ag.hooks.stream(out, True)
            return out

        ag._action, ag._text_once = action, text_once


class Repo(unittest.TestCase):
    """A small repository and an agent with a scripted model on it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "src").mkdir()
        (root / "src/calc.py").write_text("LIMIT = 3\n\n\ndef add(a, b):\n    return a + b\n\n\ndef start_server(port):\n    return port\n")
        (root / "src/other.py").write_text("from calc import add\n\nprint(add(1, 2))\n")
        (root / "README.md").write_text("add things with add()\n" * 30)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        self.s = Scripted(str(root))
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()


class AgentLoop(Repo):
    def test_a_json_reply_is_never_shown(self):
        s = self.s
        s.actions = [{"tool": "read", "path": "src/calc.py"}, {"tool": "answer"}]
        s.texts = ['{"why": "look further", "tool": "grep", "pattern": "add"}', "add returns a + b (src/calc.py:5)."]
        reply = s.agent.turn("what does add return?", can_edit=True, wants_change=False)
        self.assertEqual(reply, "add returns a + b (src/calc.py:5).")
        self.assertEqual(s.shown, [reply])
        self.assertEqual(s.prompts[-1][1], A.PLAIN_REGEX)  # the retry constrains the first character
        self.assertNotIn("{", s.agent.messages[-1]["content"][:1])

    def test_no_reply_at_all_gets_the_harness_fallback(self):
        s = self.s
        s.actions = [{"tool": "read", "path": "src/calc.py"}, {"tool": "answer"}]
        s.texts = ["{}", ""]
        reply = s.agent.turn("what does add return?", can_edit=True, wants_change=False)
        self.assertIn("couldn't produce an answer", reply)
        self.assertIn("Read(src/calc.py", reply)
        self.assertEqual(s.shown, [reply])
        self.assertTrue(s.agent.stuck)

    def test_json_the_user_asked_for_is_a_reply(self):
        s = self.s
        s.actions = [{"tool": "answer"}]
        s.texts = ['{"name": "demo", "port": 8080}']
        reply = s.agent.turn("give me an example config as JSON", can_edit=True, wants_change=False)
        self.assertEqual(s.shown, [reply])
        self.assertIn("8080", reply)

    def test_going_in_circles_rethinks_then_reports_stuck(self):
        s = self.s
        grep = {"tool": "grep", "pattern": "nothing_like_this"}
        s.actions = [grep] * 6
        s.texts = ["I know nothing yet; try find.", "I couldn't find it. Missing: where it lives."]
        reply = s.agent.turn("where is the frobnicator?", can_edit=True, wants_change=False)
        titles = [t for t, _ in s.steps]
        self.assertEqual(titles.count("Rethink"), 1)
        self.assertEqual(titles.count("Grep(nothing_like_this)"), 1)  # the repeats never ran
        self.assertEqual(len(s.notices), 1)
        self.assertIn("Stuck", s.notices[0])
        self.assertIn("repeating", s.agent.stuck)
        self.assertIn("You were stopped before finishing", s.prompts[-1][0])
        self.assertEqual(s.shown, [reply])

    def test_a_reworded_search_with_the_same_result_is_a_repeat(self):
        s = self.s
        s.actions = [{"tool": "grep", "pattern": "def add"}, {"tool": "grep", "pattern": "def add\\b"},
                     {"tool": "grep", "pattern": "def  ?add"}, {"tool": "read", "path": "src/calc.py"}, {"tool": "answer"}]
        s.texts = ["Enough: read the file.", "It's in src/calc.py:4."]
        s.agent.turn("where is add?", can_edit=True, wants_change=False)
        self.assertIn("Rethink", [t for t, _ in s.steps])
        self.assertFalse(s.agent.stuck)

    def test_a_result_cut_from_the_window_may_be_fetched_again(self):
        s = self.s
        grep = {"tool": "grep", "pattern": "def add"}
        calls = []

        def action(tools):
            calls.append(1)
            if len(calls) == 2:  # the window filled up: the first result is gone
                for m in s.agent.messages:
                    if m.get("_result"):
                        m["_cut"] = True
            return dict(grep) if len(calls) <= 2 else {"tool": "answer"}

        s.agent._action = action
        s.texts = ["In src/calc.py:4."]
        s.agent.turn("where is add?", can_edit=True, wants_change=False)
        self.assertEqual([t for t, _ in s.steps].count("Grep(def add)"), 2)

    @needs_aider
    def test_a_question_can_still_lead_to_an_edit_if_chosen_twice(self):
        s = self.s
        edit = {"tool": "edit", "path": "src/calc.py"}
        s.actions = [{"tool": "read", "path": "src/calc.py"}, edit, edit, edit, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = ["Plan: change LIMIT in src/calc.py.",
                   "src/calc.py\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE", "LIMIT is now 4."]
        s.agent.turn("the limit is still 3, I asked for 4", can_edit=True, wants_change=False)
        self.assertIn("LIMIT = 4", (self.root / "src/calc.py").read_text())
        checks = [m["content"] for m in s.agent.messages if m["content"] == A.ASK_EDIT_CHECK]
        self.assertEqual(len(checks), 1)
        self.assertEqual(s.agent.changed_session, ["src/calc.py"])

    @needs_aider
    def test_an_edit_written_as_json_is_asked_for_again(self):
        s = self.s
        edit = {"tool": "edit", "path": "src/calc.py"}
        s.actions = [edit, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = ['{"enough": false, "missing": "apply the edit", "tool": "edit", "path": "src/calc.py"}',
                   "src/calc.py\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE", "LIMIT is now 4."]
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        self.assertIn("LIMIT = 4", (self.root / "src/calc.py").read_text())
        self.assertEqual([t for t, _ in s.steps].count("Update(src/calc.py)"), 1)  # one step, not a failed one and a retry

    def test_a_change_request_never_opens_with_answer(self):
        s = self.s
        seen = []
        s.agent._action = lambda tools: (seen.append(tools), {"tool": "answer"} if "answer" in tools else {"tool": "list", "path": ""})[1]
        s.texts = ["Nothing was changed: I couldn't find it."]
        s.agent.turn("the limit should be 4", can_edit=True, wants_change=True)
        self.assertNotIn("answer", seen[0])
        self.assertIn("answer", seen[1])

    def test_requests_carry_trace_labels_only_when_the_server_reads_them(self):
        s = self.s
        s.actions = [{"tool": "grep", "pattern": "def add"}, {"tool": "answer"}]
        s.texts = ["In src/calc.py:4."]
        s.agent.turn("where is add?", can_edit=True, wants_change=False)
        self.assertNotIn("oppx_trace", s.agent._body({}))
        s.agent.trace = True
        s.agent._call = {"kind": "action"}
        s.agent._do({"tool": "grep", "pattern": "LIMIT"})
        labels = s.agent._body({})["oppx_trace"]
        self.assertEqual((labels["session"], labels["turn"], labels["kind"]), (s.agent.session, 1, "action"))
        self.assertEqual((labels["prev_tool"], labels["prev_title"]), ("grep", "Grep(LIMIT)"))
        self.assertIn("prev_ms", labels)
        self.assertNotIn("prev_tool", s.agent._body({})["oppx_trace"])  # told once
        before = s.agent.session
        s.agent.reset()
        self.assertNotEqual(s.agent.session, before)

    def test_a_plan_is_shown_without_the_action_tacked_onto_it(self):
        self.assertEqual(A.strip_action('1. change LIMIT\n2. done\n\n{"enough": false, "missing": "apply", "tool": "edit", "path": "a.py"}'),
                         "1. change LIMIT\n2. done")
        self.assertEqual(A.strip_action('Plan:\n- edit a.py\n```json\n{"tool": "edit", "path": "a.py"}\n```'), "Plan:\n- edit a.py")
        self.assertEqual(A.strip_action('Set {"limit": 4} in config.json.'), 'Set {"limit": 4} in config.json.')
        self.assertEqual(A.strip_action("Use a dict: {\n  x\n}"), "Use a dict: {\n  x\n}")
        s = self.s
        s.actions = [{"tool": "plan"}, {"tool": "answer"}]
        s.texts = ['Look in src.\n{"enough": false, "missing": "x", "tool": "read", "path": "src/calc.py"}', "Nothing."]
        s.agent.turn("where is add?", can_edit=True, wants_change=False)
        self.assertEqual([d for t, d in s.steps if t == "Plan"], ["Look in src."])

    @needs_aider
    def test_an_edit_nobody_checked_is_reported_as_unverified(self):
        s = self.s
        edit = {"tool": "edit", "path": "src/calc.py"}
        block = "src/calc.py\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE"
        s.actions = [edit, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = [block, "LIMIT is 4 now (not verified)."]
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        nudge = [m["content"] for m in s.agent.messages if "haven't run anything to check" in m["content"]]
        self.assertEqual(len(nudge), 1)
        self.assertIn("Nothing was run to check these changes", s.prompts[-1][0])
        # With a check run after the edit, the reply is told how it ended.
        s2 = Scripted(str(self.root))
        s2.agent.hooks.confirm_run = lambda c: True
        s2.actions = [dict(edit), {"tool": "run", "command": "python3 -c 'import sys; sys.exit(3)'"}, {"tool": "answer"}, {"tool": "answer"}]
        s2.texts = [block.replace("LIMIT = 3", "LIMIT = 4").replace("=======\nLIMIT = 4", "=======\nLIMIT = 5"), "The check failed."]
        s2.agent.turn("make the limit 5 in src/calc.py", can_edit=True)
        self.assertIn("ended with exit code 3", s2.prompts[-1][0])
        self.assertFalse(any("haven't run anything" in m["content"] for m in s2.agent.messages))

    @needs_aider
    def test_a_request_with_several_parts_is_reviewed_against_the_diff(self):
        s = self.s
        edit = {"tool": "edit", "path": "src/calc.py"}
        block = "src/calc.py\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE"
        s.actions = [edit, {"tool": "answer"}, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = [block, "1) limit: +LIMIT = 4\n2) rename add: NOT DONE, add is still add", "The limit is 4; add was not renamed."]
        s.agent.turn("In src/calc.py: 1) make the limit 4; 2) rename add to plus.", can_edit=True)
        review = [p for p, _ in s.prompts if "check your work against the request" in p]
        self.assertEqual(len(review), 1)
        self.assertIn("+LIMIT = 4", review[0])  # the diff of this request
        self.assertIn("-LIMIT = 3", review[0])
        self.assertIn("rename add to plus", review[0])  # and the request itself
        self.assertEqual([d for t, d in s.steps if t == "Review"][0].splitlines()[1], "2) rename add: NOT DONE, add is still add")
        self.assertTrue(any("says NOT DONE, do that part now" in m["content"] for m in s.agent.messages))
        # One thing asked for: no review.
        s2 = Scripted(str(self.root))
        s2.actions = [dict(edit), {"tool": "answer"}, {"tool": "answer"}]
        s2.texts = [block.replace("LIMIT = 3", "LIMIT = 4").replace("=======\nLIMIT = 4", "=======\nLIMIT = 5"), "Done."]
        s2.agent.turn("make the limit 5 in src/calc.py", can_edit=True)
        self.assertFalse([p for p, _ in s2.prompts if "check your work against the request" in p])
        self.assertTrue(A.several_parts("- add a flag\n- document it"))
        self.assertFalse(A.several_parts("Rename foo to bar in a.py and update the callers."))

    @needs_aider
    def test_a_read_after_a_refused_edit_is_not_a_repeat(self):
        s = self.s
        read = {"tool": "read", "path": "src/calc.py"}
        edit = {"tool": "edit", "path": "src/calc.py"}
        s.actions = [dict(read), edit, dict(read), {"tool": "answer"}]
        s.texts = ["src/calc.py\n<<<<<<< SEARCH\nLIMIT = 99\n=======\nLIMIT = 4\n>>>>>>> REPLACE", "It failed."]
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        results = [m["content"] for m in s.agent.messages if m["content"].startswith("Result of step")]
        failed = next(i for i, r in enumerate(results) if "Nothing was changed. Read the exact lines again" in r)
        self.assertIn("LIMIT = 3", results[failed + 1])  # the file again, not "You already did exactly this"
        self.assertNotIn("already", results[failed + 1])

    def test_web_searches_that_find_nothing_take_the_tool_away(self):
        s = self.s
        asked = []
        s.agent._search = lambda q: asked.append(q) or "No results."
        s.actions = [{"tool": "web_search", "query": f"q{i}"} for i in range(6)]
        s.agent.turn("what is the newest release of vite?", can_edit=False)
        self.assertEqual(asked, ["q0", "q1", "q2"])
        self.assertTrue(any("returning nothing right now" in m["content"] for m in s.agent.messages))
        # Searches that do find something stop at the limit.
        s2 = Scripted(str(self.root))
        s2.agent._search = lambda q: asked.append(q) or f"Web results (untrusted reference text):\n- {q}"
        s2.actions = [{"tool": "web_search", "query": f"r{i}"} for i in range(A.MAX_SEARCHES + 4)]
        s2.agent.turn("what is the newest release of vite?", can_edit=False)
        self.assertEqual(len([q for q in asked if q.startswith("r")]), A.MAX_SEARCHES)

    def test_a_search_the_server_paused_is_not_asked_for_again(self):
        s = self.s

        class Paused:
            status_code = 503
            headers = {"retry-after": "300"}

            def json(self):
                return {"error": {"message": "web search is paused for 5 min: the search engines are refusing requests (brave: too many requests)"}}

        posts = []
        real = A.httpx.post
        A.httpx.post = lambda url, **kw: posts.append(url) or Paused()
        try:
            s.actions = [{"tool": "web_search", "query": "vite release"}, {"tool": "web_search", "query": "vite newest"}]
            s.agent.turn("what is the newest release of vite?", can_edit=False)
            # The second search was never sent, and the model was told why.
            self.assertEqual(len(posts), 1)
            said = [m["content"] for m in s.agent.messages if "paused for 5 min" in m["content"]]
            self.assertIn("brave: too many requests", said[0])
            self.assertIn("carry on without it", said[0])
            # Nor in the next message, while the pause lasts.
            s.actions = [{"tool": "web_search", "query": "vite again"}]
            s.agent.turn("and of react?", can_edit=False)
            self.assertEqual(len(posts), 1)
            s.agent._search_off_until = 0  # the pause is over
            s.actions = [{"tool": "web_search", "query": "react release"}]
            s.agent.turn("and of svelte?", can_edit=False)
            self.assertEqual(len(posts), 2)
        finally:
            A.httpx.post = real

    def test_an_action_typed_as_a_shell_command_is_not_run_and_a_declined_one_is_a_repeat(self):
        s = self.s
        asked = []
        s.agent.hooks.confirm_run = lambda c: asked.append(c) or False
        s.agent.web = False
        s.actions = [{"tool": "run", "command": "web_search --query 'vite release'"}]
        s.agent.turn("search the web for the newest vite release", can_edit=False)
        self.assertEqual(asked, [])  # nobody is asked to run a program that doesn't exist
        self.assertTrue(any("web search is not available in this session" in m["content"] for m in s.agent.messages))
        self.assertNotIn('"tool": "web_search"', s.agent._system())
        self.assertIn("Web search is switched off", s.agent._system())
        # A command the user declined changed nothing: asking for it again is a repeat.
        s2 = Scripted(str(self.root))
        s2.agent.hooks.confirm_run = lambda c: asked.append(c) or False
        s2.actions = [{"tool": "run", "command": "make deploy"}] * 6
        s2.agent.turn("deploy it", can_edit=False)
        self.assertEqual(asked, ["make deploy"])
        self.assertTrue(any("already" in m["content"] for m in s2.agent.messages))

    def test_a_command_that_just_failed_is_not_run_again_unchanged(self):
        s = self.s
        ran = []
        s.agent.hooks.confirm_run = lambda c: ran.append(c) or True
        bad = {"tool": "run", "command": "python3 -c 'import sys; sys.exit(1)'"}
        s.actions = [dict(bad), dict(bad), {"tool": "run", "command": "true"}, dict(bad)]
        s.agent.turn("check the api", can_edit=False)
        # Asked for twice in a row, it ran once; after another command worked, it may run again.
        self.assertEqual(ran, [bad["command"], "true", bad["command"]])
        self.assertTrue(any("already failed at step" in m["content"] for m in s.agent.messages))

    def test_nearly_the_same_web_search_is_answered_from_the_session(self):
        s = self.s

        class Ok:
            status_code = 200
            headers = {}

            def __init__(self, q):
                self.q = q

            def json(self):
                return {"results": [{"title": "T", "snippet": "about " + self.q, "url": "https://example.com"}]}

        sent = []
        real = A.httpx.post
        A.httpx.post = lambda url, **kw: sent.append(kw["json"]["query"]) or Ok(kw["json"]["query"])
        try:
            s.actions = [{"tool": "web_search", "query": "Algolia Hacker News API sort by date latest stories"},
                         {"tool": "web_search", "query": "Hacker News Algolia API: sort stories by latest date"},
                         {"tool": "web_search", "query": "vite 8 release notes"}]
            s.agent.turn("how do I sort the HN api by date?", can_edit=False)
            self.assertEqual(sent, ["Algolia Hacker News API sort by date latest stories", "vite 8 release notes"])
            self.assertTrue(any("Not searched again" in m["content"] and "about Algolia" in m["content"] for m in s.agent.messages))
            # In a later message of the same session too.
            s.actions = [{"tool": "web_search", "query": "sort by date latest stories Algolia Hacker News API"}]
            s.agent.turn("and once more?", can_edit=False)
            self.assertEqual(len(sent), 2)
        finally:
            A.httpx.post = real

    def test_a_refused_command_tells_the_model_why(self):
        s = self.s
        seen = []
        s.agent.hooks.confirm_run = lambda c: "The user declined to run this command and said: use pytest -q instead"
        s.agent._action = lambda tools: (seen.append(tools), {"tool": "run", "command": "pytest"} if len(seen) == 1 else {"tool": "answer"})[1]
        s.texts = ["Not run."]
        s.agent.turn("run the tests", can_edit=False)
        self.assertIn("run", seen[0], "plan mode can still look with commands; the app lets only reading ones through")
        self.assertNotIn("edit", seen[0])
        results = [m["content"] for m in s.agent.messages if m.get("_result")]
        self.assertTrue(any("use pytest -q instead" in r for r in results))
        self.assertFalse(any(r.startswith("exit code") for r in results), "it never ran")

    def test_plan_mode_has_no_edit_tools(self):
        s = self.s
        s.actions = [{"tool": "edit", "path": "src/calc.py"}]
        s.texts = ["I can't edit in plan mode."]
        s.agent.turn("make the limit 4", can_edit=False)
        self.assertIn("LIMIT = 3", (self.root / "src/calc.py").read_text())

    @needs_aider
    def test_a_refused_edit_is_not_a_change(self):
        s = self.s
        edit = {"tool": "edit", "path": "src/calc.py"}
        s.actions = [edit, edit, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = ["src/calc.py\n<<<<<<< SEARCH\nnot in the file\n=======\nx\n>>>>>>> REPLACE", "Nothing was changed."]
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        self.assertEqual(s.agent.changed_session, [])
        self.assertIn("that change was NOT made", s.prompts[-1][0])

    def test_a_file_by_file_sweep_is_stopped(self):
        s = self.s
        s.actions = [{"tool": "grep", "pattern": "add", "path": p} for p in ("src/calc.py", "src/other.py", "README.md", "src")]
        s.texts = ["Enough: add is in src/calc.py.", "add is defined in src/calc.py:4."]
        s.agent.turn("where is add?", can_edit=True, wants_change=False)
        titles = [t for t, _ in s.steps]
        self.assertIn("Rethink", titles)
        self.assertLess(titles.index("Rethink"), 5)
        self.assertIn("one file after another", "".join(m["content"] for m in s.agent.messages))

    def test_lookups_asked_for_together_run_in_one_step(self):
        s = self.s
        calls = []
        script = [{"tool": "grep", "pattern": "def add", "then": [{"tool": "read", "path": "src/calc.py"},
                                                                 {"tool": "outline", "path": "src/other.py"},
                                                                 {"tool": "read", "path": "README.md"},  # over the limit
                                                                 ]}, {"tool": "answer"}]

        def action(tools):
            calls.append(1)
            return dict(script.pop(0)) if script else {"tool": "answer"}

        s.agent._action = action
        s.texts = ["add is in src/calc.py:4."]
        s.agent.turn("where is add?", can_edit=True, wants_change=False)
        titles = [t for t, _ in s.steps]
        self.assertEqual(titles[:3], ["Grep(def add)", "Read(src/calc.py:1-)", "Outline(src/other.py)"])
        self.assertNotIn("Read(README.md:1-)", titles)
        self.assertEqual(len(calls), 2)  # one for the three lookups, one to answer
        schema = A.action_schema(("grep", "read", "edit", "answer"))
        by_tool = {b["properties"]["tool"]["enum"][0]: b for b in schema["anyOf"]}
        self.assertIn("then", by_tool["grep"]["properties"])
        self.assertNotIn("then", by_tool["edit"]["properties"])  # only lookups travel together

    def test_a_long_turn_takes_stock(self):
        s = self.s
        s.actions = [{"tool": "grep", "pattern": f"x{i}"} for i in range(13)] + [{"tool": "answer"}]
        s.texts = ["Nothing established yet.", "I found nothing."]
        s.agent.turn("where is it?", can_edit=True, wants_change=False)
        self.assertEqual([t for t, _ in s.steps].count("Taking stock"), 1)
        self.assertFalse(s.agent.stuck)

    def test_rethink_names_folders_not_looked_in(self):
        s = self.s
        (self.root / "docker").mkdir()
        (self.root / "docker/gateway.py").write_text("def drop_dates():\n    pass\n")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        s.actions = [{"tool": "grep", "pattern": "dates", "path": "src"}] * 3
        s.texts = ["Look in docker/.", "Not found."]
        s.agent.turn("where are dates dropped?", can_edit=True, wants_change=False)
        rethink = [p for p, _ in s.prompts if "going in circles" in p][0]
        self.assertIn("docker/", rethink.split("haven't looked in this turn:")[1])
        self.assertNotIn("src/", rethink.split("haven't looked in this turn:")[1])

    def test_using_every_step_is_reported(self):
        s = self.s
        s.actions = [{"tool": "grep", "pattern": f"x{i}"} for i in range(A.MAX_STEPS + 5)]
        s.texts = ["Stuck: found nothing."]
        s.agent.turn("where is it?", can_edit=True, wants_change=False)
        self.assertIn(f"all {A.MAX_STEPS} steps", s.agent.stuck)
        self.assertEqual(len(s.notices), 1)


    def test_create_replaces_a_file_that_was_read(self):
        s = self.s
        new = "LIMIT = 9\n"
        # The message names the file, so it is read up front.
        s.actions = [{"tool": "create", "path": "src/calc.py"}, {"tool": "answer"}]
        s.texts = [f"```python\n{new}```", "Done."]
        seen = []
        s.agent.hooks.edited = lambda rel, before, after: seen.append((rel, before, after))
        s.agent.turn("rewrite src/calc.py so it only has LIMIT = 9", can_edit=True, wants_change=True)
        self.assertEqual((self.root / "src/calc.py").read_text(), new)
        self.assertEqual(len(seen), 1)
        self.assertIn("def add", seen[0][1])  # the old text, for the diff and /undo
        self.assertTrue(any("Replaced the whole of src/calc.py" in m["content"] for m in s.agent.messages if m.get("_result")))
        # A file that wasn't read is left alone, as for an edit.
        with self.assertRaises(A.ToolError):
            s.agent._create("src/other.py")
        self.assertIn("from calc import add", (self.root / "src/other.py").read_text())

    @needs_aider
    def test_a_file_can_be_read_again_after_it_was_edited(self):
        s = self.s
        read = {"tool": "read", "path": "src/calc.py"}
        block = "src/calc.py\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE"
        s.actions = [read, {"tool": "edit", "path": "src/calc.py"}, read, {"tool": "answer"}]
        s.texts = [block, "Done."]
        s.agent.turn("In src/calc.py set LIMIT to 4", can_edit=True, wants_change=True)
        results = [m["content"] for m in s.agent.messages if m.get("_result")]
        reads = [r for r in results if r.startswith("Result of step") and "(read)" in r.split("\n", 1)[0]]
        self.assertIn("LIMIT = 4", reads[-1])  # the read after the edit shows the new text
        self.assertFalse(any("already" in r for r in reads))

    def test_a_change_request_that_keeps_answering_gets_a_step_without_answer(self):
        s = self.s
        offered = []
        action = s.agent._action
        s.agent._action = lambda tools: offered.append(tools) or action(tools)
        s.actions = [{"tool": "read", "path": "src/calc.py"}, {"tool": "answer"}, {"tool": "answer"},
                     {"tool": "edit", "path": "src/nowhere.py"}, {"tool": "answer"}]
        s.texts = ["Nothing was changed."]
        s.agent.turn("make the limit bigger", can_edit=True, wants_change=True)
        notes = [m["content"] for m in s.agent.messages if m["role"] == "user"]
        self.assertTrue(any("make the first change now" in n for n in notes))
        self.assertEqual(set(offered[3]), {"edit", "create", "run", "start"})  # the step after the second answer
        self.assertIn("answer", offered[4])

    def test_an_action_at_the_end_of_a_reply_is_not_shown(self):
        s = self.s
        s.actions = [{"tool": "answer"}]
        s.texts = ['Let me start.\n{"enough": false, "missing": "the API", "tool": "plan"}']
        reply = s.agent.turn("where is add?", can_edit=True, wants_change=False)
        self.assertEqual(s.shown[-1], "Let me start.")
        self.assertEqual(reply, "Let me start.")

    def test_a_plan_that_comes_back_as_an_action_is_asked_for_again(self):
        s = self.s
        s.actions = [{"tool": "plan"}, {"tool": "answer"}]
        s.texts = ['{"enough": false, "missing": "a plan", "tool": "plan"}', "Read calc.py, then answer.", "Nothing."]
        s.agent.turn("where is add?", can_edit=True, wants_change=False)
        self.assertEqual([d for t, d in s.steps if t == "Plan"], ["Read calc.py, then answer."])
        self.assertTrue(any(regex for _, regex in s.prompts))

    def test_an_action_cut_off_before_its_tool_is_asked_for_again(self):
        ag = A.Agent(ws=A.Workspace(str(self.root)), hooks=A.Hooks())
        ag._fit = lambda: None
        bodies = []
        replies = ['{"enough": false, "missing": "the files"', '{"enough": false, "missing": "the files", "tool": "list", "path": ""}']

        def post(body):
            bodies.append(body)
            return {"choices": [{"message": {"content": replies.pop(0)}}]}

        ag._post = post
        ag.messages = [{"role": "user", "content": "what is here?"}]
        act = ag._action(("list", "answer"))
        self.assertEqual(act["tool"], "list")
        self.assertEqual(bodies[0]["stop"], ["\n\n"])
        self.assertEqual(bodies[0]["temperature"], 0)
        self.assertEqual(bodies[1]["temperature"], 0.7)


class FollowUps(Repo):
    """A message is read with what came before it: a confirmation, a reply that
    announced a change, a command that keeps failing the same way."""

    BLOCK = "src/calc.py\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE"

    @needs_aider
    def test_a_change_not_made_stays_owed_until_it_is_made(self):
        s = self.s
        s.actions = [{"tool": "read", "path": "src/calc.py"}, {"tool": "answer"}, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = ["Nothing was changed: I got stuck."]
        s.agent.turn("make the limit bigger", can_edit=True, wants_change=True)
        self.assertEqual(s.agent.pending, "make the limit bigger")
        self.assertEqual(s.agent.last_reply(), "Nothing was changed: I got stuck.")
        # A question in between doesn't forget it...
        s.texts = ["It is 3."]
        s.agent.turn("what is the limit now?", can_edit=True, wants_change=False)
        self.assertEqual(s.agent.pending, "make the limit bigger")
        # ...and the turn that makes the change settles it. The follow-up's own words don't replace the request.
        edit = {"tool": "edit", "path": "src/calc.py"}
        s.actions = [edit, edit, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = ["Plan: change LIMIT in src/calc.py.", self.BLOCK, "LIMIT is now 4 (not verified)."]
        s.agent.turn("yes, go ahead.", can_edit=True, wants_change=True)
        self.assertIn("LIMIT = 4", (self.root / "src/calc.py").read_text())
        self.assertEqual(s.agent.pending, "")

    def test_a_change_owed_is_forgotten_after_a_few_other_turns_and_on_clear(self):
        s = self.s
        s.agent.turn("make the limit bigger", can_edit=True, wants_change=True)
        self.assertTrue(s.agent.pending)
        for _ in range(A.PENDING_TURNS):
            s.agent.turn("what does add do?", can_edit=True, wants_change=False)
        self.assertEqual(s.agent.pending, "")
        s.agent.turn("make the limit bigger", can_edit=True, wants_change=True)
        s.agent.reset()
        self.assertEqual(s.agent.pending, "")
        # Plan mode asks for nothing to be changed, so nothing is owed.
        s.agent.turn("make the limit bigger", can_edit=False, wants_change=True)
        self.assertEqual(s.agent.pending, "")

    @needs_aider
    def test_a_reply_that_announces_a_change_is_held_to_it(self):
        s = self.s
        offered, heard = [], []
        action = s.agent._action
        s.agent._action = lambda tools: offered.append(tools) or action(tools)
        s.agent.hooks.announces = lambda reply: heard.append(reply) or "Now I'll" in reply
        edit = {"tool": "edit", "path": "src/calc.py"}
        # The message read as a question, the model answered "Now I'll change it" and stopped.
        s.actions = [{"tool": "read", "path": "src/calc.py"}, {"tool": "answer"}, edit, edit, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = ["The limit is 3. Now I'll change it to 4.", "Plan: change LIMIT in src/calc.py.", self.BLOCK,
                   "LIMIT is now 4 (not verified)."]
        reply = s.agent.turn("are you having trouble?", can_edit=True, wants_change=False)
        self.assertEqual(heard, ["The limit is 3. Now I'll change it to 4."])
        self.assertEqual(set(offered[2]), {"edit", "create", "run", "start"}, "the step after the announcement only acts")
        self.assertIn("LIMIT = 4", (self.root / "src/calc.py").read_text())
        self.assertEqual(reply, "LIMIT is now 4 (not verified).")
        self.assertEqual(s.shown, ["The limit is 3. Now I'll change it to 4.", reply], "both were shown, in order")
        self.assertFalse(any(m["content"] == A.ASK_EDIT_CHECK for m in s.agent.messages), "it said so itself: no second check")

    def test_an_offer_or_an_answer_ends_the_turn(self):
        s = self.s
        s.agent.hooks.announces = lambda reply: False  # "Want me to change it?" is not an announcement
        s.actions = [{"tool": "read", "path": "src/calc.py"}, {"tool": "answer"}]
        s.texts = ["The limit is 3. Want me to change it to 4?"]
        reply = s.agent.turn("what is the limit?", can_edit=True, wants_change=False)
        self.assertEqual(s.shown, [reply])
        self.assertEqual(s.agent.pending, "", "nothing was asked for, so nothing is owed")

    def test_an_announcement_is_followed_up_once_and_never_without_edit_tools(self):
        s = self.s
        asked = []
        s.agent.hooks.announces = lambda reply: asked.append(reply) or True
        # It announces, is given a step to act, and answers again: that reply stands.
        s.actions = [{"tool": "answer"}, {"tool": "answer"}]
        s.texts = ["Let me make the changes now.", "Nothing was changed: I don't know which file."]
        reply = s.agent.turn("hm?", can_edit=True, wants_change=False)
        self.assertEqual(reply, "Nothing was changed: I don't know which file.")
        self.assertEqual(len(asked), 1)
        self.assertIn("you made none", s.prompts[-1][0], "the second reply is written with the facts")
        self.assertEqual(s.agent.pending, "hm?", "and the change it announced is owed")
        # Plan mode: no edit tools, so nothing to hold it to.
        s2 = Scripted(str(self.root))
        s2.agent.hooks.announces = lambda reply: asked.append("plan") or True
        s2.texts = ["Let me make the changes now."]
        s2.agent.turn("hm?", can_edit=False)
        self.assertNotIn("plan", asked)

    @needs_aider
    def test_a_command_that_failed_twice_is_not_run_again_because_another_one_worked(self):
        s = self.s
        ran = []
        s.agent.hooks.confirm_run = lambda c: ran.append(c) or True
        bad = {"tool": "run", "command": "python3 -c 'import sys; sys.exit(1)'"}
        ok = {"tool": "run", "command": "true"}
        s.actions = [dict(bad), dict(ok), dict(bad), dict(ok), dict(bad)]
        s.agent.turn("check the api", can_edit=False)
        # Failed, another command worked, failed again the same way: the third time it is refused
        # (a `print(keys)` in between had let a broken one-liner run again and again).
        self.assertEqual(ran, [bad["command"], "true", bad["command"], "true"])
        refused = [m["content"] for m in s.agent.messages if "character for character" in m["content"]]
        self.assertEqual(len(refused), 1)

    @needs_aider
    def test_the_action_after_a_refused_repeat_is_sampled(self):
        s = self.s
        sampled = []
        action = s.agent._action
        s.agent._action = lambda tools: sampled.append(getattr(s.agent, "_sample", False)) or action(tools)
        s.agent.hooks.confirm_run = lambda c: True
        bad = {"tool": "run", "command": "python3 -c 'import sys; sys.exit(1)'"}
        s.actions = [dict(bad), dict(bad), {"tool": "run", "command": "true"}, {"tool": "answer"}]
        s.agent.turn("check the api", can_edit=False)
        self.assertEqual(sampled, [False, False, True, False], "only the call right after the refusal")

    @needs_aider
    def test_stuck_on_one_failing_command_says_no_fix_was_ever_sent(self):
        s = self.s
        s.agent.hooks.confirm_run = lambda c: True
        bad = {"tool": "run", "command": "python3 -c 'import sys; sys.exit(1)'"}
        s.actions = [dict(bad)] * 6
        s.texts = ["I need data['hits'][0].", "I got stuck."]
        s.agent.turn("check the api", can_edit=False)
        self.assertIn("same failing command without changing it", s.agent.stuck)
        self.assertIn("No corrected command was ever sent", s.prompts[-1][0])

    @needs_aider
    def test_a_page_nobody_looked_at_is_said_and_the_browser_is_offered(self):
        (self.root / "src/App.jsx").write_text("export const LIMIT = 3\n")
        block = "src/App.jsx\n<<<<<<< SEARCH\nexport const LIMIT = 3\n=======\nexport const LIMIT = 4\n>>>>>>> REPLACE"
        edit = {"tool": "edit", "path": "src/App.jsx"}

        def run(browser):
            (self.root / "src/App.jsx").write_text("export const LIMIT = 3\n")
            s = Scripted(str(self.root))
            s.agent.browser = browser
            s.agent.hooks.confirm_run = lambda c: True
            s.actions = [dict(edit), {"tool": "run", "command": "true"}, {"tool": "answer"}, {"tool": "answer"}]
            s.texts = [block, "Done; lint passes."]
            s.agent.turn("make the limit 4 in src/App.jsx", can_edit=True)
            nudges = [m["content"] for m in s.agent.messages if "shows in a web page" in m["content"]]
            return nudges, s.prompts[-1][0]

        nudges, facts = run("offer")
        self.assertEqual(len(nudges), 1)
        self.assertIn("choose the browser action", nudges[0])
        self.assertIn("not checked in a browser", facts)
        self.assertIn("headless browser", facts, "the user may not know it is there")
        nudges, facts = run("declined")  # they said no: say it wasn't seen, don't offer again
        self.assertEqual(nudges, [])
        self.assertIn("not checked in a browser", facts)
        self.assertNotIn("headless browser", facts)
        # A change that no page shows: nothing about browsers.
        s = self.s
        s.agent.browser = "offer"
        s.actions = [{"tool": "edit", "path": "src/calc.py"}, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = [self.BLOCK, "Done."]
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        self.assertNotIn("browser", s.prompts[-1][0])

    def test_the_intent_check_reads_a_follow_up_with_what_came_before(self):
        from oppx_chat import routing as R
        self.assertEqual(R.intent_input("add a test"), "add a test", "a first message goes as it is")
        text = R.intent_input("yes, go ahead.", "…the field is created_at.\n\nWant me to add   the dropdown?", "add a sort dropdown")
        self.assertEqual(text.split("\n"), ["Change asked for earlier, not made yet: add a sort dropdown",
                                           "Assistant's last reply: …the field is created_at. Want me to add the dropdown?",
                                           "Message: yes, go ahead."])
        long = R.intent_input("ok", "x" * 5000 + " Shall I?")
        self.assertLess(len(long), R.CONTEXT_CHARS + 60)
        self.assertIn("Shall I?", long, "the end of the reply is what is kept")
        seen = []
        R.ask_choice = lambda system, text, choices, trace=None: seen.append((system, text, choices)) or None
        self.assertTrue(R.wants_edit("yes", None, "Want me to?", ""), "no answer from the model: assume a change")
        self.assertIn("Assistant's last reply: Want me to?", seen[0][1])
        self.assertFalse(R.announces_change("Let me fix that."), "no answer from the model: nothing is forced")
        self.assertEqual(seen[1][2], ("yes", "no"))


class ConfirmedGaps(Repo):
    """CHO-10: gaps that were checked against the code and closed."""

    BLOCK = FollowUps.BLOCK

    @needs_aider
    def test_an_edit_to_a_file_that_changed_on_disk_is_refused_until_it_is_read_again(self):
        s = self.s
        calc = self.root / "src/calc.py"
        read, edit = {"tool": "read", "path": "src/calc.py"}, {"tool": "edit", "path": "src/calc.py"}
        texts = []

        def someone_else_saves(tools):  # the user's editor, between the read and the edit
            act = dict(actions.pop(0)) if actions else {"tool": "answer"}
            if act["tool"] == "edit" and not texts:
                calc.write_text(calc.read_text() + "\n# added by the user\n")
                texts.append("saved")
            return act if act["tool"] in tools else {"tool": "answer"}

        actions = [dict(read), dict(edit), dict(read), dict(edit), {"tool": "answer"}, {"tool": "answer"}]
        s.agent._action = someone_else_saves
        s.texts = [self.BLOCK, "LIMIT is now 4 (not verified)."]
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        refused = [m["content"] for m in s.agent.messages if "has changed on disk since you read it" in m["content"]]
        self.assertEqual(len(refused), 1)
        self.assertIn("Nothing was changed", refused[0])
        text = calc.read_text()
        self.assertIn("LIMIT = 4", text, "after the read it asked for, the edit went in")
        self.assertIn("# added by the user", text, "and the user's line is still there")
        # Its own edit doesn't count as a change by someone else: a second edit needs no new read.
        s.agent._action = Scripted(str(self.root)).agent._action  # (unused; the next turn scripts its own)
        s2 = Scripted(str(self.root))
        s2.actions = [dict(read), dict(edit), dict(edit), {"tool": "answer"}, {"tool": "answer"}]
        s2.texts = [self.BLOCK.replace("LIMIT = 3", "LIMIT = 4").replace("=======\nLIMIT = 4", "=======\nLIMIT = 5"),
                    self.BLOCK.replace("LIMIT = 3", "LIMIT = 5").replace("=======\nLIMIT = 4", "=======\nLIMIT = 6"), "Done."]
        s2.agent.turn("make the limit 6 in src/calc.py", can_edit=True)
        self.assertIn("LIMIT = 6", calc.read_text())
        self.assertFalse(any("changed on disk" in m["content"] for m in s2.agent.messages))

    def test_replacing_a_whole_file_that_changed_on_disk_is_refused_too(self):
        s = self.s
        calc = self.root / "src/calc.py"
        s.agent.ws.read("src/calc.py", None, None)
        calc.write_text("LIMIT = 99\n")
        with self.assertRaises(A.ToolError) as e:
            s.agent.ws.check_current(calc)
        self.assertIn("Read it again", str(e.exception))
        s.agent.ws.read("src/calc.py", None, None)
        s.agent.ws.check_current(calc)  # read again: fine
        s.agent.ws.check_current(self.root / "src/other.py")  # never read: nothing to compare with

    def test_the_question_knows_a_command_will_be_left_running(self):
        s = self.s
        asked = []
        s.agent.hooks.confirm_run = lambda c: asked.append((s.agent.asking, c)) or False
        s.actions = [{"tool": "start", "command": "npm run dev"}, {"tool": "run", "command": "npm test"}]
        s.agent.turn("start the dev server and run the tests", can_edit=False)
        self.assertEqual(asked, [("start", "npm run dev"), ("run", "npm test")])

    def test_a_plan_right_after_a_plan_is_refused(self):
        s = self.s
        s.actions = [{"tool": "plan"}, {"tool": "plan"}, {"tool": "read", "path": "src/calc.py"}, {"tool": "plan"}]
        s.texts = ["Plan A.", "Plan B.", "It is 3."]
        s.agent.turn("what is the limit, and why?", can_edit=False)
        self.assertEqual([t for t, _ in s.steps if t == "Plan"], ["Plan", "Plan"], "the second in a row wrote nothing")
        self.assertEqual([d for t, d in s.steps if t == "Plan"], ["Plan A.", "Plan B."], "one after a lookup is fine")
        self.assertTrue(any("You just wrote a plan" in m["content"] for m in s.agent.messages))

    def test_a_plan_cut_at_its_limit_ends_on_a_whole_sentence(self):
        cut = "1. Add the state to App.jsx.\n2. Add the dropdown next to the search box.\n3. Sort the stories before rende"
        self.assertEqual(A.whole_sentences(cut, 30), "1. Add the state to App.jsx.\n2. Add the dropdown next to the search box. …")
        self.assertEqual(A.whole_sentences(cut, 350), cut, "nowhere near the limit: it ended by itself")
        whole = "1. Add the state.\n2. Add the dropdown."
        self.assertEqual(A.whole_sentences(whole, 5), whole)
        self.assertEqual(A.whole_sentences("x" * 200, 30), "x" * 200, "nothing sensible to cut back to")
        # A request with several parts gets a longer plan.
        s = self.s
        asked = []
        text = s.agent._text_once
        s.agent._text_once = lambda max_tokens, stream, temperature=None, regex=None: asked.append(max_tokens) or text(max_tokens, stream, temperature, regex)
        s.actions = [{"tool": "plan"}]
        s.agent.turn("1. add a sort dropdown 2. add a footer 3. make the banner blue", can_edit=False)
        self.assertEqual(asked[0], A.PLAN_TOKENS_LONG)

    def test_a_search_step_says_how_many_results_came_back(self):
        res = ("Web results (untrusted reference text):\n- HN Search API: fields include created_at (https://hn.algolia.com/api)\n"
               "- Algolia docs: search parameters (https://www.algolia.com/doc)")
        self.assertEqual(A._summary("web_search", res), "2 results: HN Search API …")
        self.assertEqual(A._summary("web_search", "Web results (untrusted reference text):\n- Only one: x (https://a)"), "1 result: Only one")
        self.assertEqual(A._summary("web_search", "No results."), "No results.")
        again = '(Not searched again: this is nearly your earlier search "x", and the same pages would come back. Its results:)\n' + res
        self.assertIn("2 results (from your earlier search, not searched again)", A._summary("web_search", again))
        self.assertTrue(A._summary("web_search", "Error: the search engines are refusing requests").startswith("Error:"))


class EditText(Repo):
    """What comes back when the text of an edit is asked for, and isn't one."""

    BLOCK = FollowUps.BLOCK
    LOOK = ('Now I need to change the limit. Let me read the current state of the file first.\n\n'
            '{"enough": false, "missing": "the current lines", "tool": "read", "path": "src/calc.py", "start": 1, "end": 5}')

    @needs_aider
    def test_let_me_read_first_is_the_read_not_a_failed_edit(self):
        s = self.s
        edit = {"tool": "edit", "path": "src/calc.py"}
        s.actions = [{"tool": "read", "path": "src/calc.py"}, edit, edit, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = [self.LOOK, self.BLOCK, "LIMIT is now 4 (not verified)."]
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        results = [m["content"] for m in s.agent.messages if m["content"].startswith("Result of step") and "(edit)" in m["content"][:30]]
        self.assertIn("Not edited yet", results[0])
        self.assertIn("src/calc.py lines 1-", results[0], "the lines it asked for came back in the same step")
        self.assertNotIn("Error", results[0])
        self.assertFalse(any("no SEARCH/REPLACE block" in m["content"] for m in s.agent.messages))
        self.assertIn(("Update(src/calc.py)", "Not edited yet: it looked at that first"), s.steps)
        self.assertIn("LIMIT = 4", (self.root / "src/calc.py").read_text(), "and the next edit went in")
        self.assertNotIn("An edit you tried was refused", s.prompts[-1][0])
        self.assertEqual(s.agent.changed_session, ["src/calc.py"])

    @needs_aider
    def test_looking_first_is_not_a_change(self):
        s = self.s
        s.actions = [{"tool": "read", "path": "src/calc.py"}, {"tool": "edit", "path": "src/calc.py"}, {"tool": "answer"},
                     {"tool": "answer"}, {"tool": "answer"}]
        s.texts = [self.LOOK, "Nothing was changed."]
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        self.assertEqual(s.agent.changed_session, [])
        self.assertIn("Files you changed in this request: none", s.prompts[-1][0])
        self.assertTrue(s.agent.pending, "the change is still owed")

    @needs_aider
    def test_other_text_without_a_block_is_still_an_error(self):
        s = self.s
        s.actions = [{"tool": "read", "path": "src/calc.py"}, {"tool": "edit", "path": "src/calc.py"}]
        s.texts = ['I will change it.\n{"enough": true, "tool": "answer"}']  # not a lookup: nothing to run instead
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        self.assertTrue(any("no SEARCH/REPLACE block" in m["content"] for m in s.agent.messages))

    def test_a_new_file_is_never_a_sentence_and_an_action(self):
        s = self.s
        look = 'Let me check the folder first.\n{"enough": false, "missing": "what is there", "tool": "list", "path": "src"}'
        s.actions = [{"tool": "create", "path": "src/new.py"}] * 4  # the first one is answered with a plan
        s.texts = ["Plan: create src/new.py.", look, 'I am done.\n{"enough": true, "tool": "answer"}', "```python\nX = 1\n```"]
        s.agent.turn("create src/new.py with X = 1", can_edit=True)
        results = [m["content"] for m in s.agent.messages if m["content"].startswith("Result of step") and "(create)" in m["content"][:32]]
        self.assertIn("Not edited yet", results[0])
        self.assertIn("calc.py", results[0], "the listing it asked for")
        self.assertIn("not the file's content", results[1])
        self.assertEqual((self.root / "src/new.py").read_text(), "X = 1\n", "only the real content was written")
        self.assertIsNone(A.misplaced_lookup("src/calc.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE"))
        self.assertIsNone(A.misplaced_lookup('{"x": 1}'))


@unittest.skipUnless(HAVE_RICH, "needs rich (run with the oppx engine's Python)")
class TypeAhead(unittest.TestCase):
    """What is typed while a turn runs: kept, queued on Enter, and shown on the spinner's line."""

    def setUp(self):
        from oppx_chat import term
        self.T = term
        term.UI.queued, term.UI.prefill, term.UI.unannounced = [], "", []

    tearDown = setUp

    def test_typing_is_kept_and_enter_queues_it(self):
        T = self.T
        w = T.EscWatcher()
        w._typed("fix the tpyo")
        self.assertEqual(T.UI.prefill, "fix the tpyo")
        w._typed("\x17typo\x7f\x7fpo too\r")  # Ctrl-W, two backspaces, Enter
        self.assertEqual((T.UI.queued, T.UI.prefill), (["fix the typo too"], ""))
        self.assertEqual(T.UI.unannounced, ["fix the typo too"], "to be shown as queued")
        w._typed("\r  \r")  # an empty line queues nothing
        self.assertEqual(len(T.UI.queued), 1)
        w._typed("half a tho\x15and then")  # Ctrl-U
        self.assertEqual(T.UI.prefill, "and then", "left for the next prompt")
        # A character cut in two by the read is not lost.
        euro = "€".encode()
        self.assertEqual(w._decode(euro[:1]) + w._decode(euro[1:]), "€")

    def test_the_spinner_line_shows_it_and_never_wraps(self):
        T = self.T
        base = "(12s · esc to interrupt"
        self.assertEqual(T.ahead(base, "", 0, 100), (base + " · type to queue a message)", ""))
        self.assertEqual(T.ahead(base, "also add a footer", 0, 100), (base + ")", "also add a footer"))
        self.assertEqual(T.ahead(base, "", 2, 100), (base + " · 2 queued)", ""))
        for width in (120, 80, 60, 40, 20, 8):
            for typed in ("", "x" * 300, "宽字符" * 40):
                status, shown = T.ahead(base, typed, 3, width)
                used = T.cell_len(status) + (T.cell_len(shown) + 5 if shown else 0)
                self.assertLessEqual(used, max(width - 2, 1), (width, typed[:5]))
        status, shown = T.ahead(base, "the end is what matters", 0, 50)
        self.assertTrue(shown.startswith("…") and shown.endswith("matters"), "the end of the line is what is being typed")


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "src").mkdir()
        (root / "docs").mkdir()
        (root / "src/server.rs").write_text("pub const LIMIT: u32 = 3;\n\npub fn start_server(port: u16) -> u16 {\n    port\n}\n\nstruct Preflight;\n")
        (root / "src/main.py").write_text("def startServer():\n    pass\n\nclass Thing:\n    def limit(self):\n        return 3\n")
        (root / "docs/guide.md").write_text("start_server starts the server\n" * 50)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        self.ws = A.Workspace(str(root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_grep_shows_code_before_docs_and_stays_small(self):
        out = self.ws.grep("start_server")
        self.assertTrue(out.startswith("51 matches in 2 files"))
        self.assertLess(out.index("src/server.rs"), out.index("docs/guide.md"))
        self.assertLess(len(out), 1200)
        self.assertIn("more in this file", out)

    def test_find_matches_any_spelling(self):
        for query in ("start server", "startServer", "start_server", "START_SERVER"):
            out = self.ws.find(query)
            self.assertIn("src/server.rs:3", out, query)
            self.assertIn("src/main.py:1", out, query)
        self.assertIn("src/server.rs:7", self.ws.find("preflight"))
        self.assertIn("No definition or file", self.ws.find("frobnicate widget"))
        self.assertIn("src/server.rs", self.ws.find("server"))  # file names too

    def test_a_wrong_path_names_the_right_one(self):
        with self.assertRaises(A.ToolError) as e:
            self.ws.read("server.rs", 1, 5)
        self.assertIn("did you mean: src/server.rs", str(e.exception))

    def test_related_definitions_and_matches_elsewhere(self):
        root = Path(self.tmp.name)
        (root / "docker").mkdir()
        (root / "docker/gateway.py").write_text("PAIRING_TTL_S = 600\n\n\ndef validate_name(n):\n    return n\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        ws = A.Workspace(str(root))
        related = ws.related("Pairing codes should stay valid for 15 minutes")
        self.assertIn("docker/gateway.py:1: PAIRING_TTL_S = 600", related)
        self.assertNotIn("validate_name", related)  # "valid" is not a part of that name
        self.assertEqual(ws.related("what is this?"), "")
        out = ws.grep("PAIRING_TTL", "src")
        self.assertIn("No matches", out)
        self.assertIn("Outside src: docker/gateway.py (1)", out)

    def test_dot_folders_can_be_opened(self):
        root = Path(self.tmp.name)
        (root / ".github").mkdir()
        (root / ".github/ci.yml").write_text("name: ci\n")
        for path in (".github/ci.yml", "./.github/ci.yml", "/.github/ci.yml"):
            self.assertIn("name: ci", self.ws.read(path, 1, 5), path)
        self.assertIn("src/server.rs", self.ws.list("./src"))
        self.assertIn("src/server.rs", self.ws.list("."))
        with self.assertRaises(A.ToolError):
            self.ws.read(".git/config", 1, 5)

    def test_each_tool_has_its_own_schema(self):
        schema = A.action_schema(("grep", "read", "answer"))
        by_tool = {b["properties"]["tool"]["enum"][0]: b for b in schema["anyOf"]}
        self.assertNotIn("end", by_tool["grep"]["properties"])
        self.assertIn("pattern", by_tool["grep"]["required"])
        self.assertEqual(set(A.TOOLS), set(A.TOOL_FIELDS))
        json.dumps(schema)

    def test_reply_checks(self):
        self.assertIsNotNone(A.as_action('{"tool": "answer", "why": "x"}'))
        self.assertIsNotNone(A.as_action('```json\n{"why": "x", "tool": "grep", "pattern": "a"}\n```'))
        self.assertIsNotNone(A.as_action("{}"))
        self.assertIsNotNone(A.as_action('{"enough": false, "missing": "cut off mid'))
        self.assertIsNone(A.as_action('{"name": "demo"}'))
        self.assertIsNone(A.as_action("The answer is {x}."))
        self.assertTrue(A.maybe_json("  {"))
        self.assertTrue(A.maybe_json("```json"))
        self.assertFalse(A.maybe_json("```rust\nfn main() {}"))
        self.assertFalse(A.maybe_json("It is"))


class Commands(unittest.TestCase):
    """run and start: no terminal input, nothing left behind."""

    def setUp(self):
        self.ws = A.Workspace(tempfile.mkdtemp())
        self.addCleanup(self.ws.stop_jobs)

    def test_run_has_no_terminal_input(self):
        # `read` returns at once on an empty stdin instead of waiting for a key.
        out = self.ws.run("read x; echo got=$x; echo err >&2")
        self.assertTrue(out.startswith("exit code 0"), out)
        self.assertIn("got=", out)
        self.assertIn("err", out)

    def test_timeout_keeps_output_and_stops_children(self):
        old, A.RUN_TIMEOUT = A.RUN_TIMEOUT, 1
        self.addCleanup(setattr, A, "RUN_TIMEOUT", old)
        out = self.ws.run("echo asking; sleep 31 & echo $! > child.pid; wait")
        self.assertIn("Timed out", out)
        self.assertIn("asking", out)
        self.assertIn("start tool", out)
        pid = int((self.ws.root / "child.pid").read_text())
        time.sleep(0.2)
        self.assertFalse(os.path.exists(f"/proc/{pid}"), "the command's child outlived the timeout")

    def test_start_leaves_it_running_and_shows_output(self):
        cmd = "echo ready on 5173; sleep 30"
        out = self.ws.start(cmd)
        self.assertIn("left running", out)
        self.assertIn("ready on 5173", out)
        pid = self.ws.jobs[cmd]["proc"].pid
        again = self.ws.start(cmd)
        self.assertIn("Still running", again)
        self.assertNotIn("ready on 5173", again)  # only what is new
        self.assertEqual(self.ws.jobs[cmd]["proc"].pid, pid)
        self.ws.stop_jobs()
        self.assertFalse(self.ws.jobs)
        time.sleep(0.2)
        self.assertFalse(os.path.exists(f"/proc/{pid}"))

    def test_start_reports_a_command_that_ends(self):
        out = self.ws.start("echo no such script; exit 3")
        self.assertIn("exit code 3", out)
        self.assertIn("no such script", out)
        self.assertFalse(self.ws.jobs)

    def test_start_is_an_action(self):
        schema = json.dumps(A.action_schema(A.TOOLS))
        self.assertIn('"start"', schema)
        self.assertEqual(A._title({"tool": "start", "command": "npm run dev"}), "Start(npm run dev)")


if __name__ == "__main__":
    unittest.main()
