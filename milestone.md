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

      * `POST /v1/run` `{prompt, files: {path: content}}`: runs Aider on a throwaway git repo and returns `{exit_code, output, diff}`. The client then applies the diff locally. *This was the first prototype of server-side execution. It has been superseded by the client-side agent design (see "Architecture decision") and is kept only as an optional one-shot mode.*

    * `docker/server/aider-local.sh` (installed as `aider-local`): interactive Aider connected to the in-container SGLang server, for manual testing.

      ```bash
      # The repo root (this repo) is mounted at /workspace, so Aider edits it directly.
      # Running as your own uid keeps the files Aider writes owned by you.
      docker exec -it -u $(id -u):$(id -g) -e HOME=/tmp -w /workspace openphalanx-backend aider-local
      ```

      *Removed in 0.2.0 together with Aider (see the note below).* `aider-local` ran with `--yes-always`, so it doesn't ask for confirmation, and with `--no-auto-commits --no-dirty-commits`, so it never commits. Review its changes with `git diff` and commit them yourself. Any extra arguments are files to put in the chat at startup. For example, append `$(git ls-files)` to load every tracked file (about 7.5k of the 32k-token context for this repo).

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

  * **0.2.0 change: Aider removed from the server image.** Once the agent moved to the client (see "Design decision" in the README), Aider in the image was dead weight. It was 589 MB, and `/v1/run` and `aider-local` depended on it.

    * `agent_server.py` is now `gateway.py`, running in a 37 MB venv (`fastapi`, `uvicorn`, `httpx`).

    * `/v1/run` is replaced by `/v1/whoami`, a token-protected check that `openbase status` will use.

    * The image is 52.6 GB, down from 53.3 GB. Aider becomes a client-side dependency (Step 4.4).

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

    * The requirement is a **conservative** estimate, revised after measuring real usage (Qwen2.5-Coder-14B-AWQ on the 3090: weights 9.43 GiB in VRAM for a 9.29 GiB download, CUDA graphs 1.27 GiB, CUDA context and allocator 1.08 GiB, plus activation peaks). It is the sum of:

      * **Weights:** download size × 1.05 for repacking and padding. FP8 weights count at **2×** on GPUs without native FP8 (compute capability below 8.9, e.g. the 3090).

      * **KV cache:** 1.25 × one full context window, so a full-length request can coexist with a cached prefix or a second request. The KV formula matches SGLang's own allocation within 1%.

      * **Runtime:** 2 GiB + 15% of the weights, because CUDA graphs and activations grow with the model. This replaces the earlier flat 3 GiB.

    * Effect on a 24 GB card: 14B-AWQ at 32k needs 20.7 GiB (fits, 1.8 GiB spare). 32B-AWQ at 8k needs 26.2 GiB and is blocked; the old estimate called it "tight" at 23.0 GiB and would have let it try. `cargo run -p openphalanx-core --example catalog -- <context>` prints the table for the local GPU.

    * The Models page shows the breakdown (weights + KV + runtime) next to the total, and labels the file size "Download" so it isn't mistaken for VRAM. "Best fit for this GPU" is computed: the catalog model with the most parameters that fits with headroom at the selected context. The catalog's static flag is now just "Tested".

    * Starting is **refused** when free VRAM is below the requirement, and flagged as **tight** with less than 1.5 GiB spare. Both are checked before the image pull and again right before launch.

    * `--mem-fraction-static` is computed from the VRAM actually free at launch, not a fixed 0.85, so SGLang never claims memory another process holds.

  * **Model catalog** (`catalog.json`):

    * Official Qwen coder repos only, each pinned to a commit. Weight sizes and attention shapes come from the repo's file listing and `config.json`.

    * Downloads go to `~/.local/share/openphalanx/models`, resume after interruption, and verify every weight file against Hugging Face's published SHA-256.

    * Copies already in `~/.cache/huggingface` are reused.

    * Custom models can be a local folder, or a Hugging Face URL or repo id. Those are resolved to a commit, and the VRAM need is shown before anything downloads.

  * **Registry:** the image is `ghcr.io/chophilip21/openphalanx-backend:<app version>`, and its base image is pinned by digest. Publish with `scripts/publish-image.sh`. `0.1.0` is published (`sha256:e586aefb…`, still contains Aider). `0.2.0` is built and passes the lifecycle test, but is not pushed yet. Package visibility must be **public** for the GUI to pull it without credentials. When the pull fails in a source checkout, the GUI builds from `docker/` instead.

