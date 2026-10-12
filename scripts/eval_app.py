#!/usr/bin/env python3
"""Building and improving a small web app end to end, scored per requirement.

The CHO-8 task as a repeatable run: the agent gets one prompt in a fresh
repository and nobody helps. Afterwards this script, not the agent's reply,
decides what was done: it builds the app, serves it, opens it in a headless
browser, types the keyword and reads the issue tracker's state.

    scripts/eval_app.py --oppx target/debug/oppx [--task build,improve] [--runs 5]
    scripts/eval_app.py --agent claude --runs 1        # the same prompts through Claude Code, as the bar

Two tasks:
  build    the CHO-8 prompt: search the web, open an issue, build the app,
           check it in the browser, close the issue
  improve  five changes to the app as the first run left it
           (scripts/fixtures/hn-dashboard), then lint, build and a browser check

What stands in for the user:
  * Questions can't be answered (print mode), so what a user would have said
    "always" to is allowed beforehand: npm, npx, mkdir, cp and mv, the
    browser, and the issue tracker. Everything else is declined.
  * "Linear" is a small connector written by this script (list, read, save,
    comment; one file of state), so runs leave nothing in a real workspace.
  * A run is `--turns` messages at most: the prompt, then "Continue…" for as
    long as a required check fails. The score after the first turn is kept.

Web search is off unless `--web` is given. Every run would search the same
few things through the server's SearXNG, and the public engines behind it
block an address that asks too often (a day of these runs got all of them
suspended). Without it the build task's first step can't be done; no check
depends on it. Use `--web` for a single run, not for a batch.

`oppx` must be paired. Needs Node, npm, and the browser oppx installed
(`oppx browser install`). Results and logs go to target/eval_app/<time>/.
"""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "scripts/fixtures/hn-dashboard"
CHECKER = ROOT / "scripts/eval_app_check.mjs"
ENGINE = Path(os.environ.get("OPPX_ENGINE_DIR") or Path.home() / ".local/share/oppx/engine")
PLAYWRIGHT = ENGINE / "playwright/node_modules"
ISSUE = "Build HN Keyword Dashboard MVP"

BUILD = ("I need a single-page tech news digest dashboard that fetches top stories from Hacker News using the official Firebase "
         "REST endpoints or Algolia Search API, but I need to confirm the best endpoint for keyword filtering. You can choose "
         "whatever tech stack for this, react, Svelte, whatever that you find easier. Please run this end-to-end: 1. Search the "
         "web to find good API endpoint to fetch relevant HN news. 2. Create a new Linear task titled 'Build HN Keyword Dashboard "
         "MVP' under my personal workspace. 3. Build a working frontend app in this repository that fetches top HN stories, lets "
         "me type a filter keyword, and highlights matching stories with live comment count badges. 4. Spin up a local preview "
         "server, run Playwright to open the app, type 'LLM' into the search input, and verify that filtered cards appear without "
         "DOM or console errors. 5. Mark the Linear issue as Done with a brief summary of the endpoint used.")
IMPROVE = ("Improve the HN dashboard app in hn-dashboard/: 1) filter the cards live as I type (debounced), without pressing "
           "Search; 2) show the current front page stories by default instead of all-time results; 3) highlight the matching "
           "keyword inside each title; 4) remove dependencies that are not used; 5) fix the lint warning from npm run lint. Then "
           "run the lint and the build, start the dev server, and check in the browser that typing Rust filters the cards with "
           "no console errors.")
CONTINUE = "Continue until every step of my request is done."

# name -> (prompt, keyword typed by the checker, the checks that must pass)
TASKS = {
    "build": (BUILD, "LLM", ("issue created", "app exists", "builds", "stories shown", "keyword filters", "no console errors",
                             "issue done")),
    "improve": (IMPROVE, "Rust", ("builds", "lint clean", "live filter", "debounced", "front page", "keyword marked",
                                  "unused dep removed", "keyword filters", "no console errors")),
}

# What a user would have answered "always" to. A rule names what is run.
ALLOW = ["npm *", "npx *", "mkdir *", "cp *", "mv *"]

