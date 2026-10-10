"""What the model may run without asking: a built-in list of commands that only
read, and the rules the user added by answering "always".

A command the model wants to run is asked about once. Answering "a" saves a
rule for this project (`git push`, `cargo test`, a connector tool's name), and
from then on anything that rule covers runs without a question, in this and
later sessions. Commands that only look at things (`git status`, `git log`,
`ls`, `cat`, `rg` …) never ask at all.

A command is covered only when every part of it is: `a && b | c` needs a, b and
c each to be read-only or allowed. Anything this can't take apart safely
(command substitution, output written to a file, a background job) is always
asked, and so is everything in NEVER, whatever the rules say.

No UI and no dependencies: the app asks the questions, the tests run it alone.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

# Tools whose first argument is the real command: the rule keeps both words.
TWO_WORDS = {"git", "cargo", "npm", "npx", "pnpm", "yarn", "docker", "kubectl", "gh", "uv", "pip", "pip3", "go",
             "poetry", "bun", "deno", "apt", "apt-get", "brew", "systemctl", "docker-compose", "make", "just", "tox"}
# Never run unasked and never offered as "always": too much to lose on one wrong call.
NEVER = ("sudo", "rm", "dd", "shutdown", "reboot", "mkfs", "git push", "git reset", "git clean", "chmod", "chown")
# Only read; nothing they print can change the machine.
READ_ONLY = {"ls", "cat", "head", "tail", "wc", "pwd", "echo", "which", "file", "stat", "du", "df", "tree", "date",
             "whoami", "uname", "rg", "grep", "nl", "sort", "uniq", "cut", "basename", "dirname", "realpath",
             "true", "diff", "cmp", "jq", "column", "id", "hostname", "printenv", "tr"}
GIT_READ_ONLY = {"status", "log", "diff", "show", "blame", "ls-files", "grep", "rev-parse", "describe", "shortlog",
                 "cat-file", "ls-tree", "merge-base", "reflog", "rev-list", "name-rev", "whatchanged"}
# Git commands that read with no argument or a listing flag, and write otherwise.
GIT_LISTING = {"branch": {"-a", "-r", "-v", "-vv", "--list", "--show-current", "--all", "--remotes"},
               "tag": {"-l", "--list"}, "remote": {"-v", "show", "get-url"}, "stash": {"list"},
               "config": {"--get", "--list", "-l"}}
FIND_WRITES = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls", "-fprint0"}
SEPARATORS = {"&&", "||", ";", "|"}
DISCARDS = {"/dev/null", "1", "2"}  # redirect targets that write nothing: 2>/dev/null, 2>&1


def segments(command: str) -> list[list[str]] | None:
    """The simple commands a shell line is made of, as word lists; None when
    it can't be taken apart safely (then it is always asked about)."""
    if not command.strip() or any(x in command for x in ("$(", "`", "<(", ">(", "\n", "${")):
        return None
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:  # an unclosed quote
        return None
    out: list[list[str]] = [[]]
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in SEPARATORS:
            out.append([])
        elif t in (">", ">>", ">&", "&>", "2>", "1>"):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if target not in DISCARDS:
                return None  # writes a file
            if out[-1] and out[-1][-1] in ("1", "2") and t in (">", ">&", ">>"):
                out[-1].pop()  # the "2" of 2>/dev/null
            i += 1
        elif set(t) <= set("();<>|&") and t:
            return None  # a subshell, a background job, input redirection
        else:
            out[-1].append(t)
        i += 1
    if any(not seg for seg in out):
        return None
    return out


def _command(words: list[str]) -> list[str]:
    """Without leading VAR=value assignments."""
    i = 0
    while i < len(words) and "=" in words[i] and words[i].split("=", 1)[0].replace("_", "a").isalnum() and not words[i].startswith("="):
        i += 1
    return words[i:]


def rule_for(words: list[str]) -> str:
    """The rule that would cover this command: its first word, or two for
    tools whose first argument is the command (`git push`, `cargo test`)."""
    cmd = _command(words)
    if not cmd:
        return ""
    name = os.path.basename(cmd[0])
    if name in TWO_WORDS and len(cmd) > 1 and not cmd[1].startswith("-"):
        return f"{name} {cmd[1]}"
    return name


