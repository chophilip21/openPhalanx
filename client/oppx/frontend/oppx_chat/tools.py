"""Carrying out one action (`Doing`, mixed into `Agent`): lookups, edits and new
files (the second, plain-text call), commands and their guards, connectors, the
browser and web search."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import time

import httpx

from . import documents, writers
from .mcp import ConnectorDown, McpError, constraint, missing_required
from .prompts import (
    ARGUMENT_TOKENS, BROWSER_DECLINED, BROWSER_STARTED, CONNECTOR_DECLINED, CONNECTOR_DOWN, CONNECTOR_HEAD,
    CONNECTOR_SAME, CREATE_PROMPT, EDIT_PROMPT, LOOKED_FIRST, RESULT_CHARS, REWRITE_PROMPT, SAME_SEARCH,
    SEARCHES_KEPT, STATEFUL_REPEATS, TOOLS,
)
from .actions import (
    ContextOverflow, ToolError, _action_not_text, _fenced, _has_payload, _outer_braces, _query_words,
    _regex_literal, _summary, _title, as_action, clean_arguments, degenerate, mcp_kind, misplaced_lookup,
    strip_action,
)
from .workspace import apply_edit, balance_warning, changed_region


class Doing:
    """One action carried out; mixed into `Agent`."""

    def _do(self, act: dict) -> str:
        tool, path = act["tool"], act.get("path") or ""
        title = _title(act)
        began = time.monotonic()
        try:
            if tool == "list":
                res = self.ws.list(path)
            elif tool == "find":
                res = self.ws.find(act.get("query") or act.get("pattern") or "")
            elif tool == "grep":
                res = self.ws.grep(act.get("pattern", ""), path)
            elif tool == "outline":
                res = self.ws.outline(path)
            elif tool == "read":
                res = self.ws.read(path, act.get("start"), act.get("end"))
            elif tool == "edit":
                res = self._edit(path)
            elif tool == "create":
                res = self._create(path)
            elif tool in ("run", "start"):
                cmd = act.get("command", "").strip()
                if not cmd:
                    raise ToolError(f"{tool} needs a command")
                self._ran = False
                self._not_a_program(cmd)
                self._no_such_folder(cmd)
                self._cut_at_quote(cmd)
                self.asking = tool
                allowed = self.hooks.confirm_run(cmd)
                if allowed is not True:
                    # A refusal can carry what the user said instead, or why
                    # it can't run (a deny rule, plan mode).
                    res = allowed if isinstance(allowed, str) and allowed else "The user declined to run this command."
                else:
                    self._ran = True  # the files may differ now; a declined command changed nothing
                    self.hooks.waiting("Running" if tool == "run" else "Starting")
                    res = self.ws.run(cmd) if tool == "run" else self.ws.start(cmd)
            elif tool == "web_search":
                res = self._search(act.get("query", ""))
            elif tool == "browser":
                res = self._browser()
            elif self.connectors is not None and tool in self.connectors.tools():
                title, res = self._connector(tool)
            else:
                raise ToolError(f"unknown tool {tool}")
        except ToolError as e:
            res = f"Error: {e}"
        if len(res) > RESULT_CHARS:
            res = res[:RESULT_CHARS] + "\n… (cut; ask for less)"
        # The trace learns what the action was, and how long it ran here, with
        # the next request (an edit's time is the model writing it: not counted).
        self._prev = {"prev_tool": tool, "prev_title": title[:200]}
        if tool not in ("edit", "create"):
            self._prev["prev_ms"] = round((time.monotonic() - began) * 1000)
        self._trail.append(f"- {title} → {_summary(tool, res)[:120]}")
        self.hooks.step(title, _summary(tool, res))
        return res

    def _connector(self, name: str) -> tuple[str, str]:
        """Calls a connector (MCP) tool. The model picked it by name; here it
        writes the arguments, constrained by the tool's own JSON Schema, the
        user confirms, and the connector runs it. Returns the step's title
        (with the arguments) and the result."""
        hub = self.connectors
        tool = hub.tools()[name]
        try:
            args = self._arguments(name, tool.schema) if tool.schema.get("properties") else {}
        except ToolError as e:
            self._called[f"{name} bad_args"] = f"bad_args:{e}"
            raise
        shown = json.dumps(args, ensure_ascii=False)
        title = f"{name}({shown[1:-1][:160]})"
        # The arguments go in the result, not into the action: merged into the
        # action, the next argument call copied the whole action (enough,
        # missing, tool, arguments) as the arguments, and Linear refused them
        # eight times in a row.
        called = f"\n(Called {name} with {shown[:2000]}.)"
        key = f"{name} {json.dumps(args, sort_keys=True)}"
        if tool.server in getattr(hub, "stateful", ()):
            # Clicking a button twice is normal; reading the page again is new
            # only after something changed it. A changing call made over and
            # over is still a loop.
            if tool.read_only:
                key += f" @{self._state_version}"
            else:
                last, times = self._state_last
                times = times + 1 if last == key else 1
                self._state_last = (key, times)
                if times > STATEFUL_REPEATS:
                    return title, (f"{CONNECTOR_SAME} {times - 1} times in a row; it isn't getting anywhere. "
                                   "Look at the page (snapshot), try something else, or answer.")
                self._state_version += 1
                key += f" #{self._state_version}"
        if key in self._called:
            before = self._called[key]
            if before.startswith("failed:"):
                return title, (f"{CONNECTOR_SAME}, and it failed then: {before[7:300]}. Calling it again won't help: "
                               "change the arguments (the tool's own fields, at the top level), or answer.")
            return title, f"{CONNECTOR_SAME}; its result is above. Use it, or answer."
        absent = missing_required(tool.schema, args)
        if absent:
            raise ToolError(f"{name} needs {', '.join(absent)}; choose it again and give every required argument")
        allowed = self.hooks.confirm_tool(name, json.dumps(args, ensure_ascii=False, indent=2), tool.read_only)
        if allowed is not True:
            said = f" {allowed}" if isinstance(allowed, str) and allowed else ""
            return title, CONNECTOR_DECLINED + said + " Don't call it again unless they ask; carry on without it, or answer."
        self.hooks.waiting(f"Calling {name}")
        try:
            out = hub.call(name, args, self.hooks.interrupted)
        except ConnectorDown as e:
            # Not this call's fault, and not fixable from here: the user is
            # told how to get it back, now and in the reply, and the model
            # stops trying (its tools leave the list).
            steps = "\n".join(f"{i}. {s}" for i, s in enumerate(e.steps, 1))
            self.hooks.notice(f"The {e.name} connector stopped working: {mcp_kind(e.kind)}.\n{steps}")
            self.down[e.name] = (mcp_kind(e.kind), e.steps)
            return title, (f"{CONNECTOR_DOWN}: {e.name} ({mcp_kind(e.kind)}; {e.error[:200]}). Its tools are gone until the user "
                           f"fixes it; nothing you can run repairs it, so don't try it again or work around it with other "
                           f"tools. What the user has to do:\n{steps}\nDo what is left of the request that doesn't need "
                           f"{e.name}, then answer: say first that {e.name} stopped working and what was not done because "
                           "of it, and give these steps as they are.")
        except McpError as e:
            self._called[key] = f"failed:{e}"  # the same call again is a repeat, not another try
            return title, f"Error: {name} failed: {e}"
        self._called[key] = title
        return title, CONNECTOR_HEAD + out + called

    def _browser(self) -> str:
        """The model wants the browser: the user is asked (once a session)."""
        if self.browser == "running":
            return "The browser is already running; use its playwright.* tools."
        if self.browser != "offer" or self.connectors is None:
            return BROWSER_DECLINED.format(why="")
        got = self.hooks.start_browser()
        names = [n for n in self.connectors.names() if n.startswith("playwright.")]
        if got is True and names:
            self.browser = "running"
            # Its tools join the connectors' list in the system prompt (one
            # uncached request, then cached again).
            self.reset_system()
            return BROWSER_STARTED.format(names=", ".join(names))
        self.browser = "declined"
        why = f" ({got})" if isinstance(got, str) and got else ""
        return BROWSER_DECLINED.format(why=why)

    def _arguments(self, name: str, schema: dict) -> dict:
        """The arguments for a connector tool, written by the model as JSON
        constrained by the tool's schema. A schema the server's decoder can't
        compile is asked for again unconstrained and checked here."""
        # As for an edit's text: the action that chose the tool reads as a
        # sentence during this call, so the model writes arguments, not an action.
        last = self.messages[-1] if self.messages and self.messages[-1]["role"] == "assistant" else None
        saved = last["content"] if last else ""
        if last and saved.startswith("{"):
            last["content"] = f"I'll call {name} now."
        self.messages.append({"role": "user", "content": self.connectors.tool_prompt(name)})
        self.hooks.waiting("Writing arguments")
        try:
            def ask(extra: dict) -> dict:
                return self._sized(lambda: self._post(self._body({"max_tokens": self._room(ARGUMENT_TOKENS), **extra})))
            try:
                data = ask({"response_format": {"type": "json_schema", "json_schema": {
                    "name": "arguments", "schema": constraint(schema)}}})
            except ContextOverflow:
                raise
            except RuntimeError:
                data = ask({})
        finally:
            self.messages.pop()
            if last:
                last["content"] = saved
        msg = data["choices"][0]["message"]
        text = msg.get("content") or msg.get("reasoning_content") or ""
        braces = _outer_braces(text)
        try:
            args = json.loads(braces or text)
        except json.JSONDecodeError as e:
            raise ToolError(f"the arguments for {name} weren't valid JSON ({str(e)[:80]}); choose it again") from e
        if not isinstance(args, dict):
            raise ToolError(f"the arguments for {name} must be a JSON object")
        return clean_arguments(args, schema)

    def _has_read(self, rel: str) -> bool:
        """Whether a read of this file is in the conversation (as Claude
        Code requires before an edit: edits need the file's exact text)."""
        head = f"{rel} lines "
        if any(m.get("_result") and head in m["content"][:400] and "[output removed" not in m["content"]
               for m in self.messages):
            return True
        # A file it wrote whole, unchanged since, whose text is still in the
        # conversation: it knows the exact text. (Qwen3.6 created hn.py, chose
        # create again, and spent two steps on "read it first" and a read.)
        text = self._wrote.get(rel)
        if not text:
            return False
        try:
            same = self.ws.path(rel).read_text(encoding=self.ws.encoding) == text
        except (OSError, UnicodeDecodeError, ToolError):
            return False
        return same and any(m["role"] == "assistant" and text in m["content"] for m in self.messages)

    def _written(self, prompt: str, max_tokens: int, path: str, fenced: bool = False) -> str:
        """The text of an edit or a new file. The action that asked for it is
        the last thing the model sees, and as JSON it invites the same JSON
        again (Qwen3.8 answered an edit prompt with its own action four times
        in a row): it reads as a sentence for this call, and a reply that is
        still an action is asked for once more with the first character
        constrained. So is a sentence followed by an action that isn't a
        lookup (Qwen3.6, asked for a new file: "I'll create all three files."
        and the create again, six times in a row); a new file's retry must
        open its code block."""
        last = self.messages[-1] if self.messages and self.messages[-1]["role"] == "assistant" else None
        saved = last["content"] if last else ""
        if last and saved.startswith("{"):
            last["content"] = f"I'll write the change to {path} now."
        self._call = {"kind": "edit", "tool": "edit", "title": f"Write({path})"}
        self.messages.append({"role": "user", "content": prompt})
        try:
            reply = self._text(max_tokens, stream=False)
            if not _has_payload(reply, path) and (not reply.strip() or as_action(reply) is not None
                                                  or _action_not_text(reply, path) or degenerate(reply)):
                self._event("retry")
                # An edit's blocks open with the file's name; a new file's code block holds only the code.
                start = "with ``` and then the file's code (not its name)" if fenced else "with the file's name and the first block"
                what = "the same line over and over" if degenerate(reply) else "a JSON action"
                self.messages[-1] = {"role": "user", "content": prompt + f"\n\nYour last output was {what}, not "
                                     f"the text. Write the text itself now, starting {start}."}
                # The opening is fixed, so the reply can't be a sentence, an action or a
                # repeated token: a new file's fence, or an edit's name, fence and SEARCH line.
                regex = r"```[\s\S]*" if fenced else _regex_literal(path) + r"\n```[^\n]*\n<<<<<<< SEARCH\n[\s\S]*"  # regex: runtime (a literal path, then no repeat that can overlap)
                try:
                    reply = self._text(max_tokens, stream=False, regex=regex)
                except ContextOverflow:
                    raise
                except RuntimeError:  # a server without regex constraints
                    reply = self._text(max_tokens, stream=False)
        finally:
            self.messages.pop()
            if last:
                last["content"] = saved
        return reply

    def _edit(self, path: str) -> str:
        rel = self.ws.rel(self.ws.path(path))  # exists?
        if not self._has_read(rel):
            raise ToolError(f"read the lines you're changing in {rel} first (read with start and end); "
                            "an edit must copy the file's current text exactly")
        self.ws.check_current(self.ws.path(path))
        self.hooks.waiting("Editing")
        reply = self._written(EDIT_PROMPT.format(path=path), 4096, path)
        # Keep the blocks in the conversation as the action's text. A reply with
        # no block is kept short: 4,096 tokens of `<tool_call>` left in view were
        # written again by the very next edit.
        kept = reply if "<<<<<<< SEARCH" in reply else reply[:600]
        self.messages[-1] = {"role": "assistant", "content": self.messages[-1]["content"] + "\n" + kept}
        if "<<<<<<< SEARCH" not in reply:
            look = misplaced_lookup(reply)
            if look:
                # It needs to see something before it can write the edit (the
                # file as its own last edit left it, say). That is not a
                # failed edit: do the lookup now, in this step.
                self._event("looked_first")
                return LOOKED_FIRST + self._do(look)
        rel, before, after = apply_edit(self.ws, path, reply)
        self.ws.mark(self.ws.path(path))
        self._before.setdefault(rel, before)
        self.hooks.edited(rel, before, after)
        return f"Edited {rel}. The changed lines now read:\n{changed_region(before, after)}{balance_warning(before, after)}"

    def _create(self, path: str) -> str:
        p = self.ws.path(path, must_exist=False)
        before = None
        if p.exists():
            # Rewriting a whole file as SEARCH/REPLACE means copying all of it
            # first: asked to replace Vite's starter App.jsx that way,
            # Qwen3.6 wrote one sentence and no block. Read first, as for an edit.
            if p.is_dir():
                raise ToolError(f"{path} is a folder")
            if not self._has_read(self.ws.rel(p)):
                raise ToolError(f"{path} already exists. Read it first; then edit it, or create it again to replace all of it")
            self.ws.check_current(p)
            before = self.ws.view(p)
            if writers.writable(p):
                self.hooks.original(self.ws.rel(p), p.read_bytes())  # /undo puts the file back as it was
        elif what := documents.unsupported(p):
            raise ToolError(f"{path} is a {what}, and {what}s can't be made (pictures, PDFs and documents can: "
                            "for slides, offer a PDF or a Word document instead)")
        self.hooks.waiting("Writing")
        prompt = (CREATE_PROMPT if before is None else REWRITE_PROMPT).format(path=path)
        if writers.writable(p):
            prompt += writers.hint(p)
        reply = self._written(prompt, 8192, path, fenced=True)
        self.messages[-1] = {"role": "assistant", "content": self.messages[-1]["content"] + "\n" + reply}
        body = _fenced(reply, path)
        if body is None or as_action(body) is not None:
            # Not the file: a sentence and an action ("Let me check the folder
            # first." and a list as JSON) would have been written into it.
            look = misplaced_lookup(reply)
            if look:
                self._event("looked_first")
                return LOOKED_FIRST.replace("make the edit", "write the file") + self._do(look)
            if strip_action(reply) != reply.strip():
                raise ToolError("your reply was not the file's content: it ended in a JSON action. Write the whole "
                                "file, in one code block")
        if body is None and before is not None:
            # A rewrite replaces everything: words without a code block ("Done.") must not.
            raise ToolError(f"your reply had no code block, so {path} was left as it was. To replace it, write its "
                            "whole new content in one fenced code block")
        content = body if body is not None else reply
        p.parent.mkdir(parents=True, exist_ok=True)
        if writers.writable(p):
            try:
                made = writers.write(p, content)
            except writers.WriteError as e:
                raise ToolError(f"{path} wasn't written: {e}") from e
            self.ws.mark(p)
            rel = self.ws.rel(p)
            self._before.setdefault(rel, before)
            self.hooks.edited(rel, before, self.ws.view(p))
            check = "" if documents.is_image(p) else " Read it to check what it holds."
            return f"{'Replaced' if before is not None else 'Created'} {rel} ({made}), from your {writers.SOURCE[p.suffix.lower()]}.{check}"
        p.write_text(content, encoding=self.ws.encoding)
        self.ws.mark(p)
        rel = self.ws.rel(p)
        self._wrote[rel] = content
        self._before.setdefault(rel, before)
        self.hooks.edited(rel, before, content)
        if before is not None:
            return f"Replaced the whole of {rel} ({len(content.splitlines())} lines; it had {len(before.splitlines())})."
        return f"Created {rel} ({len(content.splitlines())} lines)."

    def _not_a_program(self, cmd: str):
        """One of the agent's own actions, or a connector's name, typed as a
        shell command: say what it is, instead of asking the user to run a
        program that doesn't exist. (`playwright browser navigate http://…`
        with the browser's tools on offer; `web_search --query '…'` nine
        times in a row in a session started with --no-web.)"""
        words = cmd.split()
        first = words[0].lower() if words else ""
        if not first or shutil.which(words[0]):
            return
        if first in TOOLS:
            if first == "web_search" and not (self.web and time.monotonic() >= self._search_off_until):
                raise ToolError("web_search is not a program, and web search is not available in this session (switched off, "
                                "or paused by the server). Carry on without it, from what you know and what the files "
                                "say, and say in your reply that nothing was looked up on the web.")
            raise ToolError(f'{first} is not a program: it is one of your actions. Choose it directly, as {{"tool": "{first}", …}}.')
        names = sorted(self.connectors.tools()) if self.connectors is not None else []
        mine = [n for n in names if n.split(".", 1)[0].lower() == first or n in cmd]
        if mine:
            raise ToolError(f"{first} is not a program here: it is a connector, and its tools are chosen directly as the "
                            f'action, e.g. {{"tool": "{mine[0]}"}}. Its tools: ' + ", ".join(mine[:12])
                            + (" …" if len(mine) > 12 else ""))

    @staticmethod
    def _cut_at_quote(cmd: str):
        """A command that ends inside a quoted string: the model wrote a bare
        `"` in the JSON string, which ends it, so the rest was lost (Qwen3.6:
        `python3 -c 'import re; p = re.compile(r` five times in a row). Bash
        would only say "unexpected EOF"; say why. A heredoc may hold any
        quotes, so it is left to the shell."""
        if "<<" in cmd:
            return
        try:
            shlex.split(cmd)
        except ValueError:
            raise ToolError(f"the command stops inside a quoted string, so it was cut short: `{cmd[-60:]}`. In the "
                            'JSON string, write a double quote inside the command as \\" (a backslash before it), '
                            "or put the code in a file with create and run that file") from None

    def _no_such_folder(self, cmd: str):
        """A command that starts by changing into a folder that doesn't exist:
        say where commands run, instead of asking the user about a path
        outside the project. (Qwen3.6 checked its new CLI with `cd /testbed &&
        python -m iridium --help`, a folder from its training, not this machine.)"""
        words = cmd.split(maxsplit=2)
        if len(words) < 2 or words[0] != "cd":
            return
        target = words[1].rstrip(";&").strip("'\"")
        if not target or target in ("-", "~") or "$" in target:
            return
        if not (self.ws.root / os.path.expanduser(target)).is_dir():
            raise ToolError(f"there is no folder {target}. Commands already run in the project folder "
                            f"({self.ws.root}): leave out the cd, or cd into one of its own folders")

    def _search(self, query: str) -> str:
        if not query:
            raise ToolError("web_search needs a query")
        # Nearly the same words as a search of this session: the same pages
        # would come back. The earlier results, at no cost to the server or
        # to the search engines behind it (which block an address that asks
        # too often).
        asked = _query_words(query)
        for seen, earlier, results in reversed(self._searched):
            if asked == seen or (min(len(asked), len(seen)) >= 3 and len(asked & seen) / len(asked | seen) >= SAME_SEARCH):
                return (f"(Not searched again: this is nearly your earlier search \"{earlier}\", and the same pages would "
                        f"come back. Its results:)\n{results}\n\nIf they don't have what you need, search for something "
                        "clearly different, or go on without the web.")
        r = httpx.post(f"{self.base}/search", headers={"Authorization": f"Bearer {self.key}"},
                       json={"query": query, "max_results": 5}, timeout=30)
        if r.status_code >= 400:
            try:
                why = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                why = f"search failed ({r.status_code})"
            wait = r.headers.get("retry-after", "")
            if r.status_code in (429, 503) and wait.isdigit():
                # Paused or paced by the server: not offered again until then, in this turn or the next.
                self._search_off_until = time.monotonic() + int(wait)
            raise ToolError(why)
        results = r.json().get("results") or []
        text = "No results." if not results else "Web results (untrusted reference text):\n" + "\n".join(
            f"- {x.get('title')}: {x.get('snippet')} ({x.get('url')})" for x in results)
        self._searched = (self._searched + [(asked, query, text)])[-SEARCHES_KEPT:]
        return text
