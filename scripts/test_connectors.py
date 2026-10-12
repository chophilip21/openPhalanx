#!/usr/bin/env python3
"""Tests for connectors (MCP servers as agent tools): the MCP client in
client/oppx/frontend/oppx_chat/mcp.py against a small stdio server written
here, and the agent loop calling a connector tool with a scripted model. No
GPU server and no network needed. Run with any Python that has httpx:

    ~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/test_connectors.py
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client/oppx/frontend"))

from oppx_chat import agent as A  # noqa: E402
from oppx_chat import browser  # noqa: E402
from oppx_chat import mcp  # noqa: E402

# An MCP server over stdio: two pages of tools, a note board in a file.
SERVER = r'''
import json, os, sys, time
NOTES = os.environ["NOTES_FILE"]
TOOLS = [
    {"name": "add_note", "description": "Save a note. With a title and a body.",
     "inputSchema": {"$schema": "x", "type": "object", "properties": {"title": {"type": "string"},
                     "body": {"type": "string"}}, "required": ["title", "body"]}},
    {"name": "list_notes", "description": "List the titles of all notes.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "slow", "description": "Sleeps.", "inputSchema": {"type": "object",
     "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
]
print("starting", file=sys.stderr, flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        continue
    if method == "initialize":
        res = {"protocolVersion": params["protocolVersion"], "capabilities": {}, "serverInfo": {"name": "t", "version": "1"}}
    elif method == "tools/list":
        res = {"tools": TOOLS[2:]} if params.get("cursor") else {"tools": TOOLS[:2], "nextCursor": "2"}
    elif method == "tools/call":
        name, a = params["name"], params.get("arguments") or {}
        if os.path.exists(NOTES + ".locked"):
            res = {"content": [{"type": "text", "text": "401 Unauthorized: the access token has expired"}], "isError": True}
        elif name == "add_note":
            open(NOTES, "a").write(a["title"] + "\n")
            res = {"content": [{"type": "text", "text": "Saved " + a["title"]}]}
        elif name == "list_notes":
            res = {"content": [{"type": "text", "text": open(NOTES).read() if os.path.exists(NOTES) else ""},
                               {"type": "image", "data": "", "mimeType": "image/png"}]}
        elif name == "slow":
            time.sleep(a["seconds"]); res = {"content": []}
        else:
            res = {"content": [{"type": "text", "text": "it broke"}], "isError": True}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no such method"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)
'''


class Sandbox(unittest.TestCase):
    """A scratch home for the connector files, and a repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        (self.dir / "server.py").write_text(SERVER)
        self.env = dict(os.environ)
        os.environ.update(OPPX_MCP_CONFIG=str(self.dir / "mcp.json"), XDG_STATE_HOME=str(self.dir / "state"),
                          NOTES_FILE=str(self.dir / "notes.txt"))
        self.hubs = []

    def tearDown(self):
        for h in self.hubs:
            h.close()
        os.environ.clear()
        os.environ.update(self.env)
        self.tmp.cleanup()

    def notes(self, **extra) -> dict:
        return {"command": sys.executable, "args": [str(self.dir / "server.py")], **extra}

    def write(self, mine: dict, theirs: dict | None = None):
        (self.dir / "mcp.json").write_text(json.dumps({"mcpServers": mine}))
        if theirs is not None:
            (self.repo / mcp.PROJECT_FILE).write_text(json.dumps({"mcpServers": theirs}))

    def hub(self, **kw) -> mcp.Hub:
        hub = mcp.Hub(self.repo)
        self.hubs.append(hub)
        hub.connect(**kw)
        return hub