* \[ \] **Step 3.2: Configure GUI Controls & Status Dashboard**

  * Expose container logs, GPU VRAM status, and agent connection metrics in the Tauri frontend UI.

  * 🚧 Compiles (clippy clean) and launches with `npx tauri dev`; the UI is awaiting its first visual review.

  * `npx tauri build --bundles deb` produces `target/release/bundle/deb/Openphalanx_<version>_amd64.deb` (5.9 MiB).

  * ✅ Fixed: the GUI's 2 s status poll was filling SGLang's log with `GET /v1/models` and `GET /metrics` access lines. `start-sglang.sh` now passes `--uvicorn-access-log-exclude-prefixes /metrics /v1/models /health`. A full start → pair → revoke run now produces a 65-line log with no poll lines. Code is in `app/` (Tauri 2 + Svelte 5 + Vite, Linux `.deb`/AppImage).

    * **Server:** a VPN-style power button, the pairing code with its countdown, the selected model with a VRAM bar, and the pre-flight checklist. While running it also shows prefix-cache hit rate, tokens/s, tasks and paired devices.

    * **Models:** the catalog table with a "Fits / Tight / Won't fit" badge per context length, plus download, use and delete actions and custom models.

    * **Devices:** paired devices, with revoke.

    * **Logs:** live container output.

  * Building the native app needs the WebKitGTK system libraries:

    ```bash
    sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev libayatana-appindicator3-dev librsvg2-dev libxdo-dev
    cd app && npm install && npx tauri dev     # or: npx tauri build
    ```

### Pairing & security model

* **Public API** (`:9090`): TLS only, using a self-signed certificate generated on first start and kept in `~/.local/share/openphalanx/backend-state`. It accepts only `/health` and `/v1/pair`. Everything else (`/v1/whoami`, and the OpenAI-compatible inference endpoints added in Phase 4) requires a device token.

* **Pairing codes:** pressing Start shows an 8-character code from a 30-letter alphabet with no lookalike characters (about 39 bits). Each code is single-use, expires after 10 minutes, and is burned after 5 wrong attempts. Every failed attempt also waits 1 s.

* **Device tokens:** a client trades the code for a random 256-bit **device token**. The server stores only its SHA-256 hash. Devices stay paired across restarts and can be revoked from the Devices page. A new code is needed only to add a device.

* **Certificate fingerprint:** the GUI shows the TLS fingerprint. `openbase pair` shows it too, for the user to compare, and then pins it (trust on first use), so later connections can't be intercepted even though the certificate is self-signed.

* **Code handling:** prompts (which contain code) pass through the gateway to SGLang and are never written to disk on the server. The server never runs a command on a client's behalf.

* **Admin API** (`:9091`): published on `127.0.0.1` only, and requires a random per-launch admin token passed through the container's environment. The GUI uses it for status, pairing and devices.

## Phase 4: Client-Side Agent & `openbase` CLI

