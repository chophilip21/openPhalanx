# CLAUDE.md

Working notes for developing this repo: architecture, operations, commands and conventions. End-user docs are in `README.md`; the roadmap and step-by-step progress are in `milestone.md`.

## Conventions

* **Docs:**
  * `README.md` is for end users only: what the project is, plus the minimum commands to run the server and a client. Keep it at 50–100 lines.
  * Everything operational or developer-facing goes here.
  * Roadmap status (phases, steps, checkboxes, verification notes) goes in `milestone.md`.
* **Agents never commit directly:** any agent launcher or config (Aider, `oppx aider`, etc.) must use `--no-auto-commits --no-dirty-commits`. The user reviews with `git diff` and commits themselves.
* **Commits:** never commit or push unless the user explicitly asks for that commit. Finish with an uncommitted diff and a summary.
* **Model-agnostic:** everything that makes the experience seamless must work with any model SGLang serves. Use constrained decoding (regex or JSON schema) rather than per-model tool-call or reasoning parsers, put unavoidable per-model differences in catalog data, and check accuracy with probe sets against the loaded model rather than tuning prompts to one model.
* **Branching:** work happens on `dev`; `main` holds the initial history only.
* **Toolchain on the server node:** `cargo` is in `~/.cargo/bin` (not on the non-interactive `PATH`), so use `export PATH=$HOME/.cargo/bin:$PATH`. `uv`/`uvx` are in `~/.local/bin`.
* **Versioning:** the backend image tag equals the workspace version in `Cargo.toml`. Bump `Cargo.toml`, `app/package.json` and `app/src-tauri/tauri.conf.json` together when `docker/` changes.
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
| Backend image (SGLang + gateway) | Working, including the authenticated OpenAI-compatible inference API. `0.2.0` is built locally but not yet pushed to GHCR |
| Server GUI (`app/`, Linux) | Builds and runs; first UI review pending |
| Pairing, TLS and device tokens | Working |
| `oppx` client CLI | Working: `pair` (certificate pinning), `status`, `unpair`, `aider` (local Aider through a pinned loopback proxy), `proxy`, `servers`, `use` |

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

## Using the server GUI

### 1. Get the app

From source, in development mode with hot reload:

```bash
cd app
npm install
npx tauri dev
```

Or build an installable package and install it:

```bash
cd app && npm install && npx tauri build --bundles deb    # or: --bundles appimage
sudo apt install ../target/release/bundle/deb/Openphalanx_0.2.0_amd64.deb
```

### 2. Choose a model

Open **Models**:

* The table shows each model's VRAM need at the selected context length, with a **Fits / Tight / Won't fit** badge measured against your GPU's free memory. The need is deliberately conservative and much larger than the download size. It covers weights as loaded, KV cache for the full context plus 25%, and runtime memory. **Best fit for this GPU** marks the largest model that fits with headroom.
* Click **Download** on a model that fits. The default, Qwen2.5-Coder-14B-Instruct-AWQ, is already selected, and it is reused if it is in `~/.cache/huggingface`.
* To use your own model, enter a local folder (with `config.json` and `.safetensors`) or a Hugging Face URL under **Add your own model**. Its VRAM need is checked before anything downloads.

### 3. Start

On **Server**, check that the pre-flight list is green, then press the power button:

* The first start downloads the backend image (about 16 GB). Loading the model then takes about 3–4 minutes.
* The button turns green when SGLang is ready, and a **pairing code** appears. Use it to pair a client. Codes are single-use and expire after 10 minutes, but paired devices stay paired.
* **Logs** shows live SGLang output. **Devices** lists paired clients and lets you revoke them.

Start is disabled whenever free VRAM is below what the selected model needs. Close other GPU programs, or choose a smaller model or context length.

## Run the backend without the GUI

Useful on a headless machine or while developing the backend. The container serves the model in `MODEL_PATH` (a Hugging Face repo id or a path inside the container):