# The stand-in tracker: Linear's tool names, small schemas, state in one file.
TRACKER = r'''
import json, os, sys
STATE = os.environ["TRACKER_FILE"]
STATUSES = ["Backlog", "Todo", "In Progress", "Done", "Canceled"]
def load():
    return json.load(open(STATE)) if os.path.exists(STATE) else {"issues": [], "calls": []}
def save(d):
    json.dump(d, open(STATE, "w"), indent=1)
def text(s): return {"content": [{"type": "text", "text": s if isinstance(s, str) else json.dumps(s)}]}
def error(s): return {"content": [{"type": "text", "text": s}], "isError": True}
def find(d, key):
    return next((i for i in d["issues"] if key in (i["id"], i["title"])), None)
TOOLS = [
 {"name": "list_teams", "description": "List the teams of the workspace.", "inputSchema": {"type": "object", "properties": {}},
  "annotations": {"readOnlyHint": True}},
 {"name": "list_issue_statuses", "description": "List the statuses an issue can have.",
  "inputSchema": {"type": "object", "properties": {"team": {"type": "string"}}}, "annotations": {"readOnlyHint": True}},
 {"name": "list_issues", "description": "List issues. Optional text to search titles for.",
  "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}, "annotations": {"readOnlyHint": True}},
 {"name": "get_issue", "description": "Read one issue by its identifier (e.g. ME-1).",
  "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
  "annotations": {"readOnlyHint": True}},
 {"name": "save_issue", "description": "Create an issue (no id), or update one (with its id). state is a status name.",
  "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"},
   "description": {"type": "string"}, "team": {"type": "string"}, "state": {"type": "string"}}}},
 {"name": "save_comment", "description": "Add a comment to an issue.",
  "inputSchema": {"type": "object", "properties": {"issueId": {"type": "string"}, "body": {"type": "string"}},
   "required": ["issueId", "body"]}},
]
def call(name, a):
    d = load(); d["calls"].append(name)
    try:
        if name == "list_teams": return text([{"key": "ME", "name": "Personal"}])
        if name == "list_issue_statuses": return text(STATUSES)
        if name == "list_issues":
            q = (a.get("query") or "").lower()
            return text([{k: i[k] for k in ("id", "title", "state")} for i in d["issues"] if q in i["title"].lower()])
        if name == "get_issue":
            i = find(d, a.get("id", ""))
            return text(i) if i else error("No issue " + str(a.get("id")))
        if name == "save_issue":
            state = a.get("state")
            if state and state.lower() not in [s.lower() for s in STATUSES]:
                return error("Unknown state %r. One of: %s" % (state, ", ".join(STATUSES)))
            if a.get("id"):
                i = find(d, a["id"])
                if not i: return error("No issue " + a["id"])
            else:
                if not a.get("title"): return error("title is required to create an issue")
                i = {"id": "ME-%d" % (len(d["issues"]) + 1), "title": "", "description": "", "state": "Backlog", "comments": []}
                d["issues"].append(i)
            for k in ("title", "description"):
                if a.get(k): i[k] = a[k]
            if state: i["state"] = next(s for s in STATUSES if s.lower() == state.lower())
            return text({k: i[k] for k in ("id", "title", "state")})
        if name == "save_comment":
            i = find(d, a.get("issueId", ""))
            if not i: return error("No issue " + str(a.get("issueId")))
            i["comments"].append(a.get("body", ""))
            return text("Comment added to " + i["id"])
        return error("Unknown tool " + name)
    finally:
        save(d)
for line in sys.stdin:
    try: m = json.loads(line)
    except ValueError: continue
    method, mid = m.get("method"), m.get("id")
    if mid is None: continue
    if method == "initialize":
        res = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "tracker", "version": "1"}}
    elif method == "tools/list": res = {"tools": TOOLS}
    elif method == "tools/call": res = call(m["params"]["name"], m["params"].get("arguments") or {})
    else: res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n"); sys.stdout.flush()
'''


