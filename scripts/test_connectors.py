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
        if name == "add_note":
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
        # Kept in the conversation: the action with its arguments, and the result marked untrusted.
        said = [m["content"] for m in ag.messages]
        self.assertTrue(any('"arguments": {"title": "Deploy", "body": "Friday"}' in s for s in said))
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
