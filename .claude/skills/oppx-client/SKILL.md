---
name: oppx-client
description: "The oppx command line (client/oppx/src): commands, pair self, config file, agent isolation, the terminal UI, the private coding engine and --update. Read before changing main.rs, ui.rs, engine.rs, update.rs or api.rs."
---

# oppx client CLI: pairing, config, terminal UI, engine, --update

Part of the developer notes; the index is in `CLAUDE.md`.

## Client (`oppx`)

```bash
cargo install --path client/oppx              # or: cargo build -p oppx (binary in target/debug/oppx)
oppx pair <server> <code> [--fingerprint FP | --yes] [--as NAME] [--device-name NAME] [--force]
oppx pair self                                # the server on this machine: no code or fingerprint to copy
oppx status [NAME]                            # certificate pinned ✓, token valid ✓, model ready + context
oppx [PROMPT] [--no-web] [--classic]          # chat frontend (--classic: Aider's UI); unpaired: banner + how to pair
oppx -c | -r [ID] | -p PROMPT                 # continue latest session · resume (picker or id) · print mode (one answer)
oppx --update                                 # update oppx (release download, or source pull + rebuild) and its engine
oppx aider [--server NAME] [--no-web] [-- aider args]   # local Aider via pinned loopback proxy; never commits
oppx proxy [NAME] [--port N] [--no-web]       # proxy only; prints OPENAI_API_BASE and a per-run local key
oppx search "query" [-n N]                    # web search via the server's SearXNG (Markdown; use /run in Aider)
oppx mcp add NAME [-e KEY=VALUE] -- COMMAND [ARGS]    # add a connector (MCP server) that runs on this machine
oppx mcp add NAME --url URL [-H 'Name: value']        # ... or one reached over HTTP
oppx mcp [list] | oppx mcp remove NAME        # what's added (yours and this repo's) · drop one
oppx browser [on | off | install]             # the chat's headless browser: setting and install (status without an argument)
oppx unpair [NAME] [--local-only]             # self-revoke on the server, then forget locally
oppx servers | oppx use NAME
```

* **`oppx pair self`** (`local_pairing`): reads the backend container's admin token with `docker inspect`, asks the loopback admin API for a pairing code and the certificate fingerprint, then runs the normal pairing against `127.0.0.1` with that fingerprint pinned; saved as `local`. It needs Docker access, which already lets a user read that token by hand, so it grants nothing new; without it the command says to pair with a code. The code it requests replaces the one the app is showing. Tested against the gateway alone.
* **Config:** `~/.config/oppx/config.json` (`0600`), or `OPPX_CONFIG` / `--config`.
* **Agent isolation:** the agent sees only the per-run local key; the device token stays in `oppx`.
* **Testing without a GUI:** pairing codes can be issued through the admin API: `curl -X POST -H "x-admin-token: $ADMIN_TOKEN" http://127.0.0.1:9091/admin/pairing`. The token is in the container env: `docker inspect openphalanx-backend --format '{{json .Config.Env}}'`.
* **Terminal UI** (`client/oppx/src/ui.rs`, `console` + `indicatif`):
  * There's an OPENPHALANX banner: large 5-row block letters at 95 columns or more, a compact 2-row version below that, with a green-to-blue gradient. It also has spinners on stderr, rounded panels, and ✓ / ! / ✗ rows.
  * Output is plain when stdout or stderr isn't a terminal, or when `NO_COLOR` is set. This matters because `/run oppx search` output goes into Aider's chat. Keep it that way.
  * To preview in a pty, use `script -qec "stty rows 40 cols 120; oppx status" /dev/null`. The rows matter: a pty with 0 rows reports no size, so the compact banner is used.
* **Startup checks run together** (`preflight_inner`): health, whoami and `/v1/info` go out at once, one round trip of waiting instead of three over a WAN. They are then checked in the old order, so the errors read the same. A certificate that doesn't match the pin fails the TLS handshake before any request is written, so nothing, the token included, is sent to it either way. The connect line ends with "sees pictures" or "text only".
* **Coding engine** (`client/oppx/src/engine.rs`): users never install Aider.
  * `oppx` keeps a private engine in `~/.local/share/oppx/engine/` (`OPPX_ENGINE_DIR` overrides): `bin/uv`, a uv-managed Python 3.12 (`UV_PYTHON_PREFERENCE=only-managed`, so a system 3.13 can't break it), and `aider-chat==agent::AIDER_VERSION` installed with `uv tool install` into `tools/`, with `engine::EXTRAS` (pypdf, fpdf2, resvg-py; see the `documents` topic) next to it. A private copy missing an extra counts as not current and is installed again. About 740 MB.
  * **Lookup order:** the private engine at the pinned version, then an `aider` on `PATH` at exactly the pinned version (handy on dev machines), otherwise install the private one (`engine::ensure`). Versions are read from the venv's `aider_chat-X.Y.Z.dist-info` folder, not by importing Aider (2 s).
  * uv is reused when present (the private copy, `PATH`, `~/.local/bin`, `~/.cargo/bin`); otherwise Astral's installer runs with `UV_UNMANAGED_INSTALL` into `engine/bin`, with no shell-profile changes.
  * The frontend runs on the venv's `python` next to the real launcher. uv writes a `#!/bin/sh` trampoline instead of a Python shebang when the path is long, so the shebang alone isn't reliable.
  * To test a first install, run with `HOME` pointing to an empty folder and `PATH=/usr/bin:/bin` (verified: about 7 s on this network, uv download included).
* **`--update`** (`client/oppx/src/update.rs`) has two modes:
  * **Release builds** (`OPPX_RELEASE_TARGET` set at build time by the release workflow): read GitHub's latest release, download `oppx-<target>.tar.gz`, check it against `SHA256SUMS`, unpack next to the current binary and rename over it. A 404 means no release yet.
  * **Source builds:** work on the checkout `oppx` was built from (`CARGO_MANIFEST_DIR/../..`), and refuse if it has local changes. Fetch and fast-forward, then rebuild with `cargo install --locked --path client/oppx` when anything was pulled, or when the commit embedded at build time (`build.rs`, shown in `oppx --version`) differs from HEAD. To test in isolation, use a scratch clone with a local bare remote and `CARGO_INSTALL_ROOT` set to a scratch folder.
  * **The binary that runs is the one updated.** `cargo install` writes to cargo's bin folder; when the running `oppx` is a copy elsewhere (e.g. `~/.local/bin`, first on `PATH`), a source update used to say "installed" while `oppx --version` stayed at the old version. It now copies the new build over the running file (`replace_running`: written beside it, renamed over it) or fails saying where the new one is, and the last line gives the version read from the installed binary. Run as in the recipe above with the copy in another folder: 2d07f09 → c1d49fc in both places. A release binary updating itself was run too (the real 0.5.1 release → 0.6.1).
  * Both modes then install or refresh the private engine.
* **Stand-in agents:** a fake `aider` script on `PATH` (printing its args and env) is a quick way to test `oppx aider` without the real agent.