def sh(cmd: str, cwd: Path, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(["bash", "-c", cmd], cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} s"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_app(repo: Path) -> Path | None:
    """The folder of the app: the nearest package.json with a build script, or
    failing that an index.html."""
    found = []
    for depth, pattern in enumerate(("package.json", "*/package.json", "*/*/package.json")):
        for p in sorted(repo.glob(pattern)):
            if "node_modules" in p.parts:
                continue
            try:
                scripts = json.loads(p.read_text()).get("scripts") or {}
            except (ValueError, OSError):
                continue
            if "build" in scripts:
                found.append((depth, p.parent))
    if found:
        return min(found)[1]
    for pattern in ("index.html", "*/index.html"):
        for p in sorted(repo.glob(pattern)):
            if "node_modules" not in p.parts:
                return p.parent
    return None


def lint_clean(app: Path, pkg: dict) -> tuple[bool, str]:
    if "lint" not in (pkg.get("scripts") or {}):
        return False, "no lint script"
    code, out = sh("npm run lint 2>&1", app, 180)
    dirty = re.search(r": (warning|error) |\b(warning|error)\s+[\w-]+\(|\b[1-9]\d* (warnings?|errors?|problems?)\b", out)
    return code == 0 and not dirty, out[-600:]


def unused_deps(app: Path, pkg: dict) -> list[str]:
    """Dependencies no source file imports (the fixture's unused one included)."""
    src = ""
    for p in list(app.glob("src/**/*")) + list(app.glob("*.js")) + list(app.glob("*.ts")) + list(app.glob("*.html")):
        if p.is_file() and p.suffix in (".js", ".jsx", ".ts", ".tsx", ".svelte", ".vue", ".html", ".css"):
            src += p.read_text(errors="replace")
    return [d for d in (pkg.get("dependencies") or {}) if not re.search(rf"""['"]{re.escape(d)}(/[^'"]*)?['"]""", src)]


def score(task: str, repo: Path, tracker: Path, keyword: str, log: Path) -> dict:
    """Every check of the task as True/False, with the details beside them."""
    c: dict = dict.fromkeys({k for _, _, need in TASKS.values() for k in need}, False)
    notes: dict = {}
    state = json.loads(tracker.read_text()) if tracker.exists() else {"issues": [], "calls": []}
    issue = next((i for i in state["issues"] if ISSUE.lower() in i["title"].lower()), None)
    if task == "build":
        c["issue created"] = issue is not None
        c["issue done"] = bool(issue) and issue["state"] == "Done"
        said = " ".join([issue["description"], *issue["comments"]]).lower() if issue else ""
        notes["summary names the endpoint"] = "algolia" in said or "firebase" in said
        notes["tracker calls"] = state["calls"]

    app = repo / "hn-dashboard" if task == "improve" else find_app(repo)
    c["app exists"] = app is not None and app.exists()
    served = None
    pkg = {}
    if c["app exists"]:
        notes["app"] = str(app.relative_to(repo)) or "."
        if (app / "package.json").exists():
            pkg = json.loads((app / "package.json").read_text())
            code, out = sh("npm install --no-audit --no-fund 2>&1 && npm run build 2>&1", app, 600)
            c["builds"] = code == 0
            if code:
                notes["build output"] = out[-800:]
            served = next((d for d in (app / "dist", app / "build") if (d / "index.html").exists()), None) if code == 0 else None
            c["lint clean"], notes["lint"] = lint_clean(app, pkg)
            left = unused_deps(app, pkg)
            c["unused dep removed"] = not left
            notes["unused deps"] = left
        else:
            c["builds"] = True  # nothing to build: plain files
            served = app
    page = {}
    if served:
        # Chrome asks for /favicon.ico when the page names no icon, and a 404 there is
        # logged as a console error that says nothing about the app.
        if not (served / "favicon.ico").exists():
            (served / "favicon.ico").write_bytes(b"")
        port = free_port()
        server = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "-d", str(served)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.8)
            p = subprocess.run(["node", str(CHECKER), str(PLAYWRIGHT), f"http://127.0.0.1:{port}/", keyword],
                               capture_output=True, text=True, timeout=120)
            page = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {"failed": (p.stdout + p.stderr)[-600:]}
        except (subprocess.TimeoutExpired, ValueError) as e:
            page = {"failed": str(e)[:300]}
        finally:
            server.terminate()
    notes["page"] = page
    c["stories shown"] = page.get("stories", 0) >= 5
    c["keyword filters"] = bool(page.get("filtered"))
    c["no console errors"] = bool(page.get("loaded")) and not page.get("errors")
    c["live filter"] = bool(page.get("live"))
    c["debounced"] = bool(page.get("live")) and page.get("requests_while_typing", 99) <= 2
    c["front page"] = bool(page.get("front_page"))
    c["keyword marked"] = bool(page.get("keyword_marked"))
    notes["matching stories set apart"] = bool(page.get("card_marked"))
    log.write_text(json.dumps({"checks": c, "notes": notes}, indent=1))
    return c


