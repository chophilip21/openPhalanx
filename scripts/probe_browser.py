#!/usr/bin/env python3
"""Does the loaded model ask for the browser when a message needs a running
page looked at, and leave it alone otherwise? Runs the real agent loop on a
small web app (written to a scratch folder) and records whether the model
chooses the `browser` action within its first three actions. Nothing is
started or edited: the browser, edits and commands stop the probe.

    oppx proxy --no-web     # prints OPENAI_API_BASE and OPENAI_API_KEY
    OPENAI_API_BASE=… OPENAI_API_KEY=… \\
      ~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/probe_browser.py [--no-hint] [--runs N]

Run it after changing the browser's prompt line or hint, and on a new model.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client/oppx/frontend"))
if "--reasoning" in sys.argv:
    os.environ["OPPX_REASONING"] = "1"  # read when the routing module loads

from oppx_chat import agent as A  # noqa: E402
from oppx_chat import mcp  # noqa: E402
from oppx_chat.routing import wants_edit  # noqa: E402

APP = {
    "package.json": json.dumps({"name": "shop", "private": True, "scripts": {"dev": "vite --port 5173", "build": "vite build",
                                "test": "vitest"}, "dependencies": {"react": "18.3.1", "react-dom": "18.3.1"},
                                "devDependencies": {"vite": "5.4.0", "vitest": "2.0.0"}}, indent=2),
    "index.html": '<!doctype html>\n<html><body><div id="root"></div><script type="module" src="/src/main.jsx"></script></body></html>\n',
    "src/main.jsx": 'import React from "react";\nimport { createRoot } from "react-dom/client";\nimport App from "./App";\n\ncreateRoot(document.getElementById("root")).render(<App />);\n',
    "src/App.jsx": ('import { useEffect, useState } from "react";\nimport Counter from "./Counter";\nimport { formatPrice } from "./price";\n\n'
                    'export default function App() {\n  const [products, setProducts] = useState([]);\n  const [dark, setDark] = useState(false);\n'
                    '  useEffect(() => { fetch("/api/products").then(r => r.json()).then(setProducts); }, []);\n'
                    '  return (\n    <main className={dark ? "dark" : ""}>\n      <nav><button onClick={() => setDark(!dark)}>Theme</button></nav>\n'
                    '      <ul>{products.map(p => <li key={p.id}>{p.name} {formatPrice(p.cents)}</li>)}</ul>\n'
                    '      <form onSubmit={e => e.preventDefault()}><input name="email" /><button>Sign up</button></form>\n'
                    '      <Counter />\n    </main>\n  );\n}\n'),
    "src/Counter.jsx": ('import { useState } from "react";\n\nexport default function Counter() {\n  const [n, setN] = useState(0);\n'
                        '  const increment = () => setN(n + 1);\n  return <button onClick={increment}>Clicked {n} times</button>;\n}\n'),
    "src/price.js": 'export function formatPrice(cents) {\n  return `$${(cents / 100).toFixed(2)}`;\n}\n',
    "vite.config.js": 'export default { server: { port: 5173, proxy: { "/api": "http://localhost:8000" } } };\n',
    "README.md": "# Shop\n\nA small React shop. Start it with `npm run dev`; it serves on http://localhost:5173.\n",
}

# (message, should the browser be chosen)
CASES = [
    ("Open http://localhost:5173 and tell me if there are any console errors", True),
    ("The sign up form on my dev server doesn't do anything when I click the button. Can you check it in the browser?", True),
    ("npm run dev is running. Does the home page actually show the product list?", True),
    ("Check whether the Theme button really changes the page colors in the running app", True),
    ("My app shows a blank white page at localhost:5173. What's going wrong?", True),
    ("Visually inspect the landing page for layout problems; the dev server is up", True),
    ("Go to http://127.0.0.1:5173, click the counter three times and tell me what it says", True),
    ("Are any network requests failing when the page loads? It's running on port 5173", True),
    ("Use playwright to click through the sign up flow on the preview build", True),
    ("After my last change the nav bar overlaps the list on the page. Can you look at it running?", True),
    ("What does the Counter component do?", False),
    ("Rename the increment function to addOne", False),
    ("Why does App.jsx use useEffect?", False),
    ("Add a reset button to the counter", False),
    ("What port does the dev server use?", False),
    ("Write a unit test for formatPrice", False),
    ("Explain how the products get loaded", False),
    ("What's the latest version of React?", False),
    ("Fix the typo in the README", False),
    ("How do I start the dev server?", False),
    ("Add a footer to the home page component", False),
    ("Why does Counter re-render on every click?", False),
    ("Make the landing page heading say Welcome", False),
    ("Where is the sign up form handled in the code?", False),
]


class Stop(Exception):
    pass


def make_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="probe-browser-"))
    for rel, text in APP.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def first_moves(repo: Path, text: str, reasoning: bool, context: int) -> list[str]:
    moves: list[str] = []

    def stop(why):
        def raise_(*a, **k):
            moves.append(why)
            raise Stop()
        return raise_

    hooks = A.Hooks(start_browser=stop("browser"), confirm_run=stop("run"), confirm_tool=stop("connector"))
    ag = A.Agent(ws=A.Workspace(str(repo)), hooks=hooks, context=context, reasoning=reasoning,
                 connectors=mcp.Hub(repo), browser="offer")
    action = ag._action

    def counted(tools):
        if len(moves) >= 3:
            raise Stop()
        act = action(tools)
        moves.append(act.get("tool", "?"))
        return act

    ag._action = counted
    ag._answer = stop("answer")
    ag._edit = ag._create = stop("edit")
    ag._think = lambda *a, **k: ""
    try:
        ag.turn(text, wants_change=wants_edit(text))  # as the chat does
    except Stop:
        pass
    return moves


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-hint", action="store_true", help="without the note added when a message reads like it needs a page")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--reasoning", action="store_true", help="the server's model reasons (/v1/info says so)")
    ap.add_argument("--context", type=int, default=32768)
    a = ap.parse_args()
    if not os.environ.get("OPENAI_API_BASE"):
        sys.exit("set OPENAI_API_BASE and OPENAI_API_KEY (from `oppx proxy --no-web`)")
    if a.no_hint:
        A.browser_hint = lambda text: ""
    repo = make_repo()
    right = total = 0
    misses = []
    began = time.time()
    for _ in range(a.runs):
        for text, want in CASES:
            moves = first_moves(repo, text, a.reasoning, a.context)
            chose = "browser" in moves
            ok = chose == want
            right += ok
            total += 1
            print(f"{'ok  ' if ok else 'MISS'} {'+' if want else '-'} {' > '.join(moves):40.40}  {text[:70]}", flush=True)
            if not ok:
                misses.append(text)
    pos = [c for c in CASES if c[1]]
    print(f"\n{right}/{total} right ({'no hint' if a.no_hint else 'with hint'}), {time.time() - began:.0f} s; "
          f"{len(pos) * a.runs} should choose the browser, {(len(CASES) - len(pos)) * a.runs} shouldn't")
    return 0 if right == total else 1


if __name__ == "__main__":
    sys.exit(main())
