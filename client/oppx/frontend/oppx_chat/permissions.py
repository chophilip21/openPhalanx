"""What the model may run without asking, modelled on Claude Code's permissions.

* Commands that only read (`git status`, `git log`, `ls`, `cat`, `rg`,
  `sed -n 1,5p`, `node --version` …) inside the project never ask.
* Anything else is asked once. "Always" saves an allow rule for the project
  (`cargo test`, `npm run`, `python -m pytest`, a connector tool's name); from
  then on what the rule covers runs without a question, in every session.
* A rule covers what a command does, not where: an argument outside the
  project is asked about whatever the rules say, and "always" then allows
  that folder (`dirs`), never a folder that holds credentials.
* Deny rules refuse a command outright; they win over everything, and are
  looked for behind wrappers, in loops and in the text given to a shell.
* A command is covered only when every part of it is: `a && b | c` needs a, b
  and c each to be read-only or allowed. What can't be taken apart safely
  (command substitution, a background job, a here-document) is always asked.
* Some commands are asked every time and can't be remembered: the ones that
  destroy or publish (`rm`, `git push`, `git checkout -- .`, `npm publish`),
  and the ones that run arbitrary code given as text (`bash -c`, `eval`).

The line is read the way `sh` reads it (`tokenize`): quotes decide what is a
pattern and what is shell syntax, so `grep 'a|b$' f` is one command with one
pattern, and `ls x#; rm y` is two commands.

Rules are lists of words matched from the start of a command ("cargo test"
covers `cargo test -p x`, not `cargo testx` or `cargo build`). A tool's name
alone ("cargo", "git") covers only the bare call; "git *", written by hand,
covers every git command that isn't asked every time. A command that uses a
tool's dangerous side (`find -delete`, `sed -i`, `curl -d`, `git -c …`) gets a
rule for that exact command only.

No UI and no dependencies: the app asks the questions, the tests run it alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
from pathlib import Path

# Tools whose first argument is the real command: the rule keeps both words.
TWO_WORDS = {"git", "cargo", "npm", "npx", "pnpm", "yarn", "docker", "kubectl", "gh", "uv", "pip", "pip3", "go",
             "poetry", "bun", "deno", "apt", "apt-get", "brew", "systemctl", "docker-compose", "make", "just", "tox",
             "pipenv", "pipx"}
# Their own options before the subcommand (without a value, with one): skipped
# to find it, so `make -j8 test` is `make test` and `npm --prefix app run x` is
# `npm run`. An option not listed here makes the rule the exact command.
GLOBAL_FLAGS = {
    "cargo": ({"-q", "--quiet", "-v", "--verbose", "--locked", "--offline", "--frozen"}, {"--color"}),
    "npm": ({"-s", "--silent", "--no-fund", "--no-audit"}, {"--prefix", "-w", "--workspace"}),
    "pnpm": ({"-r", "--recursive", "-s", "--silent"}, {"-C", "--dir", "--filter", "-F"}),
    "yarn": ({"-s", "--silent"}, {"--cwd"}),
    "make": ({"-s", "-k", "-B", "--silent", "--keep-going"}, {"-C", "-f"}),
    "docker": (set(), {"--context", "-H", "--host", "-l", "--log-level"}),
    "docker-compose": (set(), {"-f", "--file", "-p", "--project-name"}),
    "kubectl": (set(), {"-n", "--namespace", "--context", "--cluster"}),
    "gh": (set(), {"-R", "--repo"}),
    "go": (set(), {"-C"}),
    "uv": ({"-q", "--quiet", "-v", "--verbose", "--offline", "--no-progress"}, {"--directory", "--project"}),
    "pip": ({"-q", "--quiet", "-v", "--verbose"}, set()),
    "pip3": ({"-q", "--quiet", "-v", "--verbose"}, set()),
    "apt": ({"-y", "-q", "-qq"}, set()),
    "apt-get": ({"-y", "-q", "-qq"}, set()),
    "systemctl": ({"--user", "--no-pager"}, set()),
}
# A third word that names what is done: `gh pr view`, `docker compose up`.
THREE_WORDS = {("docker", "compose")}
GIT_THREE = {"stash", "remote", "worktree", "submodule", "bisect"}
# Subcommands that run another program: the rule names that program too
# (`uv run pytest`), and what it runs is judged like any other command.
RUNNERS = {"uv": {"run"}, "poetry": {"run"}, "pipenv": {"run"}, "pipx": {"run"}, "npm": {"exec", "x"},
           "pnpm": {"exec", "dlx"}, "yarn": {"exec", "dlx"}, "bun": {"x"}, "go": {"run"}, "deno": {"run"}}
SOLO_RUNNERS = {"npx", "bunx", "uvx", "pnpx"}
# These run a command somewhere else (a container): the rule is the exact command.
EXACT_RUNNERS = {("docker", "exec"), ("docker", "run"), ("kubectl", "exec"), ("docker-compose", "exec"),
                 ("docker-compose", "run")}
RUNNER_FLAGS = {"-y", "--yes", "-q", "--quiet", "--silent", "--no-install", "--locked", "--frozen", "--no-sync",
                "--offline"}
# Interpreters: the rule names what is run (`python -m pytest`, `node build.js`), never the interpreter alone.
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php"}
INLINE_CODE = {"-c", "-e", "--eval", "-p", "--print", "-"}
PYTHON_FLAGS = ({"-u", "-B", "-O", "-OO", "-q", "-s", "-S", "-E", "-I", "-v", "-b", "-d"}, {"-W", "-X"})
NODE_CODE_FLAGS = {"--eval", "--print", "--require", "--import", "--loader", "--experimental-loader"}
# Asked every time, never remembered: one wrong unattended call loses work or publishes it.
ALWAYS_ASK = ("sudo", "su", "doas", "rm", "dd", "shutdown", "reboot", "chmod", "chown", "truncate", "shred",
              "kill", "pkill", "killall", "unlink", "wipefs", "fdisk", "parted", "crontab")
GIT_ALWAYS = {"push", "reset", "clean", "rebase", "filter-branch", "filter-repo", "rm", "prune", "update-ref"}
PUBLISH = {
    "npm": {"publish", "unpublish", "deprecate", "login", "logout", "adduser", "owner", "token"},
    "pnpm": {"publish"}, "yarn": {"publish"}, "bun": {"publish"}, "poetry": {"publish"}, "uv": {"publish"},
    "cargo": {"publish", "yank", "login", "logout", "owner"},
    "docker": {"push", "login", "logout", "rm", "rmi"},
    "kubectl": {"delete", "drain", "cordon"},
}
SHELLS = {"bash", "sh", "zsh", "fish", "dash", "ksh"}
# These run whatever they are given: a rule for them would allow anything.
OPAQUE = {"eval", "exec", "watch", "parallel", "ssh", "setsid", "pkexec", "busybox", "strace", "ltrace", "flock",
          "script", "runuser", "chroot", "systemd-run", "unshare", "nsenter", "expect", "at", "batch", "gdb"}
# Run another command given after them; the command inside is what counts.
WRAPPERS = {"env", "command", "nohup", "time", "nice", "ionice", "stdbuf", "timeout", "builtin"}
WRAPPER_VALUES = {"env": {"-u", "--unset", "-C", "--chdir"}, "timeout": {"-s", "--signal", "-k", "--kill-after"},
                  "nice": {"-n", "--adjustment"}, "ionice": {"-c", "-n", "-p"}, "stdbuf": {"-i", "-o", "-e"},
                  "time": {"-o", "--output", "-f", "--format"}}
XARGS_FLAGS = ({"-0", "-r", "-t", "-x", "--null", "--no-run-if-empty", "--verbose"},
               {"-n", "-I", "-P", "-L", "-d", "-s", "-E"})
# Deny rules look behind these for the command they run (after this many words of their own).
PEEL = {"sudo": 0, "doas": 0, "setsid": 0, "pkexec": 0, "busybox": 0, "strace": 0, "ltrace": 0, "exec": 0, "watch": 0,
        "parallel": 0, "systemd-run": 0, "unshare": 0, "nsenter": 0, "flock": 1, "chroot": 1, "ssh": 1}
# Only read; nothing they print can change the machine.
READ_ONLY = {"cd", "ls", "find", "cat", "head", "tail", "wc", "pwd", "echo", "which", "file", "stat", "du", "df", "tree",
             "date", "whoami", "uname", "rg", "grep", "egrep", "fgrep", "nl", "sort", "uniq", "cut", "basename",
             "dirname", "realpath", "true", "false", "diff", "cmp", "jq", "column", "id", "hostname", "tr", "test", "[",
             "printf", "sleep", "ps", "pgrep", "lsof", "md5sum", "sha1sum", "sha256sum", "sha512sum", "cksum", "xxd",
             "hexdump", "od", "strings", "readlink", "tac", "rev", "comm", "seq", "nproc", "free", "uptime", "zcat",
             "type", "nvidia-smi", "fold", "paste", "join", "expand", "arch", "lscpu", ":"}
# Subcommands of the two-word tools that only list or show.
SUB_READ_ONLY = {
    "cargo": {"tree", "metadata", "version", "locate-project", "pkgid"},
    "npm": {"ls", "list", "prefix", "root"}, "pnpm": {"ls", "list"}, "yarn": {"list", "why"},
    "pip": {"list", "show", "freeze", "check"}, "pip3": {"list", "show", "freeze", "check"},
    "docker": {"ps", "images", "version"}, "go": {"version", "env", "list"}, "uv": {"version", "tree"},
    "systemctl": {"status", "is-active", "is-enabled", "list-units"}, "brew": {"list", "info"},
    "apt": {"list", "show"},
}
VERSION_FLAGS = {"--version", "-V"}
VERSIONED = TWO_WORDS | INTERPRETERS | READ_ONLY | {"rustc", "rustup", "gcc", "g++", "clang", "java", "javac", "tsc",
                                                    "pytest", "ruff", "mypy", "eslint", "prettier", "vite", "curl",
                                                    "wget", "sed", "awk", "bash", "sh", "zsh", "cmake", "ninja"}
VERSIONED_V = {"node", "npm", "npx", "pnpm", "yarn", "bun", "deno"}  # `-v` is the version there
# Arguments that turn a reading command into one that writes or runs something.
WRITING_ARGS = {
    "find": {"-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls", "-fprint0", "-files0-from"},
    "sort": {"-o", "--output", "--compress-program"}, "tree": {"-o"}, "date": {"-s", "--set"},
    "rg": {"--pre", "--pre-glob", "--hostname-bin"}, "file": {"-C", "--compile", "-f", "--files-from"},
    "xxd": {"-r", "-revert"}, "nvidia-smi": {"-f", "--filename", "-r", "--gpu-reset"},
    "git": {"--output", "-O", "--open-files-in-pager", "--ext-diff"},
}
GIT_READ_ONLY = {"status", "log", "diff", "show", "blame", "ls-files", "grep", "rev-parse", "describe", "shortlog",
                 "cat-file", "ls-tree", "merge-base", "rev-list", "name-rev", "whatchanged", "for-each-ref", "show-ref",
                 "var", "version", "check-ignore", "diff-tree", "show-branch", "cherry", "range-diff",
                 "count-objects"}
# Git commands that read with no argument or a listing flag, and write otherwise.
GIT_LISTING = {"branch": {"-a", "-r", "-v", "-vv", "--list", "-l", "--show-current", "--all", "--remotes", "--color",
                          "--no-color", "--column"},
               "tag": {"-l", "--list", "-n"}}
GIT_FORCES_LIST = {"--list", "-l"}  # with these, further arguments are patterns
# These only exist in the listing form, and take a value.
GIT_LIST_VALUED = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}
GIT_LIST_OPTIONS = {"--sort", "--format"}  # take a value, and list only when no name follows
GIT_CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--list", "-l"}
GIT_CONFIG_FLAGS = GIT_CONFIG_READS | {"--global", "--local", "--system", "--show-origin", "--show-scope",
                                       "--name-only", "-z"}
# Git's own options before the subcommand; -C and -c change where and how it runs.
GIT_GLOBAL_FLAGS = {"--no-pager", "-p", "--paginate", "--no-optional-locks"}
SEPARATORS = {"&&", "||", ";", "|"}
KEYWORDS = {"if", "then", "else", "elif", "while", "do", "!", "{"}  # what follows them is the command
CLOSERS = {"done", "fi", "}"}
UNPARSED = {"case", "function", "select", "[[", "((", "coproc", "until"}
DECLARE = {"export", "readonly", "local", "declare", "typeset"}
# Arguments that are text, not places: their tool never opens them.
NO_PATHS = {"echo", "printf", "test", "[", "true", "false", "sleep", "seq", ":", "type", "which", "command"}
PATTERN_FIRST = {"grep", "egrep", "fgrep", "rg", "sed", "awk", "jq"}  # the first plain argument is a pattern
SEARCH_FLAGS = {"-e", "--regexp", "-g", "--glob", "--iglob", "--include", "--exclude", "--exclude-dir", "--grep",
                "--author", "--committer", "-S", "-G", "--format", "--pretty", "--since", "--until", "--sort",
                "--type", "-t"}
PATTERN_FLAGS = {  # options whose value is a pattern or a name
    "grep": SEARCH_FLAGS, "egrep": SEARCH_FLAGS, "fgrep": SEARCH_FLAGS, "rg": SEARCH_FLAGS, "git": SEARCH_FLAGS,
    "awk": {"-F", "-v"}, "cut": {"-d", "--delimiter"}, "sort": {"-t", "-k"}, "jq": {"--arg", "--argjson"},
    "find": {"-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-wholename", "-printf", "-newermt"},
    "column": {"-s"}, "tr": set(), "date": {"-d", "--date"}, "curl": {"-H", "--header", "-w", "--write-out"},
}
# Variables that say where things are, not secrets: their value is read like a typed one.
PLAIN_VARS = {"HOME", "PWD", "OLDPWD", "USER", "LOGNAME", "PATH", "SHELL", "TMPDIR", "LANG", "TERM", "CARGO_HOME",
              "VIRTUAL_ENV", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"}
# "Always" is never offered for these places: they hold credentials, or are too wide.
BROAD_DIRS = ("/", "/home", "/root", "/etc", "/var", "/usr", "/boot")
SECRET_ROOTS = ("/proc", "/sys", "/dev", "/run", "/root", "/boot")
SECRET_HOME = {".ssh", ".gnupg", ".aws", ".config", ".kube", ".docker", ".azure", ".password-store", ".mozilla",
               ".claude", ".netrc", ".npmrc", ".pypirc", ".git-credentials"}
# Folders everything on the machine writes into: "always" there is for the one file, not the folder.
SHARED_DIRS = ("/tmp", "/var/tmp", "/dev/shm")
REDIRECT = ">"  # the rule that allows output redirected into files of the project
YES = {"y", "yes", "ok", "okay", "k", "sure", "yep", "yeah", "yup", "yes please", "please", "please do", "go",
       "go ahead", "do it", "run it", "fine", "alright"}
ALWAYS = {"a", "always"}
SESSION = {"s", "session"}

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPS = ("&&", "||", ";;", "|&", "&>>", "&>", ">>", ">&", ">|", "<<<", "<<", "<&", "<>", "<(", ">(",
        ";", "|", "&", "(", ")", "<", ">")
_ASSIGNED = re.compile(r"(?:^|[\s;&|(])(?:(?:export|local|declare|readonly|typeset)\s+(?:-\w+\s+)*)?([A-Za-z_]\w*)\+?="
                       r"|\bfor\s+([A-Za-z_]\w*)|\bread\s+(?:-\w+\s+)*([A-Za-z_]\w*)")
_BRACES = re.compile(r"\{[^{}\s]*(,|\.\.)[^{}\s]*\}")


# ---------------------------------------------------------------------------
# Reading the line the way sh does
# ---------------------------------------------------------------------------

class W(str):
    """A word of a command with the shell's quoting undone."""
    quoted = False   # part of it was quoted or escaped
    unknown = False  # holds something this can't follow: an unset or secret variable, {a,b}, ~user
    vars: tuple = ()  # variables set on this line (a loop's `f`), left out of the text