```bash
export ADMIN_TOKEN=$(openssl rand -hex 32)
mkdir -p ~/.local/share/openphalanx/backend-state

docker run -d --name openphalanx-backend --gpus all --ipc=host \
  -p 9090:9090 -p 127.0.0.1:9091:9091 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v ~/.local/share/openphalanx/backend-state:/state \
  -e ADMIN_TOKEN \
  ghcr.io/chophilip21/openphalanx-backend:0.2.0

# When the admin API answers, issue a pairing code:
curl -s -X POST -H "x-admin-token: $ADMIN_TOKEN" http://127.0.0.1:9091/admin/pairing

# Readiness (sglang turns "ready" after ~4 min):
curl -s -H "x-admin-token: $ADMIN_TOKEN" http://127.0.0.1:9091/admin/status | jq .sglang
```

Optional environment variables: `MODEL_PATH`, `CONTEXT_LENGTH`, `MEM_FRACTION_STATIC` (default `0.85`; the GUI computes it from free VRAM instead), `SGLANG_EXTRA_ARGS`. The GUI shows a container started this way as "running outside the app", and can stop it.

## Client (`oppx`)

```bash
cargo install --path client/oppx              # or: cargo build -p oppx (binary in target/debug/oppx)
oppx pair <server> <code> [--fingerprint FP | --yes] [--as NAME] [--device-name NAME] [--force]
oppx status [NAME]                            # certificate pinned ✓, token valid ✓, model ready + context
oppx [PROMPT] [--no-web] [--classic]          # chat frontend (--classic: Aider's UI); unpaired: banner + how to pair
oppx -c | -r [ID] | -p PROMPT                 # continue latest session · resume (picker or id) · print mode (one answer)
oppx --update                                 # pull the source checkout, rebuild if changed, install pinned engine
oppx aider [--server NAME] [--no-web] [-- aider args]   # local Aider via pinned loopback proxy; never commits
oppx proxy [NAME] [--port N] [--no-web]       # proxy only; prints OPENAI_API_BASE and a per-run local key
oppx search "query" [-n N]                    # web search via the server's SearXNG (Markdown; use /run in Aider)
oppx unpair [NAME] [--local-only]             # self-revoke on the server, then forget locally
oppx servers | oppx use NAME
```

* **Config:** `~/.config/oppx/config.json` (`0600`), or `OPPX_CONFIG` / `--config`.
* **Agent isolation:** the agent sees only the per-run local key; the device token stays in `oppx`.
* **Testing without a GUI:** pairing codes can be issued through the admin API: `curl -X POST -H "x-admin-token: $ADMIN_TOKEN" http://127.0.0.1:9091/admin/pairing`. The token is in the container env: `docker inspect openphalanx-backend --format '{{json .Config.Env}}'`.
* **Terminal UI** (`client/oppx/src/ui.rs`, `console` + `indicatif`):
  * There's an OPENPHALANX banner: large 5-row block letters at 95 columns or more, a compact 2-row version below that, with a green-to-blue gradient. It also has spinners on stderr, rounded panels, and ✓ / ! / ✗ rows.
  * Output is plain when stdout or stderr isn't a terminal, or when `NO_COLOR` is set. This matters because `/run oppx search` output goes into Aider's chat. Keep it that way.
  * To preview in a pty, use `script -qec "stty rows 40 cols 120; oppx status" /dev/null`. The rows matter: a pty with 0 rows reports no size, so the compact banner is used.
