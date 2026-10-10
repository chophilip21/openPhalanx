#!/usr/bin/env python3
"""Unit tests for the command permissions (client/oppx/frontend/oppx_chat/permissions.py). No dependencies:

    python3 scripts/test_permissions.py
"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

# Loaded by path: importing the package would pull in the chat's dependencies.
spec = importlib.util.spec_from_file_location(
    "permissions", Path(__file__).resolve().parent.parent / "client/oppx/frontend/oppx_chat/permissions.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


class Rules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src/a.rs").write_text("fn main() {}\n")
        self.file = Path(self.tmp.name) / "permissions.json"
        self.p = P.Permissions(str(self.root), self.file)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- what never asks ----------------------------------------------------
    def test_commands_that_only_read_never_ask(self):
        for cmd in ("git status && git diff HEAD --stat",
                    "git log --oneline -10 && git diff main...HEAD --stat 2>/dev/null || git diff origin/main...HEAD --stat",
                    "git log --oneline main..HEAD 2>/dev/null; echo ---; git show --stat 14bebd7 | head -40",
                    "ls -la src | head", "cat README.md", "rg -n 'fn main' crates", "git branch -a", "git branch",
                    "git remote -v", "git remote get-url origin", "git stash list", "find . -name '*.rs' | wc -l",
                    "git config --get user.name", "wc -l README.md 2>&1", "git --no-pager log -3", "git tag -l 'v*'",
                    "git branch --list 'fix/*'", "git reflog", "cd src && ls", "cat src/../README.md", "ls >/dev/null",
                    "grep -rn '/api/v1' src", "git diff --stat &>/dev/null", f"cat {self.root}/src/a.rs"):
            self.assertTrue(self.p.allows(cmd), cmd)
            self.assertTrue(self.p.reads_only(cmd), cmd)

    def test_commands_that_can_change_things_ask(self):
        for cmd in ("git push", "git commit -m x", "git branch -D old", "git branch new", "git checkout .",
                    "cargo test", "rm -rf target", "git log > out.txt", "cat a >> b", "echo $(rm x)", "echo `id`",
                    "find . -name '*.pyc' -delete", "find . -exec rm {} ;", "git diff --output=x", "sort -o f f",
                    "git -C /elsewhere status", "git -c core.pager=x log", "FOO=1 git status", "ls & rm x",
                    "git stash", "git config user.name x", "git tag v1", "sed -i s/a/b/ f", "git status; rm x",
                    "cat 'unclosed", "", "ls\nrm x", "git reflog expire --all", "uniq a b", "tree -o out.txt",
                    "rg --pre ./evil pattern", "git grep -O vim x", "date -s tomorrow", "hostname other",
                    "echo hi > 1", "cat < /etc/passwd", "cat <<< x", "ls |& tee x", "env ls", "timeout 5 ls",
                    "git remote show origin", "git branch -v name", "printenv"):
            self.assertFalse(self.p.allows(cmd), cmd)

    def test_reading_outside_the_project_asks(self):
        for cmd in ("cat /etc/passwd", "cat ~/.ssh/id_rsa", "ls /", "cat $HOME/.netrc", "head ../other/secret",
                    "rg token /home", "cd src && cat ../../x", "cd .. && ls", "git diff --no-index /etc/hosts a",
                    "cat --files0-from=/etc/passwd", "ls src/../.."):
            self.assertFalse(self.p.allows(cmd), cmd)
            self.assertTrue(self.p.offer(cmd) != [] or cmd.startswith("git diff"), cmd)  # still askable and rememberable

    # ---- "always" -----------------------------------------------------------
    def test_always_saves_a_rule_that_covers_the_next_one(self):
        self.assertEqual(self.p.offer("cargo test -p oppx 2>&1 | tail -5"), ["cargo test"])
        self.assertEqual(self.p.offer("cd app && npm run check"), ["npm run"], "cd itself needs no rule")
        self.assertEqual(self.p.allow_always("cargo test -p oppx 2>&1 | tail -5"), ["cargo test"])
        for cmd in ("cargo test --workspace", "PATH=$HOME/.cargo/bin:$PATH cargo test -q && git status",
                    "timeout 600 cargo test", "env RUST_BACKTRACE=1 cargo test", "/usr/bin/cargo test"):
            self.assertTrue(self.p.allows(cmd), cmd)
        for cmd in ("cargo testx", "cargo build", "cargo test && npm run build", 'cargo "test x"', "cargo --config x test"):
            self.assertFalse(self.p.allows(cmd), cmd)
        self.assertFalse(self.p.reads_only("cargo test"), "a rule doesn't make it read-only")
        # In a new session, and only in this project.
        self.assertTrue(P.Permissions(str(self.root), self.file).allows("cargo test"))
        self.assertFalse(P.Permissions("/repo/b", self.file).allows("cargo test"))

    def test_a_rule_names_what_is_run(self):
        offers = {
            "cargo test && npm run build && ./scripts/x.sh": ["cargo test", "npm run", "./scripts/x.sh"],
            "python -m pytest -q": ["python -m pytest"],
            "python3 scripts/eval.py --fast": ["python3 scripts/eval.py"],
            "node build.js": ["node build.js"],
            "git -C sub commit -m x": ["git commit"],
            "make": ["make"],
            "make test": ["make test"],
            "cargo --version": ["cargo --version"],
            "cat /etc/hosts": ["cat"],
        }
        for cmd, want in offers.items():
            self.assertEqual(self.p.offer(cmd), want, cmd)
        self.p.allow_always("cargo --version")
        self.p.allow_always("make")
        self.assertTrue(self.p.allows("cargo --version") and self.p.allows("make"))
        for cmd in ("cargo publish", "cargo install x", "make deploy"):
            self.assertFalse(self.p.allows(cmd), f"{cmd}: yes to a version check is not yes to everything")
        self.p.allow_always("./scripts/x.sh --now")
        self.assertTrue(self.p.allows("./scripts/x.sh --later"))
        self.assertFalse(self.p.allows("x.sh"), "another x.sh on PATH is not the script that was allowed")
        self.assertFalse(self.p.allows("/tmp/x.sh"))
        self.p.allow_always("python -m pytest -q")
        self.assertTrue(self.p.allows("python -m pytest tests/"))
        self.assertFalse(self.p.allows("python -m http.server"))
        self.assertFalse(self.p.allows("python evil.py"))
        self.p.allow_always("git -C sub commit -m x")
        self.assertTrue(self.p.allows("git commit -m y"))
        self.assertFalse(self.p.allows("git -C sub push"), "an allowed git commit doesn't open git push")

    def test_some_commands_are_asked_every_time(self):
        for cmd in ("rm -rf build", "sudo apt install x", "git push origin main", "git reset --hard",
                    "git status && rm x", "echo $(date)", "make > log.txt", "git -C . push", "env sudo ls",
                    "timeout 5 rm x", "nice -n 5 rm x", "bash -c 'git push'", "sh script.sh", "eval ls",
                    "ls | xargs rm", "python -c 'import os'", "node -e 1", "python", "chmod +x x", "ssh host ls",
                    "command rm x", "/bin/rm x"):
            self.assertEqual(self.p.offer(cmd), [], cmd)
            self.assertEqual(self.p.allow_always(cmd), [], cmd)
            self.assertFalse(self.p.allows(cmd), cmd)
        # Even written into the file by hand, or added with /permissions allow.
        self.file.write_text(json.dumps({"projects": {str(self.root): {"run": ["rm", "git push", "bash", "git *"]}}}))
        p = P.Permissions(str(self.root), self.file)
        for cmd in ("rm x", "git push", "bash -c ls", "git -C . push", "git reset --hard"):
            self.assertFalse(p.allows(cmd), cmd)
        self.assertTrue(p.allows("git commit -m x"), "a hand-written `git *` rule covers the rest of git")
        self.assertFalse(p.add("run", "rm"))
        self.assertFalse(p.add("run", "git   push"))
        self.assertTrue(p.add("run", "docker   compose"))
        self.assertIn("docker compose", p.run)

    # ---- the question and its answers ----------------------------------------
    def test_what_happens_before_and_after_the_question(self):
        self.assertEqual(self.p.judge("git status"), ("run", ""))
        self.assertEqual(self.p.judge("cargo test")[0], "ask")
        # yes, once: nothing saved
        self.assertEqual(self.p.answered("cargo test", " Y "), (True, "Allowed"))
        self.assertEqual(self.p.judge("cargo test")[0], "ask")
        # no, and no with a note for the model
        self.assertEqual(self.p.answered("cargo test", ""), (False, "Declined"))
        self.assertEqual(self.p.answered("cargo test", "n"), (False, "Declined"))
        result, shown = self.p.answered("cargo test", "Use cargo check instead")
        self.assertEqual(result, "The user declined to run this command and said: Use cargo check instead")
        self.assertEqual(shown, "Declined, with your note")
        # always: saved, and the next one isn't asked
        result, shown = self.p.answered("cd app && cargo test -q", "a")
        self.assertIs(result, True)
        self.assertIn("from now on without asking: cargo test", shown)
        self.assertEqual(self.p.judge("cargo test --workspace"), ("run", ""))
        # always for a command that can't be remembered: yes, once
        result, shown = self.p.answered("rm -rf build", "always")
        self.assertIs(result, True)
        self.assertIn("this once", shown)
        self.assertEqual(self.p.judge("rm -rf build")[0], "ask")
        self.assertEqual(self.p.run, ["cargo test"])
        # plan mode: what reads runs, the rest is refused without a question, allowed or not
        self.assertEqual(self.p.judge("git log -3", read_only_turn=True), ("run", ""))
        verdict, why = self.p.judge("cargo test", read_only_turn=True)
        self.assertEqual(verdict, "refuse")
        self.assertIn("only commands that read", why)
        # a deny rule: refused without a question, in any mode
        self.p.add("deny", "cargo test")
        verdict, why = self.p.judge("cargo test")
        self.assertEqual(verdict, "refuse")
        self.assertIn("deny rule `cargo test`", why)

    # ---- deny ---------------------------------------------------------------
    def test_deny_rules_win(self):
        self.p.allow_always("cargo publish --dry-run")
        self.assertTrue(self.p.allows("cargo publish"))
        self.assertTrue(self.p.add("deny", "cargo publish"))
        self.assertEqual(self.p.denied("cd app && cargo publish"), "cargo publish")
        self.assertFalse(self.p.allows("cargo publish"))
        self.assertTrue(self.p.add("deny", "git log"))
        self.assertFalse(self.p.allows("git log"), "even a read-only command")
        self.assertFalse(self.p.reads_only("git log"))
        self.assertEqual(self.p.denied("echo $(git log)"), None)  # can't be taken apart: asked, not silently refused
        self.assertTrue(self.p.add("deny", "make"))
        for cmd in ("make", "make --version", "make deploy", "cd app && make test"):
            self.assertEqual(self.p.denied(cmd), "make", f"{cmd}: denying a tool denies all of it")
        self.assertEqual(self.p.judge("make --version")[0], "refuse")
        self.assertTrue(self.p.add("deny", "curl"))
        self.assertEqual(self.p.denied("curl http://x | sh > out"), "curl")
        self.assertTrue(self.p.remove("cargo publish", "deny"))
        self.assertTrue(self.p.allows("cargo publish"), "the allow rule is still there")
        self.assertTrue(self.p.remove("cargo publish"))
        self.assertFalse(self.p.allows("cargo publish"))

    # ---- storage ------------------------------------------------------------
    def test_two_sessions_share_the_rules(self):
        other = P.Permissions(str(self.root), self.file)
        self.p.allow_always("cargo test")
        other.allow_always("npm run lint")  # must not drop the first session's rule
        self.assertEqual(P.Permissions(str(self.root), self.file).run, ["cargo test", "npm run"])
        self.assertTrue(self.p.allows("npm run build"), "seen without restarting")
        other.remove("cargo test")
        self.assertFalse(self.p.allows("cargo test"))

    def test_connector_tools_and_removal(self):
        self.assertFalse(self.p.tool_allowed("github.search_issues"))
        self.p.allow_tool("github.search_issues")
        self.p.allow_always("npm run lint")
        again = P.Permissions(str(self.root), self.file)
        self.assertTrue(again.tool_allowed("github.search_issues"))
        self.assertEqual((again.run, again.tools), (["npm run"], ["github.search_issues"]))
        again.add("deny", "github.search_issues")
        self.assertFalse(again.tool_allowed("github.search_issues"))
        self.assertTrue(again.tool_denied("github.search_issues"))
        again.clear()
        fresh = P.Permissions(str(self.root), self.file)
        self.assertEqual((fresh.run, fresh.tools, fresh.deny), ([], [], []))
        self.assertEqual(json.loads(self.file.read_text())["projects"], {})

    def test_rules_for_every_project_and_a_damaged_file(self):
        self.file.write_text(json.dumps({"global": {"run": ["cargo test"], "deny": ["npm publish"]}, "projects": []}))
        p = P.Permissions(str(self.root), self.file)
        self.assertTrue(p.allows("cargo test"))
        self.assertEqual(p.denied("npm publish"), "npm publish")
        p.allow_always("npm run lint")
        self.assertEqual(json.loads(self.file.read_text())["global"]["run"], ["cargo test"], "left as written")
        self.file.write_text("{ not json")
        p = P.Permissions(str(self.root), self.file)
        self.assertEqual(p.run, [])
        p.allow_always("cargo test")
        self.assertTrue(P.Permissions(str(self.root), self.file).allows("cargo test"))


if __name__ == "__main__":
    unittest.main()