class Client(Sandbox):
    def test_connects_lists_every_page_and_calls(self):
        self.write({"notes": self.notes()})
        hub = self.hub()
        c = hub.connectors["notes"]
        self.assertEqual((c.state, c.error), ("connected", ""))
        self.assertEqual(list(hub.tools()), ["notes.add_note", "notes.list_notes", "notes.slow", "notes.fail"])
        self.assertEqual(hub.names(read_only=True), ("notes.list_notes",), "plan mode: only what says it reads")
        self.assertEqual(hub.call("notes.add_note", {"title": "T", "body": "B"}), "Saved T")
        self.assertEqual(hub.call("notes.list_notes", {}), "T\n\n[image: not shown]")
        with self.assertRaisesRegex(mcp.McpError, "it broke"):
            hub.call("notes.fail", {})
        with self.assertRaisesRegex(mcp.McpError, "isn't connected"):
            hub.call("notes.nope", {})

    def test_the_prompt_lists_one_line_per_tool(self):
        self.write({"notes": self.notes()})
        hub = self.hub()
        section = hub.prompt_section()
        self.assertIn('- {"tool": "notes.add_note"}: Save a note.\n', section, "the first sentence only")
        self.assertNotIn("inputSchema", section)
        self.assertEqual(hub.tokens(), len(section) // 3)
        # The schema is shown only for the tool being called, without "$schema".
        prompt = hub.tool_prompt("notes.add_note")
        self.assertIn('"required": ["title", "body"]', prompt)
        self.assertNotIn("$schema", prompt)
        self.assertEqual(mcp.Hub(self.dir / "empty").prompt_section(), "")

    def test_a_connector_that_fails_says_why_and_stops_nothing(self):
        os.environ.pop("NOT_SET_ANYWHERE", None)
        self.write({"notes": self.notes(), "nocmd": {"command": "no-such-command-here"},
                    "novar": {"command": "x", "env": {"K": "${NOT_SET_ANYWHERE}"}},
                    "dies": {"command": "sh", "args": ["-c", "echo boom >&2; exit 3"]},
                    "bad.name": self.notes(), "old": {"type": "sse", "url": "http://127.0.0.1:1/sse"}})
        hub = self.hub()
        why = {n: c.error for n, c in hub.connectors.items() if c.state == "failed"}
        self.assertEqual(hub.connectors["notes"].state, "connected")
        self.assertIn("command not found", why["nocmd"])
        self.assertIn("NOT_SET_ANYWHERE", why["novar"])
        self.assertIn("boom", why["dies"], "the last line it wrote to stderr")
        self.assertIn("letters, digits", why["bad.name"])
        self.assertIn("SSE", why["old"])
        self.assertEqual(list(hub.tools())[0], "notes.add_note")

    def test_values_come_from_the_environment(self):
        os.environ["SERVER_FILE"] = str(self.dir / "server.py")
        self.write({"notes": {"command": sys.executable, "args": ["${SERVER_FILE}"], "env": {"X": "${NOPE:-fallback}"}}})
        self.assertEqual(self.hub().connectors["notes"].state, "connected")

    def test_a_call_can_be_interrupted(self):
        self.write({"notes": self.notes()})
        hub = self.hub()
        start = time.time()
        with self.assertRaisesRegex(mcp.McpError, "interrupted"):
            hub.call("notes.slow", {"seconds": 5}, cancelled=lambda: time.time() - start > 0.3)
        self.assertLess(time.time() - start, 3)

    def test_closing_stops_the_program(self):
        self.write({"notes": self.notes()})
        hub = self.hub()
        proc = hub.connectors["notes"].client.proc
        hub.close()
        self.assertIsNotNone(proc.wait(timeout=5))

    def test_a_repositorys_connector_waits_for_consent(self):
        self.write({"notes": self.notes()}, {"notes": {"command": "evil"}, "proj": self.notes()})
        hub = self.hub()
        self.assertEqual(hub.connectors["notes"].definition.scope, "user", "a repository can't replace yours")
        self.assertEqual(hub.connectors["proj"].state, "waiting")
        self.assertNotIn("proj.add_note", hub.tools())
        asked = []
        hub.connect(ask=lambda d: asked.append(d.name) or True)
        self.assertEqual((asked, hub.connectors["proj"].state), (["proj"], "connected"))
        hub.connect(ask=lambda d: self.fail("agreed already"))
        self.assertEqual(hub.connectors["proj"].state, "connected")
        # Another command under the same name is a new question.
        self.write({}, {"proj": self.notes(env={"A": "1"})})
        hub.connect()
        self.assertEqual(hub.connectors["proj"].state, "waiting")
        hub.connect(ask=lambda d: False)
        self.assertEqual(hub.connectors["proj"].state, "waiting")

    def test_a_broken_file_is_reported_not_raised(self):
        (self.dir / "mcp.json").write_text("{not json")
        hub = self.hub()
        self.assertEqual(hub.connectors, {})
        self.assertIn("can't read", hub.error)

    def test_warns_about_small_windows_and_heavy_lists(self):
        small = mcp.cost_warning(1200, 32768, adding=True)
        self.assertIn("small context window (32k tokens)", small)
        self.assertIn("1,200 tokens", small)
        self.assertEqual(mcp.cost_warning(1200, 32768, adding=False), "", "not at every start")
        self.assertEqual(mcp.cost_warning(1200, 131072, adding=True), "")
        self.assertIn("15.3%", mcp.cost_warning(20000, 131072, adding=False))
        self.assertIn("small", mcp.cost_warning(4000, 32768, adding=False), "a heavy list, whenever")
        self.assertEqual(mcp.cost_warning(0, 32768, adding=True), "")


class Recovery(Sandbox):
    """A connector that stops working: why, how to get it back, and getting it back."""

    def definition(self, spec: dict, name: str = "linear") -> mcp.ServerDef:
        return mcp.ServerDef(name, "user", spec)

    def test_says_what_went_wrong_and_how_to_fix_it(self):
        remote = self.definition({"command": "npx", "args": ["-y", "mcp-remote", "https://mcp.linear.app/mcp"]})
        # mcp-remote answers nothing while it waits for the login in the browser.
        kind, steps = mcp.diagnose(remote, "it didn't start within 30 s")
        self.assertEqual(kind, "auth")
        self.assertIn("/mcp reconnect linear", steps[0])
        self.assertIn("login in the browser", steps[0])
        self.assertIn("~/.mcp-auth", steps[1])
        self.assertIn("oppx -c", steps[1])
        self.assertEqual(mcp.diagnose(remote, "no answer within 120 s")[0], "auth")
        # A token in a header, read from the environment: say which variable.
        http = self.definition({"url": "https://mcp.linear.app/mcp", "headers": {"Authorization": "Bearer ${LINEAR_API_KEY}"}})
        kind, steps = mcp.diagnose(http, "it refused the request (HTTP 401): it needs authorization.")
        self.assertEqual(kind, "auth")
        self.assertIn("export LINEAR_API_KEY=", steps[0])
        self.assertIn("--force", steps[1])
        # The other ways it goes wrong.
        self.assertEqual(mcp.diagnose(remote, "command not found: npx")[0], "missing")
        self.assertIn("Node.js", mcp.diagnose(remote, "command not found: npx")[1][0])
        local = self.definition(self.notes(), "notes")
        self.assertEqual(mcp.diagnose(local, "the connector exited (Traceback …)")[0], "gone")
        self.assertIn("mcp-notes.log", mcp.diagnose(local, "the connector exited")[1][1])
        self.assertEqual(mcp.diagnose(http, "can't reach https://mcp.linear.app/mcp: ConnectError")[0], "network")
        # A tool that refuses its arguments is not a connector that is down.
        self.assertEqual(mcp.diagnose(local, "Invalid arguments: the login field is required")[0], "other")
        self.assertEqual(mcp.diagnose(local, "no answer within 120 s")[0], "slow")

    def test_a_connector_whose_program_stopped_is_started_again_for_the_call(self):
        self.write({"notes": self.notes()})
        hub = self.hub()
        hub.connectors["notes"].client.proc.kill()
        hub.connectors["notes"].client.proc.wait()
        time.sleep(0.3)
        self.assertIn("Saved Deploy", hub.call("notes.add_note", {"title": "Deploy", "body": "x"}))
        self.assertEqual(hub.connectors["notes"].state, "connected")

    def test_a_lost_login_takes_the_connector_down_with_the_way_back(self):
        self.write({"notes": self.notes()})
        hub = self.hub()
        lock = Path(os.environ["NOTES_FILE"] + ".locked")
        lock.write_text("")
        with self.assertRaises(mcp.ConnectorDown) as e:
            hub.call("notes.add_note", {"title": "Deploy", "body": "x"})
        self.assertEqual((e.exception.name, e.exception.kind), ("notes", "auth"))
        self.assertIn("/mcp reconnect notes", " ".join(e.exception.steps))
        self.assertEqual(hub.connectors["notes"].state, "failed")
        self.assertNotIn("notes.add_note", hub.tools())  # no longer offered to the model
        why, steps = hub.connectors["notes"].recovery()
        self.assertEqual(why, "it needs you to log in again")
        # Once the user has logged in again, one reconnect brings it back.
        lock.unlink()
        self.assertEqual(hub.reconnect("notes").state, "connected")
        self.assertIn("Saved Deploy", hub.call("notes.add_note", {"title": "Deploy", "body": "x"}))
        self.assertIsNone(hub.reconnect("nothing-by-that-name"))
        # A tool's own failure leaves the connector alone.
        with self.assertRaises(mcp.McpError) as e:
            hub.call("notes.fail", {})
        self.assertNotIsInstance(e.exception, mcp.ConnectorDown)
        self.assertEqual(hub.connectors["notes"].state, "connected")


class InTheAgent(Sandbox):
    """The agent picks a connector tool by name, then writes its arguments."""

    def agent(self, actions: list, arguments: list, allow=True):
        self.write({"notes": self.notes()})
        hub = self.hub()
        self.asked, self.steps, self.bodies = [], [], []
        hooks = A.Hooks(step=lambda t, d: self.steps.append((t, d)),
                        confirm_tool=lambda name, args, ro: self.asked.append((name, json.loads(args), ro)) or allow)
        ag = A.Agent(ws=A.Workspace(str(self.repo)), hooks=hooks, connectors=hub)

        def post(body):
            self.bodies.append(body)
            kind = body["response_format"]["json_schema"]["name"]
            done = {"why": "done", "tool": "answer"}  # once the script runs out (the loop may ask twice)
            text = arguments.pop(0) if kind == "arguments" else (actions.pop(0) if actions else done)
            return {"choices": [{"message": {"content": text if isinstance(text, str) else json.dumps(text)}}]}

        ag._post = post
        ag._text = lambda *a, **k: "All done."
        return ag

    def test_a_connector_that_goes_down_is_reported_with_the_steps(self):
        ag = self.agent([{"why": "save", "tool": "notes.add_note"}, {"why": "again", "tool": "notes.add_note"},
                         {"why": "done", "tool": "answer"}], [{"title": "Deploy", "body": "Friday"}, {"title": "Deploy", "body": "Friday"}])
        notices, prompts = [], []
        ag.hooks.notice = notices.append
        ag._text = lambda *a, **k: prompts.append(ag.messages[-1]["content"]) or "The notes connector stopped working."
        Path(os.environ["NOTES_FILE"] + ".locked").write_text("")
        ag.turn("Save a note titled Deploy")
        # The user is told at once, with numbered steps.
        self.assertEqual(len(notices), 1)
        self.assertIn("The notes connector stopped working: it needs you to log in again.", notices[0])
        self.assertIn("1. ", notices[0])
        self.assertIn("/mcp reconnect notes", notices[0])
        # The model is told not to retry, and its tools are gone: the second call never happened.
        down = [m["content"] for m in ag.messages if m["content"].startswith("Result of step") and A.CONNECTOR_DOWN in m["content"]]
        self.assertEqual(len(down), 1)
        self.assertIn("don't try it again", down[0])
        self.assertEqual(len(self.asked), 1)
        later = json.dumps(self.bodies[-1].get("response_format") or {})
        self.assertNotIn("notes.add_note", later)
        # And the reply is written with the steps to pass on.
        self.assertIn("stopped working during this request", prompts[-1])
        self.assertIn("/mcp reconnect notes", prompts[-1])
        self.assertIn("ask you to continue", prompts[-1])

    def test_a_connectors_name_as_a_shell_command_is_not_run(self):
        ag = self.agent([{"why": "save", "tool": "run", "command": "notes add_note Deploy"}, {"why": "done", "tool": "answer"}], [])
        ran = []
        ag.hooks.confirm_run = lambda c: ran.append(c) or True
        ag.turn("Save a note titled Deploy")
        self.assertEqual(ran, [])  # nobody was asked to run a program that doesn't exist
        said = [m["content"] for m in ag.messages if "is not a program here" in m["content"]]
        self.assertEqual(len(said), 1)
        self.assertIn('{"tool": "notes.add_note"}', said[0])

    def test_calls_the_tool_with_schema_constrained_arguments(self):
        ag = self.agent([{"why": "save it", "tool": "notes.add_note"}, {"why": "done", "tool": "answer"}],
                        [{"title": "Deploy", "body": "Friday"}])
        self.assertEqual(ag.turn("Save a note titled Deploy"), "All done.")
        self.assertEqual((self.dir / "notes.txt").read_text(), "Deploy\n")
        self.assertEqual(self.asked, [("notes.add_note", {"title": "Deploy", "body": "Friday"}, False)])
        # The model chose among the built-in tools and the connector's.
        first = json.dumps(self.bodies[0]["response_format"])
        self.assertIn("notes.add_note", first)
        self.assertIn("notes.list_notes", first)
        self.assertIn("Connected tools", self.bodies[0]["messages"][0]["content"])
        # The arguments were constrained by the tool's own schema.
        schema = self.bodies[1]["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["required"], ["title", "body"])
        self.assertNotIn("$schema", schema)
        self.assertIn("Now write the arguments for notes.add_note", self.bodies[1]["messages"][-1]["content"])
        # Kept in the conversation: the arguments with the result (not inside the action, which the
        # next argument call copied), and the result marked untrusted.
        said = [m["content"] for m in ag.messages]
        self.assertTrue(any('Called notes.add_note with {"title": "Deploy", "body": "Friday"}' in s for s in said))
        self.assertFalse(any(s.startswith("{") and '"arguments"' in s for s in said))
        self.assertTrue(any(A.CONNECTOR_HEAD + "Saved Deploy" in s for s in said))
        self.assertFalse(any("Now write the arguments" in s for s in said), "the instruction isn't kept")
        self.assertEqual(self.steps[0], ('notes.add_note("title": "Deploy", "body": "Friday")', "Saved Deploy"))

    def test_a_declined_call_does_not_run(self):
        ag = self.agent([{"why": "save", "tool": "notes.add_note"}, {"why": "done", "tool": "answer"}],
                        [{"title": "X", "body": "Y"}], allow=False)
        ag.turn("Save a note")
        self.assertFalse((self.dir / "notes.txt").exists())
        self.assertTrue(any(A.CONNECTOR_DECLINED in m["content"] for m in ag.messages))

    def test_a_tool_without_arguments_needs_no_second_call(self):
        ag = self.agent([{"why": "look", "tool": "notes.list_notes"}, {"why": "done", "tool": "answer"}], [])
        ag.turn("What notes are there?")
        self.assertEqual(self.asked, [("notes.list_notes", {}, True)])
        self.assertEqual(len(self.bodies), 2, "two actions, no arguments call")

    def test_the_same_call_twice_is_a_repeat_and_other_arguments_are_not(self):
        pick = {"why": "save", "tool": "notes.add_note"}
        ag = self.agent([pick, pick, pick, {"why": "done", "tool": "answer"}],
                        [{"title": "A", "body": "1"}, {"title": "A", "body": "1"}, {"title": "B", "body": "2"}])
        ag.turn("Save notes A and B")
        self.assertEqual((self.dir / "notes.txt").read_text(), "A\nB\n")
        self.assertEqual(sum(A.CONNECTOR_SAME in m["content"] for m in ag.messages), 1)

    def test_arguments_written_inside_an_action_are_unwrapped(self):
        pick = {"enough": False, "missing": "save it", "tool": "notes.add_note"}
        wrapped = {"enough": False, "missing": "save it", "tool": "notes.add_note", "arguments": {"title": "W", "body": "x"}}
        ag = self.agent([pick, {"why": "done", "tool": "answer"}], [wrapped])
        ag.turn("Save a note W")
        self.assertEqual((self.dir / "notes.txt").read_text(), "W\n")
        self.assertEqual(self.asked[0][1], {"title": "W", "body": "x"})
        self.assertIn("I'll call notes.add_note now.", json.dumps(self.bodies[1]["messages"][-2]),
                      "the action reads as a sentence during the argument call")
        self.assertEqual(A.clean_arguments({"tool": "x", "q": 1}, {"properties": {"q": {}}}), {"q": 1})
        self.assertEqual(A.clean_arguments({"tool": "x", "q": 1}, {"properties": {"tool": {}, "q": {}}}), {"tool": "x", "q": 1},
                         "a tool that really has a `tool` field keeps it")

    def test_a_failed_call_is_not_tried_again_unchanged(self):
        pick = {"why": "break", "tool": "notes.fail"}
        ag = self.agent([pick, pick, pick, {"why": "done", "tool": "answer"}], [])
        ag.turn("Run the failing tool")
        results = [m["content"] for m in ag.messages if m.get("_result")]
        self.assertEqual(sum("Error: notes.fail failed" in r for r in results), 1, "it ran once")
        self.assertTrue(any("it failed then" in r for r in results))
        self.assertEqual(len(self.asked), 1, "the user was asked once")

    def test_missing_and_unreadable_arguments_are_errors_for_the_model(self):
        pick = {"why": "save", "tool": "notes.add_note"}
        ag = self.agent([pick, pick, {"why": "done", "tool": "answer"}], [{"title": "A"}, "not json at all"])
        ag.turn("Save a note")
        errors = [m["content"] for m in ag.messages if "Error:" in m["content"]]
        self.assertIn("needs body", errors[0])
        self.assertIn("weren't valid JSON", errors[1])
        self.assertEqual(self.asked, [], "never offered to the user")

    def test_with_edits_off_only_read_only_tools_are_offered(self):
        ag = self.agent([{"why": "done", "tool": "answer"}], [])
        ag.turn("What notes are there?", can_edit=False)
        offered = json.dumps(self.bodies[0]["response_format"])
        self.assertIn("notes.list_notes", offered)
        self.assertNotIn("notes.add_note", offered)

    def test_a_connector_call_counts_as_the_work_asked_for(self):
        ag = self.agent([{"why": "save", "tool": "notes.add_note"}, {"why": "done", "tool": "answer"}],
                        [{"title": "A", "body": "1"}])
        facts = []
        answer = ag._answer
        ag._answer = lambda text="", f="", stuck="": facts.append(f) or answer(text, f, stuck)
        ag.turn("Save a note", wants_change=True)
        self.assertIn("Connector tools that ran in this request: notes.add_note", facts[0])
        self.assertNotIn("you made none", facts[0], "not told that nothing was done")
        self.assertFalse(any("haven't changed anything yet" in m["content"] for m in ag.messages))
        self.assertNotIn("only read", facts[0])

    def test_a_reply_after_only_reading_is_told_nothing_was_written(self):
        ag = self.agent([{"why": "look", "tool": "notes.list_notes"}, {"why": "done", "tool": "answer"}], [])
        facts = []
        answer = ag._answer
        ag._answer = lambda text="", f="", stuck="": facts.append(f) or answer(text, f, stuck)
        ag.turn("Save a note titled A")
        self.assertIn("All of them only read: nothing was created", facts[-1])


class Browser(Sandbox):
    """The headless browser: offered as one action, asked for once a session,
    then its tools are connector tools (a stand-in server plays Playwright)."""

    def agent(self, actions: list, answer: bool | str = True, state: str = "offer"):
        self.write({})
        hub = self.hub()
        self.asked, self.bodies = [], []

        def start():
            self.asked.append("browser")
            if answer is True:
                c = hub.start(mcp.ServerDef("playwright", "user", self.notes()), hidden={"fail"},
                              postprocess=lambda text: text.replace("Saved", "Opened"), stateful=True)
                return c.state == "connected"
            return answer

        hooks = A.Hooks(start_browser=start, confirm_tool=lambda name, args, ro: True)
        ag = A.Agent(ws=A.Workspace(str(self.repo)), hooks=hooks, connectors=hub, browser=state)

        def post(body):
            self.bodies.append(body)
            kind = body["response_format"]["json_schema"]["name"]
            if kind == "arguments":
                return {"choices": [{"message": {"content": json.dumps({"title": "page", "body": "x"})}}]}
            text = actions.pop(0) if actions else {"enough": True, "tool": "answer"}
            return {"choices": [{"message": {"content": json.dumps(text)}}]}

        ag._post = post
        ag._text = lambda *a, **k: "Done."
        return ag

    @staticmethod
    def offered(body) -> str:
        return json.dumps(body["response_format"])

    def test_started_on_request_then_its_tools_are_offered(self):
        ag = self.agent([{"enough": False, "missing": "see the page", "tool": "browser"},
                         {"enough": False, "missing": "open it", "tool": "playwright.add_note"},
                         {"enough": True, "tool": "answer"}])
        ag.turn("Open http://localhost:3000 and tell me what the console says")
        self.assertEqual(self.asked, ["browser"], "asked once")
        self.assertEqual(ag.browser, "running")
        first, after = self.bodies[0], self.bodies[1]
        self.assertIn('"browser"', self.offered(first))
        self.assertIn('{"tool": "browser"}', first["messages"][0]["content"])
        self.assertNotIn("playwright.", self.offered(first))
        self.assertIn("playwright.add_note", self.offered(after))
        self.assertNotIn('"enum": ["browser"]', self.offered(after), "not offered again once it runs")
        self.assertNotIn("playwright.fail", self.offered(after), "hidden tools are never offered")
        self.assertIn("playwright.add_note", after["messages"][0]["content"], "its tools join the system prompt")
        results = [m["content"] for m in ag.messages if m.get("_result")]
        self.assertTrue(any(r.startswith("Result of step 1 (browser):\nThe browser is running") for r in results))
        self.assertTrue(any(A.CONNECTOR_HEAD + "Opened page" in r for r in results), "results go through postprocess")
        # A URL in the message: the model is pointed at the browser.
        self.assertTrue(any("mentions http://localhost:3000" in r for r in results))
        ag.turn("And now?")
        self.assertEqual(self.asked, ["browser"], "not asked again in the session")

    def test_the_same_click_twice_runs_twice(self):
        # Found live: the second click on a button was refused as a repeat.
        start = {"enough": False, "missing": "the page", "tool": "browser"}
        click = {"enough": False, "missing": "click", "tool": "playwright.add_note"}
        look = {"enough": False, "missing": "look", "tool": "playwright.list_notes"}
        ag = self.agent([start, click, click, look, look, click, look, {"enough": True, "tool": "answer"}])
        ag.turn("Click Add twice on http://localhost:3000")
        self.assertEqual((self.dir / "notes.txt").read_text(), "page\n" * 3, "every click ran")
        results = [m["content"] for m in ag.messages if m.get("_result")]
        self.assertEqual(sum(A.CONNECTOR_SAME in r for r in results), 1, "only looking again at an unchanged page")
        ag2 = self.agent([start] + [click] * 7 + [{"enough": True, "tool": "answer"}])
        ag2.turn("Click it a lot")
        self.assertEqual((self.dir / "notes.txt").read_text().count("page"), 3 + A.STATEFUL_REPEATS, "a loop is still stopped")

    def test_a_no_is_final_for_the_session(self):
        ag = self.agent([{"enough": False, "missing": "see the page", "tool": "browser"},
                         {"enough": True, "tool": "answer"}], answer="the user said no")
        ag.turn("Does the login button on the dev server work?")
        self.assertEqual(ag.browser, "declined")
        results = [m["content"] for m in ag.messages if m.get("_result")]
        self.assertTrue(any("isn't available in this session (the user said no). Don't ask for it again" in r for r in results))
        self.assertNotIn('"enum": ["browser"]', self.offered(self.bodies[1]))
        ag.turn("Try again")
        self.assertEqual(self.asked, ["browser"])
        self.assertNotIn('"enum": ["browser"]', self.offered(self.bodies[-1]))

    def test_not_offered_when_the_session_cannot_have_one(self):
        ag = self.agent([{"enough": True, "tool": "answer"}], state="none")
        ag.turn("Open http://localhost:3000")
        self.assertNotIn('"enum": ["browser"]', self.offered(self.bodies[0]))
        self.assertNotIn('{"tool": "browser"}', self.bodies[0]["messages"][0]["content"])
        self.assertFalse(any("mentions http" in m["content"] for m in ag.messages), "no hint either")

    def test_a_reconnect_keeps_the_browser(self):
        self.write({"notes": self.notes()})
        hub = self.hub()
        hub.start(mcp.ServerDef("playwright", "user", self.notes()), hidden={"fail"})
        hub.connect()
        self.assertEqual(hub.connectors["playwright"].state, "connected")
        self.assertIn("playwright.add_note", hub.tools())
        self.assertNotIn("playwright.fail", hub.tools())
        self.assertIn("notes.fail", hub.tools(), "hiding is per connector")

    def test_snapshots_and_console_files_are_put_inline(self):
        snap, log = self.dir / "page.yml", self.dir / "console.log"
        snap.write_text("- heading \"Hi\" [ref=e1]\n- button \"Go\" [ref=e2]\n")
        log.write_text("[LOG] old\n[ERROR] boom @ app.js:3\n")
        text = (f"### Page\n- Page URL: http://localhost:3000/\n### Snapshot\n- [Snapshot]({snap})\n"
                f"### Events\n- New console entries: {log}#L2")
        out = browser.inline_files(text)
        self.assertIn('```yaml\n- heading "Hi" [ref=e1]\n- button "Go" [ref=e2]\n```', out)
        self.assertIn("[ERROR] boom @ app.js:3", out)
        self.assertNotIn("[LOG] old", out, "only the new entries")
        ranged = browser.inline_files(f"- New console entries: {log}#L1-L1")
        self.assertIn("[LOG] old", ranged)
        self.assertNotIn("boom", ranged)
        self.assertNotIn("-L1", ranged, "a range is read, not left behind")
        self.assertEqual(browser.inline_files("- [Snapshot](/nope.yml)"), "- [Snapshot](/nope.yml)", "a missing file stays a link")

    def test_the_hint(self):
        self.assertIn("localhost:5173", browser.hint("the page at localhost:5173 is blank"))
        self.assertIn("https://example.com/a", browser.hint("what does https://example.com/a show?"))
        self.assertTrue(browser.hint("check the console errors on the dev server"))
        self.assertTrue(browser.hint("is anything failing? it's running on port 5173"))
        self.assertTrue(browser.hint("npm run dev is up. Does the home page actually show the list?"))
        self.assertEqual(browser.hint("Add a footer to the home page component"), "")
        self.assertEqual(browser.hint("rename parse_args to parse_cli"), "")
        self.assertEqual(browser.hint("why does the gateway return 413?"), "")

    def test_the_command_line(self):
        d = browser.definition("abc123")
        args = d.spec["args"]
        for flag in ("--headless", "--isolated", "--image-responses", "--output-dir"):
            self.assertIn(flag, args)
        self.assertTrue(args[args.index("--output-dir") + 1].endswith("oppx/browser/abc123"), "outside the repository")
        self.assertIn("browser_run_code_unsafe", browser.HIDDEN)
        self.assertIn("playwright.browser_evaluate", browser.EVERY_TIME)


if __name__ == "__main__":
    unittest.main(verbosity=1)
