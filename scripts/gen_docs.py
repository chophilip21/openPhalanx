#!/usr/bin/env python3
"""Generates the documentation site's sources from the repository.

Run on every release (the release workflow), so the published docs always
match the released code:

    scripts/gen_docs.py --oppx target/release/oppx --out target/docs
    mdbook build target/docs        # HTML in target/docs/book

Pages:
* Introduction: README.md (the end-user page).
* Client CLI reference: `oppx --help` and every subcommand's help, from the
  binary itself, so it can't drift from the code.
* Gateway API reference: every route in docker/server/gateway.py with its
  method, authentication and docstring (read with `ast`; no imports needed).
* Operations and development: CLAUDE.md.
* Changelog: CHANGELOG.md (written by scripts/bump_version.py).
"""

import argparse
import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cli_reference(oppx: str) -> str:
    def help_of(*args: str) -> str:
        r = subprocess.run([oppx, *args, "--help"], capture_output=True, text=True, env={"NO_COLOR": "1", "PATH": ""})
        return r.stdout.strip()

    main = help_of()
    version = subprocess.run([oppx, "--version"], capture_output=True, text=True).stdout.strip()
    out = ["# Client CLI reference", "", f"Generated from `{version}`.", "", "```text", main, "```", ""]
    # Subcommands are listed under "Commands:" as "  name  description".
    section = main.split("Commands:", 1)[1].split("\n\n", 1)[0] if "Commands:" in main else ""
    for line in section.splitlines():
        m = re.match(r"^\s{2}(\S+)\s", line)
        if m and m[1] != "help":
            out += [f"## `oppx {m[1]}`", "", "```text", help_of(m[1]), "```", ""]
    return "\n".join(out)


def api_reference(gateway: Path) -> str:
    tree = ast.parse(gateway.read_text())
    rows = {"app": [], "admin": []}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            target = getattr(dec.func.value, "id", "")
            if target not in rows or not dec.args:
                continue
            method = dec.func.attr.upper()
            path = ast.literal_eval(dec.args[0])
            src = ast.get_source_segment(gateway.read_text(), node) or ""
            if target == "admin":
                auth = "admin token"
            elif "device(" in src or "require_device" in src or "_device_from" in src:
                auth = "device token"
            else:
                auth = "none"
            doc = (ast.get_docstring(node) or "").strip().replace("\n", " ")
            rows[target].append((method, path, auth, doc))
    out = [
        "# Gateway API reference",
        "",
        "Generated from `docker/server/gateway.py`.",
        "",
        "## Public API (port 9090, TLS)",
        "",
        "Clients authenticate with `Authorization: Bearer <device token>`. `oppx` adds it; agents talk to `oppx`'s loopback proxy.",
        "",
        "| Method | Path | Auth | Description |",
        "|---|---|---|---|",
    ]
    out += [f"| {m} | `{p}` | {a} | {d} |" for m, p, a, d in rows["app"]]
    out += ["", "## Admin API (127.0.0.1:9091)", "", "Used by the server app; needs the per-launch `x-admin-token`.", "",
            "| Method | Path | Description |", "|---|---|---|"]
    out += [f"| {m} | `{p}` | {d} |" for m, p, _, d in rows["admin"]]
    return "\n".join(out) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--oppx", default="target/release/oppx", help="the oppx binary to document")
    p.add_argument("--out", default="target/docs", help="mdBook root to write")
    args = p.parse_args()
    out = Path(args.out)
    src = out / "src"
    src.mkdir(parents=True, exist_ok=True)

    def copy(name: str, dest: str, title: str):
        text = (ROOT / name).read_text() if (ROOT / name).exists() else f"# {title}\n\nNothing yet.\n"
        (src / dest).write_text(text)

    copy("README.md", "introduction.md", "Openphalanx")
    (src / "cli.md").write_text(cli_reference(args.oppx))
    (src / "api.md").write_text(api_reference(ROOT / "docker/server/gateway.py"))
    copy("CLAUDE.md", "development.md", "Operations and development")
    copy("CHANGELOG.md", "changelog.md", "Changelog")
    (src / "SUMMARY.md").write_text(
        "# Summary\n\n[Introduction](introduction.md)\n\n"
        "- [Client CLI reference](cli.md)\n- [Gateway API reference](api.md)\n"
        "- [Operations and development](development.md)\n- [Changelog](changelog.md)\n"
    )
    (out / "book.toml").write_text(
        '[book]\ntitle = "Openphalanx"\nauthors = ["Openphalanx contributors"]\nlanguage = "en"\nsrc = "src"\n\n'
        '[output.html]\ngit-repository-url = "https://github.com/chophilip21/openPhalanx"\n'
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
