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


try:  # edits are applied with Aider's matcher; CI runs without it
    import aider  # noqa: F401
    HAVE_AIDER = True
except ImportError:
    HAVE_AIDER = False
needs_aider = unittest.skipUnless(HAVE_AIDER, "needs aider-chat (run with the oppx engine's Python)")


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


if __name__ == "__main__":
    unittest.main()
