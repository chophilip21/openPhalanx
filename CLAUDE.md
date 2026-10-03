# CLAUDE.md

Working notes for developing this repo: architecture, operations, commands and conventions. End-user docs are in `README.md`; the roadmap and step-by-step progress are in `milestone.md`.

## Conventions

* **Docs:**
  * `README.md` is for end users only: what the project is, plus the minimum commands to run the server and a client. Keep it at 50–100 lines.
  * Everything operational or developer-facing goes here.
  * Roadmap status (phases, steps, checkboxes, verification notes) goes in `milestone.md`.
* **Agents never commit directly:** any agent launcher or config (Aider, `openbase aider`, etc.) must use `--no-auto-commits --no-dirty-commits`. The user reviews with `git diff` and commits themselves.
* **Branching:** work happens on `dev`; `main` holds the initial history only.
* **Toolchain on the server node:** `cargo` is in `~/.cargo/bin` (not on the non-interactive `PATH`), so use `export PATH=$HOME/.cargo/bin:$PATH`. `uv`/`uvx` are in `~/.local/bin`.
* **Versioning:** the backend image tag equals the workspace version in `Cargo.toml`. Bump `Cargo.toml`, `app/package.json` and `app/src-tauri/tauri.conf.json` together when `docker/` changes.
* **Verify before claiming:** run `cargo test -p openphalanx-core`, `cargo clippy --workspace --all-targets`, `(cd app && npm run check)`, and for backend changes the lifecycle example (about 4 min, needs the GPU).

## Architecture

```
flowchart TB
    subgraph ClientNode ["Client Node (Work Laptop)"]
        direction TB
        CLI["openbase CLI<br/>(Rust: pairing, pinned-TLS proxy)"]
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

* **Client:** the coding agent (Aider) runs on your laptop next to your repo, so it sees the whole codebase and runs your own tests, linters and git. The `openbase` CLI pairs the laptop with a server, then launches the agent pointed at a local proxy. The proxy pins the server's TLS certificate and adds the device token, so the agent never handles either.
* **Server:** stores no code and executes nothing on a client's behalf. It serves SGLang behind an authenticated gateway, and the Tauri GUI manages the container, VRAM safety, models and paired devices. SGLang's RadixAttention caches repeated prompt prefixes, so multi-turn agent sessions skip most of the prompt processing.

## Status

| Component | State |
|---|---|
| Backend image (SGLang + gateway) | Working, including the authenticated OpenAI-compatible inference API. `0.2.0` is built locally but not yet pushed to GHCR |
| Server GUI (`app/`, Linux) | Builds and runs; first UI review pending |
| Pairing, TLS and device tokens | Working |
| `openbase` client CLI | Not started. Until it exists, clients can't connect for real (see `milestone.md`, Phase 4) |

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

## Connecting a client before `openbase` exists

Pair with `curl`, then point the client's own Aider at the gateway. `-k` and `--no-verify-ssl` skip certificate checks, so use this only on a trusted network; `openbase` (Phase 4) replaces it with a pinned-certificate local proxy.

```bash
TOKEN=$(curl -sk https://$SERVER:9090/v1/pair -H 'content-type: application/json' \
  -d '{"code":"ABCD-EFGH","device_name":"my-laptop"}' | jq -r .token)
OPENAI_API_BASE=https://$SERVER:9090/v1 OPENAI_API_KEY=$TOKEN \
  aider --model openai/openphalanx-coder --no-verify-ssl --no-auto-commits --no-dirty-commits
```

The admin API can issue a code without the GUI: `curl -X POST -H "x-admin-token: $ADMIN_TOKEN" http://127.0.0.1:9091/admin/pairing`. The token is in the container env: `docker inspect openphalanx-backend --format '{{json .Config.Env}}'`.

## Ports and files

| | |
|---|---|
| `9090/tcp` (all interfaces) | Public gateway API, TLS only. `/health` and `/v1/pair` are open. `/v1/whoami`, `/v1/models` and `/v1/chat/completions` (OpenAI-compatible, streaming) need a device token |
| `9091/tcp` (`127.0.0.1` only) | Admin API for the GUI; needs the per-launch admin token |
| `~/.config/openphalanx/settings.json` | Selected model, context length, GPU index, custom models |
| `~/.local/share/openphalanx/models/` | Models downloaded by the GUI (verified, pinned to a commit) |
| `~/.local/share/openphalanx/backend-state/` | Backend TLS certificate and paired devices (hashed tokens). Deleting it unpairs every client and changes the fingerprint |
| `~/.cache/huggingface/hub/` | Existing Hugging Face cache; reused read-only when a model is already there |

Open `9090/tcp` in your firewall for the clients' network. Never expose `9091`.

## Security model

* **TLS:** the public API uses a self-signed certificate created on first start. Clients pin its SHA-256 fingerprint when they pair (shown in the GUI), so later connections can't be intercepted.
* **Pairing codes:** 8 characters, about 39 bits. Each is single-use, valid for 10 minutes, and burned after 5 wrong attempts, with a 1 s delay after each miss.
* **Device tokens:** each client trades a pairing code for its own random 256-bit device token. The server stores only the SHA-256 hash, and you can revoke any device from the GUI.
* **Admin API:** the admin API is reachable from the server machine only, and requires a random token generated at each launch.
* **No code stored or executed:** the server keeps no code and runs no commands for clients. Request bodies (prompts) are never logged; only per-device request and token counts are kept. Model weights are mounted read-only, and the backend never downloads weights on its own.

## Development

```bash
cargo test -p openphalanx-core                                  # unit tests (no GPU or Docker needed)
cargo test -p openbase                                          # client unit tests
cargo run -p openbase -- servers                                # client CLI (config: OPENBASE_CONFIG or --config)
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
client/openbase/         client CLI: config.rs (paired servers, 0600 file), main.rs (clap commands)
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
