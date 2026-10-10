# Contributing to OpenPhalanx

Architecture and design notes are in [`CLAUDE.md`](CLAUDE.md). This page covers what a change needs to be accepted and how to test it locally.

## Rules

* **Clean and concise code.** Match the code around you. Prefer the simplest version that works.
* **Docstrings on every public function.** Say what it does and anything a caller must know.
* **Comments are two lines at most.** Explain why, not what. If it needs more, the code should be simpler.
* **Unit tests are required.** New behaviour and bug fixes come with a test.
* **Small pull requests.** Aim for at most **300 changed lines** and **6 files**. Break larger work into stacked pull requests, each one reviewable alone.
* **Every pull request is reviewed by a code owner** before it merges.
* **Model-agnostic.** Use constrained decoding and catalog data, not parsers or prompts tuned to one model.

## Pull requests

Branch from `dev`. A pull request into `main` is a release, and its title prefix sets the version: `[fix]` or `[bug]` patch, `[feature]` minor, `[major]` major. `[docs]`, `[ci]`, `[chore]`, `[test]` and `[refactor]` don't release.

## Checks

Run these before every commit. CI runs the same ones.

```bash
cargo test -p openphalanx-core && cargo test -p oppx
cargo clippy --workspace --all-targets -- -D warnings
(cd app && npm install && npm run check)
```

## Test the server

You need Linux with an NVIDIA GPU, Docker and the NVIDIA Container Toolkit. Build requirements are in `CLAUDE.md`.

```bash
cd app && npm install && npx tauri dev               # the app, with hot reload
cargo run -p openphalanx-core --example lifecycle    # start → pair → requests → revoke, without the app
```

The first start builds the backend image (a 16 GB download, once). Changes to the backend (`docker/`, starting, stopping, pairing) need the lifecycle run.

## Test the client

The client needs a running server; the one on your own machine is fine.

```bash
cargo build -p oppx
export OPPX_CONFIG=/tmp/opx-test/oppx.json           # a scratch pairing, away from your real one
target/debug/oppx pair self                          # pairs with the server on this machine
target/debug/oppx status
```

Then chat in a throwaway copy of a repository, because edit requests change files:

```bash
git clone --local . /tmp/opx-test/repo && cd /tmp/opx-test/repo
/path/to/target/debug/oppx
```

For changes to the agent, prompts or routing, compare before and after with `scripts/eval_edits.py --oppx target/debug/oppx`.
