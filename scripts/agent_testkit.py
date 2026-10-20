"""What the agent tests share: the package on the path, a scripted model
(`Scripted`) and a small repository with an agent on it (`Repo`)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client/oppx/frontend"))

from oppx_chat import agent as A  # noqa: E402


try:  # edits are applied with Aider's matcher; CI runs without it
    import aider  # noqa: F401
    HAVE_AIDER = True
except ImportError:
    HAVE_AIDER = False


needs_aider = unittest.skipUnless(HAVE_AIDER, "needs aider-chat (run with the oppx engine's Python)")


try:  # the terminal module draws with rich, which CI doesn't install either
    import rich  # noqa: F401
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


class Scripted:
    """An Agent whose model calls are played from lists."""

    def __init__(self, repo: str):
        self.shown, self.steps, self.notices, self.prompts = [], [], [], []
        self.actions, self.texts = [], []
        hooks = A.Hooks(step=lambda t, d: self.steps.append((t, d)), notice=self.notices.append,
                        stream=lambda text, final: self.shown.append(text) if final else None)
        self.agent = ag = A.Agent(ws=A.Workspace(repo), hooks=hooks)
        ag._fit = lambda: None

        def action(tools):
            act = dict(self.actions.pop(0)) if self.actions else {"tool": "answer"}
            if act["tool"] not in tools:
                act = {"tool": "answer"}
            return act

        def text_once(max_tokens, stream, temperature=None, regex=None):
            self.prompts.append((ag.messages[-1]["content"], regex))
            out = self.texts.pop(0) if self.texts else "Done."
            if stream:
                acc = ""
                for word in out.split(" "):
                    acc += word + " "
                    ag.hooks.stream(acc.strip(), False)
                ag.hooks.stream(out, True)
            return out

        ag._action, ag._text_once = action, text_once


class Repo(unittest.TestCase):
    """A small repository and an agent with a scripted model on it."""

    BLOCK = "src/calc.py\n<<<<<<< SEARCH\nLIMIT = 3\n=======\nLIMIT = 4\n>>>>>>> REPLACE"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "src").mkdir()
        (root / "src/calc.py").write_text("LIMIT = 3\n\n\ndef add(a, b):\n    return a + b\n\n\ndef start_server(port):\n    return port\n")
        (root / "src/other.py").write_text("from calc import add\n\nprint(add(1, 2))\n")
        (root / "README.md").write_text("add things with add()\n" * 30)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        self.s = Scripted(str(root))
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()