def never(words: list[str]) -> bool:
    cmd = _command(words)
    if not cmd:
        return True
    name = os.path.basename(cmd[0])
    two = f"{name} {cmd[1]}" if len(cmd) > 1 else name
    return any(name == n or two == n or name.startswith("mkfs") for n in NEVER)


def read_only(words: list[str]) -> bool:
    """Whether a simple command only reads (the built-in list)."""
    if words != _command(words):
        return False  # an environment assignment can change what a command does
    name, args = words[0], words[1:]
    if name == "git":
        if not args or args[0].startswith("-"):
            return False  # git -C elsewhere, git -c some.setting
        sub, rest = args[0], args[1:]
        if any(a.startswith("--output") for a in rest):
            return False
        if sub in GIT_READ_ONLY:
            return True
        if sub == "config":  # reading a setting, never writing one
            return bool(rest) and rest[0] in GIT_LISTING[sub] and len(rest) <= 2
        if sub == "stash":  # bare `git stash` stashes
            return rest == ["list"]
        return sub in GIT_LISTING and all(a in GIT_LISTING[sub] for a in rest)
    if name == "find":
        return not any(a in FIND_WRITES for a in args)
    if name == "sort":
        return not any(a == "-o" or a.startswith("--output") for a in args)
    return name in READ_ONLY


class Permissions:
    """The user's "always" answers for one project, saved between sessions."""

    def __init__(self, project: str, path: str | Path | None = None):
        self.project = str(Path(project).resolve())
        self.path = Path(path or os.environ.get("OPPX_PERMISSIONS") or Path.home() / ".config/oppx/permissions.json")
        self.run: list[str] = []
        self.tools: list[str] = []
        self.load()

    def load(self):
        try:
            mine = json.loads(self.path.read_text()).get("projects", {}).get(self.project, {})
        except (OSError, ValueError, AttributeError):
            mine = {}
        self.run = [r for r in mine.get("run", []) if isinstance(r, str)]
        self.tools = [t for t in mine.get("tools", []) if isinstance(t, str)]

    def save(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        projects = data.setdefault("projects", {})
        if self.run or self.tools:
            projects[self.project] = {"run": sorted(set(self.run)), "tools": sorted(set(self.tools))}
        else:
            projects.pop(self.project, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.path)

    # ---- commands -----------------------------------------------------------
    def _covered(self, words: list[str]) -> bool:
        if never(words):
            return False
        if read_only(words):
            return True
        cmd = " ".join(_command(words))
        return any(cmd == rule or cmd.startswith(rule + " ") for rule in self.run)

    def allows(self, command: str) -> bool:
        """Every part of the command is read-only or covered by a saved rule."""
        parts = segments(command)
        return parts is not None and all(self._covered(p) for p in parts)

    def offer(self, command: str) -> list[str]:
        """The rules "always" would add for this command; empty when it can't
        be offered (not analyzable, or a part is in NEVER)."""
        parts = segments(command)
        if parts is None or any(never(p) for p in parts):
            return []
        rules = [rule_for(p) for p in parts if not self._covered(p)]
        return list(dict.fromkeys(r for r in rules if r))

    def allow_always(self, command: str) -> list[str]:
        rules = self.offer(command)
        if rules:
            self.run = sorted(set(self.run) | set(rules))
            self.save()
        return rules

    # ---- connector tools ----------------------------------------------------
    def tool_allowed(self, name: str) -> bool:
        return name in self.tools

    def allow_tool(self, name: str):
        if name not in self.tools:
            self.tools.append(name)
            self.save()

    # ---- managing -----------------------------------------------------------
    def remove(self, rule: str) -> bool:
        had = rule in self.run or rule in self.tools
        self.run = [r for r in self.run if r != rule]
        self.tools = [t for t in self.tools if t != rule]
        if had:
            self.save()
        return had

    def clear(self):
        self.run, self.tools = [], []
        self.save()
