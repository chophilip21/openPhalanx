#!/usr/bin/env python3
"""Every regular expression in the repository: compiled, and checked for
catastrophic backtracking (ReDoS), so that no one ships a pattern that takes
exponential or polynomial (quadratic or worse) time on some input.

Found automatically, not listed by hand: Python patterns through the syntax
tree (re.compile/search/match/fullmatch/findall/finditer/sub/subn/split, with
constants, concatenations and f-strings resolved), TypeScript, Svelte and
JavaScript patterns through the TypeScript parser (scripts/regexcheck/check.mjs),
and Rust's Regex::new. Python's `re` and JavaScript backtrack, so each of their
patterns goes through recheck (an automaton analysis, pinned in
scripts/regexcheck/package.json). Rust's regex crate can't backtrack (linear
time by design), so its patterns only have to compile.

A pattern built at run time from something only known then (a pattern the
model writes) can't be checked here: its call site says so with a
`# regex: runtime (why it is safe)` comment, and anything else unresolved fails.

    scripts/check_regexes.py            # check everything; exit 1 on a finding
    scripts/check_regexes.py --list     # every pattern found, and where
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_DIRS = ["client/oppx/frontend", "docker/server", "scripts", "app/src-tauri", "crates", "packaging"]
JS_DIRS = ["app/src", "scripts"]
RUST_DIRS = ["crates", "client/oppx/src", "app/src-tauri/src"]
SKIP = ("node_modules", "target", ".git", "__pycache__", "dist", "build")
FUNCS = {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split"}
FLAG_BITS = {"I": "i", "IGNORECASE": "i", "M": "m", "MULTILINE": "m", "S": "s", "DOTALL": "s",
             "X": "x", "VERBOSE": "x", "A": "", "ASCII": "", "U": "", "UNICODE": ""}
RUNTIME = re.compile(r"#\s*regex:\s*runtime\b")


class Unresolved(Exception):
    pass


# ---- Python ----------------------------------------------------------------

class Scope:
    """String constants a pattern may be built from: module and class level,
    and the enclosing function's simple assignments."""

    def __init__(self, tree: ast.Module):
        self.module = self._consts(tree.body)

    @staticmethod
    def _consts(body) -> dict[str, ast.expr]:
        out = {}
        for node in body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                out[node.targets[0].id] = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                out[node.target.id] = node.value
            elif isinstance(node, ast.ClassDef):
                out.update({f"{node.name}.{k}": v for k, v in Scope._consts(node.body).items()})
                out.update(Scope._consts(node.body))
        return out


def resolve(node: ast.expr, names: dict[str, ast.expr], depth: int = 0) -> str:
    """The pattern text of an expression, or Unresolved."""
    if depth > 20:
        raise Unresolved("too deep")
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return node.value if isinstance(node.value, str) else node.value.decode("latin-1")
    if isinstance(node, ast.Name) and node.id in names:
        return resolve(names[node.id], names, depth + 1)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and f"{node.value.id}.{node.attr}" in names:
        return resolve(names[f"{node.value.id}.{node.attr}"], names, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return resolve(node.left, names, depth + 1) + resolve(node.right, names, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant):
        raise Unresolved("%-formatting")
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                inner = v.value
                # re.escape(x) is a literal; a number (a count in {n}) is a number.
                if isinstance(inner, ast.Call) and getattr(inner.func, "attr", "") == "escape":
                    out.append("x")
                else:
                    out.append(resolve(inner, names, depth + 1))
        return "".join(out)
    if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "join" and isinstance(node.func.value, ast.Constant):
        sep = node.func.value.value
        arg = node.args[0] if node.args else None
        if isinstance(arg, (ast.List, ast.Tuple)):
            return sep.join(resolve(e, names, depth + 1) for e in arg.elts)
        if isinstance(arg, ast.Name) and arg.id in names and isinstance(names[arg.id], (ast.List, ast.Tuple)):
            return sep.join(resolve(e, names, depth + 1) for e in names[arg.id].elts)
        raise Unresolved("join over a runtime value")
    if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "escape":
        return "x"
    raise Unresolved(type(node).__name__)


def flags_of(node: ast.expr | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "re":
        if node.attr not in FLAG_BITS:
            raise Unresolved(f"flag re.{node.attr}")
        return FLAG_BITS[node.attr]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return flags_of(node.left) + flags_of(node.right)
    if isinstance(node, ast.Constant) and node.value == 0:
        return ""
    raise Unresolved("flags")


def python_patterns(path: Path, root: Path = ROOT) -> list[dict]:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    scope = Scope(tree)
    found = []
    uses = methods_used(tree)

    def visit(node, names):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            body = node.body if isinstance(node.body, list) else []
            local = dict(names)
            for sub in ast.walk(node) if body else []:
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                    local.setdefault(sub.targets[0].id, sub.value)
            names = local
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            names = dict(names)
            for gen in node.generators:
                if isinstance(gen.target, ast.Name) and isinstance(gen.iter, (ast.List, ast.Tuple)):
                    names[gen.target.id] = gen.iter
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FUNCS
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "re" and node.args):
            fn = node.func.attr
            flag_node = next((k.value for k in node.keywords if k.arg == "flags"), None)
            if flag_node is None:
                pos = {"compile": 1, "search": 2, "match": 2, "fullmatch": 2, "findall": 2, "finditer": 2,
                       "split": 3, "sub": 4, "subn": 4}[fn]
                flag_node = node.args[pos] if len(node.args) > pos else None
            where = f"{path.relative_to(root)}:{node.lineno}"
            context = " ".join(lines[max(0, node.lineno - 3):node.end_lineno or node.lineno])
            mode = fn
            if fn == "compile":
                # A compiled pattern is checked for how it is used: anchored
                # (match, fullmatch) only when every use in the file is.
                mode = loosest(uses.get(assigned_to.get(id(node), ""), set()))
            try:
                for pattern in expand(node.args[0], names):
                    flags = flags_of(flag_node)
                    found.append({"where": where, "lang": "python", "source": pattern, "flags": flags, "mode": mode})
            except Unresolved as e:
                found.append({"where": where, "lang": "python", "unresolved": str(e),
                              "runtime": bool(RUNTIME.search(context))})
        for child in ast.iter_child_nodes(node):
            visit(child, names)

    assigned_to = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Call):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if len(targets) == 1 and isinstance(targets[0], (ast.Name, ast.Attribute)):
                assigned_to[id(n.value)] = targets[0].id if isinstance(targets[0], ast.Name) else targets[0].attr
    visit(tree, scope.module)
    return found