def left_running(repo: Path) -> list[str]:
    """Processes still alive whose working directory is inside the run's
    repository: what the agent started and didn't stop. They are killed."""
    found = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cwd = os.readlink(p / "cwd")
            if cwd == str(repo) or cwd.startswith(str(repo) + "/"):
                found.append((int(p.name), (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()[:80]))
        except OSError:
            continue
    for pid, _ in found:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return [cmd for _, cmd in found]


class Oppx:
    name = "oppx"

    def __init__(self, args, work: Path):
        self.bin, self.work, self.calls, self.written = os.path.abspath(args.oppx), work, 0, 0
        self.flags = [] if args.web else ["--no-web"]
        cfg = Path(os.environ.get("OPPX_CONFIG") or Path.home() / ".config/oppx/config.json")
        data = json.loads(cfg.read_text())
        data["browser_auto"] = True  # print mode can't be asked; this is the user's "always"
        self.config = work / "config.json"
        self.config.write_text(json.dumps(data))
        self.config.chmod(0o600)

    def env(self, repo: Path, tracker: Path, server: Path) -> dict:
        (self.work / "permissions.json").write_text(json.dumps({"global": {"run": ALLOW, "deny": [], "tools": ["linear.*"]}}))
        (self.work / "mcp.json").write_text(json.dumps({"mcpServers": {"linear": {
            "command": sys.executable, "args": [str(server)], "env": {"TRACKER_FILE": str(tracker)}}}}))
        self.dump = self.work / "requests.jsonl"
        return dict(os.environ, OPPX_CONFIG=str(self.config), OPPX_PERMISSIONS=str(self.work / "permissions.json"),
                    OPPX_MCP_CONFIG=str(self.work / "mcp.json"), OPPX_DUMP_REQUESTS=str(self.dump), OPPX_ENGINE="agent",
                    NO_COLOR="1")

    def turn(self, prompt: str, first: bool, repo: Path, env: dict, timeout: int) -> str:
        cmd = [self.bin, *self.flags, "-p", prompt] if first else [self.bin, *self.flags, "-c", "-p", prompt]
        try:
            p = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                               start_new_session=True)
            out = p.stdout + p.stderr
        except subprocess.TimeoutExpired as e:
            out = f"{(e.stdout or b'').decode(errors='replace')}\n[timed out after {timeout} s]"
        self.calls = self.written = 0
        if self.dump.exists():
            for line in self.dump.read_text().splitlines():
                try:
                    usage = json.loads(line).get("usage") or {}
                except ValueError:
                    continue
                self.calls += 1
                self.written += usage.get("completion_tokens") or 0
        return out


class Claude:
    """Claude Code on the same prompt, with the same tracker and the same
    browser server, and the same things allowed without asking."""
    name = "claude"

    def __init__(self, args, work: Path):
        self.work, self.calls, self.written, self.cost = work, 0, 0, 0.0

    def env(self, repo: Path, tracker: Path, server: Path) -> dict:
        (self.work / "mcp.json").write_text(json.dumps({"mcpServers": {
            "linear": {"command": sys.executable, "args": [str(server)], "env": {"TRACKER_FILE": str(tracker)}},
            "playwright": {"command": "node", "args": [str(PLAYWRIGHT / "@playwright/mcp/cli.js"), "--headless", "--isolated",
                                                       "--browser", "chrome"]}}}))
        return dict(os.environ)

    def turn(self, prompt: str, first: bool, repo: Path, env: dict, timeout: int) -> str:
        allowed = ["Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch", "mcp__linear", "mcp__playwright",
                   *(f"Bash({r.split()[0]}:*)" for r in ALLOW)]
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits", "--mcp-config",
               str(self.work / "mcp.json"), "--strict-mcp-config", "--allowedTools", *allowed]
        if not first:
            cmd.insert(1, "-c")
        try:
            p = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                               start_new_session=True)
        except subprocess.TimeoutExpired:
            return f"[timed out after {timeout} s]"
        try:
            r = json.loads(p.stdout)
            self.written += (r.get("usage") or {}).get("output_tokens") or 0
            self.cost += r.get("total_cost_usd") or 0
            return str(r.get("result") or "") + f"\n[turns {r.get('num_turns')}, ${r.get('total_cost_usd')}]"
        except ValueError:
            return p.stdout + p.stderr


