#!/usr/bin/env python3
"""scripts/check_regexes.py itself: that it finds every form of pattern, reads
Python the way Python runs it, and fails on a pattern that backtracks.
The recheck part needs `npm ci --prefix scripts/regexcheck` (skipped without it)."""

import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_regexes as C  # noqa: E402

HAS_CHECKER = shutil.which("node") and (C.ROOT / "scripts/regexcheck/node_modules/recheck").exists()


def found(code: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "m.py"
        f.write_text(textwrap.dedent(code))
        return C.python_patterns(f, Path(tmp))


class Finds(unittest.TestCase):
    def test_every_form_is_found(self):
        pats = found(r'''
            import re
            WORD = r"\w+"
            A = re.compile(r"^a+$")
            B = re.compile(WORD + r"\d")
            C = re.compile(rf"(?:{WORD})+?x", re.I | re.M)
            LIST = [re.compile(p) for p in (r"one", r"two")]
            def f(text, n):
                local = r"\s*#"
                re.search(local, text)
                re.sub(r"[ \t]+$", "", text, flags=re.M)
                return re.findall(f"{re.escape(text)}y", text)
        ''')
        sources = {p.get("source") for p in pats}
        for want in (r"^a+$", r"\w+\d", r"(?:\w+)+?x", "one", "two", r"\s*#", r"[ \t]+$", "xy"):
            self.assertIn(want, sources)
        self.assertEqual(next(p for p in pats if p.get("source") == r"(?:\w+)+?x")["flags"], "im")

    def test_a_pattern_known_only_at_run_time_must_be_marked(self):
        pats = found('''
            import re
            def f(p, s):
                re.search(p, s)
                # regex: runtime (the caller's fixed words)
                re.match(p, s)
        ''')
        self.assertEqual([(p["unresolved"] != "", p["runtime"]) for p in pats], [(True, False), (True, True)])

    def test_a_compiled_pattern_is_checked_as_it_is_used(self):
        pats = found('''
            import re
            ONLY_FULL = re.compile(r"a")
            ANCHORED = re.compile(r"b")
            ANYWHERE = re.compile(r"c")
            UNUSED = re.compile(r"d")
            ONLY_FULL.fullmatch("x"); ANCHORED.match("x"); ANCHORED.fullmatch("x"); ANYWHERE.match("x"); ANYWHERE.sub("", "x")
        ''')
        self.assertEqual({p["source"]: p["mode"] for p in pats}, {"a": "fullmatch", "b": "match", "c": "search", "d": "search"})


class PythonSemantics(unittest.TestCase):
    def test_lines_end_only_at_newline(self):
        js, flags = C.python_to_js(r"^a.$", "m", "search")
        self.assertEqual(flags, "")
        self.assertEqual(js, r"(?<![^\n])a[^\n](?![^\n])")
        js, _ = C.python_to_js(r"[.^$]\Aa\Z", "", "search")
        self.assertEqual(js, r"[.^$](?<![\s\S])a(?![\s\S])")

    def test_syntax_and_anchoring(self):
        self.assertEqual(C.python_to_js(r"(?P<n>a)(?P=n){,3}", "", "match")[0], r"^(?:(?<n>a)\k<n>{0,3})")
        self.assertEqual(C.python_to_js(r"(?ix) a  b # comment", "", "fullmatch"), (r"^(?:ab)$", "i"))


@unittest.skipUnless(HAS_CHECKER, "needs Node.js and `npm ci --prefix scripts/regexcheck`")
class Verdicts(unittest.TestCase):
    def verdicts(self, *items):
        py = []
        for source, mode in items:
            js, flags = C.python_to_js(source, "", mode)
            py.append({"where": source, "source": source, "js": js, "jsflags": flags})
        return C.analyze(py, [])

    def test_backtracking_fails_and_linear_passes(self):
        result = self.verdicts((r"(a+)+$", "search"), (r"\s+$", "search"), (r"^\s*#", "search"),
                               (r"(?<![ \t])[ \t]+\n", "search"))
        by_python = {c["python"]: c["verdict"] for c in result["checked"]}
        self.assertEqual(by_python[r"(a+)+$"], "exponential")
        self.assertEqual(by_python[r"\s+$"], "poly-2")
        self.assertEqual(by_python[r"^\s*#"], "linear")
        self.assertEqual(by_python[r"(?<![ \t])[ \t]+\n"], "linear")
        self.assertEqual(len(result["problems"]), 2)

    def test_anchoring_matters(self):
        by_python = {c["python"]: c["verdict"] for c in self.verdicts((r"\s+x", "search"))["checked"]}
        self.assertEqual(by_python[r"\s+x"], "poly-2")
        by_python = {c["python"]: c["verdict"] for c in self.verdicts((r"\s+x", "match"))["checked"]}
        self.assertEqual(by_python[r"\s+x"], "linear")


if __name__ == "__main__":
    unittest.main()