* **Chat frontend** (`client/oppx/frontend/oppx_chat.py`, embedded with `include_str!`):
  * **How it runs:** `oppx` writes the file to `~/.cache/oppx/oppx_chat-<version>.py` and runs it with the Python from the `aider` launcher's shebang, so `aider`, `prompt_toolkit` and `rich` are importable.
  * **How it hooks into Aider:** it sets `aider.main.InputOutput` to its own `OppxIO`, and `base_coder.WaitingSpinner` to its `Thinking` spinner. It calls `aider.main.main(argv, return_coder=True)`, then loops over `coder.run_one()` itself, handling `SwitchCoder` the way Aider's main loop does.
  * **What it changes on screen:**
    * It hides Aider's announcements, URLs and `Tokens:` lines.
    * Edit blocks are hidden while they stream, and each turn ends with a Claude-style diff (`⏺ Update(file)` plus colored lines).
    * Edit-retry noise collapses to one line.
    * Routine confirmations are auto-accepted. Commands the model proposes, and going over the context window, are asked.
  * **Aider version:** it relies on Aider internals, so it's tested with **aider-chat 0.86.2** and warns on other versions; installs are pinned to that version. Crashes go to `~/.local/state/oppx/last-crash.txt` with a hint to use `--classic`.
  * **Claude Code parity:**
    * Keys: Ctrl-C clears the line, twice on an empty line exits; Ctrl-D exits; Esc (or Ctrl-C) interrupts the model; Shift+Tab toggles plan mode (`/ask`, no edits); `\`+Enter or Esc-Enter adds a new line; `?` shows shortcuts.
    * Input prefixes: `!cmd` becomes `/run`, `@file` adds the file, `# note` appends to `OPENPHALANX.md`.
    * Commands: `/clear /compact /cost /context /status /model /init /memory /review /export /doctor /vim /resume /exit`, plus Aider's `/add /drop /run /test /lint /web` and our `/search`. Other Claude-only commands report "not available".
  * **Esc interrupt:** `EscWatcher` holds the terminal in cbreak mode during a turn and turns a lone Esc into SIGINT. It pauses whenever a question needs an answer. Keystrokes typed during a turn are kept: finished lines are queued as the next messages, and a partial line is pre-filled.
  * **Interrupts:** Aider's `keyboard_interrupt` (which exits on a double press) is replaced. A mid-stream interrupt is detected from the "I see that you interrupted…" note Aider records; check the coder returned after `SwitchCoder` too.
  * **Ask/edit routing:** each normal message goes through a one-token `ask`/`edit` classification by the server's model through the proxy (about 40 ms, 14/14 on probes). Questions run as `/ask`, so they can't edit. Without it, Qwen-14B in diff mode deleted `mul` when asked "what does mul return?". The gateway skips the web-search router for requests that carry `regex` or `response_format`.
  * **Context manager** (`ContextManager` in `oppx_chat.py`):
    * **Hooks:** it wraps `Coder.format_messages`, so every request Aider builds is fitted first.
    * **Budget:** `(context − 4096 reserved for the answer) / 1.10`, with the margin covering the gap between Aider's generic token count and the model's real tokenizer. That's 26,065 tokens at 32k.
    * **Making room, cheapest loss first:**
      1. summarize the conversation (`summarizer.summarize_all`);
      2. set aside files the model requested that are least recently used;
      3. halve the repo map down to 1,024 tokens;
      4. set aside older files you added or that were edited.

      Files used in the current turn are never set aside. Set-aside files stay visible as signatures in the repo map.
    * **Repo map:** capped at 8,192 tokens (`map_mul_no_files=1`). Aider alone lets it grow to about 28k when no files are in the chat, which caused a 46k-token request on this repo.
    * **File requests:** the model's requests go through `confirm_ask`. A single file over 60% of the budget is refused with a reason.
    * **Final guard:** `check_tokens` is replaced, so a request is never sent over the limit, and Aider's "proceed anyway / providers won't charge" text is suppressed.
    * **Visibility:** `/context` shows the breakdown, and the status bar shows the percentage used.
  * **Memory:** `OPENPHALANX.md` (and `AGENTS.md` if present) in the repo root is loaded read-only every turn. `/init` asks the model to write it.
  * **Sessions:** one file per conversation in `~/.local/state/oppx/history/<repo>-<id>/<YYYYmmdd-HHMMSS>.md`, with a shared `input.history`. The old single per-repo file is migrated as session `00000000-000000`. `-c` and `-r` pass `--restore-chat-history`.
  * **Testing:** drive it in a pty (Python `pty.fork`, 120×40 via `TIOCSWINSZ`). The driver must **answer cursor-position requests** (`ESC[6n` → `ESC[30;1R`), or prompt_toolkit never draws the status bar. Render the raw bytes with `pyte` to see the real screen.
* **`--update`** (`client/oppx/src/update.rs`):
  * It works on the checkout `oppx` was built from (`CARGO_MANIFEST_DIR/../..`), and refuses if that checkout has local changes.
  * It fetches and fast-forwards. It rebuilds with `cargo install --locked --path client/oppx` when it pulled anything, or when the commit embedded at build time (`build.rs`, shown in `oppx --version`) differs from the checkout's HEAD.
  * It makes sure `aider-chat==agent::AIDER_VERSION` is installed, preferring `uv tool install --python 3.12`, then `pipx`.
  * To test it in isolation, use a scratch clone with a local bare remote, plus `CARGO_INSTALL_ROOT`, `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` set to scratch directories.
