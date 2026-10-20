"""The repository as the agent sees it (`Workspace`: find, grep, list, outline,
read, run, start) and applying an edit's SEARCH/REPLACE blocks to a file."""

from __future__ import annotations

import json
import atexit
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import documents, writers
from .prompts import (
    CODE_DEF, CONST_DEF, FIND_HITS, GREP_DOCS, GREP_FILES, GREP_HITS, GREP_PER_FILE, LIST_FILES, LOW_RANK,
    READ_LINES, READ_PAD, RESULT_CHARS, SCAN_PAGES, START_QUIET, START_WAIT, STOPWORDS, WHOLE_FILE,
)
from .actions import ToolError


# The loaded model takes pictures (the server's /v1/info, from SGLang).
VISION = os.environ.get("OPPX_VISION") == "1"
RUN_TIMEOUT = 120


class Workspace:
    """The repository the tools work in. Paths stay inside it."""

    def __init__(self, root: str, repo_map=None, encoding: str = "utf-8"):
        self.root = Path(root).resolve()
        self.repo_map = repo_map  # Aider's RepoMap, for outlines
        self.encoding = encoding
        self.jobs: dict[str, dict] = {}  # commands left running by `start`, by command line
        self.seen: dict[str, str] = {}  # file -> a digest of what it held when the model last read or wrote it

    def mark(self, p: Path):
        """The model now knows this file as it is on disk (it read it, or wrote it)."""
        try:
            self.seen[self.rel(p)] = hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            self.seen.pop(self.rel(p), None)

    def check_current(self, p: Path):
        """Refuses a change to a file that isn't what the model read any more
        (the user's editor, a formatter, a dev server's code generator): the
        edit would be written against lines that are gone. As in Claude Code."""
        rel = self.rel(p)
        try:
            now = hashlib.sha1(p.read_bytes()).hexdigest()
        except OSError:
            return
        if rel in self.seen and self.seen[rel] != now:
            raise ToolError(f"{rel} has changed on disk since you read it (the user, a formatter or a command changed it). "
                            "Read it again, then make the change against what it holds now")

    def path(self, rel: str, must_exist: bool = True) -> Path:
        rel = (rel or "").strip()
        while rel.startswith(("./", "/")):  # not lstrip("./"): that ate the dot of .github, .env…
            rel = rel[2:] if rel.startswith("./") else rel[1:]
        rel = "" if rel == "." else rel
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"{rel} is outside the repository")
        if ".git" in p.relative_to(self.root).parts:
            raise ToolError("the .git folder is off limits")
        if must_exist and not p.exists():
            near = self.similar_paths(rel)
            hint = "did you mean: " + ", ".join(near) if near else "use find or list to get the right path"
            raise ToolError(f"{rel} doesn't exist ({hint})")
        return p

    def similar_paths(self, rel: str, limit: int = 5) -> list[str]:
        """Real paths a wrong one may have meant: the same file name
        elsewhere, the same folder, or a close spelling."""
        import difflib
        files = self.files()
        name = rel.rstrip("/").split("/")[-1].lower()
        if not name:
            return []
        same = [f for f in files if f.split("/")[-1].lower() == name]
        dirs = sorted({"/".join(f.split("/")[:i]) + "/" for f in files for i in range(1, f.count("/") + 1)
                       if f.split("/")[i - 1].lower() == name})
        close = difflib.get_close_matches(rel, files, n=limit, cutoff=0.75)
        stem = name.rsplit(".", 1)[0]
        part = [f for f in files if len(stem) >= 4 and stem in f.split("/")[-1].lower()]
        return list(dict.fromkeys(same + dirs + close + part))[:limit]

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root))

    def files(self) -> list[str]:
        try:
            out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=self.root,
                                 capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                return [f for f in out.stdout.splitlines() if f]
        except (OSError, subprocess.TimeoutExpired):
            pass
        return [str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts]

    def read_text(self, p: Path) -> str:
        """A file's text as it is on disk: what edits work on."""
        if documents.kind(p) or documents.unsupported(p):
            again = " To change it, create it again in full" if writers.writable(p) else ""
            raise ToolError(f"{self.rel(p)} is a {documents.kind(p) or documents.unsupported(p)}: it can't be edited as text.{again}")
        try:
            return p.read_text(encoding=self.encoding)
        except UnicodeDecodeError as e:
            raise ToolError(f"{self.rel(p)} isn't a text file") from e

    def text_of(self, p: Path) -> str:
        """What read, outline and grep see: a file's text, or a document's
        (PDF, Word, Excel…) turned into text. Kept until the file changes."""
        if what := documents.unsupported(p):
            raise ToolError(f"{self.rel(p)} is a {what}, and {what}s can't be read (pictures, PDFs and documents can). "
                            "Nothing tells what it holds: say so, and don't guess from other files")
        if documents.is_image(p):
            raise ToolError(f"{self.rel(p)} is a picture: read it to see it")
        if not documents.kind(p):
            return self.read_text(p)
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        cache = self.__dict__.setdefault("_docs", {})
        if key not in cache:
            try:
                cache[key] = documents.text(p)
            except documents.DocumentError as e:
                raise ToolError(f"{self.rel(p)}: {e}") from e
        return cache[key]

    def view(self, p: Path) -> str:
        """A file as a person reviewing a change sees it (a document's text)."""
        try:
            return self.text_of(p) if documents.kind(p) and not documents.is_image(p) else (
                "(a picture)" if documents.is_image(p) else p.read_text(encoding=self.encoding, errors="replace"))
        except (ToolError, OSError):
            return ""

    def take_images(self) -> list[dict]:
        """Pictures the last tools opened, for the result message to carry."""
        images, self.images = getattr(self, "images", []), []
        return images

    def see(self, rel: str, source, what: str) -> str:
        """Attaches a picture to the result, or says why the model can't see it."""
        if not VISION:
            raise ToolError(f"{rel} is {what}, and the loaded model can't see pictures. Tell the user; "
                            "a model that can (Qwen3.5 or later, Gemma 4) would")
        try:
            url, w, h, tokens = documents.picture(source)
        except documents.DocumentError as e:
            raise ToolError(f"{rel}: {e}") from e
        self.__dict__.setdefault("images", []).append({"url": url, "tokens": tokens, "name": rel})
        return f"{w}x{h} pixels, attached to this message as a picture"

    # ---- tools ------------------------------------------------------------
    def list(self, path: str) -> str:
        base = self.path(path)
        prefix = "" if base == self.root else self.rel(base).rstrip("/") + "/"
        files = [f for f in self.files() if f.startswith(prefix)]
        if not files:
            return f"No files under {path or 'the repository'}."
        if len(files) <= LIST_FILES:
            return "\n".join(files)
        # Too many: one line per subfolder with its file count.
        counts: dict[str, int] = {}
        for f in files:
            parts = f[len(prefix):].split("/")
            key = prefix + (parts[0] + "/" if len(parts) > 1 else parts[0])
            counts[key] = counts.get(key, 0) + 1
        return f"{len(files)} files; by folder (list one to see its files):\n" + "\n".join(
            f"{k}  ({n} file{'s' if n != 1 else ''})" if k.endswith("/") else k for k, n in sorted(counts.items()))

    def grep(self, pattern: str, path: str = "") -> str:
        """Matching lines, grouped by file, code before documentation. A
        search that matches everywhere comes back as a count per file rather
        than as pages of lines (those filled the window and hid the code)."""
        if not pattern:
            raise ToolError("grep needs a pattern")
        base = self.path(path) if path else self.root
        args = ["git", "grep", "-n", "-I", "-E", "--untracked", "-e", pattern]
        if base != self.root:
            args += ["--", self.rel(base)]
        try:
            out = subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as e:
            raise ToolError("grep took too long; narrow the path") from e
        if out.returncode not in (0, 1):
            raise ToolError((out.stderr.strip() or "grep failed")[:300] + " (the pattern is an extended regex)")
        by_file: dict[str, list[tuple[str, str]]] = {}
        for ln in out.stdout.splitlines():
            f, _, rest = ln.partition(":")
            no, _, text = rest.partition(":")
            by_file.setdefault(f, []).append((no, text))
        by_file.update(self.grep_documents(pattern, base))
        elsewhere = ""
        if base != self.root:
            # Models guess a folder and keep looking there: say where else the
            # pattern is, so one search shows the whole repository.
            try:
                rest_out = subprocess.run(["git", "grep", "-c", "-I", "-E", "--untracked", "-e", pattern], cwd=self.root,
                                          capture_output=True, text=True, timeout=30).stdout
            except subprocess.TimeoutExpired:
                rest_out = ""
            counts = [(f, int(n)) for f, _, n in (ln.rpartition(":") for ln in rest_out.splitlines())
                      if n.isdigit() and f not in by_file]
            counts.sort(key=lambda c: (c[0].lower().endswith(LOW_RANK), -c[1], c[0]))
            if counts:
                elsewhere = ("\nOutside " + self.rel(base) + ": " + ", ".join(f"{f} ({n})" for f, n in counts[:8])
                             + (f", and {len(counts) - 8} more files" if len(counts) > 8 else ""))
        if not by_file:
            return f"No matches for {pattern!r}" + (f" in {path}." if path else ".") + elsewhere
        total = sum(len(v) for v in by_file.values())
        # Code first, then by how often it matches (the file about it, usually).
        order = sorted(by_file, key=lambda f: (f.lower().endswith(LOW_RANK), -len(by_file[f]), f))
        head = f"{total} match{'es' if total != 1 else ''} in {len(by_file)} file{'s' if len(by_file) != 1 else ''}"
        out_lines, shown, listed = [], 0, 0
        for f in order:
            hits = by_file[f]
            if shown >= GREP_HITS:
                break
            listed += 1
            take = hits[:min(GREP_PER_FILE, GREP_HITS - shown)] if len(by_file) > 1 else hits[:GREP_HITS]
            out_lines.append(f"{f} ({len(hits)}):")
            out_lines += [f"{no:>6}| {text.strip()[:180]}" for no, text in take]
            if len(hits) > len(take):
                out_lines.append(f"      … {len(hits) - len(take)} more in this file")
            shown += len(take)
        rest = order[listed:]
        if rest:
            out_lines.append("Also in: " + ", ".join(f"{f} ({len(by_file[f])})" for f in rest[:GREP_FILES])
                             + (f", and {len(rest) - GREP_FILES} more files" if len(rest) > GREP_FILES else ""))
        if rest or total > shown:
            out_lines.append("(not everything is shown: make the pattern more specific rather than going file by file)")
        return head + ":\n" + "\n".join(out_lines) + elsewhere

    def grep_documents(self, pattern: str, base: Path) -> dict[str, list[tuple[str, str]]]:
        """git grep skips binary files, so PDFs, Word files and the like are
        searched in their text (line numbers as read shows them)."""
        prefix = "" if base == self.root else self.rel(base)
        docs = [f for f in self.files() if documents.kind(Path(f)) and not documents.is_image(Path(f))
                and (not prefix or f == prefix or f.startswith(prefix.rstrip("/") + "/"))][:GREP_DOCS]
        if not docs:
            return {}
        found = {}
        for f in docs:
            try:
                text = self.text_of(self.root / f)
            except (ToolError, OSError):
                continue
            # The model's pattern goes to grep -E, as for code (git grep -E):
            # the same regex flavour, and no backtracking pattern run by
            # Python's re on a whole document (scripts/check_regexes.py).
            try:
                out = subprocess.run(["grep", "-n", "-E", "-e", pattern], input=text, capture_output=True,
                                     text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            hits = [(no, line) for no, _, line in (ln.partition(":") for ln in out.stdout.splitlines())]
            if hits:
                found[f] = hits
        return found

    # ---- definitions ------------------------------------------------------
    def symbols(self) -> list[tuple[str, str, int, str]]:
        """Every definition in the repository: (name, file, line, source line).
        Built once per session, from Aider's tree-sitter tags where it knows
        the language and a regex elsewhere; `warm` starts that at session
        start, and a caller meanwhile waits for it rather than building twice."""
        with self.__dict__.setdefault("_symbols_lock", threading.Lock()):
            if getattr(self, "_symbols", None) is None:
                self._symbols = self._build_symbols()
            return self._symbols

    def warm(self):
        """Builds the definitions index in the background (0.8 s for this
        repository with a cold tag cache, more for a big one), so the first
        message doesn't wait for it."""
        threading.Thread(target=self.symbols, name="oppx-symbols", daemon=True).start()

    def _build_symbols(self) -> list[tuple[str, str, int, str]]:
        found: list[tuple[str, str, int, str]] = []
        for rel in self.files():
            if rel.lower().endswith(LOW_RANK) or rel.lower().endswith((".png", ".ico", ".icns", ".jpg", ".woff2")):
                continue
            p = self.root / rel
            try:
                if p.stat().st_size > 600_000:
                    continue
                lines = p.read_text(encoding=self.encoding).splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            defs: dict[int, str] = {}
            if self.repo_map is not None:
                try:
                    for tag in self.repo_map.get_tags(str(p), rel):
                        if tag.kind == "def":
                            defs[tag.line + 1] = tag.name
                except Exception:  # noqa: BLE001 - the regex below still applies
                    pass
            for i, ln in enumerate(lines, 1):
                m = (CODE_DEF.match(ln) or CONST_DEF.match(ln)) if i not in defs else None
                if m and (not ln[:1].isspace() or m.group(0).lstrip().startswith(("pub", "fn", "def", "async", "export", "function", "class"))):
                    defs[i] = m.group(1)
            found += [(name, rel, no, lines[no - 1].strip()[:150]) for no, name in sorted(defs.items()) if no <= len(lines)]
        return found

    def related(self, text: str, limit: int = 10) -> str:
        """Definitions whose names contain words of a message (whole name
        parts: "pairing" finds PAIRING_TTL_S and new_pairing_code, "valid"
        doesn't find validate_name), most words and rarest words first: where
        to start, before the model has guessed a single name."""
        import math
        words = {w for w in re.findall(r"[a-z][a-z0-9]{3,}", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).lower())
                 if w not in STOPWORDS}
        words |= {w[:-1] for w in words if w.endswith("s") and len(w) > 4}
        if not words:
            return ""

        def parts(name: str) -> set[str]:
            return {p for p in re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).lower()) if p}

        # Not tests: their sentence-long names match any message.
        names = [(p, name, rel, no, line) for name, rel, no, line in self.symbols()
                 if not name.lower().startswith("test") and "/tests/" not in rel and len(p := parts(name)) <= 5]
        seen_in = {w: sum(1 for p, *_ in names if w in p) for w in words}
        weight = {w: 1 / math.log(2 + c) for w, c in seen_in.items() if 0 < c <= 80}  # words in everything say nothing
        scored = []
        for p, name, rel, no, line in names:
            hit = [w for w in weight if w in p]
            if hit:
                scored.append((-len(hit), -sum(weight[w] for w in hit), rel.lower().endswith(LOW_RANK), len(name), rel, no, line))
        scored.sort()
        return "\n".join(f"  {rel}:{no}: {line}" for *_, rel, no, line in scored[:limit])

    def find(self, query: str) -> str:
        """Definitions and files whose name contains the query's words (any
        spelling: start_server, startServer and "start server" are the same)."""
        words = [w for w in re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", query or "").lower()) if w]
        if not words:
            raise ToolError("find needs a name or part of one")
        flat = "".join(words)

        def score(name: str) -> int:
            n = re.sub(r"[^a-z0-9]", "", name.lower())
            if n == flat:
                return 0
            if n.startswith(flat) or n.endswith(flat):
                return 1
            if flat in n:
                return 2
            return 3 if all(w in n for w in words) else 9

        hits = sorted((score(name), rel.lower().endswith(LOW_RANK), rel, no, name, text)
                      for name, rel, no, text in self.symbols() if score(name) < 9)
        files = [f for f in self.files() if all(w in f.lower() for w in words)]
        out = []
        if files:
            out.append(f"Files named like it ({len(files)}):\n" + "\n".join(f"  {f}" for f in files[:15]))
        if hits:
            out.append(f"Definitions ({len(hits)}):\n" + "\n".join(
                f"  {rel}:{no}: {text}" for _, _, rel, no, _, text in hits[:FIND_HITS])
                + (f"\n  … {len(hits) - FIND_HITS} more (use a longer name)" if len(hits) > FIND_HITS else ""))
        if not out:
            # One word at a time: which part of the name exists at all?
            parts = []
            for w in words:
                n = sum(1 for name, *_ in self.symbols() if w in name.lower())
                parts.append(f"{w!r}: {n} definition{'s' if n != 1 else ''}")
            return (f"No definition or file named like {query!r}. " + ("By word: " + ", ".join(parts) + ". " if len(words) > 1 else "")
                    + "Try another name for it, or grep for a word that would appear in the code.")
        return "\n".join(out)

    def outline(self, path: str) -> str:
        p = self.path(path)
        if documents.is_image(p):
            return self.read(path, None, None)
        text = self.text_of(p)
        lines = text.splitlines()
        if documents.kind(p):
            # A document's outline is its parts (pages, sheets, chapters) and
            # headings, each part with its first line of text.
            heads = [i for i, ln in enumerate(lines) if re.match(r"--- \w+ \d+ of \d+", ln) or re.match(r"#{1,6} \S", ln)]
            marks = [lines[i] for i in heads if lines[i].startswith("--- ")]
            body = "\n".join(f"{i + 1:>5}| {lines[i]}" + (f" {lines[i + 1].strip()[:140]}" if lines[i].startswith("--- ") and i + 1 < len(lines) else "")
                              for i in heads[:300])
            size = f"{len(marks)} {marks[0].split()[1]}s, " if marks else ""  # "3 pages", "2 sheets"
            return f"{self.rel(p)}: {documents.kind(p)}, {size}{len(lines)} lines of text\n{body or '(no headings: read it in parts)'}"
        defs = []
        if self.repo_map is not None:
            try:
                for tag in self.repo_map.get_tags(str(p), self.rel(p)):
                    if tag.kind == "def":
                        defs.append(tag.line + 1)
            except Exception:  # noqa: BLE001 - fall back to the regex outline
                defs = []
        if not defs:
            pat = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:def|class|fn|struct|enum|trait|impl|type|interface|function|const|export\s+(?:default\s+)?(?:function|class|const))\b")
            defs = [i + 1 for i, ln in enumerate(lines) if pat.match(ln)]
        defs = sorted(set(defs))
        if not defs:
            return f"{self.rel(p)}: {len(lines)} lines, no definitions found; read it in parts."
        body = "\n".join(f"{n:>5}| {lines[n - 1].strip()[:160]}" for n in defs[:300])
        return f"{self.rel(p)}: {len(lines)} lines\n{body}"

    def read(self, path: str, start: int | None, end: int | None) -> str:
        p = self.path(path)
        if p.is_dir():
            raise ToolError(f"{path} is a folder; use list")
        if documents.is_image(p):
            self.mark(p)
            return f"{self.rel(p)}: {documents.kind(p)} picture, " + self.see(self.rel(p), p, "a picture")
        try:
            lines = self.text_of(p).splitlines()
        except ToolError as e:
            if not isinstance(e.__cause__, documents.NoText):
                raise
            return self.read_scan(p, e.__cause__.pages, start)
        self.mark(p)
        n = len(lines)
        start = max(1, start or 1)
        end = min(n, end or start + READ_LINES - 1, start + READ_LINES - 1)
        # Ranges are often guessed from an outline: show the lines around
        # them too (a doc comment sits just above the line an outline names).
        start, end = max(1, start - READ_PAD), min(n, end + READ_PAD)
        if n == 0:
            return f"{self.rel(p)} is empty."
        if start > n:
            raise ToolError(f"{self.rel(p)} has only {n} lines")
        numbered = [f"{i:>5}| {line}" for i, line in enumerate(lines, 1)]
        room = RESULT_CHARS - 200  # the header and the "not shown" line
        if n <= WHOLE_FILE and sum(len(x) + 1 for x in numbered) <= room:
            # Small files whole: a model reading a narrow window missed the
            # function it was looking for, though the window held it. Only
            # when it fits: a 272-line file shown whole for lines 205-272 was
            # cut at line 209, so the lines asked for were the ones not shown.
            start, end = 1, n
        # Whole lines up to the room there is, and the header says which: a
        # result cut later, mid-line, would claim lines the model never saw.
        size = 0
        for i in range(start, end + 1):
            size += len(numbered[i - 1]) + 1
            if size > room and i > start:
                end = i - 1
                break
        body = "\n".join(numbered[start - 1:end])
        rest = f"\n(lines {end + 1}-{n} not shown)" if end < n else ""
        return f"{self.rel(p)} lines {start}-{end} of {n}:\n{body}{rest}"

    def read_scan(self, p: Path, pages: int, start: int | None) -> str:
        """A PDF without text (a scan): its pages as pictures, a few per read."""
        rel = self.rel(p)
        first = min(max(1, start or 1), pages)
        last = min(pages, first + SCAN_PAGES - 1)
        shown = []
        for page in range(first, last + 1):
            try:
                image = documents.pdf_page_image(p, page)
            except documents.DocumentError as e:
                raise ToolError(f"{rel}: {e}") from e
            shown.append(f"page {page}: " + self.see(rel, image, f"a PDF of {pages} scanned pages"))
        self.mark(p)
        more = (f"\n(pages {last + 1}-{pages} not shown: read with \"start\": {last + 1} for the next ones)"
                if last < pages else "")
        return f"{rel}: PDF of {pages} pages without text (a scan); pages {first}-{last} as pictures:\n" + "\n".join(shown) + more

    def run(self, command: str) -> str:
        # No terminal input: a command that asks a question (npm create, an
        # installer) used to wait on the user's terminal, unseen, until the
        # timeout. Its own process group, so the timeout stops what it started
        # too: killing only the shell left `npm create vite` running.
        p = subprocess.Popen(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
        try:
            out, _ = p.communicate(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_group(p)
            try:
                out, _ = p.communicate(timeout=5)
            except subprocess.TimeoutExpired as e:  # something it started in another session still holds the pipe
                out = e.output if isinstance(e.output, str) else (e.output or b"").decode(errors="replace")
            return (f"Timed out after {RUN_TIMEOUT} s and was stopped. If it was waiting for an answer: it has no terminal, so pass "
                    "the answers as flags. If it is a server or a watcher, it never finishes: use the start tool.\n"
                    "Output until then:\n" + (_tail(out) or "(none)"))
        except BaseException:  # interrupted (Esc, Ctrl-C)
            _kill_group(p)
            raise
        return f"exit code {p.returncode}\n{_tail(out)}"

    def start(self, command: str) -> str:
        """Starts a command that keeps running (a dev server) and returns its
        first output; called again with the same command, what it has printed
        since. Everything started is stopped when the session ends."""
        job = self.jobs.get(command)
        if job and job["proc"].poll() is None:
            new = self._job_output(job)
            return (f"Still running (pid {job['proc'].pid}), not started again. " +
                    ("New output:\n" + new if new else "No new output since you last looked."))
        if not self.jobs:
            atexit.register(self.stop_jobs)
        log = tempfile.NamedTemporaryFile(prefix="oppx-start-", suffix=".log", delete=False)
        p = subprocess.Popen(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        job = self.jobs[command] = {"proc": p, "log": log.name, "seen": 0}
        # Until it has said something and gone quiet (a server printing its
        # address), or ended (a typo, a port in use).
        began = quiet = time.monotonic()
        size = 0
        while p.poll() is None and time.monotonic() - began < START_WAIT:
            time.sleep(0.2)
            now = os.path.getsize(log.name)
            if now != size:
                size, quiet = now, time.monotonic()
            elif size and time.monotonic() - quiet >= START_QUIET:
                break
        text = self._job_output(job)
        if p.poll() is not None:
            self._forget(command)
            return f"It ended at once with exit code {p.returncode}; nothing is left running.\n{text}"
        return (f"Started and left running (pid {p.pid}); it stops when this session ends, or with: kill -- -{p.pid}\n"
                f"Output so far:\n{text or '(none yet)'}")

    def _job_output(self, job: dict) -> str:
        try:
            with open(job["log"], "rb") as f:
                f.seek(job["seen"])
                data = f.read()
        except OSError:
            return ""
        job["seen"] += len(data)
        # Dev servers colour their output and redraw lines.
        return _tail(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", data.decode(errors="replace")))

    def _forget(self, command: str):
        job = self.jobs.pop(command, None)
        if job:
            try:
                os.unlink(job["log"])
            except OSError:
                pass

    def stop_jobs(self):
        for command in list(self.jobs):
            _kill_group(self.jobs[command]["proc"])
            self._forget(command)


def _dump(body: dict, reply, usage):
    """Debugging aid (OPPX_DUMP_REQUESTS=file): every model call and its reply
    as one JSON line, as the Aider engine writes its requests."""
    path = os.environ.get("OPPX_DUMP_REQUESTS")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"engine": "agent", "request": body, "reply": reply, "usage": usage}) + "\n")


def _tail(text: str) -> str:
    text = (text or "").strip()
    return "…" + text[-RESULT_CHARS:] if len(text) > RESULT_CHARS else text


def _kill_group(p: subprocess.Popen):
    """Stops a command and everything it started (it runs in its own session)."""
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            p.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        # The shell is gone; anything it left in the group gets the same signal next round.
        try:
            os.killpg(p.pid, 0)
        except (ProcessLookupError, PermissionError):
            return

def with_filename(reply: str, rel: str) -> str:
    """Adds the filename line Aider's parser needs above each SEARCH block
    that lacks one: the agent already knows the file, and Qwen3.6 dropped
    the line on every try, so a 25-step edit turn changed nothing."""
    out: list[str] = []
    for line in reply.splitlines(keepends=True):
        if line.strip().startswith("<<<<<<< SEARCH"):
            at = len(out) - 1 if out and out[-1].strip().startswith(("```", "~~~")) else len(out)
            named = at > 0 and out[at - 1].strip().strip("`*").endswith(rel)
            if not named:
                out.insert(at, rel + "\n")
        out.append(line)
    return "".join(out)


def _image_tokens(m: dict) -> int:
    """What a message's pictures cost in the prompt (none once it was cut)."""
    return 0 if m.get("_cut") else sum(img["tokens"] for img in m.get("_images") or [])


def apply_edit(ws: Workspace, path: str, reply: str) -> tuple[str, str, str]:
    """Applies the SEARCH/REPLACE blocks in `reply` to `path` with Aider's
    matcher (exact, then whitespace-tolerant). Returns (rel, before, after)."""
    p = ws.path(path)
    rel = ws.rel(p)
    before = ws.read_text(p)  # refuses a document or picture before Aider is even loaded
    from aider.coders.editblock_coder import DEFAULT_FENCE, do_replace, find_original_update_blocks

    content = before
    blocks = 0
    try:
        found = list(find_original_update_blocks(with_filename(reply, rel), valid_fnames=[rel]))
    except ValueError as e:
        raise ToolError(f"couldn't parse the edit: {str(e)[:400]}") from e
    for fname, search, replace in found:
        if fname and fname.strip() not in (rel, path):
            raise ToolError(f"the edit names {fname}, but you are editing {rel}")
        # A SEARCH that matches several places would be applied to the first,
        # often the wrong one (seen with a bare closing brace).
        if search.strip() and content.count(search) > 1:
            at = [content[:i].count("\n") + 1 for i in _find_all(content, search)][:6]
            raise ToolError(f"the SEARCH text matches {content.count(search)} places in {rel} (lines "
                            f"{', '.join(map(str, at))}); include more surrounding lines so it matches only one")
        new = do_replace(str(p), content, search, replace, DEFAULT_FENCE)
        if new is None:
            new = _spacing_match(content, search, replace)
        if new is None:
            raise ToolError(f"SEARCH text not found in {rel}.{_nearest(content, search)}")
        content = new
        blocks += 1
    if not blocks:
        if not reply.strip():
            raise ToolError("your edit was empty. If you need to see more of the file, read it first, then edit")
        raise ToolError("no SEARCH/REPLACE block found in your reply, which began: " + repr(reply.strip()[:300])
                        + ". Use exactly the <<<<<<< SEARCH / ======= / >>>>>>> REPLACE format")
    p.write_text(content, encoding=ws.encoding)
    return rel, before, content


def _spacing_match(content: str, search: str, replace: str) -> str | None:
    """The edit applied where SEARCH matches line for line once the spaces at
    each line's ends are ignored, if that is exactly one place; None otherwise.
    Aider's matcher forgives indentation off by the same amount on every line,
    not one line off by one: a continuation line with 10 spaces for the
    file's 9 cost six refused edits and a hex dump to find. A REPLACE line
    equal to a SEARCH line keeps the file's own text, so the slip isn't copied
    into the file."""
    want = search.splitlines()
    while want and not want[-1].strip():
        want.pop()
    if not any(w.strip() for w in want):
        return None
    lines = content.splitlines(keepends=True)
    key = [w.strip() for w in want]
    at = [i for i in range(len(lines) - len(want) + 1)
          if all(lines[i + k].strip() == key[k] for k in range(len(want)))]
    if len(at) != 1:
        return None
    i = at[0]
    file_line = {}
    for k, w in enumerate(key):
        file_line.setdefault(w, lines[i + k].rstrip("\r\n"))
    eol = "\r\n" if lines[i].endswith("\r\n") else "\n"
    new = [file_line.get(r.strip(), r) if r.strip() else r for r in replace.splitlines()]
    body = "".join(x + eol for x in new)
    if not lines[i + len(want) - 1].endswith(("\n", "\r")):
        body = body[:-len(eol)]  # the match ran to the file's end, which has no newline
    return "".join(lines[:i]) + body + "".join(lines[i + len(want):])


def _find_all(text: str, part: str):
    i = text.find(part)
    while i != -1:
        yield i
        i = text.find(part, i + 1)


def balance_warning(before: str, after: str) -> str:
    """A note when an edit changes the file's bracket balance, which usually
    means a block was cut or duplicated (language-agnostic, so a hint only)."""
    deltas = []
    for o, c in ("{}", "()", "[]"):
        d = (after.count(o) - after.count(c)) - (before.count(o) - before.count(c))
        if d:
            deltas.append(f"{o}{c} {'+' if d > 0 else ''}{d}")
    if not deltas:
        return ""
    return (f"\nWarning: this edit changed the bracket balance of the file ({', '.join(deltas)}); "
            "check the lines above for a missing or extra bracket and fix it.")


def _nearest(content: str, search: str) -> str:
    """The lines of the file that best match a SEARCH text that didn't, so
    the model can copy them exactly on its next try."""
    import difflib

    want = [ln.strip() for ln in search.splitlines() if ln.strip()]
    lines = content.splitlines()
    if not want or not lines:
        return ""
    best, best_i = 0.0, -1
    for i, ln in enumerate(lines):
        r = difflib.SequenceMatcher(None, ln.strip(), want[0]).ratio()
        if r > best:
            best, best_i = r, i
    if best < 0.5:
        return " Nothing like its first line is in the file; read the file to find the right place."
    lo, hi = max(0, best_i - 3), min(len(lines), best_i + len(want) + 3)
    body = "\n".join(f"{i + 1:>5}| {lines[i]}" for i in range(lo, hi))
    return f" The closest lines in the file are:\n{body}\nCopy SEARCH text exactly from the file (without the line numbers) and try again."


def changed_region(before: str, after: str, context: int = 3) -> str:
    """The edited lines with a little context, numbered, for the model to check."""
    import difflib

    a, b = before.splitlines(), after.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    spans = [(j1, j2) for tag, _, _, j1, j2 in sm.get_opcodes() if tag != "equal"]
    if not spans:
        return "(no change)"
    lo = max(0, spans[0][0] - context)
    hi = min(len(b), spans[-1][1] + context)
    if hi - lo > 80:
        hi = lo + 80
    return "\n".join(f"{i + 1:>5}| {b[i]}" for i in range(lo, hi))
