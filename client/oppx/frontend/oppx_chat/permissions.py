"""What the model may run without asking, modelled on Claude Code's permissions.

* Commands that only read (`git status`, `git log`, `ls`, `cat`, `rg` …) inside
  the project never ask.
* Anything else is asked once. "Always" saves an allow rule for the project
  (`cargo test`, `npm run`, `python -m pytest`, a connector tool's name); from
  then on what the rule covers runs without a question, in every session.
* Deny rules refuse a command outright; they win over everything.
* A command is covered only when every part of it is: `a && b | c` needs a, b
  and c each to be read-only or allowed. What can't be taken apart safely
  (command substitution, output written to a file, a background job) is
  always asked.
* Some commands are asked every time and can't be remembered (ALWAYS_ASK):
  the ones that destroy or publish, and the ones that run arbitrary code given
  as text (`bash -c`, `eval`, `xargs`), where a rule would allow anything.

Rules are lists of words matched from the start of a command ("cargo test"
covers `cargo test -p x`, not `cargo testx` or `cargo build`). A tool's name
alone ("cargo", "git") covers only the bare call; "git *", written by hand,
covers every git command that isn't asked every time.

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
# Interpreters: the rule names what is run (`python -m pytest`, `node build.js`), never the interpreter alone.
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php"}
INLINE_CODE = {"-c", "-e", "--eval", "-p", "--print", "-"}
# Asked every time, never remembered: one wrong unattended call loses work or publishes it.
ALWAYS_ASK = ("sudo", "su", "doas", "rm", "dd", "shutdown", "reboot", "chmod", "chown", "truncate", "shred",
              "git push", "git reset", "git clean")
# These run whatever text they are given: a rule for them would allow anything.
OPAQUE = {"bash", "sh", "zsh", "fish", "dash", "ksh", "eval", "xargs", "source", ".", "exec", "watch", "parallel", "ssh"}
# Run another command given after them; the command inside is what counts.
WRAPPERS = {"env", "command", "nohup", "time", "nice", "ionice", "stdbuf", "timeout", "builtin"}
# Only read; nothing they print can change the machine.
READ_ONLY = {"cd", "ls", "find", "cat", "head", "tail", "wc", "pwd", "echo", "which", "file", "stat", "du", "df", "tree",
             "date", "whoami", "uname", "rg", "grep", "nl", "sort", "uniq", "cut", "basename", "dirname", "realpath",
             "true", "diff", "cmp", "jq", "column", "id", "hostname", "tr"}
# Arguments that turn a reading command into one that writes or runs something.
WRITING_ARGS = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls", "-fprint0"},
    "sort": {"-o", "--output"}, "tree": {"-o"}, "date": {"-s", "--set"}, "rg": {"--pre", "--pre-glob"},
    "git": {"--output", "-O", "--open-files-in-pager", "--ext-diff"},
}
GIT_READ_ONLY = {"status", "log", "diff", "show", "blame", "ls-files", "grep", "rev-parse", "describe", "shortlog",
                 "cat-file", "ls-tree", "merge-base", "rev-list", "name-rev", "whatchanged"}
# Git commands that read with no argument or a listing flag, and write otherwise.
GIT_LISTING = {"branch": {"-a", "-r", "-v", "-vv", "--list", "-l", "--show-current", "--all", "--remotes"},
               "tag": {"-l", "--list", "-n"}}
GIT_FORCES_LIST = {"--list", "-l"}  # with these, further arguments are patterns
# Git's own options before the subcommand; -C and -c change where and how it runs.
GIT_GLOBAL_FLAGS = {"--no-pager", "-p", "--paginate", "--no-optional-locks"}
SEPARATORS = {"&&", "||", ";", "|"}


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
        elif t in (">", ">>", "&>", ">&"):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            # Only what writes nothing: >/dev/null, 2>/dev/null, &>/dev/null, 2>&1.
            # (`> 1` is a file called 1; only `>&1` is the other stream.)
            harmless = target in ("1", "2") if t == ">&" else target == "/dev/null"
            if not harmless:
                return None
            if out[-1] and out[-1][-1] in ("1", "2") and t != "&>":
                out[-1].pop()  # the "2" of 2>/dev/null
            i += 1
        elif t and set(t) <= set("();<>|&"):
            return None  # a subshell, a background job, input redirection, a heredoc
        else:
            out[-1].append(t)
        i += 1
    if any(not seg for seg in out):
        return None
    return out


def _is_assignment(word: str) -> bool:
    name, eq, _ = word.partition("=")
    return bool(eq) and bool(name) and name.replace("_", "a").isalnum() and not name[0].isdigit()


def unwrap(words: list[str]) -> tuple[list[str], bool]:
    """The command actually run, without leading VAR=value assignments and
    wrappers (`env`, `timeout 60`, `nice -n 5`, `time`, `nohup` …), and
    whether anything was stripped."""
    cmd, stripped = list(words), False
    while cmd:
        if _is_assignment(cmd[0]):
            cmd, stripped = cmd[1:], True
            continue
        name = os.path.basename(cmd[0])
        if name not in WRAPPERS:
            break
        cmd, stripped = cmd[1:], True
        while cmd and (cmd[0].startswith("-") or (name == "env" and _is_assignment(cmd[0]))):
            # an option of the wrapper, and its value when written apart (nice -n 5)
            flag = cmd.pop(0)
            if name != "env" and flag in ("-n", "-c", "-k", "-s", "-i", "-o", "-e") and cmd and not cmd[0].startswith("-"):
                cmd.pop(0)
        if name == "timeout" and cmd:
            cmd = cmd[1:]  # the duration
    return cmd, stripped


def _git_sub(args: list[str]) -> tuple[str | None, list[str], bool]:
    """A git command's subcommand and its arguments, and whether options that
    redirect git (`-C dir`, `-c key=value`, `--git-dir`) came before it."""
    redirected, i = False, 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-C", "-c"):
            redirected, i = True, i + 2
        elif args[i] in GIT_GLOBAL_FLAGS:
            i += 1
        else:
            redirected, i = True, i + 1  # --git-dir=…, --work-tree=…, anything unknown
    return (args[i] if i < len(args) else None), args[i + 1:], redirected


def key_of(words: list[str]) -> list[str] | None:
    """The words that identify what a command does, for rules and for
    ALWAYS_ASK: `git push`, `cargo test`, `python -m pytest`, `./scripts/x.sh`,
    `ls`. None when no rule should cover it (inline code, a bare interpreter)."""
    cmd, _ = unwrap(words)
    if not cmd:
        return None
    name = os.path.basename(cmd[0])
    if name in OPAQUE:
        return None
    if name == "git":
        sub, _, _ = _git_sub(cmd[1:])
        return ["git", sub] if sub else None
    if name in INTERPRETERS:
        args = cmd[1:]
        if not args or args[0] in INLINE_CODE:
            return None
        if args[0] == "-m" and len(args) > 1:
            return [name, "-m", args[1]]
        return None if args[0].startswith("-") else [name, args[0]]
    if name in TWO_WORDS:
        # `cargo test`, and `cargo --version` as itself: saying yes to a
        # version check must not allow every cargo command.
        return [name, cmd[1]] if len(cmd) > 1 else [name]
    # A script or program run by path keeps its path: a rule for ./scripts/x.sh
    # must not cover every x.sh.
    return [cmd[0] if "/" in cmd[0] else name]


def inline_key(words: list[str]) -> str | None:
    """`python3 -c`, `node -e`: an interpreter given its code on the command
    line. No rule is saved for it (it runs anything), but the user may allow
    it for the rest of a session: a model that reads an API's JSON with
    one-liners was asked about every single one."""
    cmd, _ = unwrap(words)
    if not cmd or os.path.basename(cmd[0]) not in INTERPRETERS:
        return None
    args = cmd[1:]
    return f"{os.path.basename(cmd[0])} {args[0]}" if args and args[0] in INLINE_CODE and args[0] != "-" else None


def always_ask(words: list[str]) -> bool:
    """Asked every time, whatever the rules say."""
    cmd, _ = unwrap(words)
    if not cmd:
        return True
    name = os.path.basename(cmd[0])
    if name in OPAQUE or name.startswith("mkfs"):
        return True
    key = key_of(words)
    if key is None:
        return True
    return name in ALWAYS_ASK or " ".join(key) in ALWAYS_ASK


def _outside(arg: str, project: Path | None, moved: bool) -> bool:
    """Whether an argument points outside the project: reading there is asked
    about, like opening a file outside the working folder."""
    value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
    if value in ("/dev/null", "-", ""):
        return False
    if "$" in value or value.startswith("~"):
        return True
    if value.startswith("/"):
        if project is None:
            return True
        try:
            Path(value).resolve().relative_to(project)
            return False
        except ValueError:
            # "/api/v1" in a grep is a pattern, not a path: only real places count.
            first = "/" + value.strip("/").split("/", 1)[0]
            return os.path.exists(first)
    if ".." in value.split("/"):
        if project is None or moved:
            return True  # after a `cd`, relative to somewhere this can't follow
        try:
            (project / value).resolve().relative_to(project)
            return False
        except ValueError:
            return True
    return False


def read_only(words: list[str], project: Path | None = None, moved: bool = False) -> bool:
    """Whether a simple command only reads, and only inside the project."""
    cmd, stripped = unwrap(words)
    if stripped or not cmd:
        return False  # an environment variable or a wrapper can change what a command does
    name, args = cmd[0], cmd[1:]
    if any(_outside(a, project, moved) for a in args):
        return False
    if any(a.split("=", 1)[0] in WRITING_ARGS.get(name, ()) for a in args):
        return False
    if name == "git":
        sub, rest, redirected = _git_sub(args)
        if redirected or sub is None:
            return False
        if sub in GIT_READ_ONLY:
            return True
        if sub == "config":  # reading a setting, never writing one
            return bool(rest) and rest[0] in ("--get", "--list", "-l") and len(rest) <= 2
        if sub == "stash":  # bare `git stash` stashes
            return rest == ["list"]
        if sub == "remote":
            return not rest or rest == ["-v"] or (rest[0] == "get-url" and len(rest) == 2)
        if sub == "reflog":  # expire and delete rewrite it
            return not rest or rest[0] == "show"
        if sub in GIT_LISTING:
            flags = [a for a in rest if a.startswith("-")]
            names = [a for a in rest if not a.startswith("-")]
            if any(f not in GIT_LISTING[sub] for f in flags):
                return False
            return not names or any(f in GIT_FORCES_LIST for f in flags)
        return False
    if name == "uniq":
        return len([a for a in args if not a.startswith("-")]) <= 1  # a second file is written
    if name == "hostname":
        return not args
    return name in READ_ONLY


def _covers(rule: str, key_words: list[str], cmd: list[str], deny: bool = False) -> bool:
    """A rule covers a command when its words open the command's key, or the
    command itself (rules written by hand may be longer than a key). An allow
    rule that is only a tool's name covers just the bare call; a deny rule
    like that covers everything the tool does (`deny make` means no make)."""
    want = rule.split()
    if not want:
        return False
    normal = [cmd[0] if "/" in want[0] else os.path.basename(cmd[0])] + cmd[1:]
    if want[-1] == "*":  # written by hand: "git *" is every git command
        want = want[:-1]
        return bool(want) and normal[:len(want)] == want
    if not deny and len(want) == 1 and want[0] in TWO_WORDS | INTERPRETERS:
        return len(normal) == 1 and normal == want  # the tool alone is only the bare call
    return key_words[:len(want)] == want or normal[:len(want)] == want


class Permissions:
    """One project's rules, saved between sessions and shared by every session
    open on the project (each change is read from and written to the file)."""

    def __init__(self, project: str, path: str | Path | None = None):
        self.project = Path(project).resolve()
        self.path = Path(path or os.environ.get("OPPX_PERMISSIONS") or Path.home() / ".config/oppx/permissions.json")
        self.run: list[str] = []
        self.deny: list[str] = []
        self.tools: list[str] = []
        # Allowed for this session only, never saved (the browser, once the
        # user said yes to it), and the tools that stay asked regardless.
        self.session_tools: set[str] = set()
        # Allowed until the chat ends, never saved: ordinary rules, and inline code (`python3 -c`).
        self.session_run: list[str] = []
        self.session_inline: set[str] = set()
        self.session_except: set[str] = set()
        self._stamp = None
        self.load()

    # ---- storage ------------------------------------------------------------
    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def load(self):
        data = self._read()
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        mine = projects.get(str(self.project)) if isinstance(projects.get(str(self.project)), dict) else {}
        every = data.get("global") if isinstance(data.get("global"), dict) else {}  # written by hand: all projects

        def both(key: str) -> list[str]:
            found = set()
            for src in (mine, every):
                rules = src.get(key, [])
                found |= {" ".join(r.split()) for r in (rules if isinstance(rules, list) else []) if isinstance(r, str) and r.strip()}
            return sorted(found)

        self.run, self.deny, self.tools = both("run"), both("deny"), both("tools")
        self._stamp = self._mtime()

    def _mtime(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _fresh(self):
        """Another session may have added a rule since this one read the file."""
        if self._mtime() != self._stamp:
            self.load()

    def _change(self, key: str, add=(), remove=()):
        """Read, change and write this project's list in one go, so a rule
        another session saved meanwhile isn't lost."""
        data = self._read()
        if not isinstance(data.get("projects"), dict):
            data["projects"] = {}
        projects = data["projects"]
        mine = projects.get(str(self.project)) if isinstance(projects.get(str(self.project)), dict) else {}
        lists = {k: {r for r in (mine.get(k) if isinstance(mine.get(k), list) else []) if isinstance(r, str)}
                 for k in ("run", "deny", "tools")}
        lists[key] = (lists[key] | set(add)) - set(remove)
        if any(lists.values()):
            projects[str(self.project)] = {k: sorted(v) for k, v in lists.items()}
        else:
            projects.pop(str(self.project), None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.path)
        self.load()

    # ---- commands -----------------------------------------------------------
    def denied(self, command: str) -> str | None:
        """The deny rule that forbids this command, if one does. A command
        that can't be taken apart is checked word by word as one line, too."""
        self._fresh()
        if not self.deny:
            return None
        for words in segments(command) or [command.split()]:
            cmd, _ = unwrap(words)
            key = key_of(words) or []
            for rule in self.deny:
                if cmd and _covers(rule, key, cmd, deny=True):
                    return rule
        return None

    def _covered(self, words: list[str], moved: bool) -> bool:
        if inline_key(words) in self.session_inline:
            return True
        if always_ask(words):
            return False
        if read_only(words, self.project, moved):
            return True
        cmd, _ = unwrap(words)
        key = key_of(words)
        return bool(cmd) and key is not None and any(_covers(rule, key, cmd) for rule in (*self.run, *self.session_run))

    def allows(self, command: str) -> bool:
        """Every part of the command is read-only or covered by an allow rule,
        and no deny rule forbids it."""
        self._fresh()
        parts = segments(command)
        if parts is None or self.denied(command):
            return False
        moved = False
        for words in parts:
            if not self._covered(words, moved):
                return False
            moved = moved or words[0] == "cd"
        return True

    def reads_only(self, command: str) -> bool:
        """Every part only reads (no rule involved): what plan mode may run."""
        parts = segments(command)
        if parts is None or self.denied(command):
            return False
        moved = False
        for words in parts:
            if always_ask(words) or not read_only(words, self.project, moved):
                return False
            moved = moved or words[0] == "cd"
        return True

    # ---- the decision (the app only prints and reads the answer) -----------
    def judge(self, command: str, read_only_turn: bool = False) -> tuple[str, str]:
        """What to do with a command before anyone is asked: ("run", ""),
        ("refuse", what the model is told), or ("ask", "")."""
        denied = self.denied(command)
        if denied:
            return "refuse", (f"Refused: the user's deny rule `{denied}` forbids this command. Don't try to do it "
                              "another way; carry on without it, or tell the user.")
        if read_only_turn:
            # Plan mode and /ask: looking is fine, anything else waits.
            if self.reads_only(command):
                return "run", ""
            return "refuse", ("Not run: edits are off for this request, so only commands that read can run "
                              "(git status, git log, ls, cat, rg …). Say what you would run and why.")
        return ("run", "") if self.allows(command) else ("ask", "")

    def answered(self, command: str, said: str) -> tuple[bool | str, str]:
        """The user's answer to "Allow?": (True to run, False, or a sentence
        for the model), and a line to show. "a" saves the rules on offer; for
        a command that can't be remembered it is a yes for this once. Anything
        that isn't yes, always or no is what the user wants done instead."""
        answer = said.strip().lower()
        rules = self.offer(command)
        if answer in ("a", "always") and rules:
            self.allow_always(command)
            return True, f"Allowed, and from now on without asking: {', '.join(rules)} (/permissions to change)"
        session = self.offer_session(command)
        if answer in ("s", "session") and session:
            self.allow_session(command)
            return True, f"Allowed, and for the rest of this session without asking: {', '.join(session)}"
        if answer in ("a", "always") and session:
            # The parts a rule can name are saved; the inline code is for this session.
            self.allow_session(command)
            keep = self.savable(session)
            if keep:
                self._change("run", add=keep)
                return True, (f"Allowed, and from now on without asking: {', '.join(keep)} (/permissions to change); "
                              f"for this session: {', '.join(r for r in session if r not in keep)}")
            return True, f"Allowed, and for the rest of this session without asking: {', '.join(session)}"
        if answer in ("y", "yes", "a", "always", "s", "session"):
            once = answer[0] in "as"
            return True, "Allowed" + (" this once (this kind of command is asked every time)" if once else "")
        if answer in ("", "n", "no"):
            return False, "Declined"
        return f"The user declined to run this command and said: {said.strip()}", "Declined, with your note"

    def offer(self, command: str) -> list[str]:
        """The rules "always" would add for this command; empty when it can't
        be remembered (not analyzable, or a part is asked every time)."""
        self._fresh()
        parts = segments(command)
        if parts is None or any(always_ask(p) for p in parts):
            return []
        rules, moved = [], False
        for words in parts:
            if not self._covered(words, moved):
                key = key_of(words)
                if key is None:
                    return []
                rules.append(" ".join(key))
            moved = moved or words[0] == "cd"
        return list(dict.fromkeys(rules))

    def offer_session(self, command: str) -> list[str]:
        """What "for this session" would allow: only for a command that can't
        get a saved rule because a part runs inline code (`python3 -c`), and
        nothing in it is of the kind asked every time (sudo, rm, a shell
        running text, something that can't be taken apart)."""
        self._fresh()
        parts = segments(command)
        if parts is None:
            return []
        rules, inline, moved = [], False, False
        for words in parts:
            if not self._covered(words, moved):
                code = inline_key(words)
                if code:
                    inline = True
                    rules.append(code)
                elif always_ask(words) or key_of(words) is None:
                    return []
                else:
                    rules.append(" ".join(key_of(words)))
            moved = moved or words[0] == "cd"
        return list(dict.fromkeys(rules)) if inline else []

    @staticmethod
    def savable(session: list[str]) -> list[str]:
        """Of what `offer_session` names, the rules that may be saved (not the inline code)."""
        return [r for r in session if not (len(r.split()) == 2 and r.split()[0] in INTERPRETERS and r.split()[1] in INLINE_CODE)]

    def allow_session(self, command: str) -> list[str]:
        """Allows what `offer_session` names until the chat ends. Nothing is written."""
        rules = self.offer_session(command)
        for words in segments(command) or []:
            code = inline_key(words)
            if code in rules:
                self.session_inline.add(code)
        self.session_run += [r for r in rules if r not in self.session_inline and r not in self.session_run]
        return rules

    def allow_always(self, command: str) -> list[str]:
        rules = self.offer(command)
        if rules:
            self._change("run", add=rules)
        return rules

    def add(self, kind: str, rule: str) -> bool:
        """A rule written by the user (/permissions allow|deny …). An allow
        rule for something that is asked every time is refused."""
        rule = " ".join(rule.split())
        if not rule or kind not in ("run", "deny", "tools"):
            return False
        if kind == "run" and always_ask(rule.split()):
            return False
        self._change(kind, add=[rule])
        return True

    # ---- connector tools ----------------------------------------------------
    @staticmethod
    def _connector_of(name: str) -> str:
        return name.split(".", 1)[0] if "." in name else name

    def tool_allowed(self, name: str) -> bool:
        """Allowed by its own rule, or by its connector's ("linear.*")."""
        self._fresh()
        if self.tool_denied(name):
            return False
        wild = f"{self._connector_of(name)}.*"
        session = name not in self.session_except and (name in self.session_tools or wild in self.session_tools)
        return name in self.tools or wild in self.tools or session

    def allow_for_session(self, rule: str, unless=()):
        """Allows a tool, or every tool of a connector ("playwright.*"), until
        the chat ends; `unless` stay asked."""
        self.session_tools.add(rule)
        self.session_except.update(unless)

    def tool_denied(self, name: str) -> bool:
        self._fresh()
        return name in self.deny or f"{self._connector_of(name)}.*" in self.deny

    def allow_tool(self, name: str):
        self._change("tools", add=[name])

    def judge_tool(self, name: str, read_only: bool) -> tuple[str, str]:
        """("run", ""), ("refuse", why) or ("ask", "") for a connector call.
        A tool its connector marks as only reading runs without a question,
        like a command that only reads; deny rules still win."""
        if self.tool_denied(name):
            return "refuse", "The user's deny rule forbids this tool; carry on without it, or tell the user."
        if read_only or self.tool_allowed(name):
            return "run", ""
        return "ask", ""

    def answered_tool(self, name: str, said: str) -> tuple[bool | str, str]:
        """The answer to "Call this connector tool?": y once, a always for this
        tool, c always for every tool of its connector, N, or what to do instead."""
        answer = said.strip().lower()
        connector = self._connector_of(name)
        if answer in ("a", "always"):
            self.allow_tool(name)
            return True, f"Allowed, and from now on without asking: {name} (/permissions to change)"
        if answer in ("c", "connector") and connector != name:
            self.allow_tool(f"{connector}.*")
            return True, f"Allowed, and from now on without asking: every {connector} tool (/permissions to change)"
        if answer in ("y", "yes"):
            return True, "Allowed"
        if answer in ("", "n", "no"):
            return False, "Declined"
        return f"The user declined this call and said: {said.strip()}", "Declined, with your note"

    # ---- managing -----------------------------------------------------------
    def remove(self, rule: str, kind: str | None = None) -> bool:
        """Removes a rule from one list, or from every list it is in."""
        self._fresh()
        rule = " ".join(rule.split())
        had = [k for k in ((kind,) if kind else ("run", "deny", "tools")) if rule in getattr(self, k)]
        for k in had:
            self._change(k, remove=[rule])
        return bool(had)

    def clear(self):
        for k in ("run", "deny", "tools"):
            self._change(k, remove=list(getattr(self, k)))