* **Stand-in agents:** a fake `aider` script on `PATH` (printing its args and env) is a quick way to test `oppx aider` without the real agent.

## Web search

* **SearXNG:** the `openphalanx-searxng` container is pinned by digest and runs on the private `openphalanx` Docker network with **no published ports**. Its settings live in `crates/openphalanx-core/searxng/settings.yml` and are written to `~/.local/share/openphalanx/searxng/settings.yml` (0644, read-only mount, `FORCE_OWNERSHIP=false`). The secret is passed as `SEARXNG_SECRET`, and `--log-driver none` keeps queries out of logs. The GUI starts it before the backend when `settings.web_search` is on (the default; toggle on the Server page). The backend gets `SEARXNG_URL=http://openphalanx-searxng:8080`.
* **Gateway:**
  * `POST /v1/search {query, max_results}` returns `{results: [{title, url, snippet}]}`.
  * A chat request with `X-Oppx-Web-Search: auto` (sent by `oppx` unless `--no-web`) goes through the **router**, in two steps:
    1. **Decide:** a one-token, regex-constrained `yes`/`no` call to the same model, on the last ~4,000 characters of the user's **whole latest turn**. That's every user message since the last assistant message, because Aider follows the question with reminders such as "Reply in English.". The few-shot examples sit in a fixed system prompt, so they're cached. Median about 50 ms; 16/16 correct on mixed phrasings.
    2. **Query:** only on `yes`, a constrained-JSON call writes the search query.
  * **Speculative routing:** the answer starts at the same time as the router, and its response is held unread until the router decides. With no search, the router costs **about 27 ms** of time-to-first-token (68 ms against 39 ms measured). With a search, the speculative answer is closed (SGLang aborts it) and the request is resent with `<web_search_results>` appended to the end of the latest message, with an "untrusted" note.
  * Native tool calling was tried and doesn't work with Qwen2.5-Coder-14B (see `milestone.md`, Step 4.5).
* **Client:** `oppx`, `oppx aider` and `oppx proxy` send the header by default; `--no-web` opts out (`--web` is accepted as a hidden no-op). `oppx search` calls `/v1/search`.
* **Manual run (without the GUI):**

  ```bash
  docker network create openphalanx
  docker run -d --name openphalanx-searxng --network openphalanx --log-driver none \
    -e FORCE_OWNERSHIP=false -e SEARXNG_SECRET=$(openssl rand -hex 32) \
    -v $PWD/crates/openphalanx-core/searxng/settings.yml:/etc/searxng/settings.yml:ro \
    searxng/searxng@sha256:c642712fcedcdaa78fac44f71eada86aff510745826ba1bd1a368211fea2ce7f
  # then add to the backend's docker run: --network openphalanx -e SEARXNG_URL=http://openphalanx-searxng:8080
  ```

## Ports and files

| | |
|---|---|
| `9090/tcp` (all interfaces) | Public gateway API, TLS only. `/health` and `/v1/pair` are open. `/v1/whoami`, `/v1/unpair` (self-revoke), `/v1/search`, `/v1/models` and `/v1/chat/completions` (OpenAI-compatible, streaming) need a device token |
| `9091/tcp` (`127.0.0.1` only) | Admin API for the GUI; needs the per-launch admin token |
| `~/.config/openphalanx/settings.json` | Selected model, context length, GPU index, custom models |
| `~/.local/share/openphalanx/models/` | Models downloaded by the GUI (verified, pinned to a commit) |
| `~/.local/share/openphalanx/backend-state/` | Backend TLS certificate and paired devices (hashed tokens). Deleting it unpairs every client and changes the fingerprint |
| `~/.local/share/openphalanx/searxng/settings.yml` | SearXNG settings (no secrets), mounted read-only |
| `~/.cache/huggingface/hub/` | Existing Hugging Face cache; reused read-only when a model is already there |

Open `9090/tcp` in your firewall for the clients' network. Never expose `9091`.

