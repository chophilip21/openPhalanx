# OpenPhalanx

open-source platform that pools distributed GPU VRAM across local machines to run heavy coding agentic models alongside a headless agent harness, enabling a lightweight CLI client to execute remote AI engineering tasks.

```
flowchart TB
    subgraph ClientNode ["Client Node (Work Laptop)"]
        direction TB
        CLI["openbase CLI<br/>(Go Thin Client)"]
        FS[("Local Workspace<br/>& Filesystem")]
       
        CLI <-->|"Scans Context / Applies Diffs"| FS
    end

    subgraph ServerNode ["Server Node (Primary Rig)"]
        direction TB
        GUI["Openphalanx GUI<br/>(Tauri / Rust Orchestrator)"]
        Harness["Headless Agent Harness<br/>(Goose / Aider Server)"]
        Master["SGLang Server<br/>(Inference Master)"]
       
        GUI -.->|"Orchestrates"| Harness
        GUI -.->|"Orchestrates"| Master
        Harness <-->|"Local HTTP API"| Master
    end

    CLI <==>|"Agent Client Protocol (ACP)"| Harness

    classDef clientStyle fill:#0f172a,stroke:#38bdf8,stroke-width:2px,color:#fff;
    classDef serverStyle fill:#1e1b4b,stroke:#818cf8,stroke-width:2px,color:#fff;
    classDef modelStyle fill:#422006,stroke:#fb923c,stroke-width:2px,color:#fff;

    class CLI,FS clientStyle;
    class GUI,Harness serverStyle;
    class Master modelStyle;
```


Openphalanx operates on a decoupled client-server architecture where a lightweight Go CLI client on your local workspace handles file-system scanning and patch application while sending low-bandwidth task requests over the Agent Client Protocol (ACP) to a remote, headless agent harness running on your primary server node. The server hosts the execution context and coordinates with SGLang, a high-performance inference engine that leverages RadixAttention to cache repetitive agent context. This enables you to run heavy coding models at peak throughput directly on the server's GPUs without consuming client resources or shuffling massive context windows across the wire.

## Phase 1: Environment & Tooling Audit

* \[x\] **Step 1.1: Verify Server Hardware & Docker Environment**

  * Query primary host (Desktop) GPU specs using `nvidia-smi`.

  * Verify Docker Desktop / Docker Engine is installed and running on the primary server.

  * Verify `nvidia-container-toolkit` is installed so Docker containers can access local GPUs via `--gpus all`.

  * ✅ Verified: RTX 3090 (24 GB, driver 580.159.03), Docker 29.5.3 with `nvidia` runtime, NVIDIA Container Toolkit 1.19.1, `lmsysorg/sglang:latest` pulled (SGLang 0.5.21) and sees the GPU via `--gpus all`.

* \[x\] **Step 1.2: Check Client Dependencies**

  * Verify runtime environments on the client node (`Go` or `Rust` for compiling the `openbase` thin client).

  * ✅ Verified: Rust 1.99.0 / Cargo 1.99.0 installed via rustup (`~/.cargo/bin`). Go is not installed, so Rust is the toolchain for `openbase`.

## 2.3 Check Aider can edit existing code

- [x] Edit an existing file `README.md`
- [x] Add a new section "2.3 Check Aider can edit existing code"

## Phase 2: Unified Backend Container Setup (SGLang + Aider)

* \[x\] **Step 2.1: Create Container Configurations**

  * Create a `docker/Dockerfile.server` derived from `lmsysorg/sglang:latest` that installs `aider-chat` and `supervisor`.

  * Create `supervisord.conf` to manage process lifecycle on the server:

    * **Process 1 (SGLang):** Starts model inference engine at `http://127.0.0.1:8080/v1`.

    * **Process 2 (Aider):** Starts headless agent server bound to `0.0.0.0:9090`, pointing directly to the internal SGLang instance.

  * ✅ Implemented:

    * `docker/Dockerfile.server`: adds `supervisor` (apt) and installs `aider-chat` in a separate venv (`/opt/aider`) so its pinned deps don't clash with SGLang's environment.

    * `docker/server/start-sglang.sh`: SGLang bound to `127.0.0.1:8080` only, served as `openphalanx-coder`. Configurable via `MODEL_PATH` (default `Qwen/Qwen2.5-Coder-14B-Instruct-AWQ`, which fits on a 24 GB card), `MEM_FRACTION_STATIC`, `CONTEXT_LENGTH` and `SGLANG_EXTRA_ARGS`.

    * `docker/server/supervisord.conf`: runs both processes and sends their logs to the container's stdout so `docker logs` works.

    * `docker/server/agent_server.py`: Aider has no built-in server mode, so a FastAPI wrapper on `0.0.0.0:9090` provides one:

      * `GET /health`: reports agent and SGLang status.

      * `POST /v1/run` `{prompt, files: {path: content}}`: runs Aider on a throwaway git repo and returns `{exit_code, output, diff}`. The client then applies the diff locally.

    * `docker/server/aider-local.sh` (installed as `aider-local`): interactive Aider connected to the in-container SGLang server, for manual testing.

      ```bash
      # The repo root (this repo) is mounted at /workspace, so Aider edits it directly.
      # Running as your own uid keeps the files Aider writes owned by you.
      docker exec -it -u $(id -u):$(id -g) -e HOME=/tmp -w /workspace openphalanx-backend aider-local
      ```

      `aider-local` runs with `--yes-always`, so it doesn't ask for confirmation, and with `--no-auto-commits --no-dirty-commits`, so it never commits. Review its changes with `git diff` and commit them yourself. Any extra arguments are files to put in the chat at startup. For example, append `$(git ls-files)` to load every tracked file (about 7.5k of the 32k-token context for this repo).

      Aider only sees *committed* files in its repo map. Use `/add <file>` to give the model a file's contents, `/ask` for questions, and `/run <cmd>` for shell commands. Plain chat text goes only to the LLM.