def methods_used(tree: ast.Module) -> dict[str, set[str]]:
    """name -> the methods called on it (X.match(...), self.X.search(...))."""
    out: dict[str, set[str]] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in FUNCS - {"compile"}:
            v = n.func.value
            name = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else None
            if name and name != "re":
                out.setdefault(name, set()).add(n.func.attr)
    return out


def loosest(methods: set[str]) -> str:
    """The least anchored way a pattern is used; "search" when unknown (it may
    be used elsewhere, or passed around)."""
    if methods and methods <= {"fullmatch"}:
        return "fullmatch"
    if methods and methods <= {"match", "fullmatch"}:
        return "match"
    return "search"


def expand(node: ast.expr, names: dict[str, ast.expr]) -> list[str]:
    """The patterns an argument stands for: one, or each item of a literal
    list a comprehension runs over (`[re.compile(p) for p in (...)]`)."""
    if isinstance(node, ast.Name) and node.id in names and isinstance(names[node.id], (ast.List, ast.Tuple)):
        return [resolve(e, names) for e in names[node.id].elts]
    return [resolve(node, names)]


# Python syntax -> JavaScript syntax, for recheck.

def python_to_js(source: str, flags: str, mode: str) -> tuple[str, str]:
    if "x" in flags:
        source = strip_verbose(source)
        flags = flags.replace("x", "")
    m = re.match(r"\(\?([aiLmsux]+)\)", source)  # leading inline flags
    if m:
        flags += "".join(c for c in m.group(1) if c in "ims")
        if "x" in m.group(1):
            source = strip_verbose(source[m.end():])
        else:
            source = source[m.end():]
    source = re.sub(r"\(\?P<(\w+)>", r"(?<\1>", source)
    source = re.sub(r"\(\?P=(\w+)\)", r"\\k<\1>", source)
    # Python's {,n} means {0,n}; JavaScript reads it as text.
    source = re.sub(r"(?<!\\)\{,(\d+)\}", r"{0,\1}", source)
    source = python_lines(source, multiline="m" in flags, dotall="s" in flags)
    flags = flags.replace("m", "")
    if mode == "match":
        source = f"^(?:{source})"
    elif mode == "fullmatch":
        source = f"^(?:{source})$"  # JavaScript's ^ and $ without the m flag: the very start and end
    return source, "".join(dict.fromkeys(flags))