## Security model

* **TLS:** the public API uses a self-signed certificate created on first start. Clients pin its SHA-256 fingerprint when they pair (shown in the GUI), so later connections can't be intercepted.
* **Pairing codes:** 8 characters, about 39 bits. Each is single-use, valid for 10 minutes, and burned after 5 wrong attempts, with a 1 s delay after each miss.
* **Device tokens:** each client trades a pairing code for its own random 256-bit device token. The server stores only the SHA-256 hash, and you can revoke any device from the GUI.
* **Admin API:** the admin API is reachable from the server machine only, and requires a random token generated at each launch.
* **Web search:** SearXNG is reachable only from the gateway (private network, no ports) and keeps no logs. Automatic search is on by default (the user's choice; `--no-web` opts out). Router-written queries leave the server for public search engines, so they can carry fragments of a request. Results are injected as untrusted reference text.
* **Safety bars** (gateway, any client):
  * forces `n=1` and drops `best_of`, and clamps `max_tokens` to half the context;
  * answers `413` for bodies over 8 characters per context token, before SGLang sees them;
  * allows at most 4 requests in flight per device and 8 server-wide, queueing for up to 120 s before a `503` with `retry-after`;
  * limits each device to 120 requests a minute (`429`).

  The settings live in `gateway.py`, and the `MAX_ACTIVE_REQUESTS`, `MAX_DEVICE_REQUESTS`, `QUEUE_WAIT_S` and `RATE_PER_MINUTE` variables override them. Slots are released through an idempotent cleanup that also runs as the response's background task, so a client that disconnects before streaming starts can't leak one. SGLang itself rejects over-length input with a clean 400 and queues overload: 12 concurrent 20k-token requests all completed.
* **No code stored or executed:** the server keeps no code and runs no commands for clients. Request bodies (prompts) are never logged; only per-device request and token counts are kept. Model weights are mounted read-only, and the backend never downloads weights on its own.

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

docker build -f docker/Dockerfile.server -t ghcr.io/chophilip21/openphalanx-backend:0.2.0 docker/
scripts/publish-image.sh                                        # build and push to GHCR (needs write:packages)
```

The backend image tag follows the version in the root `Cargo.toml`, so bump both together when changing `docker/`. The base SGLang image is pinned by digest in `docker/Dockerfile.server`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No permission to use Docker" | `sudo usermod -aG docker $USER`, then log out and back in |
| "Docker has no nvidia runtime" | Install NVIDIA Container Toolkit, then `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| "Port 9090 is already in use" | Another program, or an old backend container, holds the port: `docker ps`, then `docker rm -f openphalanx-backend` |
| Start disabled with "Needs X but only Y of VRAM is free" | Close other GPU programs (`nvidia-smi` lists them), or choose a smaller model or context |
| Image download fails with "denied" or "unauthorized" | The GHCR package is private: make it public, or `docker login ghcr.io`. From a source checkout, the GUI builds the image locally instead |
| `oppx aider` crashes with `No module named 'audioop'` / `'pyaudioop'` | Aider was installed with Python 3.13, which removed `audioop` (needed by Aider's `pydub` dependency). Reinstall with `uv tool install --force --python 3.12 aider-chat` |
| Backend stops during start-up | The GUI shows the reason; the full output is on **Logs** or in `docker logs openphalanx-backend` |

## Repository layout

```
app/                     Tauri 2 + Svelte 5 server GUI (Linux)
  src/                   frontend: pages, components, typed command bindings
  src-tauri/             Rust shell: Tauri commands, 2 s status monitor, log streaming
crates/openphalanx-core/ Docker, GPU, VRAM, model catalog, downloads, pre-flight (no Tauri, unit-tested)
  catalog.json           curated models pinned to Hugging Face commits
docker/                  backend image: SGLang + gateway under supervisord (no agent code)
  server/gateway.py      TLS gateway: pairing, device tokens, admin API
client/oppx/             client CLI: config.rs (paired servers, 0600 file), tls.rs (fingerprint pinning),
                         api.rs (gateway calls), main.rs (clap commands)
scripts/publish-image.sh build and push the backend image to GHCR
milestone.md             roadmap and progress
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
