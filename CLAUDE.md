# CLAUDE.md

Working notes for developing this repo. This file holds the conventions, the architecture in brief, the commands and the layout; each topic's detail (how it works, what was measured and verified) is in its own file under `.claude/skills/`, listed in **Topics** below. End-user docs are in `README.md`; the release history is in `CHANGELOG.md`.

## Conventions

* **Docs:**
  * `README.md` is for end users only: what the project is, plus the minimum commands to run the server and a client. Keep it at 200 lines at most.
  * Everything operational or developer-facing goes in the matching topic file under `.claude/skills/<topic>/SKILL.md`, including what was measured and verified. This file stays an index: add here only what applies to every change. A new topic gets its own folder, a `name` and a `description` saying when to read it, and a row in **Topics**.
* **Agents never commit directly:** any agent launcher or config (Aider, `oppx aider`, etc.) must use `--no-auto-commits --no-dirty-commits`. The user reviews with `git diff` and commits themselves.
* **Commits:** never commit or push unless the user explicitly asks for that commit. Finish with an uncommitted diff and a summary.
* **Model-agnostic:** everything that makes the experience seamless must work with any model SGLang serves. Use constrained decoding (regex or JSON schema) rather than per-model tool-call or reasoning parsers, put unavoidable per-model differences in catalog data, and check accuracy with probe sets against the loaded model rather than tuning prompts to one model.
* **Branching and releases:** work happens on `dev`. A PR from `dev` into `main` is a release: its title prefix sets the version bump, in any letter case (`[bug]`/`[fix]` patch, `[feature]` minor, `[major]` major; `[docs]`, `[ci]`, `[chore]`, `[test]`, `[refactor]` don't release). See "Releasing" below.
* **Toolchain on the server node:** `cargo` is in `~/.cargo/bin` (not on the non-interactive `PATH`), so use `export PATH=$HOME/.cargo/bin:$PATH`. `uv`/`uvx` are in `~/.local/bin`.
* **Versioning:** the backend image tag equals the workspace version in `Cargo.toml`. The release workflow bumps `Cargo.toml`, `Cargo.lock`, `app/package.json`, `app/package-lock.json` and `app/src-tauri/tauri.conf.json` together (`scripts/bump_version.py`); don't bump by hand.
* **Regular expressions:** every pattern in the repository must pass `scripts/check_regexes.py` (CI runs it). It finds the patterns by itself (Python's syntax tree, the TypeScript parser), compiles each, and proves with recheck that none backtracks exponentially or polynomially. Python's `re` and JavaScript backtrack, so a harmless-looking `\s+$` searched over a long run of spaces is quadratic. When it flags one, pin where a match may start (`(?<![ \t])[ \t]+\n`), keep a repeat from crossing its own delimiter (`[^{}]*`, `_([^_]+?)_`), or use string methods. A pattern only known at run time gets a `# regex: runtime (why it is safe)` comment; a pattern the model writes runs through `grep -E`, never Python's `re`.
* **File size:** no source file (code or tests) over 1,000 lines. When a change would push a file past that, split it first into modules by responsibility (e.g. the agent loop, its guards, its prompts, the edit/create text), keeping the public names importable from where they were. A file already over the limit is split before it grows further.
* **Verify before claiming:** run `cargo test -p openphalanx-core`, `cargo clippy --workspace --all-targets`, `(cd app && npm run check)`, and for backend changes the lifecycle example (about 4 min, needs the GPU).

## Architecture

```
flowchart TB
    subgraph ClientNode ["Client Node (Work Laptop)"]
        direction TB
        CLI["oppx CLI<br/>(Rust: pairing, pinned-TLS proxy)"]
        Agent["Coding agent<br/>(Aider, runs locally)"]
        FS[("Local Workspace<br/>git, tests, toolchain")]

        CLI -->|"Launches, injects endpoint"| Agent
        Agent <-->|"Reads repo, edits files, runs tests"| FS
        Agent <-->|"OpenAI-compatible HTTP<br/>(loopback)"| CLI
    end

    subgraph ServerNode ["Server Node (Primary Rig)"]
        direction TB
        GUI["Openphalanx GUI<br/>(Tauri / Rust Orchestrator)"]
        Gateway["Agent Gateway<br/>(TLS, device tokens)"]
        Master["SGLang Server<br/>(Inference Master)"]

        GUI -.->|"Orchestrates, pairs devices"| Gateway
        GUI -.->|"Orchestrates"| Master
        Gateway <-->|"Local HTTP API"| Master
    end

    CLI <==>|"HTTPS + device token<br/>(prompts only; no code stored)"| Gateway

    classDef clientStyle fill:#0f172a,stroke:#38bdf8,stroke-width:2px,color:#fff;
    classDef serverStyle fill:#1e1b4b,stroke:#818cf8,stroke-width:2px,color:#fff;
    classDef modelStyle fill:#422006,stroke:#fb923c,stroke-width:2px,color:#fff;

    class CLI,Agent,FS clientStyle;
    class GUI,Gateway serverStyle;
    class Master modelStyle;
```

Openphalanx splits the work along a single line: **code and execution stay on the client, GPUs stay on the server.**

* **Client:** the coding agent (Aider) runs on your laptop next to your repo, so it sees the whole codebase and runs your own tests, linters and git. The `oppx` CLI pairs the laptop with a server, then launches the agent pointed at a local proxy. The proxy pins the server's TLS certificate and adds the device token, so the agent never handles either.
* **Server:** stores no code and executes nothing on a client's behalf. It serves SGLang behind an authenticated gateway, and the Tauri GUI manages the container, VRAM safety, models and paired devices. SGLang's RadixAttention caches repeated prompt prefixes, so multi-turn agent sessions skip most of the prompt processing.

## Status

| Component | State |
|---|---|
| Backend image (SGLang + gateway) | Working, including the authenticated OpenAI-compatible inference API. `0.3.0` is built locally but not yet pushed to GHCR |
| Server GUI (`app/`, Linux) | Builds and runs; first UI review pending |
| Pairing, TLS and device tokens | Working |
| `oppx` client CLI | Working: `pair` (certificate pinning), `status`, `unpair`, `aider` (local Aider through a pinned loopback proxy), `proxy`, `servers`, `use`, `mcp` (connectors: MCP servers as tools for the chat) |
| Distribution (CI, releases, installers, docs) | v0.3.0 released through GitHub Actions; backend image still published by hand |

## Requirements

**Server node**

* Linux x86-64 (the GUI is Linux-only for now).
* NVIDIA GPU with a recent driver (`nvidia-smi` must work). A 24 GB card runs the default Qwen2.5-Coder-14B-AWQ with a 32k context.
* Docker Engine, with your user in the `docker` group.
* [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), registered with Docker (`sudo nvidia-ctk runtime configure --runtime=docker`).
* Disk: about 55 GB for the backend image (16 GB download), plus the model weights (5–80 GB depending on the model).

**Building from source** additionally needs Rust (stable, via rustup), Node.js 20+ with npm, and the WebKitGTK development packages:

```bash
sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev \
  libayatana-appindicator3-dev librsvg2-dev libxdo-dev build-essential
```

## Topics

Read the topic's file before working on that part; add what you measure or verify to it. In Claude Code these are project skills, so each loads by its name when a task matches its description.

| Topic | File | What it covers |
|---|---|---|
| server-gui | [`.claude/skills/server-gui/SKILL.md`](.claude/skills/server-gui/SKILL.md) | The desktop app: Models page, starting a model, Fits / Tight / Won't fit, notifications, Force load, closing and the watchdog, in-app updates |
| trace | [`.claude/skills/trace/SKILL.md`](.claude/skills/trace/SKILL.md) | The Trace page and the gateway's request trace |
| server-settings | [`.claude/skills/server-settings/SKILL.md`](.claude/skills/server-settings/SKILL.md) | The Settings page: several GPUs in one machine (experimental), ports, hiding IP addresses, storage, how settings load |
| headless-backend | [`.claude/skills/headless-backend/SKILL.md`](.claude/skills/headless-backend/SKILL.md) | Running the backend container by hand, and `oppxs` (the server without the desktop app) |
| oppx-client | [`.claude/skills/oppx-client/SKILL.md`](.claude/skills/oppx-client/SKILL.md) | The `oppx` command line: commands, `pair self`, config, terminal UI, the private coding engine, `--update` |
| oppx-chat-frontend | [`.claude/skills/oppx-chat-frontend/SKILL.md`](.claude/skills/oppx-chat-frontend/SKILL.md) | The chat frontend: modules, the two engines, Aider hooks, routing, the Aider-engine context manager, sessions, prefix cache, pty testing |
| oppx-agent | [`.claude/skills/oppx-agent/SKILL.md`](.claude/skills/oppx-agent/SKILL.md) | The agent engine: actions, repeat and stuck handling, speed, truthful replies, guards for small models, context |
| documents | [`.claude/skills/documents/SKILL.md`](.claude/skills/documents/SKILL.md) | Documents and pictures: the formats the agent reads and makes, vision, the engine's extra packages, what was measured |
| oppx-permissions | [`.claude/skills/oppx-permissions/SKILL.md`](.claude/skills/oppx-permissions/SKILL.md) | What the agent runs without asking, saved and session rules, the question's layout, audit findings |
| agent-app-eval | [`.claude/skills/agent-app-eval/SKILL.md`](.claude/skills/agent-app-eval/SKILL.md) | Building an app end to end (CHO-8): what broke, the fixes, `scripts/eval_app.py` and its numbers |
| connectors-and-browser | [`.claude/skills/connectors-and-browser/SKILL.md`](.claude/skills/connectors-and-browser/SKILL.md) | MCP connectors and the headless browser (Playwright) |
| cluster | [`.claude/skills/cluster/SKILL.md`](.claude/skills/cluster/SKILL.md) | Discovery, roles, joining, strategies, split serving across machines, model sync, the start guard |
| web-search | [`.claude/skills/web-search/SKILL.md`](.claude/skills/web-search/SKILL.md) | SearXNG, the gateway's search router, the search guard (cache, pacing, pause) |
| security-and-ports | [`.claude/skills/security-and-ports/SKILL.md`](.claude/skills/security-and-ports/SKILL.md) | Every port and state file, and the security model |
| model-catalog | [`.claude/skills/model-catalog/SKILL.md`](.claude/skills/model-catalog/SKILL.md) | The model catalog, the VRAM estimate, KV cache sizing, YaRN, what was validated on hardware |
| benchmark | [`.claude/skills/benchmark/SKILL.md`](.claude/skills/benchmark/SKILL.md) | `scripts/bench_speed.py` and `BENCHMARK.md`: how the numbers are made on each machine, the test rig, pitfalls and open findings |
| releasing | [`.claude/skills/releasing/SKILL.md`](.claude/skills/releasing/SKILL.md) | CI, the release and publish workflows, repository settings |

## Development

```bash
cargo test -p openphalanx-core                                  # unit tests (no GPU or Docker needed)
cargo test -p oppx                                          # client unit tests
cargo run -p oppx -- pair <server> <code>                   # pair (also: status, unpair, servers, use; config via OPPX_CONFIG or --config)
# note: `cargo test`/`clippy` don't rebuild target/debug/oppx; run `cargo build -p oppx` before testing the binary
cargo clippy --workspace --all-targets
(cd app && npm run check)                                       # Svelte/TypeScript type check

cargo run -p openphalanx-core --example preflight               # pre-flight report for this machine
cargo run -p openphalanx-core --example catalog -- 32768       # catalog VRAM estimates for this GPU
cargo run -p openphalanx-core --example lifecycle               # full start → pair → token check → revoke run
cargo run -p openphalanx-core --example download -- <repo> <dir>  # verified, resumable HF download

docker build -f docker/Dockerfile.server -t openphalanx-backend:dev docker/   # the backend image by hand (the app builds its own; see below)
scripts/bench_session.py --oppx target/debug/oppx               # prefix-cache benchmark (in a scratch copy of a repo)
scripts/bench_speed.py --label 3090 --models <id,id>            # speed per model for BENCHMARK.md (app closed); --summary <results.jsonl…> for the tables
scripts/gen_docs.py --oppx target/debug/oppx --out target/docs  # docs sources; then: mdbook build target/docs
OPENAI_API_BASE=… OPENAI_API_KEY=… scripts/probe_routing.py     # via `oppx proxy --no-web`; run after any model change
OPENAI_API_BASE=… OPENAI_API_KEY=… scripts/probe_kv.py          # long-context recall + coding tasks run against tests (e.g. KV dtype checks)
scripts/eval_app.py --oppx target/debug/oppx [--task build,improve] [--runs 5]   # build and improve a small web app, scored per requirement in a browser; --agent claude for the baseline
python3 scripts/test_searchguard.py                             # the gateway's search guard (cache, pacing, pause when the engines refuse)
npm ci --prefix scripts/regexcheck && python3 scripts/check_regexes.py   # every regex compiled and checked for catastrophic backtracking (--list shows them)
python3 scripts/test_inputs.py                                  # the gateway's check of what the loaded model takes (pictures, audio, video)
scripts/eval_edits.py --oppx target/debug/oppx --engine agent   # 9 edit tasks in fresh clones (3 name no file), checked by compiling/testing; compare engines
~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/eval_nav.py --repo <scratch clone>     # 10 questions that name no file (via `oppx proxy`): answered, steps, stuck
~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/test_agent.py                          # agent loop unit tests, scripted model, no server
~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/test_agent_tools.py                    # ... its tools, edit text, commands (and test_agent_documents.py: documents, pictures)
python3 scripts/test_permissions.py                             # the agent's command allowlist (read-only commands, saved rules, what is never remembered)
python3 scripts/test_reqtrace.py                                # the gateway's request trace (prefix chains, tree, labels, content switch)
~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/bench_agent.py --repo <scratch clone>   # agent turns headless, steps and tokens
~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/test_connectors.py   # connectors: MCP client + the agent's tool calls (no server needed)
```

The backend image tag follows the version in the root `Cargo.toml`, so bump both together when changing `docker/`. The base SGLang image is pinned by digest in `docker/Dockerfile.server`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No permission to use Docker" | `sudo usermod -aG docker $USER`, then log out and back in |
| "Docker has no nvidia runtime" | Install NVIDIA Container Toolkit, then `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| "Port 9090 is already in use" | Another program, or an old backend container, holds the port: `docker ps`, then `docker rm -f openphalanx-backend` |
| Start disabled with "Needs X but only Y of VRAM is free" | Close other GPU programs (`nvidia-smi` lists them), or choose a smaller model or context |
| Backend image build fails | The first start pulls `lmsysorg/sglang` from Docker Hub: check the network and disk space (about 55 GB free). Docker Hub allows 100 anonymous pulls per 6 hours per IP; `docker login` raises that. The full build output is in the app's start error and `docker build` can be rerun by hand (see "Run the backend without the GUI") |
| `oppx aider` crashes with `No module named 'audioop'` / `'pyaudioop'` | An `aider` on `PATH` was installed with Python 3.13, which removed `audioop`. `oppx` only uses a `PATH` Aider at the pinned version; otherwise it uses its private 3.12 engine. Run `oppx --update`, or remove the old Aider |
| The server stopped after the app crashed or was killed | Intended: the backend's watchdog stops it 60 s after the app goes silent, so it can't hold the GPU. Open the app and start it again |
| A server isn't listed on the other machine, or the cluster panel says "isn't visible to other servers yet" | Another server process on that machine holds ports 9092–9094 (the app and `oppxs` are the same server; a leftover `oppxs` when the app opens, or the reverse). `oppxs free-ports` lists the holders and stops the OpenPhalanx ones after asking (`--yes` to skip); in the app, **Free the ports…** next to the message does the same. The app then starts its cluster service by itself within a few seconds |
| Backend stops during start-up | The GUI shows the reason; the full output is on **Logs** or in `docker logs openphalanx-backend` |

## Repository layout

```
.claude/skills/          the developer notes by topic (one SKILL.md each; index in this file under Topics)
app/                     Tauri 2 + Svelte 5 server GUI (Linux); src-tauri/icons/ from icon.svg (assets/openphalanx-shield-green.svg,
                         on a square canvas centred on the shield, not the arrow): npx tauri icon src-tauri/icons/icon.svg -p 32,64,128,256,512, then rename
                         256 to 128x128@2x.png and 512 to icon.png. On Wayland the dock icon never comes from the binary
                         (GTK3 sends only the app id, `openphalanx`): it's the icon of the installed .desktop entry, so
                         `tauri dev` shows the installed package's icon
  src/                   frontend: pages, components, typed command bindings
  src-tauri/             Rust shell: Tauri commands, 2 s status monitor, log streaming
crates/openphalanx-core/ Docker, GPU, VRAM, model catalog, downloads, pre-flight, cluster, split plan, app updates (no Tauri, unit-tested)
crates/openphalanx-server/ oppxs: the server without the desktop app (daemon, control API and its commands)
packaging/oppxs.service  systemd user unit for oppxs (in the .deb; install-server.sh --headless writes its own)
crates/pinned-tls/       TLS pinned to a certificate fingerprint (shared by oppx and the cluster)
  catalog.json           curated models pinned to Hugging Face commits (built with scripts/catalog_entry.py); optional per-model
                         edit_format and reasoning_parser (passed as EDIT_FORMAT, --reasoning-parser)
docker/                  backend image: SGLang + gateway under supervisord (no agent code)
  server/gateway.py      TLS gateway: pairing, device tokens, admin API
  server/reqtrace.py     the request trace behind the app's Trace page (in memory, no dependencies)
  server/inputs.py       what the loaded model takes (from SGLang at load), and the refusal for anything else
  server/searchguard.py  cache, pacing and pause for web search, so the public engines don't block the server
client/oppx/             client CLI: config.rs (paired servers, 0600 file), tls.rs (fingerprint pinning),
                         api.rs (gateway calls), engine.rs (private Aider), mcp.rs (connector files), update.rs, main.rs (clap commands)
  frontend/              chat frontend: oppx_chat/ package + run.py (embedded in the binary)
                         (oppx_chat/documents.py reads PDF, Office, EPUB and pictures; writers.py makes them)
.github/workflows/       ci.yml (every PR/push), release.yml (merged PR into main -> release)
scripts/test_connectors.py connectors (MCP): the client against a stdio server, and the agent calling a tool (scripted model)
scripts/eval_app.py      the CHO-8 app task end to end, scored per requirement (eval_app_check.mjs: the browser checks)
scripts/probe_browser.py does the loaded model ask for the browser when a message needs a running page, and only then
scripts/probe_routing.py score the search router and ask/edit check against the loaded model
scripts/check_regexes.py every regex in the repository, compiled and proven free of catastrophic backtracking (regexcheck/: recheck, pinned)
scripts/catalog_entry.py catalog entries from Hugging Face (pinned commit, weight size, attention shape); --check re-verifies
scripts/bench_session.py 10-turn prefix-cache benchmark through a real oppx session
scripts/bench_speed.py   speed per model through oppxs (one user per prompt length, several users), for BENCHMARK.md
benchmarks/results/      the raw results behind BENCHMARK.md (one JSON line per measurement, per machine and date)
scripts/bump_version.py  version bump from a PR title, plus the CHANGELOG.md entry
scripts/gen_docs.py      docs site sources (README, CLI and API reference, CLAUDE.md and its topic files, changelog)
scripts/install*.sh      client and server installers (attached to each release)
```

## Design decision: agent on the client

We compared three placements of the agent:

* **Agent on the server, sent files per task:** this was our first prototype (a `/v1/run` endpoint wrapping Aider in the image, since removed).
* **Everything on the server**, used over SSH.
* **Agent on the client, server as inference only.**

We chose the last. It keeps the codebase as the single source of truth on the client. It needs no shell access or workspaces on the server, which fits the pairing and device-token security model. It gives the agent full repo context and a local edit → test → fix loop. And it leaves VRAM pooling across machines as a server-side concern that clients never see.

The network cost of running the agent on the client is negligible. Measured on the RTX 3090 node with Qwen2.5-Coder-14B-AWQ, for an Aider-sized request of 7.2k prompt tokens and a 300-token answer:

| | Time |
|---|---|
| Prompt processing, first time / cached prefix (RadixAttention) | 3.7 s / **0.2 s** |
| Generation | 71 tokens/s |
| Total per request, cold / cached | 7.9 s / 4.5 s |
| Network added (31 KiB up, 78 KiB streamed down, ~3 ms LAN round trip) | **~5–15 ms (0.1–0.4%)** |

Speed is decided by the GPU and, above all, by prefix-cache hits, which behave the same wherever the agent runs.
