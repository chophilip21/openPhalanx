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

* \[ \] **Step 3.1: Implement Docker Engine Manager in Tauri**

  * Add container lifecycle controls to the Tauri app's Rust backend using the Docker API or `std::process::Command`.

  * Implement pre-flight checks: verify Docker daemon availability, pull/build image if missing, and monitor container runtime health.

* \[ \] **Step 3.2: Configure GUI Controls & Status Dashboard**

  * Expose container logs, GPU VRAM status, and agent connection metrics in the Tauri frontend UI.

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
