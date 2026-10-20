#!/usr/bin/env python3
"""Unit tests for the agent's tools, the text of edits and new files, typing
ahead and the slash commands (scripted model, no server). Run like test_agent.py."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agent_testkit import HAVE_RICH, Repo, Scripted, needs_aider
from oppx_chat import agent as A, workspace as W


class ConfirmedGaps(Repo):
    """CHO-10: gaps that were checked against the code and closed."""

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
        # Words and an answer instead of the file: asked again in the same step, the code block first.
        self.assertIn("Created src/new.py", results[1])
        self.assertIn(("```[\\s\\S]*"), [regex for _, regex in s.prompts])
        self.assertEqual((self.root / "src/new.py").read_text(), "X = 1\n", "only the real content was written")
        self.assertIsNone(A.misplaced_lookup("src/calc.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE"))
        self.assertIsNone(A.misplaced_lookup('{"x": 1}'))


class SpacingSlips(unittest.TestCase):
    """A SEARCH that differs from the file only in the spaces at a line's ends."""

    FILE = ('_COLS = ("id", "title",\n         "prefilter", "llm")\n\n\nclass Store:\n    pass\n')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "store.py").write_text(self.FILE)
        self.ws = A.Workspace(str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    @needs_aider
    def test_one_line_off_by_a_space_still_applies_and_keeps_the_files_spacing(self):
        reply = ('store.py\n<<<<<<< SEARCH\n_COLS = ("id", "title",\n          "prefilter", "llm")\n=======\n'
                 '_COLS = ("id", "title",\n          "prefilter", "llm", "source")\n>>>>>>> REPLACE\n')
        A.apply_edit(self.ws, "store.py", reply)
        self.assertEqual((self.root / "store.py").read_text(),
                         self.FILE.replace('"prefilter", "llm")', '"prefilter", "llm", "source")', 1)
                         .replace('         "prefilter", "llm", "source")', '          "prefilter", "llm", "source")'))

    @needs_aider
    def test_an_unchanged_line_keeps_the_files_spacing(self):
        reply = ('store.py\n<<<<<<< SEARCH\n_COLS = ("id", "title",\n          "prefilter", "llm")\n=======\n'
                 '# the columns\n_COLS = ("id", "title",\n          "prefilter", "llm")\n>>>>>>> REPLACE\n')
        A.apply_edit(self.ws, "store.py", reply)
        self.assertEqual((self.root / "store.py").read_text(), "# the columns\n" + self.FILE)

    @needs_aider
    def test_two_places_that_match_are_still_refused(self):
        (self.root / "store.py").write_text("    x = 1\n\n\n        x = 1\n")
        with self.assertRaises(A.ToolError):
            A.apply_edit(self.ws, "store.py", "store.py\n<<<<<<< SEARCH\n  x = 1\n=======\nx = 2\n>>>>>>> REPLACE\n")


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


class SymbolIndex(Repo):
    def test_built_once_while_warming(self):
        import threading
        ws = A.Workspace(str(self.root))
        builds = []
        real = ws._build_symbols

        def slow():
            builds.append(1)
            time.sleep(0.2)
            return real()
        ws._build_symbols = slow
        ws.warm()
        time.sleep(0.05)  # the warm-up is building
        results = []
        callers = [threading.Thread(target=lambda: results.append(ws.symbols())) for _ in range(3)]
        for t in callers:
            t.start()
        for t in callers:
            t.join()
        self.assertEqual(len(builds), 1)  # the callers waited for it instead of building again
        self.assertTrue(all(r is results[0] for r in results))
        self.assertIn("add", {name for name, *_ in results[0]})


class LinearHelpers(unittest.TestCase):
    """String code that replaced regexes which backtracked (scripts/check_regexes.py)."""

    def test_action_tail(self):
        for text, kept in (('Plan:\n1. do x\n\n{"tool": "read", "path": "a"}', "Plan:\n1. do x"),
                           ('Plan\n```json\n{"enough": true}\n```', "Plan"), ("no action here", "no action here"),
                           ('Text\n\n\n  {"missing": "x"}', "Text")):
            self.assertEqual(A.cut_action_tail(text), kept)

    def test_outer_braces_and_fences(self):
        self.assertEqual(A._outer_braces('x {"a": {"b": 1}} y'), '{"a": {"b": 1}}')
        self.assertIsNone(A._outer_braces("} {"))
        self.assertEqual(A._fenced("a\n```py\nx\ny\n```\nb"), "x\ny\n")
        self.assertIsNone(A._fenced("```py\nunclosed"))
        self.assertIsNone(A._fenced("no fence"))
        long = "{" * 200_000  # quadratic before: seconds; now one pass
        t = time.monotonic()
        self.assertIsNone(A._outer_braces(long))
        self.assertIsNone(A._fenced("```" * 100_000))
        self.assertLess(time.monotonic() - t, 0.5)


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
        old, W.RUN_TIMEOUT = W.RUN_TIMEOUT, 1
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
