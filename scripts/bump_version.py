#!/usr/bin/env python3
"""Bumps the project version from a PR title, in every file that carries it.

The PR title prefix decides the bump (case-insensitive):

    [bug] / [fix]          patch   0.2.0 -> 0.2.1
    [feature] / [feat]     minor   0.2.1 -> 0.3.0
    [major] / [breaking]   major   0.3.0 -> 1.0.0

Titles without a prefix (e.g. [docs], [ci], [chore]) don't release.

    scripts/bump_version.py --title "[bug] Fix pairing timeout"   # bumps, prints the new version
    scripts/bump_version.py --title "..." --check                 # validate only (CI on every PR)
    scripts/bump_version.py --current                             # print the current version
    scripts/bump_version.py --title "..." --pr 42                 # also adds a CHANGELOG.md entry

Files: Cargo.toml (workspace version, which is also the backend image tag),
Cargo.lock (workspace crates), app/package.json, app/package-lock.json and
app/src-tauri/tauri.conf.json. CLAUDE.md requires them to move together.
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIXES = {
    "bug": "patch", "fix": "patch",
    "feature": "minor", "feat": "minor",
    "major": "major", "breaking": "major",
}
NO_RELEASE = {"docs", "doc", "ci", "chore", "test", "refactor"}
WORKSPACE_CRATES = ("openphalanx-core", "openphalanx", "oppx")
TITLE = re.compile(r"^\s*\[(?P<kind>[A-Za-z]+)\]\s*(?P<rest>.+)$")


def parse_title(title: str):
    """Returns (bump or None, summary). Raises ValueError for a bad title."""
    m = TITLE.match(title)
    if not m:
        raise ValueError(
            f"PR title {title!r} needs a prefix: [bug], [feature] or [major] to release, "
            f"or one of {', '.join(f'[{k}]' for k in sorted(NO_RELEASE))} for no release"
        )
    kind = m["kind"].lower()
    if kind in PREFIXES:
        return PREFIXES[kind], m["rest"].strip()
    if kind in NO_RELEASE:
        return None, m["rest"].strip()
    raise ValueError(f"unknown prefix [{m['kind']}]; use [bug], [feature], [major], or {sorted(NO_RELEASE)}")


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def current() -> str:
    text = (ROOT / "Cargo.toml").read_text()
    m = re.search(r'(?ms)^\[workspace\.package\].*?^version\s*=\s*"([^"]+)"', text)
    if not m:
        sys.exit("no [workspace.package] version in Cargo.toml")
    return m.group(1)


def write(old: str, new: str):
    cargo = ROOT / "Cargo.toml"
    text = cargo.read_text()
    cargo.write_text(re.sub(r'(?ms)(^\[workspace\.package\].*?^version\s*=\s*")[^"]+(")', rf"\g<1>{new}\g<2>", text, count=1))

    lock = ROOT / "Cargo.lock"
    if lock.exists():
        text = lock.read_text()
        for name in WORKSPACE_CRATES:
            text = text.replace(f'name = "{name}"\nversion = "{old}"', f'name = "{name}"\nversion = "{new}"')
        lock.write_text(text)

    # JSON files: replace the top-level version (and package-lock's root
    # package) in place, keeping the files' hand-written formatting.
    for rel, count in (("app/package.json", 1), ("app/src-tauri/tauri.conf.json", 1), ("app/package-lock.json", 2)):
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text()
        text = re.sub(rf'("version":\s*"){re.escape(old)}(")', rf"\g<1>{new}\g<2>", text, count=count)
        path.write_text(text)
        if json.loads(text).get("version") != new:
            sys.exit(f"could not update the version in {rel}")


SECTION = {"patch": "Fixes", "minor": "Features", "major": "Breaking changes"}


def changelog(version: str, part: str, summary: str, pr: str | None):
    """Prepends this release to CHANGELOG.md (newest first)."""
    path = ROOT / "CHANGELOG.md"
    head = "# Changelog\n\nGenerated from merged pull request titles by `scripts/bump_version.py`.\n"
    old = path.read_text() if path.exists() else head
    body = old[len(head):] if old.startswith(head) else "\n" + old
    ref = f" (#{pr})" if pr else ""
    entry = f"\n## v{version} ({datetime.date.today().isoformat()})\n\n### {SECTION[part]}\n\n- {summary}{ref}\n"
    path.write_text(head + entry + body)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--title", help="the PR title")
    p.add_argument("--check", action="store_true", help="only validate the title")
    p.add_argument("--current", action="store_true", help="print the current version")
    p.add_argument("--pr", help="PR number, for the CHANGELOG.md entry")
    p.add_argument("--no-changelog", action="store_true", help="don't touch CHANGELOG.md")
    args = p.parse_args()
    if args.current:
        print(current())
        return
    if not args.title:
        p.error("--title is required")
    try:
        part, summary = parse_title(args.title)
    except ValueError as e:
        sys.exit(str(e))
    if args.check:
        print(f"ok: {part or 'no release'}")
        return
    if part is None:
        print("")  # nothing to release
        return
    old = current()
    new = bump(old, part)
    write(old, new)
    if not args.no_changelog:
        changelog(new, part, summary, args.pr)
    print(new)


if __name__ == "__main__":
    main()