* \[x\] **Step 2.2: Build and Test Unified Image**

  * Build the unified container image on the server node:

    ```bash
    docker build -f docker/Dockerfile.server -t openphalanx-backend:latest docker/
    ```

  * Launch the unified backend container using NVIDIA runtime pass-through:

    ```bash
    # Manual/dev run. The Phase 3 GUI normally launches the container (see below).
    docker run -d --name openphalanx-backend --gpus all \
      --shm-size 32g \
      -p 9090:9090 \
      -v ~/.cache/huggingface:/root/.cache/huggingface \
      -v "$PWD":/workspace \
      --ipc=host \
      openphalanx-backend:latest
    ```

  * Verify that port `9090` accepts incoming connections from the client network and internal communication to SGLang functions seamlessly.

  * ✅ Verified on the 3090 node (`192.168.1.77`):

    * The image builds with Aider 0.86.2. Startup takes about 3.5 min with the model already cached (weight load plus CUDA graph capture).

    * Qwen2.5-Coder-14B-AWQ runs with a 32k context and about 51.6k tokens of KV cache, using roughly 22.9 GB of 24 GB VRAM.

    * `GET http://192.168.1.77:9090/health` → `{"agent":"ok","sglang":"ready",...}`.

    * `POST /v1/run` edit task: about 8 s end to end, and the diff applies cleanly with `git apply`.

    * `POST /v1/run` new-file task: returns a proper `new file` diff. After applying it, the generated pytest suite passes.

## Phase 3: Tauri Orchestrator Integration

- The server side will be created with Tauri, distributed only for Linux for now. We will build for other OS once we are sure linux version is stable. 
- Frontend, make it simple and modern, using Svelte and Vite. Can you get your inspiration from popular VPN providers, like ExpressVPN or NordVPN. 
- We do not distribute the weights. But we must display trustable catalog of weights and there VRAM Requirement as tables (e.g qwen2.5 that we are using) and download button. User should be able to specify the path themselves as well (either local path, or url that we can attempt to download for the user)
- We never want to crash the users with OOM, so be careful not to crash. Raise warning early, and if available VRAM is less than the model requirement, do not even allow the user to start the server. 
- Use github container registry as you have suggested. 
- Upon clicking start, it should display some kind of pairing code for client to connect to. This could be random everytime, or the same. But think about security and the best practice. 

