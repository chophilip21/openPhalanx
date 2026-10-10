#!/usr/bin/env python3
"""Unit tests for the command allowlist (client/oppx/frontend/oppx_chat/permissions.py). No dependencies:

    python3 scripts/test_permissions.py
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

# Loaded by path: importing the package would pull in the chat's dependencies.
spec = importlib.util.spec_from_file_location(
    "permissions", Path(__file__).resolve().parent.parent / "client/oppx/frontend/oppx_chat/permissions.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


class Allowlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "permissions.json"
        self.p = P.Permissions("/repo/a", self.file)

    def tearDown(self):
        self.tmp.cleanup()

    def test_commands_that_only_read_never_ask(self):
        for cmd in ("git status && git diff HEAD --stat",
                    "git log --oneline -10 && git diff main...HEAD --stat 2>/dev/null || git diff origin/main...HEAD --stat",
                    "git log --oneline main..HEAD 2>/dev/null; echo ---; git show --stat 14bebd7 | head -40",
                    "ls -la src | head", "cat README.md", "rg -n 'fn main' crates", "git branch -a", "git branch",
                    "git remote -v", "git stash list", "find . -name '*.rs' | wc -l", "git config --get user.name",
                    "wc -l < /dev/null 2>&1" if False else "wc -l README.md 2>&1"):
            self.assertTrue(self.p.allows(cmd), cmd)

    def test_commands_that_can_change_things_ask(self):
        for cmd in ("git push", "git commit -m x", "git branch -D old", "git branch new", "git checkout .",
                    "cargo test", "rm -rf target", "git log > out.txt", "cat a >> b", "echo $(rm x)", "echo `id`",
                    "find . -name '*.pyc' -delete", "find . -exec rm {} ;", "git diff --output=x", "sort -o f f",
                    "git -C /elsewhere status", "FOO=1 git status", "ls & rm x", "git stash", "git config user.name x",
                    "git tag v1", "sed -i s/a/b/ f", "git status; rm x", "cat 'unclosed", "", "ls\nrm x"):
            self.assertFalse(self.p.allows(cmd), cmd)

    def test_always_saves_a_rule_that_covers_the_next_one(self):
        self.assertEqual(self.p.offer("cargo test -p oppx 2>&1 | tail -5"), ["cargo test"])
        self.assertEqual(self.p.allow_always("cargo test -p oppx 2>&1 | tail -5"), ["cargo test"])
        self.assertTrue(self.p.allows("cargo test --workspace"))
        self.assertTrue(self.p.allows("PATH=$HOME/.cargo/bin:$PATH cargo test -q && git status"))
        self.assertFalse(self.p.allows("cargo testx"), "a rule matches whole words")
        self.assertFalse(self.p.allows("cargo build"), "another subcommand is another rule")
        self.assertFalse(self.p.allows("cargo test && npm run build"), "every part must be covered")
        self.assertEqual(self.p.offer("cargo test && npm run build && ./scripts/x.sh"), ["npm run", "x.sh"])
        # In a new session, and only in this project.
        self.assertTrue(P.Permissions("/repo/a", self.file).allows("cargo test"))
        self.assertFalse(P.Permissions("/repo/b", self.file).allows("cargo test"))

    def test_some_commands_are_never_remembered(self):
        for cmd in ("rm -rf build", "sudo apt install x", "git push origin main", "git reset --hard", "git status && rm x",
                    "echo $(date)", "make > log.txt"):
            self.assertEqual(self.p.offer(cmd), [], cmd)
            self.assertEqual(self.p.allow_always(cmd), [])
            self.assertFalse(self.p.allows(cmd))
        self.p.run = ["rm", "git push"]  # even written into the file by hand
        self.assertFalse(self.p.allows("rm x"))
        self.assertFalse(self.p.allows("git push"))

    def test_connector_tools_and_removal(self):
        self.assertFalse(self.p.tool_allowed("github.search_issues"))
        self.p.allow_tool("github.search_issues")
        self.p.allow_always("npm run lint")
        again = P.Permissions("/repo/a", self.file)
        self.assertTrue(again.tool_allowed("github.search_issues"))
        self.assertEqual((again.run, again.tools), (["npm run"], ["github.search_issues"]))
        self.assertTrue(again.remove("npm run"))
        self.assertFalse(again.remove("npm run"))
        self.assertFalse(P.Permissions("/repo/a", self.file).allows("npm run lint"))
        again.clear()
        self.assertEqual(P.Permissions("/repo/a", self.file).tools, [])

    def test_a_damaged_file_is_no_rules(self):
        self.file.write_text("{ not json")
        p = P.Permissions("/repo/a", self.file)
        self.assertEqual(p.run, [])
        p.allow_always("cargo test")
        self.assertTrue(P.Permissions("/repo/a", self.file).allows("cargo test"))


if __name__ == "__main__":
    unittest.main()
