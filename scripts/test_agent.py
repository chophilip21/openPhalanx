#!/usr/bin/env python3
"""Unit tests for the agent loop (client/oppx/frontend/oppx_chat/agent.py) with
a scripted model: no server needed. Run with any Python that has httpx:

    ~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/test_agent.py
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client/oppx/frontend"))

from oppx_chat import agent as A  # noqa: E402


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


class AgentLoop(unittest.TestCase):
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

    def test_plan_mode_has_no_edit_tools(self):
        s = self.s
        s.actions = [{"tool": "edit", "path": "src/calc.py"}]
        s.texts = ["I can't edit in plan mode."]
        s.agent.turn("make the limit 4", can_edit=False)
        self.assertIn("LIMIT = 3", (self.root / "src/calc.py").read_text())

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
        self.assertIsNotNone(A.as_action('{"why": "cut off mid'))
        self.assertIsNone(A.as_action('{"name": "demo"}'))
        self.assertIsNone(A.as_action("The answer is {x}."))
        self.assertTrue(A.maybe_json("  {"))
        self.assertTrue(A.maybe_json("```json"))
        self.assertFalse(A.maybe_json("```rust\nfn main() {}"))
        self.assertFalse(A.maybe_json("It is"))


if __name__ == "__main__":
    unittest.main()