* \[x\] **Step 4.1: Authenticated Inference Gateway (server)**

  * In `docker/server/gateway.py`, add OpenAI-compatible `POST /v1/chat/completions` and `GET /v1/models` on the public TLS port. They proxy to SGLang on `127.0.0.1:8080`, streaming included, and require a device token.

  * Pin `model` to the served model so clients can't address anything else. Enforce a request-size limit, and count requests and tokens per device for the Devices page.

  * Keep prompts out of logs. Only metadata is recorded (device, token counts, latency).

  * ✅ Implemented in `docker/server/gateway.py`:

    * `GET /v1/models` and `POST /v1/chat/completions` require a device token and proxy to SGLang. Streaming is relayed event by event, and a client disconnect closes the upstream stream, which makes SGLang abort the generation.

    * `model` is overwritten with the served model; SGLang alone would accept any name.

    * Bodies over 4 MiB (`MAX_REQUEST_BYTES`) get `413`. Errors use OpenAI's `{"error": {...}}` shape, and the API answers `503` while the model is still loading.

    * **Per-device token counts:** streaming clients such as Aider don't ask for usage, so the gateway asks SGLang for it (`stream_options.include_usage`). It records the final usage event, and strips that event when the client didn't ask for it. The Devices page shows requests and tokens in and out.

  * ✅ Verified with the lifecycle example (`cargo run -p openphalanx-core --example lifecycle`):

    * 401 without a token or with a bad one, model pinned (a request for "gpt-4" is served by `openphalanx-coder`), streaming ends with `[DONE]`, and a 5 MiB body gets 413.

    * Usage is counted for both streaming and non-streaming calls, and a client that didn't ask for usage receives no usage events.

    * A marker string in a prompt appears 0 times in the container log.

  * ✅ Verified with **real Aider 0.86** on the client side: `OPENAI_API_BASE=https://<server>:9090/v1`, the device token as API key, and `--no-verify-ssl` for this test only (Step 4.4's proxy replaces that with certificate pinning). The edit was applied to the local working tree, uncommitted. The device was credited 761 prompt and 31 completion tokens, against Aider's own estimate of 753/30.

* \[ \] **Step 4.2: Scaffold `openbase` (Rust, single binary)**

  * Initialize `client/openbase` as a member of the Cargo workspace.

  * Config in `~/.config/openbase/config.json` (mode `0600`): server URL, device id, device token and the pinned certificate fingerprint. Support several named servers, with one default.

* \[ \] **Step 4.3: Pairing**

  * `openbase pair https://<SERVER_IP>:9090 <CODE> [--name <device name>]`:

    1. Connects, reads the server certificate, and shows its SHA-256 fingerprint for comparison with the GUI.

    2. Calls `/v1/pair`, then stores the device token and the pinned fingerprint.

  * `openbase status`: server reachability, model, and whether SGLang is ready.

  * `openbase unpair`: forgets the server locally. Revocation happens in the GUI.

* \[ \] **Step 4.4: Local proxy and agent launcher**

  * `openbase proxy`: listens on `127.0.0.1:<random port>`, forwards to the server over TLS pinned to the stored fingerprint, and adds the device token. The agent talks plain HTTP to loopback, so it never needs to trust a self-signed certificate or see the token.

  * `openbase aider [aider args…]`: starts the proxy, then runs the user's local Aider with `OPENAI_API_BASE` pointing at it, `--model openai/openphalanx-coder`, the model's context window, and `--no-auto-commits --no-dirty-commits` (agents never commit directly). It stops the proxy when Aider exits.

  * Check that Aider is installed and print an install hint (`pipx install aider-chat` / `uv tool install aider-chat`). A built-in Rust agent loop, which would remove the Python dependency, is a later option.

## Phase 5: End-to-End Validation & Caching Benchmark

* \[ \] **Step 5.1: Test Simple File Edit**

  * From a paired client machine, in a git repo:

    ```bash
    openbase aider --message "Create a basic HTTP server in main.py using FastAPI"
    ```

  * Confirm that the edit lands in the local working tree, uncommitted, that local tests can be run with `/run`, and compare latency with the benchmark in "Architecture decision".

* \[ \] **Step 5.2: Test RadixAttention Context Caching**

  * Execute a multi-turn modification task.

  * Verify high prefix-cache hit rates on later turns with the GUI's "Prefix cache hit" stat, or in `docker logs -f openphalanx-backend` (`#cached-token`).

## Future Improvement

* If you combine SearXNG and /web (aider), we can make something awesome.There are MCP servers, like mcp-searxng that we can leverage to fill up the gab of missing feature of automatic web search. SearXNG should reside on the server side. 
- dependabot, automatic building based on updates on aider and srglang. 
- Celery? is there anyway to utilize celery here for async work. 