def new_repo(task: str, repo: Path):
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# test repository\n")
    (repo / ".gitignore").write_text("node_modules/\ndist/\n")
    if task == "improve":
        shutil.copytree(FIXTURE, repo / "hn-dashboard")
        code, out = sh("npm install --no-audit --no-fund 2>&1", repo / "hn-dashboard", 600)
        if code:
            sys.exit(f"The fixture didn't install:\n{out[-600:]}")
    git = ["git", "-c", "user.name=eval", "-c", "user.email=eval@localhost"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "Initial commit"], cwd=repo, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agent", default="oppx", choices=["oppx", "claude"])
    ap.add_argument("--oppx", default=str(ROOT / "target/debug/oppx"))
    ap.add_argument("--task", default="build,improve")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--turns", type=int, default=3, help="messages per run at most (the prompt, then 'Continue…')")
    ap.add_argument("--timeout", type=int, default=2400, help="seconds per message")
    ap.add_argument("--web", action="store_true", help="let oppx search the web (off by default: see the note above)")
    ap.add_argument("--keep", action="store_true", help="keep each run's repository (under the results folder)")
    args = ap.parse_args()
    tasks = [t for t in args.task.split(",") if t]
    if any(t not in TASKS for t in tasks):
        sys.exit(f"Unknown task. One of: {', '.join(TASKS)}")
    if "improve" in tasks and not (FIXTURE / "package.json").exists():
        # The dummy app is kept out of git (.gitignore), so a fresh clone has none.
        sys.exit(f"The improve task starts from an app in {FIXTURE.relative_to(ROOT)}, which isn't in the repository.\n"
                 "Run the build task with --keep and copy its app there (without node_modules and dist), or use --task build.")
    if not (PLAYWRIGHT / "playwright").exists():
        sys.exit("The browser isn't installed: run `oppx browser install` first.")

    results = ROOT / "target/eval_app" / time.strftime("%Y%m%d-%H%M%S")
    results.mkdir(parents=True)
    print(f"agent: {args.agent}   results: {results.relative_to(ROOT)}")
    rows = []
    for task in tasks:
        prompt, keyword, required = TASKS[task]
        for run in range(1, args.runs + 1):
            tag = f"{task}-{run}"
            out = results / tag
            out.mkdir()
            with tempfile.TemporaryDirectory(prefix="oppx-eval-app-") as tmp:
                work = Path(tmp)
                repo = (out if args.keep else work) / "repo"
                new_repo(task, repo)
                server = work / "tracker.py"
                server.write_text(TRACKER)
                tracker = work / "tracker.json"
                agent = (Oppx if args.agent == "oppx" else Claude)(args, work)
                env = agent.env(repo, tracker, server)
                started, first, checks, turns = time.time(), None, {}, 0
                for turns in range(1, args.turns + 1):
                    reply = agent.turn(prompt if turns == 1 else CONTINUE, turns == 1, repo, env, args.timeout)
                    (out / f"turn{turns}.txt").write_text(reply)
                    orphans = left_running(repo)
                    # Scored on a copy: installing and building must not help the next message.
                    copy = work / "scored"
                    shutil.rmtree(copy, ignore_errors=True)
                    shutil.copytree(repo, copy, symlinks=True)
                    checks = score(task, copy, tracker, keyword, out / f"checks{turns}.json")
                    if first is None:
                        first = sum(checks[k] for k in required)
                    if all(checks[k] for k in required):
                        break
                took = time.time() - started
                diff = subprocess.run("git add -A -N . && git diff --stat | tail -1", shell=True, cwd=repo, capture_output=True,
                                      text=True).stdout.strip()
                # What the agent wrote, to look at after the repository is gone.
                patch = subprocess.run("git diff -- . ':!*package-lock.json'", shell=True, cwd=repo, capture_output=True, text=True)
                (out / "changes.patch").write_text(patch.stdout)
                if getattr(agent, "dump", None) and agent.dump.exists():
                    shutil.copy(agent.dump, out / "requests.jsonl")
                row = {"task": task, "run": run, "passed": sum(checks[k] for k in required), "of": len(required),
                       "first_turn": first, "turns": turns, "seconds": round(took), "calls": agent.calls,
                       "written": agent.written, "left_running": orphans, "diff": diff,
                       "checks": {k: checks[k] for k in required}}
                if args.agent == "claude":
                    row["cost_usd"] = round(agent.cost, 2)
                rows.append(row)
                (results / "results.json").write_text(json.dumps(rows, indent=1))
                failed = [k for k in required if not checks[k]]
                print(f"{tag:<10} {row['passed']}/{row['of']} (first message {first}/{row['of']})  {turns} message(s)  "
                      f"{took / 60:.1f} min  {agent.calls or '?'} model calls  "
                      f"{'left running: ' + str(len(orphans)) + '  ' if orphans else ''}"
                      f"{'failed: ' + ', '.join(failed) if failed else 'all passed'}", flush=True)

    for task in tasks:
        mine = [r for r in rows if r["task"] == task]
        if not mine:
            continue
        print(f"\n{task}: {len(mine)} run(s), all checks passed in {sum(r['passed'] == r['of'] for r in mine)}")
        for k in TASKS[task][2]:
            print(f"  {sum(r['checks'][k] for r in mine)}/{len(mine)}  {k}")
        n = len(mine)
        print(f"  average: {sum(r['seconds'] for r in mine) / n / 60:.1f} min, {sum(r['calls'] for r in mine) / n:.0f} model calls, "
              f"{sum(r['turns'] for r in mine) / n:.1f} messages")


if __name__ == "__main__":
    main()
