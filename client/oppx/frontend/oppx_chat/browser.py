"""The chat's web browser: Playwright's MCP server, headless, run on this
machine for one session (oppx installs it; `oppx browser`).

It is a connector oppx starts itself, so the model calls its tools like any
other (`playwright.browser_navigate`, …). What is different:

* It isn't running until it is needed. The model asks for it with the
  `browser` action, and the user is asked once a session; `oppx browser on`
  starts it with every session instead, without asking.
* Once the user said yes, its tools run without a question for the rest of
  the session, except the ones that run code the model wrote in the page
  (`EVERY_TIME`). Tools useless or risky for a coding agent are never offered
  (`HIDDEN`).
* Page snapshots and console logs it writes to files are put into the result
  itself: the model can't open files outside the repository.

No UI imports.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from .mcp import ServerDef

NAME = "playwright"
AUTO = os.environ.get("OPPX_BROWSER_AUTO") == "1"
CLI = os.environ.get("OPPX_BROWSER_CLI", "")
NODE = os.environ.get("OPPX_BROWSER_NODE", "")
CHANNEL = os.environ.get("OPPX_BROWSER_CHANNEL", "chrome")
BROWSERS = os.environ.get("OPPX_BROWSER_PATH", "")
OPPX_BIN = os.environ.get("OPPX_BIN", "oppx")

# Never offered: uploads and drops send the user's files to a page, run_code
# is arbitrary Playwright code, and screenshots come back as files the model
# can't see (images are left out of results); the snapshot is the page as text.
HIDDEN = frozenset({"browser_run_code_unsafe", "browser_file_upload", "browser_drop", "browser_take_screenshot"})
# Asked every time even after the yes: JavaScript the model wrote, run in the page.
EVERY_TIME = frozenset({f"{NAME}.browser_evaluate"})
SNAPSHOT_CHARS = 8000
CONSOLE_CHARS = 3000
START_TIMEOUT = 60  # the first start launches Chrome

# Words that mean a page has to be looked at: a hint for the model, which
# still decides (scripts/probe_browser.py measures both).
_URL = re.compile(r"\b(?:https?://\S+|localhost:\d+|127\.0\.0\.1:\d+|0\.0\.0\.0:\d+)", re.I)
_WORDS = re.compile(r"\b(?:browser|playwright|headless|web ?page|in the page|on the page|console (?:errors?|logs?|output)"
                    r"|dev server|npm run dev|preview build|screenshot|click(?:s|ing)? (?:on |through )|ui regression"
                    r"|visually|(?:the|my) (?:running )?(?:app|site) (?:looks|loads|shows|is running)|running app"
                    r"|(?:home|landing|login|sign ?up|settings) page (?:actually|really|shows?|looks|loads|is blank)"
                    r"|page colou?rs|(?:when )?the page loads|running on port \d+|blank (?:white )?page|(?:actually|really) (?:shows?|works?|changes?|renders?))\b", re.I)


def available() -> bool:
    return bool(CLI and NODE and Path(CLI).is_file() and Path(NODE).is_file())


def why_unavailable() -> str:
    if not NODE:
        return "it needs Node.js 18 or newer on PATH (https://nodejs.org)"
    if not CLI or not Path(CLI).is_file():
        return "it isn't installed yet"
    return ""


def install() -> tuple[bool, str]:
    """Runs `oppx browser install` (npm, and a browser if the system has no
    Chrome). Returns whether it worked and the last lines it printed."""
    try:
        done = subprocess.run([OPPX_BIN, "browser", "install"], capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    tail = " / ".join(line.strip() for line in (done.stdout + done.stderr).splitlines() if line.strip())[-300:]
    return done.returncode == 0 and Path(CLI).is_file(), tail


def output_dir(session: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "oppx" / "browser"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(tempfile.gettempdir()) / "oppx-browser"
    return base / session


def definition(session: str) -> ServerDef:
    out = output_dir(session)
    env = {"PLAYWRIGHT_BROWSERS_PATH": BROWSERS} if BROWSERS and CHANNEL != "chrome" else {}
    return ServerDef(NAME, "user", {
        "command": NODE, "args": [
            CLI, "--headless", "--browser", CHANNEL, "--isolated",
            "--output-dir", str(out), "--file-paths", "absolute",
            "--image-responses", "omit", "--codegen", "none", "--console-level", "warning"],
        "env": env})


_SNAPSHOT = "- [Snapshot]("
# A path starts after a space, "(" or "[" (how the server writes it), and holds
# none of those: `/\S+?\.log` was tried from every "/" of a long path (quadratic).
_CONSOLE_LINK = re.compile(r"(?<![^\s(\[])(?P<path>/[^\s#()\[\]]+\.log)#L(?P<line>\d+)(?:-L(?P<last>\d+))?")


def _read(path: str, limit: int, first_line: int = 1, last_line: int | None = None) -> str | None:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    text = "\n".join(lines[max(0, first_line - 1):last_line]).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n… (cut)"


def inline_files(text: str) -> str:
    """Puts the page snapshot and new console entries, which the server writes
    to files, into the result."""
    def snapshot(line: str) -> str:
        # "- [Snapshot](path)" on a line of its own; by line, not by regex.
        bare = line.rstrip()
        path = bare[len(_SNAPSHOT):-1]
        if not (bare.startswith(_SNAPSHOT) and bare.endswith(")") and path and ")" not in path):
            return line
        body = _read(path, SNAPSHOT_CHARS)
        return line if body is None else f"```yaml\n{body}\n```"

    def console(m: re.Match) -> str:
        body = _read(m["path"], CONSOLE_CHARS, int(m["line"]), int(m["last"]) if m["last"] else None)
        return m[0] if body is None else f"\n```\n{body}\n```"

    return _CONSOLE_LINK.sub(console, "\n".join(snapshot(line) for line in text.split("\n")))


def hint(text: str) -> str:
    """A note for the model when the message reads like it needs a page
    looked at ("" when it doesn't)."""
    url = _URL.search(text)
    if url:
        return (f"The message mentions {url[0].rstrip('.,;:)')}. If answering means looking at that page (what it shows, "
                "its console errors, whether something on it works), choose browser: grep and read can't see a "
                "running page.")
    if _WORDS.search(text):
        return ("If answering means looking at a running web page (what it shows, its console errors, whether "
                "something on it works), choose browser: grep and read can't see a running page.")
    return ""