* \[x\] **Step 3.1: Implement Docker Engine Manager in Tauri**

  * Add container lifecycle controls to the Tauri app's Rust backend using the Docker API or `std::process::Command`.

  * Implement pre-flight checks: verify Docker daemon availability, pull/build image if missing, and monitor container runtime health.

  * ✅ Implemented in `crates/openphalanx-core` (plain Rust, no Tauri, so it is unit-tested with `cargo test -p openphalanx-core`). The checks run on this machine with `cargo run -p openphalanx-core --example preflight`.

    * **Docker** (`docker.rs`): drives the `docker` CLI to check the daemon (with fix-it hints for permission and not-running errors), check for the nvidia runtime, and handle pull, build fallback, run, stop, inspect and logs. Containers are labelled `io.openphalanx.managed`, so the GUI reattaches to a running backend after it restarts.

    * **Crash handling:** if SGLang dies, `start-sglang.sh` stops the whole container instead of letting supervisord restart it in a loop. The GUI then sees the exit and diagnoses it from the logs (CUDA OOM, missing files, port in use).

    * **Model mounts:** the folder is mounted read-only, together with every directory its symlinks pass through, at identical paths. This is needed because the Hugging Face cache links snapshots into per-repo and hub-wide blob stores. `HF_HUB_OFFLINE=1` stops SGLang from ever downloading on its own.

  * **OOM protection** (`vram.rs`):

    * The requirement is weights + KV cache for one full context window + 3 GiB runtime overhead. The KV formula matches SGLang's own allocation for Qwen2.5-14B within 1%.

    * Starting is **refused** when free VRAM is below the requirement, and flagged as **tight** with less than 1.5 GiB spare. Both are checked before the image pull and again right before launch.

    * `--mem-fraction-static` is computed from the VRAM actually free at launch, not a fixed 0.85, so SGLang never claims memory another process holds.

  * **Model catalog** (`catalog.json`):

    * Official Qwen coder repos only, each pinned to a commit. Weight sizes and attention shapes come from the repo's file listing and `config.json`.

    * Downloads go to `~/.local/share/openphalanx/models`, resume after interruption, and verify every weight file against Hugging Face's published SHA-256.

    * Copies already in `~/.cache/huggingface` are reused.

    * Custom models can be a local folder, or a Hugging Face URL or repo id. Those are resolved to a commit, and the VRAM need is shown before anything downloads.

  * **Registry:** the image is `ghcr.io/chophilip21/openphalanx-backend:<app version>`, and its base image is pinned by digest. Publish with `scripts/publish-image.sh`. Until it's published, the GUI falls back to building from `docker/` in a source checkout.

* \[ \] **Step 3.2: Configure GUI Controls & Status Dashboard**

  * Expose container logs, GPU VRAM status, and agent connection metrics in the Tauri frontend UI.

  * 🚧 Written, not yet run: `app/` (Tauri 2 + Svelte 5 + Vite, Linux `.deb`/AppImage).

    * **Server:** a VPN-style power button, the pairing code with its countdown, the selected model with a VRAM bar, and the pre-flight checklist. While running it also shows prefix-cache hit rate, tokens/s, tasks and paired devices.

    * **Models:** the catalog table with a "Fits / Tight / Won't fit" badge per context length, plus download, use and delete actions and custom models.

    * **Devices:** paired devices, with revoke.

    * **Logs:** live container output.

  * The frontend type-checks and builds. The native app needs the WebKitGTK system libraries before it can compile:

    ```bash
    sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev
    cd app && npm install && npx tauri dev     # or: npx tauri build
    ```

### Pairing & security model

* **Public API** (`:9090`): TLS only, using a self-signed certificate generated on first start and kept in `~/.local/share/openphalanx/backend-state`. It accepts only `/health`, `/v1/pair`, and `/v1/run` with a device token.

* **Pairing codes:** pressing Start shows an 8-character code from a 30-letter alphabet with no lookalike characters (about 39 bits). Each code is single-use, expires after 10 minutes, and is burned after 5 wrong attempts. Every failed attempt also waits 1 s.

* **Device tokens:** a client trades the code for a random 256-bit **device token**. The server stores only its SHA-256 hash. Devices stay paired across restarts and can be revoked from the Devices page. A new code is needed only to add a device.

* **Certificate fingerprint:** the GUI shows the TLS fingerprint. The client should show it at pairing time and pin it (trust on first use), so later connections can't be intercepted.

* **Admin API** (`:9091`): published on `127.0.0.1` only, and requires a random per-launch admin token passed through the container's environment. The GUI uses it for status, pairing and devices.

## Phase 4: Thin Client CLI (`openbase`) Implementation

* \[ \] **Step 4.1: Scaffold `openbase` CLI Project**

  * Initialize a single-binary project (Go or Rust) inside `client/openbase`.

  * Implement configuration file handling (`~/.openbase/config.json`) to store target Server IP and Port (`9090`).

* \[ \] **Step 4.2: Implement Client Commands**

  * `openbase init --server http://<SERVER_IP>:9090`: Sets target server address.

  * `openbase run "<prompt>"`:

    1. Scans working directory file tree and Git context.

    2. Sends instruction payload over ACP / HTTP to the headless Aider instance running inside the remote server container.

    3. Streams execution output back to terminal stdout.

    4. Safely applies returned diff patches to local files.

## Phase 5: End-to-End Validation & Caching Benchmark

* \[ \] **Step 5.1: Test Simple File Edit**

  * Run a test command from the client machine:

    ```bash
    openbase run "Create a basic HTTP server in main.py using FastAPI"
    ```

  * Confirm patch generation accuracy and execution speed.

* \[ \] **Step 5.2: Test RadixAttention Context Caching**

  * Execute a multi-turn modification task.

  * Inspect container logs (`docker logs -f <container_id>`) to verify high KV-cache hit rates on SGLang during subsequent prompt turns.

## Future Improvement

* If you combine SearXNG and /web, we can make something awesome.
- dependabot, automatic building based on updates on aider and srglang. 
- Celery? is there anyway to utilize celery here for async work. 