def python_lines(source: str, multiline: bool, dotall: bool) -> str:
    """^, $ and . as Python means them, written for JavaScript: only "\n"
    ends a line in Python, where JavaScript also breaks at \r, \u2028 and
    \u2029 (which made the analysis find slow inputs Python never has)."""
    out, i, in_class = [], 0, False
    while i < len(source):
        c = source[i]
        if c == "\\" and i + 1 < len(source):
            nxt = source[i + 1]
            if not in_class and nxt == "A":
                out.append("(?<![\\s\\S])")  # the very start
            elif not in_class and nxt == "Z":
                out.append("(?![\\s\\S])")  # the very end
            else:
                out.append(source[i:i + 2])
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            out.append(c)
        elif c == "[":
            in_class = True
            out.append(c)
            if source[i + 1:i + 2] == "^":
                out.append("^")
                i += 1
            if source[i + 1:i + 2] == "]":  # "[]" or "[^]": a literal ] first
                out.append("\\]")
                i += 1
        elif c == "^":
            out.append("(?<![^\\n])" if multiline else "(?<![\\s\\S])")
        elif c == "$":
            out.append("(?![^\\n])" if multiline else "(?=\\n?(?![\\s\\S]))")
        elif c == "." and not dotall:
            out.append("[^\\n]")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def strip_verbose(source: str) -> str:
    out, i, in_class = [], 0, False
    while i < len(source):
        c = source[i]
        if c == "\\" and i + 1 < len(source):
            out.append(source[i:i + 2])
            i += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        if not in_class and c.isspace():
            i += 1
            continue
        if not in_class and c == "#":
            while i < len(source) and source[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ---- Rust --------------------------------------------------------------------

RUST_REGEX = re.compile(r"\bRegex(?:Builder)?::new\(\s*(r#*\"|\")")


def rust_patterns(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    out = []
    for m in RUST_REGEX.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        out.append({"where": f"{path.relative_to(ROOT)}:{line}", "lang": "rust"})
    return out


# ---- driver --------------------------------------------------------------------

def analyze(py: list[dict], js_dirs: list[str]) -> dict:
    """recheck on these Python patterns (converted) and on the JavaScript
    side's own under `js_dirs`: {checked, problems}."""
    checker = ROOT / "scripts/regexcheck/check.mjs"
    payload = {"root": str(ROOT), "dirs": js_dirs,
               "patterns": [{"where": f["where"], "source": f["js"], "flags": f["jsflags"], "lang": "python",
                             "python": f["source"]} for f in py]}
    try:
        run = subprocess.run(["node", str(checker)], input=json.dumps(payload), capture_output=True, text=True,
                             cwd=checker.parent)
    except FileNotFoundError as e:
        raise RuntimeError("The regex checker needs Node.js.") from e
    if run.returncode not in (0, 1):
        raise RuntimeError((run.stderr or run.stdout).strip()
                           + "\nThe regex checker couldn't run: `npm ci --prefix scripts/regexcheck` installs it.")
    return json.loads(run.stdout)


def files(dirs: list[str], exts: tuple[str, ...]):
    for d in dirs:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.suffix in exts and p.is_file() and not any(s in p.parts for s in SKIP):
                yield p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true", help="print every pattern found, and where")
    args = ap.parse_args()

    found: list[dict] = []
    for p in files(PY_DIRS, (".py",)):
        found += python_patterns(p)
    for p in files(RUST_DIRS, (".rs",)):
        found += rust_patterns(p)

    problems: list[str] = []
    runtime = [f for f in found if "unresolved" in f and f["runtime"]]
    for f in found:
        if "unresolved" in f and not f["runtime"]:
            problems.append(f"{f['where']}: a pattern that can't be read here ({f['unresolved']}): build it from "
                            "constants, or mark a pattern only known at run time with `# regex: runtime (why it is safe)`")
    py = [f for f in found if f.get("lang") == "python" and "source" in f]
    for f in py:
        try:
            # regex: runtime (compiling the patterns under test is the point)
            re.compile(f["source"], sum(getattr(re, {"i": "I", "m": "M", "s": "S", "x": "X"}[c]) for c in f["flags"]))
        except re.error as e:
            problems.append(f"{f['where']}: doesn't compile in Python: {e}")
            f["bad"] = True
        f["js"], f["jsflags"] = python_to_js(f["source"], f["flags"], f["mode"])
    rust = [f for f in found if f.get("lang") == "rust"]
    if rust:
        problems += [f"{f['where']}: a Rust regex: add it to a test that compiles it (none exist yet; this check "
                     "only knows they are linear-time)" for f in rust]

    # JavaScript side: TS/Svelte/JS patterns found there, and every pattern checked by recheck.
    try:
        result = analyze([f for f in py if not f.get("bad")], JS_DIRS)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    problems += result["problems"]
    checked = result["checked"]

    if args.list:
        for c in checked:
            print(f"{c['verdict']:>10}  {c['where']:<60} {c.get('python', c['source'])}")
        for f in runtime:
            print(f"{'runtime':>10}  {f['where']}")
    n_js = sum(1 for c in checked if c["lang"] != "python")
    print(f"{len(checked)} patterns checked ({len(checked) - n_js} Python, {n_js} TypeScript/Svelte/JavaScript), "
          f"{len(runtime)} built at run time and marked, {len(rust)} Rust.")
    if problems:
        print(f"\n{len(problems)} problem{'s' if len(problems) != 1 else ''}:")
        for p in problems:
            print("  " + p)
        return 1
    print("No pattern can backtrack catastrophically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
