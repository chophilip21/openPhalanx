#!/usr/bin/env python3
"""Unit tests for the agent loop (client/oppx/frontend/oppx_chat/agent.py) with
a scripted model: no server needed. Run with any Python that has httpx:

    ~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/test_agent.py

The tools, edit text and commands are in test_agent_tools.py; documents and
pictures in test_agent_documents.py."""

import subprocess
import unittest

from agent_testkit import Repo, Scripted, needs_aider
from oppx_chat import agent as A


class AgentLoop(Repo):
    def test_the_router_answer_can_arrive_while_the_turn_sets_up(self):
        from concurrent.futures import Future
        import threading
        s = self.s
        s.actions = [{"tool": "read", "path": "src/calc.py"}, {"tool": "answer"}]
        s.texts = ["add returns a + b."]
        routed = Future()
        threading.Timer(0.2, routed.set_result, (False,)).start()  # the ask/edit check, still deciding
        reply = s.agent.turn("what does add return?", can_edit=True, wants_change=routed)
        self.assertEqual(reply, "add returns a + b.")
        self.assertTrue(routed.done())
        self.assertFalse(s.agent._wanted)  # its answer was used: a question, nothing owed
        changed = Future()
        changed.set_result(None)  # no answer from the router: assume a change, as before
        s.actions, s.texts = [{"tool": "answer"}], ["done"]
        s.agent.turn("rename add", can_edit=True, wants_change=changed)
        self.assertTrue(s.agent._wanted)

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

    def test_a_new_file_written_as_a_sentence_and_an_action_is_asked_for_again(self):
        s = self.s
        create = {"tool": "create", "path": "src/new.py"}
        s.actions = [create, create, {"tool": "answer"}, {"tool": "answer"}]  # the first one is answered with a plan
        s.texts = ["Plan: create src/new.py.",
                   'I\'ll create the files for this step.\n\n{"enough": true, "tool": "create", "path": "src/new.py"}',
                   "```python\nLIMIT = 4\n```", "Created src/new.py."]
        s.agent.turn("create src/new.py with LIMIT = 4", can_edit=True)
        self.assertEqual((self.root / "src/new.py").read_text(), "LIMIT = 4\n")
        self.assertEqual([t for t, _ in s.steps].count("Create(src/new.py)"), 1)  # one step, not a failed one and a retry

    def test_a_code_block_holding_only_the_file_name_is_not_the_file(self):
        self.assertEqual(A._fenced("```\nsrc/new.py\n```\n```python\nX = 1\n```", "src/new.py"), "X = 1\n")
        self.assertIsNone(A._fenced("```\nnew.py\n```", "src/new.py"))
        self.assertEqual(A._fenced("```\nsrc/new.py\n```"), "src/new.py\n")  # no path given: as before
        s = self.s
        create = {"tool": "create", "path": "src/new.py"}
        s.actions = [create, create, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = ["Plan: create src/new.py.", '{"enough": false, "tool": "create", "path": "src/new.py"}',
                   '```\nsrc/new.py\n```\n\n{"enough": false, "tool": "create", "path": "src/new.py"}',
                   "Created src/new.py."]
        s.agent.turn("create src/new.py with X = 1", can_edit=True)
        self.assertFalse((self.root / "src/new.py").exists(), "the name was never written as the file")
        self.assertTrue(any("not its name" in p for p, _ in s.prompts))

    @needs_aider
    def test_a_file_it_wrote_whole_can_be_edited_without_reading_it_first(self):
        s = self.s
        create, edit = {"tool": "create", "path": "src/new.py"}, {"tool": "edit", "path": "src/new.py"}
        s.actions = [create, create, edit, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = ["Plan: create src/new.py.", "```python\nLIMIT = 3\n```",
                   "src/new.py\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE", "Done."]
        s.agent.turn("create src/new.py with LIMIT = 3, then make it 4", can_edit=True)
        self.assertEqual((self.root / "src/new.py").read_text(), "LIMIT = 4\n")
        # Changed since it was written whole: its text in the conversation is old, so a read comes first again.
        self.assertFalse(s.agent._has_read("src/new.py"))

    @needs_aider
    def test_an_edit_that_repeats_one_line_is_asked_for_again_and_not_kept(self):
        s = self.s
        edit = {"tool": "edit", "path": "src/calc.py"}
        s.actions = [{"tool": "read", "path": "src/calc.py"}, edit, edit, {"tool": "answer"}, {"tool": "answer"}]
        junk = "<tool_call>\n\n" * 300
        s.texts = ["Plan: change LIMIT in src/calc.py.", junk,
                   "src/calc.py\n```\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE\n```", "LIMIT is now 4."]
        s.agent.turn("make the limit 4 in src/calc.py", can_edit=True)
        self.assertIn("LIMIT = 4", (self.root / "src/calc.py").read_text())
        regexes = [r for _, r in s.prompts if r]
        self.assertTrue(regexes and regexes[-1].startswith("src/calc\\.py\\n```"), regexes)
        self.assertFalse(any("<tool_call>" in m["content"] for m in s.agent.messages), "the junk was not kept")

    def test_a_blockless_edit_reply_is_kept_short(self):
        self.assertTrue(A.degenerate("<tool_call>\n\n" * 30))
        self.assertFalse(A.degenerate("x = 1\n" * 5))
        self.assertFalse(A.degenerate("\n".join(f"line {i}" for i in range(40))))

    def test_lines_shown_only_because_the_file_was_small_can_be_read_once(self):
        s = self.s
        big = "\n".join(f"line {i}" for i in range(1, 201)) + "\n"
        (self.root / "src/big.py").write_text(big)
        part = {"tool": "read", "path": "src/big.py", "start": 150, "end": 200}
        s.actions = [{"tool": "read", "path": "src/big.py", "start": 1, "end": 40}, part, part, {"tool": "answer"}]
        s.texts = ["Done."]
        s.agent.turn("what is on line 180 of the big module?", can_edit=False)
        results = [m["content"] for m in s.agent.messages if m["content"].startswith("Result of step")]
        self.assertIn("src/big.py lines 1-200", results[0])      # shown whole for a read of 40 lines
        self.assertIn("line 180", results[1])                      # the lines it never asked for: read once
        self.assertIn("already shown", results[2])                 # the same read again is a repeat

    def test_a_file_read_long_ago_may_be_read_once_more(self):
        s = self.s
        read = {"tool": "read", "path": "src/calc.py"}
        s.actions = ([read] + [{"tool": "grep", "pattern": f"x{i}"} for i in range(A.FAR_BACK)]
                     + [dict(read), dict(read), {"tool": "answer"}])
        s.texts = ["Nothing there.", "Done."]
        s.agent.turn("what is LIMIT?", can_edit=False)
        results = [m["content"] for m in s.agent.messages if m["content"].startswith("Result of step")]
        reads = [r for r in results if "(read)" in r.split("\n", 1)[0]]
        self.assertIn("LIMIT = 3", reads[1])           # far back: read again, once
        self.assertIn("already did exactly this", reads[2])  # right after: a repeat

    def test_a_small_file_too_long_to_show_whole_shows_the_lines_asked_for(self):
        s = self.s
        wide = "\n".join(f"value_{i} = {'x' * 70}" for i in range(1, 273)) + "\n"  # 272 lines, over a result's room
        (self.root / "src/wide.py").write_text(wide)
        tail = {"tool": "read", "path": "src/wide.py", "start": 205, "end": 272}
        s.actions = [tail, {"tool": "read", "path": "src/wide.py", "start": 1, "end": 272}, tail, {"tool": "answer"}]
        s.texts = ["Done."]
        s.agent.turn("what is the last value?", can_edit=False)
        results = [m["content"] for m in s.agent.messages if m["content"].startswith("Result of step")]
        self.assertIn("src/wide.py lines 199-272 of 272", results[0])  # not the whole file, cut at line 209
        self.assertIn("value_272 =", results[0])
        self.assertNotIn("(cut; ask for less)", results[0])
        head, = (r for r in results[1:2])
        last = int(head.split(" lines 1-", 1)[1].split(" ", 1)[0])
        self.assertLess(last, 272)  # as many whole lines as fit, and the header says where it stopped
        self.assertIn(f"(lines {last + 1}-272 not shown)", head)
        self.assertIn("already shown", results[2])  # 205-272 really was shown

    def test_lines_a_read_could_not_fit_can_be_read_next(self):
        s = self.s
        wide = "\n".join(f"value_{i} = {'x' * 70}" for i in range(1, 273)) + "\n"
        (self.root / "src/wide.py").write_text(wide)
        s.actions = [{"tool": "read", "path": "src/wide.py", "start": 1, "end": 272},
                     {"tool": "read", "path": "src/wide.py", "start": 200, "end": 272}, {"tool": "answer"}]
        s.texts = ["Done."]
        s.agent.turn("what is the last value?", can_edit=False)
        results = [m["content"] for m in s.agent.messages if m["content"].startswith("Result of step")]
        self.assertNotIn("already shown", results[1])
        self.assertIn("value_272 =", results[1])

    @needs_aider
    def test_a_file_written_after_its_action_is_used(self):
        s = self.s
        create = {"tool": "create", "path": "src/new.py"}
        s.actions = [create, create, {"tool": "answer"}, {"tool": "answer"}]
        s.texts = ["Plan: create src/new.py.",
                   '{"tool": "create", "path": "src/new.py"}\n```python\nX = 1\n```', "Created src/new.py."]
        s.agent.turn("create src/new.py with X = 1", can_edit=True)
        self.assertEqual((self.root / "src/new.py").read_text(), "X = 1\n")
        self.assertFalse(any(r for _, r in s.prompts), "used as it came: no constrained second request")

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

    def test_a_cd_into_a_folder_that_does_not_exist_is_not_asked_about(self):
        s = self.s
        asked = []
        s.agent.hooks.confirm_run = lambda c: asked.append(c) or True
        s.actions = [{"tool": "run", "command": "cd /testbed && python -m calc --help"},
                     {"tool": "run", "command": "cd src && ls"}, {"tool": "answer"}]
        s.texts = ["Done."]
        s.agent.turn("check the cli", can_edit=False)
        self.assertEqual(asked, ["cd src && ls"])  # the project's own folder is fine
        self.assertTrue(any("there is no folder /testbed" in m["content"] and str(self.root) in m["content"]
                            for m in s.agent.messages))

    def test_a_command_cut_at_an_unescaped_quote_is_explained_not_run(self):
        s = self.s
        asked = []
        s.agent.hooks.confirm_run = lambda c: asked.append(c) or True
        cut = "python3 -c 'import re; p = re.compile(r"  # the model's bare " ended the JSON string here
        s.actions = [{"tool": "run", "command": cut}, {"tool": "run", "command": "python3 -c 'print(\"ok\")'"},
                     {"tool": "run", "command": "python3 - <<'EOF'\nprint(\"it's\")\nEOF"}, {"tool": "answer"}]
        s.texts = ["Done."]
        s.agent.turn("check the regex", can_edit=False)
        self.assertEqual(len(asked), 2)  # the whole commands, and the heredoc with a lone apostrophe in it
        self.assertNotIn(cut, asked)
        self.assertTrue(any('as \\" (a backslash before it)' in m["content"] for m in s.agent.messages))

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

    def test_a_command_printing_the_same_thing_again_with_no_edit_is_a_repeat(self):
        s = self.s
        ran = []
        s.agent.hooks.confirm_run = lambda c: ran.append(c) or True
        grep = {"tool": "run", "command": "echo 'test_a FAILED'"}  # exits 0, like pytest piped into grep
        s.actions = [dict(grep) for _ in range(12)] + [{"tool": "answer"}]
        s.texts = ["Rethinking."] * 4 + ["Stuck."]
        s.agent.turn("make the tests pass", can_edit=False)
        self.assertLess(len(ran), 8)  # not until the steps run out
        self.assertTrue(s.agent.stuck)
        self.assertTrue(any("exactly what this command printed at step" in m["content"] for m in s.agent.messages))

    def test_a_command_whose_output_changed_is_not_a_repeat(self):
        s = self.s
        out = self.root / "n"
        out.write_text("0")
        s.agent.hooks.confirm_run = lambda c: True
        poll = {"tool": "run", "command": f"cat {out} && echo $(( $(cat {out}) + 1 )) > {out}"}
        s.actions = [dict(poll) for _ in range(4)] + [{"tool": "answer"}]
        s.texts = ["Done."]
        s.agent.turn("wait for the counter", can_edit=False)
        self.assertEqual(out.read_text().strip(), "4")
        self.assertFalse(any("exactly what this command printed" in m["content"] for m in s.agent.messages))

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

    def test_runs_after_a_change_with_no_further_edit_take_stock(self):
        s = self.s
        s.agent.hooks.confirm_run = lambda c: True
        create = {"tool": "create", "path": "src/new.py"}  # the first is where the plan comes in
        s.actions = ([create, dict(create)] + [{"tool": "run", "command": f"echo {i}"} for i in range(12)]
                     + [{"tool": "answer"}, {"tool": "answer"}])
        s.texts = ["Plan: create src/new.py.", "```python\nLIMIT = 3\n```", "The tests fail at src/new.py:1.", "Done."]
        s.agent.turn("create src/new.py with LIMIT = 3 and test it", can_edit=True)
        titles, CHECK = [t for t, _ in s.steps], A.CHECK_EVERY - 1
        self.assertEqual(titles.count("Taking stock"), 1)
        self.assertGreater(titles.index("Taking stock"), titles.index("Create(src/new.py)") + CHECK)  # after a change

    def test_a_plan_stands_for_the_stock_take_that_was_due(self):
        s = self.s
        s.actions = [{"tool": "grep", "pattern": f"x{i}"} for i in range(9)] + [{"tool": "plan"},
                     {"tool": "grep", "pattern": "y"}, {"tool": "answer"}]
        s.texts = ["Plan: look for y.", "Nothing found."]
        s.agent.turn("where is it?", can_edit=True, wants_change=False)
        self.assertNotIn("Taking stock", [t for t, _ in s.steps])

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
        # The edit was taken as the cue for a plan first: the step after the plan is still one of doing.
        self.assertEqual([t for t, _ in s.steps][-1], "Plan")
        self.assertEqual(set(offered[4]), {"edit", "create", "run", "start"})

    def test_a_forced_change_that_plans_first_is_still_forced_after_the_plan(self):
        s = self.s
        offered = []
        action = s.agent._action
        s.agent._action = lambda tools: offered.append(tools) or action(tools)
        create = {"tool": "create", "path": "src/digest.py"}
        s.actions = [{"tool": "read", "path": "src/calc.py"}] + [{"tool": "grep", "pattern": f"x{i}"} for i in range(6)] + [
            {"tool": "answer"}, {"tool": "answer"}, create, dict(create), {"tool": "answer"}]
        s.texts = ["Plan: create src/digest.py.", "```python\nDIGEST = 10\n```", "Done."]
        s.agent.turn("add a digest module", can_edit=True, wants_change=True)
        titles = [t for t, _ in s.steps]
        # Ten calls in at the plan: the stock-take was due, and the plan stood for it.
        self.assertEqual(titles[-3:], ["Grep(x5)", "Plan", "Create(src/digest.py)"])
        self.assertEqual(set(offered[10]), {"edit", "create", "run", "start"})  # the step after the plan
        self.assertEqual((self.root / "src/digest.py").read_text(), "DIGEST = 10\n")

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


if __name__ == "__main__":
    unittest.main()