class Op(str):
    """Shell syntax between words: a separator or a redirection."""


class Seg(list):
    """One simple command as words, with what its redirections read and write."""

    def __init__(self, words=()):
        super().__init__(words)
        self.writes: list = []  # files its output goes into (not /dev/null)
        self.reads: list = []   # files it reads with <
        self.taint = False      # an earlier `export X=…` on the line changes its environment
        self.loops: dict = {}   # `for f in a b` before it: f -> [a, b]


def tokenize(command: str) -> list | None:
    """Words (W) and operators (Op) as sh would see them; None for what can't
    be followed (command substitution, an unclosed quote, a second line)."""
    assigned = {n for m in _ASSIGNED.finditer(command) for n in m.groups() if n}
    out: list = []
    buf: list[str] = []
    bare: list[str] = []  # the unquoted characters of the word, for ~ and {a,b}
    state = {"has": False, "quoted": False, "unknown": False, "vars": [], "tilde": False}
    n, i = len(command), 0

    def flush():
        if not state["has"]:
            return
        text = "".join(buf)
        unknown = state["unknown"] or bool(_BRACES.search("".join(bare)))
        if state["tilde"] == "home":
            text = str(Path.home()) + text[1:]
        elif state["tilde"]:
            unknown = True  # ~user
        w = W(text)
        w.quoted, w.unknown, w.vars = state["quoted"], unknown, tuple(state["vars"])
        out.append(w)
        buf.clear()
        bare.clear()
        state.update(has=False, quoted=False, unknown=False, vars=[], tilde=False)

    def expand(at: int, split: bool) -> int | None:
        """Puts the value of the $… at `at` into the word; the index after it."""
        nxt = command[at + 1] if at + 1 < n else ""
        if nxt == "(":
            return None
        if nxt == "{":
            close = command.find("}", at + 2)
            inner = command[at + 2:close]
            if close < 0 or "$" in inner or "`" in inner:
                return None
            if not _NAME.fullmatch(inner):
                state["unknown"] = True  # ${x:-y}, ${#x} …
                return close + 1
            name, end = inner, close + 1
        else:
            m = _NAME.match(command, at + 1)
            if not m:
                if nxt and (nxt.isdigit() or nxt in "?#@*!$-"):
                    state["unknown"] = True
                    return at + 2
                buf.append("$")  # a lone $, as in the pattern foo$
                return at + 1
            name, end = m.group(), m.end()
        if name in assigned:
            state["vars"].append(name)
            return end
        value = os.environ.get(name)
        if value is None or name not in PLAIN_VARS or (split and re.search(r"[\s*?\[]", value)):
            state["unknown"] = True
            return end
        buf.append(value)
        return end

    while i < n:
        c = command[i]
        if c in " \t":
            flush()
            i += 1
        elif c == "\n":
            return None
        elif c == "\\":
            if i + 1 >= n:
                return None
            if command[i + 1] != "\n":
                buf.append(command[i + 1])
                state.update(has=True, quoted=True)
            i += 2
        elif c == "'":
            close = command.find("'", i + 1)
            if close < 0:
                return None
            buf.append(command[i + 1:close])
            state.update(has=True, quoted=True)
            i = close + 1
        elif c == '"':
            i += 1
            state.update(has=True, quoted=True)
            while True:
                if i >= n:
                    return None
                d = command[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and i + 1 < n and command[i + 1] in '$`"\\\n':
                    if command[i + 1] != "\n":
                        buf.append(command[i + 1])
                    i += 2
                elif d == "`":
                    return None
                elif d == "$":
                    i = expand(i, split=False)
                    if i is None:
                        return None
                else:
                    buf.append(d)
                    i += 1
        elif c == "`":
            return None
        elif c == "$":
            if i + 1 < n and command[i + 1] in "'\"":
                return None  # $'…' and $"…"
            state["has"] = True
            i = expand(i, split=True)
            if i is None:
                return None
        elif c == "#" and not state["has"]:
            break  # a comment, to the end of the line (only at the start of a word)
        elif c in "|&;()<>":
            if c in "<>" and state["has"] and not state["quoted"] and "".join(buf).isdigit():
                buf.clear()  # the 2 of 2>/dev/null
                bare.clear()
                state["has"] = False
            flush()
            op = next(o for o in _OPS if command.startswith(o, i))
            out.append(Op(op))
            i += len(op)
        else:
            if c == "~" and not state["has"]:
                # sh expands it only when a / or the end of the word follows, unquoted.
                after = command[i + 1] if i + 1 < n else " "
                state["tilde"] = "home" if after in " \t/|&;()<>" else "user" if (after.isalnum() or after in "+-_") else False
            buf.append(c)
            bare.append(c)
            state["has"] = True
            i += 1
    flush()
    return out


def segments(command: str) -> list[Seg] | None:
    """The simple commands a shell line is made of; None when it can't be
    taken apart safely (then it is always asked about). Loop and condition
    keywords are structure, not commands: `for f in a; do rm $f; done` is the
    command `rm $f`."""
    tokens = tokenize(command)
    if not tokens:
        return None
    raw, i = [Seg()], 0
    while i < len(tokens):
        t = tokens[i]
        i += 1
        if not isinstance(t, Op):
            raw[-1].append(t)
        elif t in SEPARATORS:
            raw.append(Seg())
        elif t in (">", ">>", "&>", "&>>", ">&", ">|", "<", "<&"):
            target = tokens[i] if i < len(tokens) else None
            i += 1
            if not isinstance(target, W):
                return None
            if t in (">&", "<&") and (target.isdigit() or target == "-") and not target.quoted:
                continue  # the other stream (`> 1` is a file called 1; only `>&1` is the stream)
            if t == "<&":
                return None
            if target in ("/dev/null", "/dev/stdout", "/dev/stderr") and not target.unknown:
                continue
            (raw[-1].reads if t == "<" else raw[-1].writes).append(target)
        else:
            return None  # a subshell, a background job, a here-document
    if len(raw) > 1 and not raw[-1] and not raw[-1].writes and not raw[-1].reads:
        raw.pop()  # `ls;`
    out, loops, taint = [], {}, False
    for seg in raw:
        if not seg:
            return None
        first = seg[0]
        plain = not first.quoted
        if plain and first in CLOSERS and len(seg) == 1:
            if seg.writes or seg.reads:
                return None
            continue
        if plain and first == "for":
            if len(seg) < 3 or seg[2] != "in" or not _NAME.fullmatch(seg[1]):
                return None
            loops = {**loops, str(seg[1]): list(seg[3:])}
            continue
        if plain and first in UNPARSED:
            return None
        while seg and not seg[0].quoted and seg[0] in KEYWORDS:
            del seg[0]
        if not seg:
            return None
        body = seg[1:] if seg[0] in DECLARE and not seg[0].quoted else seg
        if body and all(_is_assignment(w) for w in body):
            if seg.writes or seg.reads:
                return None
            taint = True  # `export PATH=…`: nothing runs, what follows runs differently
            loops = {k: v for k, v in loops.items() if k not in {w.partition("=")[0].rstrip("+") for w in body}}
            continue
        seg.taint, seg.loops = taint, loops
        out.append(seg)
    return out


def _is_assignment(word: str) -> bool:
    name, eq, _ = word.partition("=")
    return bool(eq) and bool(name) and name.replace("_", "a").isalnum() and not name[0].isdigit()


def _xargs(args: list) -> list | None:
    """The command xargs runs; None when its options can't be followed."""
    plain, valued = XARGS_FLAGS
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in plain or (a.startswith("--") and "=" in a and a.split("=", 1)[0] in
                          ("--max-args", "--max-procs", "--delimiter", "--replace", "--max-lines")):
            i += 1
        elif a in valued:
            i += 2
        elif a[:2] in valued:
            i += 1
        else:
            return None
    return list(args[i:])


def unwrap(words: list[str]) -> tuple[list[str], bool]:
    """The command actually run, without leading VAR=value assignments and
    wrappers (`env`, `timeout 60`, `nice -n 5`, `time`, `nohup` …), and
    whether anything was stripped. `xargs cmd` is `cmd`. Empty when a wrapper
    hides what runs (`env -S '…'`)."""
    cmd, stripped, _ = _unwrap(words)
    return cmd, stripped


def _unwrap(words: list[str]) -> tuple[list[str], bool, bool]:
    """`unwrap`, and whether the command gets its arguments from xargs."""
    cmd, stripped, piped = list(words), False, False
    while cmd:
        if _is_assignment(cmd[0]):
            cmd, stripped = cmd[1:], True
            continue
        name = os.path.basename(cmd[0])
        if name == "xargs":
            inner = _xargs(cmd[1:])
            if inner is None:
                return [], True, True
            # What it runs is known; the names it is handed are not, so it
            # never counts as only reading (a list of paths can point anywhere).
            cmd, stripped, piped = inner or [W("echo")], True, True
            continue
        if name not in WRAPPERS or (name == "command" and len(cmd) > 1 and cmd[1] in ("-v", "-V")):
            break
        cmd, stripped = cmd[1:], True
        valued = WRAPPER_VALUES.get(name, ())
        while cmd and (cmd[0].startswith("-") or (name == "env" and _is_assignment(cmd[0]))):
            flag = cmd.pop(0)
            if name == "env" and (flag.startswith("-S") or flag.startswith("--split-string")):
                return [], True, piped
            if flag == "--":
                break
            if flag in valued and cmd:
                cmd.pop(0)  # its value, written apart (nice -n 5, env -u NAME)
        if name == "timeout" and cmd:
            cmd = cmd[1:]  # the duration
    return cmd, stripped, piped


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _where(arg: str, project: Path | None, cwd: Path | None | bool = False, dirs=(), safe=()) -> str:
    """Where an argument points: "" inside the project (or not a path, or in
    a folder the user allowed), "?" when it can't be known, else the place.
    `cwd`: where the line has `cd`-ed to (None or True: somewhere this can't
    follow; False: nowhere, still the project)."""
    if cwd is False:
        cwd = project
    elif cwd is True:
        cwd = None
    if getattr(arg, "unknown", False) or any(v not in safe for v in getattr(arg, "vars", ())):
        return "?"
    value = str(arg)
    if value.startswith("-"):
        if "=" in value:
            value = value.split("=", 1)[1]
        elif len(value) > 2 and not value.startswith("--") and value[2] == "/":
            value = value[2:]  # -f/etc/hosts
        else:
            return ""
    if value in ("/dev/null", "-", ""):
        return ""
    if value.startswith("/"):
        try:
            place = Path(value).resolve()
        except (OSError, ValueError):
            return "?"
        if project is not None and _inside(place, project):
            return ""
        # "/api/v1" in a grep is a pattern, not a path: only real places count.
        if not os.path.exists("/" + value.strip("/").split("/", 1)[0]):
            return ""
    elif project is None or cwd is None:
        # A relative name, from somewhere this can't follow.
        return "?" if cwd is None and project is not None or ".." in value.split("/") else ""
    elif ".." in value.split("/") or not _inside(cwd, project):
        place = (cwd / value).resolve()
        if _inside(place, project):
            return ""
    else:
        return ""
    return "" if any(_inside(place, Path(d)) for d in dirs) else str(place)


def grant_dir(place: str) -> str | None:
    """What "always" would allow for a place outside the project: its folder,
    or only the file itself when it lies directly in a folder every program
    shares (/tmp). None for one that is too wide or holds credentials."""
    p = Path(place)
    d = p if p.is_dir() else p.parent
    home = Path.home()
    if str(d) in SHARED_DIRS:
        return None if d == p else str(p)
    if str(d) in BROAD_DIRS or d == home or d == home.parent or any(_inside(d, Path(r)) for r in SECRET_ROOTS):
        return None
    if _inside(p, home):
        top = p.relative_to(home).parts[:1]
        if top and top[0] in SECRET_HOME:
            return None
        if p.relative_to(home).parts[:3] == (".local", "share", "keyrings"):
            return None
    return str(d)


def _git_sub(args: list[str], project: Path | None = None) -> tuple[str | None, list[str], bool]:
    """A git command's subcommand and its arguments, and whether options that
    redirect git (`-C dir` out of the project, `-c key=value`, `--git-dir`)
    came before it."""
    redirected, i = False, 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C":
            where = args[i + 1] if i + 1 < len(args) else None
            if where is None or project is None or getattr(where, "unknown", False) or getattr(where, "vars", ()) \
                    or not _inside((project / str(where)).resolve(), project):
                redirected = True  # another repository
            i += 2
        elif args[i] == "-c":
            redirected, i = True, i + 2
        elif args[i] in GIT_GLOBAL_FLAGS:
            i += 1
        else:
            redirected, i = True, i + 1  # --git-dir=…, --work-tree=…, anything unknown
    return (args[i] if i < len(args) else None), list(args[i + 1:]), redirected


def tool_sub(cmd: list[str]) -> tuple[str | None, int, bool]:
    """A two-word tool's subcommand, where it is, and whether every option
    before it was one this knows (so the rule can leave them out)."""
    name = os.path.basename(cmd[0])
    plain, valued = GLOBAL_FLAGS.get(name, ((), ()))
    i = 1
    while i < len(cmd) and (cmd[i].startswith("-") or (name == "cargo" and cmd[i].startswith("+"))):
        a = cmd[i]
        if (name == "cargo" and a.startswith("+")) or a in plain:
            i += 1
        elif name == "make" and re.fullmatch(r"-j\d*", a):
            i += 2 if a == "-j" and i + 1 < len(cmd) and cmd[i + 1].isdigit() else 1
        elif a in valued:
            i += 2
        elif "=" in a and a.split("=", 1)[0] in valued:
            i += 1
        else:
            return None, i, False
    return (cmd[i] if i < len(cmd) else None), i, True


def runner(cmd: list[str]) -> tuple[list[str], list[str] | None] | None:
    """For `uv run pytest -x`: (["uv", "run"], ["pytest", "-x"]). The second
    is None when what runs can't be told apart from the runner's own options."""
    name = os.path.basename(cmd[0])
    if name in SOLO_RUNNERS:
        prefix, rest = [name], list(cmd[1:])
    else:
        if name not in RUNNERS and not any(name == r[0] for r in EXACT_RUNNERS):
            return None
        sub, i, clean = tool_sub(cmd)
        if not clean or sub is None:
            return None
        if (name, sub) in EXACT_RUNNERS:
            return [name, sub], None
        if sub not in RUNNERS.get(name, ()):
            return None
        prefix, rest = [name, str(sub)], list(cmd[i + 1:])
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag == "--":
            break
        if flag not in RUNNER_FLAGS:
            return prefix, None
    return prefix, rest


def _shell_script(cmd: list[str]) -> str | None:
    """The script file a shell is given (`bash -eu scripts/test.sh`); None for
    text to run (-c), a bare shell, or options this doesn't know."""
    for a in cmd[1:]:
        if a.startswith("-"):
            if not re.fullmatch(r"-[euxvn]+", a):
                return None
            continue
        return None if getattr(a, "unknown", False) else str(a)
    return None


def _opaque(cmd: list[str]) -> bool:
    """Runs text it is given, so no rule can say what it does."""
    name = os.path.basename(cmd[0])
    if name in SHELLS:
        return _shell_script(cmd) is None
    if name in ("source", "."):
        return len(cmd) < 2 or cmd[1].startswith("-")
    return name in OPAQUE


def _find_execs(args: list[str]) -> list[list[str]]:
    """The commands a find runs with -exec."""
    out, i = [], 0
    while i < len(args):
        if args[i] in ("-exec", "-execdir"):
            j = i + 1
            while j < len(args) and args[j] not in (";", "+"):
                j += 1
            out.append(list(args[i + 1:j]))
            i = j
        i += 1
    return out


def _dangerous(cmd: list[str], project: Path | None = None) -> bool:
    """Uses an option that makes a reading tool write or run something."""
    name, args = os.path.basename(cmd[0]), cmd[1:]
    if name == "find":
        for inner in _find_execs(args):
            run, stripped = unwrap(inner)
            if stripped or not _reads(run, project):
                return True
    bad = WRITING_ARGS.get(name, ())
    for a in args:
        if a.split("=", 1)[0] in bad or a.startswith("--files0-from"):
            return True  # (--files0-from: a list of names that can point anywhere)
        if len(a) > 2 and a[0] == "-" and a[1] != "-" and a[:2] in bad:
            return True  # the value written onto the option: sort -oout.txt, git grep -Oless
    return False


_SED_ADDR = r"(?:(?:\d+|\$|/[^/\n]*/)(?:,(?:\d+|\$|\+\d+|/[^/\n]*/))?)?"
_SED_PRINT = re.compile(_SED_ADDR + r"\s*!?\s*[pdq=]?")
_SED_SUBST = re.compile(_SED_ADDR + r"\s*s([/|#,:@])(?:(?!\1)[^\n])*\1(?:(?!\1)[^\n])*\1[gpiI0-9]*")


def _sed_reads(args: list[str]) -> bool:
    """`sed -n 1,20p f`, `sed 's/a/b/' f`: prints, changes no file, runs nothing."""
    explicit = any(a in ("-e", "--expression") or a.startswith("--expression=") for a in args)
    scripts, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression"):
            scripts.append(args[i + 1] if i + 1 < len(args) else "")
            i += 1
        elif a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-") and len(a) > 1:
            if not re.fullmatch(r"-[nErsuz]+|--quiet|--silent|--regexp-extended|--posix", a):
                return False  # -i, -f …
        elif not explicit and not scripts:
            scripts.append(a)
        i += 1
    parts = [p.strip() for s in scripts for p in str(s).split(";")]
    return bool(scripts) and all(_SED_PRINT.fullmatch(p) or _SED_SUBST.fullmatch(p) for p in parts)


_AWK_ACTS = re.compile(r"system\s*\(|getline|ENVIRON|>>|>\s*\"|\|\s*\"|\"\s*\||print[^;}\n]*>\s*[A-Za-z_(]")


def _awk_reads(args: list[str]) -> bool:
    """An awk program that only prints: no system(), no pipes, no output files."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in ("-F", "-v"):
            i += 2
        elif a.startswith("-F") or a.startswith("-v"):
            i += 1
        else:
            return False  # -f program file, -i, --
    return i < len(args) and not _AWK_ACTS.search(str(args[i]))


_CURL_PLAIN = re.compile(r"-[sSiILfvk]+|--silent|--show-error|--include|--head|--location|--fail|--verbose|"
                         r"--insecure|--compressed|--get")
_CURL_VALUED = {"-H", "--header", "-m", "--max-time", "--connect-timeout", "-A", "--user-agent", "-w", "--write-out"}
_LOCAL_URL = re.compile(r"(https?://)?(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)(:\d+)?([/?#].*)?")


def _curl_urls(args: list[str]) -> list[str] | None:
    """The addresses a curl only fetches; None when it sends data, writes a
    file or uses an option this doesn't know."""
    urls, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ("-o", "--output"):
            if i + 1 >= len(args) or args[i + 1] != "/dev/null":
                return None
            i += 1
        elif a in _CURL_VALUED:
            if i + 1 >= len(args) or str(args[i + 1]).startswith("@"):
                return None
            i += 1
        elif a.startswith("-"):
            if not _CURL_PLAIN.fullmatch(a):
                return None
        elif "://" in a and not a.startswith(("http://", "https://")):
            return None  # file://, ftp:// …
        else:
            urls.append(str(a))
        i += 1
    return urls


def _git_reads(args: list[str], project: Path | None) -> bool:
    sub, rest, redirected = _git_sub(args, project)
    if redirected or sub is None:
        return False
    if sub in GIT_READ_ONLY:
        return True
    flags = [a for a in rest if a.startswith("-")]
    names = [a for a in rest if not a.startswith("-")]
    if sub == "config":  # reading a setting, never writing one
        if any(f not in GIT_CONFIG_FLAGS for f in flags):
            return False
        return (bool(set(flags) & GIT_CONFIG_READS) and len(names) <= 1) or (len(names) == 1 and not flags)
    if sub == "stash":  # bare `git stash` stashes
        return rest[:1] in (["list"], ["show"])
    if sub == "remote":
        return not rest or rest == ["-v"] or (rest[0] == "get-url" and len(rest) == 2)
    if sub == "reflog":  # expire and delete rewrite it
        return not rest or rest[0] == "show"
    if sub == "worktree":
        return rest[:1] == ["list"]
    if sub == "submodule":
        return not rest or rest[0] in ("status", "summary")
    if sub in GIT_LISTING:
        listing, names, i = False, [], 0
        while i < len(rest):
            a = rest[i]
            flag = a.split("=", 1)[0]
            if flag in GIT_LIST_VALUED | GIT_LIST_OPTIONS:
                listing = listing or flag in GIT_LIST_VALUED
                if "=" not in a:
                    i += 1  # its value
            elif a.startswith("-"):
                if a not in GIT_LISTING[sub] and not (sub == "tag" and re.fullmatch(r"-n\d*", a)):
                    return False
                listing = listing or a in GIT_FORCES_LIST
            else:
                names.append(a)
            i += 1
        return not names or listing
    return False


def _reads(cmd: list[str], project: Path | None = None) -> bool:
    """Whether a command, by what it is and its options, only reads (where
    its arguments point is checked apart)."""
    if not cmd:
        return False
    name, args = str(cmd[0]), list(cmd[1:])
    if "/" in name:
        return False
    if len(args) == 1 and ((args[0] in VERSION_FLAGS and name in VERSIONED) or (args[0] == "-v" and name in VERSIONED_V)):
        return True
    if _dangerous(cmd, project):
        return False
    if name == "git":
        return _git_reads(args, project)
    if name == "sed":
        return _sed_reads(args)
    if name == "awk":
        return _awk_reads(args)
    if name == "curl":  # fetching from this machine: the dev server just started
        urls = _curl_urls(args)
        return bool(urls) and all(_LOCAL_URL.fullmatch(u) for u in urls)
    if name == "command":
        return bool(args) and args[0] in ("-v", "-V")
    if name == "uniq":
        return len([a for a in args if not a.startswith("-") and not a.isdigit()]) <= 1  # a second file is written
    if name == "hostname":
        return not args or args == ["-f"]
    if name == "ps":  # BSD's `e` (ps auxe, ps e) prints every process's environment
        return not any("e" in a for a in args if not a.startswith("-"))
    if name == "xxd":
        return len([a for a in args if not a.startswith("-") and not a.isdigit()]) <= 1  # a second file is written
    if name == "tail":
        return not any(a in ("--follow", "--retry") or a.startswith("--follow=") or re.fullmatch(r"-[a-zA-Z]*[fF][a-zA-Z]*", a)
                       for a in args)  # -f never ends
    if name == "jq":  # `env` and $ENV print the environment, secrets included
        return not any("env" in str(a).lower() or "input_filename" in a for a in args)
    if name in SUB_READ_ONLY:
        sub, _, clean = tool_sub(cmd)
        if not clean or sub not in SUB_READ_ONLY[name]:
            return False
        return not (name == "go" and sub == "env" and any(a in ("-w", "-u") for a in args))
    return name in READ_ONLY


def path_args(cmd: list[str]) -> list[str]:
    """The arguments that may name a place: not the pattern of a grep, a sed
    script or what echo prints."""
    if not cmd:
        return []
    name, args = os.path.basename(cmd[0]), list(cmd[1:])
    table = name
    if name == "git":
        sub, rest, _ = _git_sub(args)
        if sub == "grep":
            name, args = "grep", rest
    if name in NO_PATHS:
        return []
    valued = PATTERN_FLAGS.get(table, ())
    given = any(a in ("-e", "--regexp", "-f", "--file") or a.startswith("--regexp=") for a in args)
    seen = name not in PATTERN_FIRST or given
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in valued:
            skip = True
        elif a.startswith("--") and a.split("=", 1)[0] in valued:
            pass
        elif not seen and not a.startswith("-") and not a.isdigit():
            seen = True  # the pattern itself
        else:
            out.append(a)
    return out


class Key(list):
    """The words a rule for a command is made of. `exact`: the rule is this
    one command and nothing that merely starts like it."""
    exact = False


def _exact(cmd: list[str]) -> Key | None:
    if any(getattr(w, "unknown", False) or getattr(w, "vars", ()) for w in cmd):
        return None
    key = Key(str(w) for w in cmd)
    key.exact = True
    return key


def key_of(words: list[str], project: Path | None = None) -> Key | None:
    """The words that identify what a command does, for rules: `git push`,
    `cargo test`, `python -m pytest`, `./scripts/x.sh`, `uv run pytest`, `ls`.
    None when no rule should cover it (inline code, a bare interpreter)."""
    cmd, _, piped = _unwrap(words)
    if piped and cmd:
        key = key_of(cmd, project)
        if key is None:
            return None
        both = Key(["xargs", *key])
        both.exact = key.exact
        return both
    if not cmd or getattr(cmd[0], "vars", ()) or getattr(cmd[0], "unknown", False):
        return None
    name = os.path.basename(cmd[0])
    if _opaque(cmd):
        return None
    if name in SHELLS:
        return Key([name, _shell_script(cmd)])
    if name in ("source", "."):
        return Key([name, str(cmd[1])])
    if (_dangerous(cmd, project) or (name in ("sed", "awk", "jq", "tail") and not _reads([W(name), *cmd[1:]], project))
            or (name == "curl" and _curl_urls(cmd[1:]) is None) or name == "wget"):
        return _exact(cmd)
    if name == "git":
        sub, rest, redirected = _git_sub(cmd[1:], project)
        if not sub:
            return None
        if redirected:
            return _exact(cmd)
        if sub in GIT_THREE and rest and not rest[0].startswith("-"):
            return Key(["git", str(sub), str(rest[0])])
        return Key(["git", str(sub)])
    if name in INTERPRETERS:
        plain, valued = PYTHON_FLAGS if name.startswith("python") else ((), ())
        args, flags, i = cmd[1:], [], 0
        while i < len(args) and args[i].startswith("-"):
            a = args[i]
            if a in INLINE_CODE:
                return None
            if a == "-m":
                return Key([name, "-m", str(args[i + 1])]) if i + 1 < len(args) else None
            if a in valued:
                i += 2
            elif a in plain or (name == "node" and a.startswith("--") and a.split("=", 1)[0] not in NODE_CODE_FLAGS):
                flags.append(str(a))
                i += 1
            else:
                return None
        if i < len(args):
            return Key([name, str(args[i])])
        return _exact([W(name), *flags]) if flags else None
    ran = runner(cmd)
    if ran:
        prefix, inner = ran
        if inner is None:
            return _exact(cmd)
        if not inner:
            return Key(prefix)
        key = key_of(inner, project)
        if key is None:
            return None
        both = Key([*prefix, *key])
        both.exact = key.exact
        return both
    if name in TWO_WORDS:
        sub, i, clean = tool_sub(cmd)
        if not clean:
            # `cargo --weird` as itself: yes to one call must not allow every cargo command.
            return _exact([W(name), *cmd[1:]])
        if sub is None:
            return Key([name])
        if (name == "gh" or (name, sub) in THREE_WORDS) and i + 1 < len(cmd) and not cmd[i + 1].startswith("-"):
            return Key([name, str(sub), str(cmd[i + 1])])
        return Key([name, str(sub)])
    # A script or program run by path keeps its path: a rule for ./scripts/x.sh
    # must not cover every x.sh.
    return Key([str(cmd[0]) if "/" in cmd[0] else name])


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


def _destructive(cmd: list[str], project: Path | None = None) -> bool:
    """The form of a command that loses work or publishes it, where other
    forms of the same command are harmless (`git checkout -- .` against
    `git checkout main`)."""
    name, args = os.path.basename(cmd[0]), cmd[1:]
    if name == "git":
        sub, rest, _ = _git_sub(args, project)
        flags = {a for a in rest if a.startswith("-")}
        first = rest[:1]
        if sub in GIT_ALWAYS:
            return True
        if sub == "checkout":
            return ("--" in rest or "." in rest or bool(flags & {"-f", "--force"})
                    or any(not a.startswith("-") and project is not None and os.path.lexists(project / str(a)) for a in rest))
        if sub == "restore":
            return not (flags & {"--staged", "-S"}) or bool(flags & {"--worktree", "-W"})
        if sub == "switch":
            return bool(flags & {"-f", "--force", "--discard-changes", "-C"})
        if sub == "branch":
            return bool(flags & {"-D", "-d", "--delete", "-M", "-f", "--force"})
        if sub == "tag":
            return bool(flags & {"-d", "--delete", "-f", "--force"})
        if sub == "stash":
            return first in (["drop"], ["clear"])
        if sub == "commit":
            return "--amend" in flags
        if sub == "worktree":
            return first in (["remove"], ["prune"])
        if sub == "remote":
            return first in (["remove"], ["rm"])
        if sub == "reflog":
            return first in (["expire"], ["delete"])
        return False
    if name not in TWO_WORDS:
        return False
    sub, at, _ = tool_sub(cmd)
    after = [str(a) for a in cmd[at:at + 3]]  # the subcommand and the two words after it
    if name == "gh":
        return (bool(set(after) & {"merge", "delete", "close", "archive"})
                or after[:2] in (["release", "create"], ["repo", "create"])
                or (sub == "api" and any(a in ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input") for a in args)))
    return sub in PUBLISH.get(name, ()) or (name == "docker" and "prune" in after)


def always_ask(words: list[str], project: Path | None = None) -> bool:
    """Asked every time, whatever the rules say."""
    cmd, _ = unwrap(words)
    if not cmd:
        return True
    name = os.path.basename(cmd[0])
    if _opaque(cmd) or name.startswith("mkfs"):
        return True
    if name in ALWAYS_ASK or _destructive(cmd, project):
        return True
    if _reads(cmd, project):
        return False  # `python3 --version`: no rule could name it, and none is needed
    ran = runner(cmd)
    if ran and ran[1]:
        return always_ask(ran[1], project)  # what `uv run` or `npx` runs
    if name == "find" and any(always_ask(inner, project) for inner in _find_execs(cmd[1:]) if inner):
        return True
    return key_of(cmd, project) is None


def read_only(words: list[str], project: Path | None = None, moved: Path | None | bool = False) -> bool:
    """Whether a simple command only reads, and only inside the project."""
    cmd, stripped = unwrap(words)
    if stripped or not cmd or getattr(words, "taint", False) or getattr(words, "writes", None):
        return False  # an environment variable or a wrapper can change what a command does
    if any(getattr(a, "unknown", False) or getattr(a, "vars", ()) for a in cmd):
        return False
    if any(_where(a, project, moved) for a in (*path_args(cmd), *getattr(words, "reads", ()))):
        return False
    return _reads(cmd, project)


def _covers(rule: str, key_words: list[str], cmd: list[str], deny: bool = False) -> bool:
    """A rule covers a command when its words open the command's key, or the
    command itself (rules written by hand may be longer than a key). An allow
    rule that is only a tool's name covers just the bare call; a deny rule
    like that covers everything the tool does (`deny make` means no make)."""
    want = rule.split()
    if not want or want == [REDIRECT]:
        return False
    normal = [str(cmd[0]) if "/" in want[0] else os.path.basename(cmd[0])] + [str(a) for a in cmd[1:]]
    if want[-1] == "*":  # written by hand: "git *" is every git command
        want = want[:-1]
        return bool(want) and normal[:len(want)] == want
    if not deny and len(want) == 1 and want[0] in TWO_WORDS | INTERPRETERS | SHELLS:
        return len(normal) == 1 and normal == want  # the tool alone is only the bare call
    if not deny and getattr(key_words, "exact", False):
        return list(key_words) == want  # find -delete, sed -i, git -c …: that command, nothing wider
    return list(key_words[:len(want)]) == want or normal[:len(want)] == want


def _rough(command: str) -> list[list[str]]:
    """The commands of a line that can't be taken apart, cut at anything that
    could start one: good enough to look for what a deny rule forbids."""
    out = []
    for piece in re.split(r"\$\(|[;&|()`\n<>]", command):
        try:
            words = shlex.split(piece)
        except ValueError:
            words = piece.replace("'", " ").replace('"', " ").split()
        while words and words[0] in KEYWORDS | CLOSERS:
            words = words[1:]
        if words:
            out.append(words)
    return out


def commands_in(command: str, project: Path | None = None, depth: int = 0) -> list[list[str]]:
    """Every command a line may run, for deny rules: its parts, and what
    their wrappers, runners, `sh -c` texts and `find -exec` are given."""
    parts = segments(command)
    out = []
    for words in (parts if parts is not None else _rough(command)):
        out += _behind(list(words), project, depth)
    return out


def _behind(words: list[str], project: Path | None, depth: int) -> list[list[str]]:
    cmd, _ = unwrap(words)
    if not cmd:  # env -S 'text', xargs with options this doesn't know
        rest = [w for w in words if not w.startswith("-") and not _is_assignment(w)][1:]
        texts = [w for w in rest if " " in w]
        if texts and depth <= 4:
            return [c for w in texts for c in commands_in(str(w), project, depth + 1)]
        return [rest] if rest else []
    out = [cmd]
    if depth > 4:
        return out
    name = os.path.basename(cmd[0])
    rest = [a for a in cmd[1:]]
    if name in PEEL:
        plain = [i for i, a in enumerate(rest) if not a.startswith("-")]
        if len(plain) > PEEL[name]:
            out += _behind(rest[plain[PEEL[name]]:], project, depth + 1)
    if name in SHELLS | {"script", "su", "runuser"}:
        for i, a in enumerate(rest[:-1]):
            if a.startswith("-") and not a.startswith("--") and a.endswith("c"):
                out += commands_in(str(rest[i + 1]), project, depth + 1)
    if name == "eval":
        out += commands_in(" ".join(rest), project, depth + 1)
    ran = runner(cmd)
    if ran and ran[1]:
        out += _behind(ran[1], project, depth + 1)
    if name == "find":
        for inner in _find_execs(rest):
            out += _behind(inner, project, depth + 1)
    return out


def _norm(said: str) -> str:
    return " ".join(said.strip().lower().rstrip("!.").split())


class Permissions:
    """One project's rules, saved between sessions and shared by every session
    open on the project (each change is read from and written to the file)."""

    LISTS = ("run", "deny", "tools", "dirs")

    def __init__(self, project: str, path: str | Path | None = None):
        self.project = Path(project).resolve()
        self.path = Path(path or os.environ.get("OPPX_PERMISSIONS") or Path.home() / ".config/oppx/permissions.json")
        self.run: list[str] = []
        self.deny: list[str] = []
        self.tools: list[str] = []
        self.dirs: list[str] = []  # folders outside the project that commands may read and write
        # Allowed for this session only, never saved (the browser, once the
        # user said yes to it), and the tools that stay asked regardless.
        self.session_tools: set[str] = set()
        # Allowed until the chat ends, never saved: ordinary rules, and inline code (`python3 -c`).
        self.session_run: list[str] = []
        self.session_dirs: list[str] = []
        self.session_inline: set[str] = set()
        self.session_except: set[str] = set()
        self.connectors: set[str] = set()  # connector names, so `/permissions allow linear.x` is a tool rule
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

        self.run, self.deny, self.tools, self.dirs = both("run"), both("deny"), both("tools"), both("dirs")
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

    @contextlib.contextmanager
    def _locked(self):
        """One writer at a time: two sessions saving at once each keep the other's rule."""
        handle = None
        try:
            import fcntl
            handle = open(self.path.with_name(self.path.name + ".lock"), "w")
            fcntl.flock(handle, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            yield
        finally:
            if handle:
                handle.close()

    def _change(self, key: str, add=(), remove=()):
        """Read, change and write this project's list in one go, so a rule
        another session saved meanwhile isn't lost."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            data = self._read()
            if not isinstance(data.get("projects"), dict):
                data["projects"] = {}
            projects = data["projects"]
            mine = projects.get(str(self.project)) if isinstance(projects.get(str(self.project)), dict) else {}
            lists = {k: {r for r in (mine.get(k) if isinstance(mine.get(k), list) else []) if isinstance(r, str)}
                     for k in self.LISTS}
            lists[key] = (lists[key] | set(add)) - set(remove)
            if any(lists.values()):
                projects[str(self.project)] = {k: sorted(v) for k, v in lists.items() if v or k != "dirs"}
            else:
                projects.pop(str(self.project), None)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                f.write(json.dumps(data, indent=1))  # 0600: it says what may run unasked
            os.replace(tmp, self.path)
        self.load()

    # ---- commands -----------------------------------------------------------
    def denied(self, command: str) -> str | None:
        """The deny rule that forbids this command, if one does: looked for in
        every command the line may run, also behind wrappers, in loops, and
        in a line that can't be taken apart."""
        self._fresh()
        if not self.deny:
            return None
        for cmd in commands_in(command, self.project):
            key = key_of(cmd, self.project) or []
            for rule in self.deny:
                if _covers(rule, key, cmd, deny=True):
                    return rule
        return None

    def _need(self, seg: Seg, moved: Path | None, rules: bool = True, inline_ok: bool = False) -> list[tuple[str, str]] | None:
        """What is still missing for one part of a line to run unasked: []
        when nothing is, the grants "always" would add, or None when it can't
        be remembered. `rules`: off in plan mode, where only reading counts."""
        if moved is None:
            return None  # after `cd -` or pushd: nobody knows where this runs
        code = inline_key(seg)
        if code and rules and code in self.session_inline:
            return []
        grants: list[tuple[str, str]] = []
        if code:
            if not inline_ok:
                return None
            grants.append(("inline", code))
        elif always_ask(seg, self.project):
            return None
        allowed = (*self.run, *self.session_run) if rules else ()
        dirs = (*self.dirs, *self.session_dirs)
        cmd, stripped = unwrap(seg)
        safe = {name for name, words in seg.loops.items()
                if all(not _where(w, self.project, moved, dirs) for w in words)}
        if cmd and getattr(cmd[0], "vars", ()):
            return None  # the program itself comes from a variable
        if any(getattr(a, "unknown", False) or any(v not in safe for v in getattr(a, "vars", ())) for a in cmd):
            return None  # $TOKEN, ${x:-y}, {a,b}: nobody knows what is run, or what is printed
        for target in seg.writes:
            # Into the project it is like an edit; anywhere else, or into .git, it is always asked.
            if _where(target, self.project, moved, (), safe) or ".git" in Path(str(target)).parts:
                return None
            if REDIRECT not in allowed:
                grants.append(("run", REDIRECT))
        for arg in (*(() if code else path_args(cmd)), *seg.reads):
            place = _where(arg, self.project, moved, dirs, safe)
            if place == "?":
                return None
            if place:
                folder = grant_dir(place)
                if folder is None:
                    return None
                grants.append(("dirs", folder))
        if not code and (stripped or seg.taint or not _reads(cmd, self.project)):
            key = key_of(seg, self.project)
            if key is None:
                return None
            if not any(_covers(rule, key, cmd) for rule in allowed):
                grants.append(("run", " ".join(key)))
        return grants

    def _grants(self, command: str, rules: bool = True, inline_ok: bool = False) -> list[tuple[str, str]] | None:
        """`_need` for a whole line; None when a part can't be remembered."""
        self._fresh()
        parts = segments(command)
        if parts is None:
            return None
        grants, cwd = [], self.project
        for seg in parts:
            need = self._need(seg, cwd, rules, inline_ok)
            if need is None:
                return None
            grants += need
            if seg[0] == "cd" and cwd is not None:
                # Later names are relative to there (a bare `cd` goes home).
                to = [a for a in seg[1:] if not a.startswith("-")]
                cwd = (cwd / str(to[0])).resolve() if to else (Path.home() if len(seg) == 1 else None)
            elif seg[0] in ("pushd", "popd"):
                cwd = None
        return list(dict.fromkeys(grants))

    @staticmethod
    def label(grant: tuple[str, str]) -> str:
        kind, what = grant
        if kind == "dirs":
            return f"files in {what}" if os.path.isdir(what) else what
        return "> (output into this project's files)" if what == REDIRECT else what

    def allows(self, command: str) -> bool:
        """Every part of the command is read-only or covered by an allow rule,
        and no deny rule forbids it."""
        return self._grants(command) == [] and not self.denied(command)

    def reads_only(self, command: str) -> bool:
        """Every part only reads (no rule involved): what plan mode may run."""
        return self._grants(command, rules=False) == [] and not self.denied(command)

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
        a command that can't be remembered it is a yes for this once. A plain
        "ok" or "sure" is a yes; anything else is what the user wants done
        instead."""
        answer = _norm(said)
        always = self._grants(command) or []
        session = self._grants(command, inline_ok=True) or []
        inline = [g for g in session if g[0] == "inline"]
        names = lambda grants: ", ".join(self.label(g) for g in grants)  # noqa: E731
        if answer in ALWAYS and always:
            self._save(always)
            return True, f"Allowed, and from now on without asking: {names(always)} (/permissions to change)"
        if answer in SESSION and inline:
            self._allow_session(session)
            return True, f"Allowed, and for the rest of this session without asking: {names(session)}"
        if answer in ALWAYS and inline:
            # The parts a rule can name are saved; the inline code is for this session.
            self._allow_session(session)
            keep = [g for g in session if g[0] != "inline"]
            if keep:
                self._save(keep)
                return True, (f"Allowed, and from now on without asking: {names(keep)} (/permissions to change); "
                              f"for this session: {names(inline)}")
            return True, f"Allowed, and for the rest of this session without asking: {names(session)}"
        if answer in YES | ALWAYS | SESSION:
            once = answer in ALWAYS | SESSION
            return True, "Allowed" + (" this once (this kind of command is asked every time)" if once else "")
        if answer in ("", "n", "no"):
            return False, "Declined"
        return f"The user declined to run this command and said: {said.strip()}", "Declined, with your note"

    def offer(self, command: str) -> list[str]:
        """What "always" would add for this command (rules, and folders
        outside the project); empty when it can't be remembered (not
        analyzable, or a part is asked every time)."""
        return [self.label(g) for g in self._grants(command) or []]

    def offer_session(self, command: str) -> list[str]:
        """What "for this session" would allow: only for a command that can't
        get a saved rule because a part runs inline code (`python3 -c`), and
        nothing in it is of the kind asked every time (sudo, rm, a shell
        running text, something that can't be taken apart)."""
        grants = self._grants(command, inline_ok=True) or []
        return [self.label(g) for g in grants] if any(g[0] == "inline" for g in grants) else []

    @staticmethod
    def savable(session: list[str]) -> list[str]:
        """Of what `offer_session` names, the rules that may be saved (not the inline code)."""
        return [r for r in session if not (len(r.split()) == 2 and r.split()[0] in INTERPRETERS and r.split()[1] in INLINE_CODE)]

    def _save(self, grants: list[tuple[str, str]]):
        for kind in ("run", "dirs"):
            add = [what for k, what in grants if k == kind]
            if add:
                self._change(kind, add=add)

    def _allow_session(self, grants: list[tuple[str, str]]):
        for kind, what in grants:
            if kind == "inline":
                self.session_inline.add(what)
            elif kind == "dirs" and what not in self.session_dirs:
                self.session_dirs.append(what)
            elif kind == "run" and what not in self.session_run:
                self.session_run.append(what)

    def allow_session(self, command: str) -> list[str]:
        """Allows what `offer_session` names until the chat ends. Nothing is written."""
        grants = self._grants(command, inline_ok=True) or []
        if not any(g[0] == "inline" for g in grants):
            return []
        self._allow_session(grants)
        return [self.label(g) for g in grants]

    def allow_always(self, command: str) -> list[str]:
        grants = self._grants(command) or []
        self._save(grants)
        return [self.label(g) for g in grants]

    def _tool_rule(self, rule: str) -> bool:
        """`linear.save_issue`, `linear.*`: a connector tool, not a command."""
        name, dot, tool = rule.partition(".")
        return bool(dot) and " " not in rule and "/" not in rule and (tool == "*" or name in self.connectors)

    def add(self, kind: str, rule: str) -> bool:
        """A rule written by the user (/permissions allow|deny …). An allow
        rule for something that is asked every time is refused."""
        rule = " ".join(rule.split())
        if not rule or kind not in self.LISTS:
            return False
        if kind == "run" and self._tool_rule(rule):
            kind = "tools"
        if kind == "run" and rule != REDIRECT and always_ask(rule.split(), self.project):
            return False
        if kind == "dirs":
            folder = grant_dir(str(Path(rule).expanduser().resolve() / "x"))
            if folder is None:
                return False
            rule = folder
        self._change(kind, add=[rule])
        return True

    # ---- connector tools ----------------------------------------------------
    @staticmethod
    def _connector_of(name: str) -> str:
        return name.split(".", 1)[0] if "." in name else name

    def every_time(self, name: str) -> bool:
        """A tool that stays asked whatever was allowed (running JavaScript in the browser)."""
        return name in self.session_except

    def tool_allowed(self, name: str) -> bool:
        """Allowed by its own rule, or by its connector's ("linear.*")."""
        self._fresh()
        if self.tool_denied(name) or self.every_time(name):
            return False
        wild = f"{self._connector_of(name)}.*"
        return name in self.tools or wild in self.tools or name in self.session_tools or wild in self.session_tools

    def allow_for_session(self, rule: str, unless=()):
        """Allows a tool, or every tool of a connector ("playwright.*"), until
        the chat ends; `unless` stay asked."""
        self.session_tools.add(rule)
        self.session_except.update(unless)

    def tool_denied(self, name: str) -> bool:
        self._fresh()
        connector = self._connector_of(name)
        return name in self.deny or f"{connector}.*" in self.deny or (connector != name and connector in self.deny)

    def allow_tool(self, name: str):
        self._change("tools", add=[name])

    def judge_tool(self, name: str, read_only: bool, trusted: bool = True) -> tuple[str, str]:
        """("run", ""), ("refuse", why) or ("ask", "") for a connector call.
        A tool its connector marks as only reading runs without a question,
        like a command that only reads; deny rules still win. `trusted`: the
        connector is the user's own. One that came with the repository says
        "read-only" about itself, so that word alone doesn't skip the question."""
        if self.tool_denied(name):
            return "refuse", "The user's deny rule forbids this tool; carry on without it, or tell the user."
        if (read_only and trusted and not self.every_time(name)) or self.tool_allowed(name):
            return "run", ""
        return "ask", ""

    def answered_tool(self, name: str, said: str) -> tuple[bool | str, str]:
        """The answer to "Call this connector tool?": y once, a always for this
        tool, c always for every tool of its connector, s until the chat ends,
        N, or what to do instead. For a tool that is asked every time the
        lasting answers are a yes for this once."""
        answer = _norm(said)
        connector = self._connector_of(name)
        lasting = not self.every_time(name)
        if answer in ALWAYS and lasting:
            self.allow_tool(name)
            return True, f"Allowed, and from now on without asking: {name} (/permissions to change)"
        if answer in ("c", "connector") and connector != name and lasting:
            self.allow_tool(f"{connector}.*")
            return True, f"Allowed, and from now on without asking: every {connector} tool (/permissions to change)"
        if answer in SESSION and lasting:
            self.session_tools.add(name)
            return True, f"Allowed, and for the rest of this session without asking: {name}"
        if answer in YES:
            return True, "Allowed"
        if answer in ALWAYS | SESSION | {"c", "connector"}:
            return True, "Allowed this once (this tool is asked every time)"
        if answer in ("", "n", "no"):
            return False, "Declined"
        return f"The user declined this call and said: {said.strip()}", "Declined, with your note"

    # ---- managing -----------------------------------------------------------
    def remove(self, rule: str, kind: str | None = None) -> bool:
        """Removes a rule from one list, or from every list it is in."""
        self._fresh()
        rule = " ".join(rule.split())
        had = [k for k in ((kind,) if kind else self.LISTS) if rule in getattr(self, k)]
        for k in had:
            self._change(k, remove=[rule])
        return bool(had)

    def clear(self):
        for k in self.LISTS:
            self._change(k, remove=list(getattr(self, k)))
