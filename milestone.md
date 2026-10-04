# Milestones

Roadmap and progress. Completed work is summarized below; details are in `CLAUDE.md` and the git history.

## Completed

### Phase 1: Environment
* Server: RTX 3090 (24 GB), driver 580, Docker 29.5 with the `nvidia` runtime, Container Toolkit 1.19, SGLang 0.5.21 sees the GPU.
* Client toolchain: Rust (Go not installed), so `oppx` is written in Rust.

### Phase 2: Backend container
* `docker/Dockerfile.server` on a digest-pinned SGLang base, run under supervisord. SGLang listens on `127.0.0.1:8080` only, served as `openphalanx-coder`.
* Configured with `MODEL_PATH`, `CONTEXT_LENGTH`, `MEM_FRACTION_STATIC` and `SGLANG_EXTRA_ARGS`.
* Qwen2.5-Coder-14B-AWQ at 32k uses 22.9 of 24 GB and is ready in about 3.5 min.
* The first prototype ran Aider in the image (`/v1/run`, `aider-local`). In 0.2.0 it was removed once the agent moved to the client: the gateway venv is 37 MB and the image went from 53.3 to 52.6 GB.

### Phase 3: Server app (Tauri 2 + Svelte 5, Linux)
* User requirements:
  * Linux first, with a VPN-style UI.
  * A trusted model catalog with VRAM tables; weights are never distributed.
  * Never OOM: refuse to start below the requirement.
  * Images on GHCR.
  * A secure pairing code.
* **3.1 Engine manager** (`crates/openphalanx-core`, unit-tested without Tauri):
  * **Docker control** through the CLI: daemon checks with fix-it hints, pull with a build fallback, run, stop, logs, reattach via a label.
  * **Crash handling:** if SGLang dies, the container stops instead of restart-looping, and the GUI diagnoses why (OOM, missing files, port in use).
  * **Models** are mounted read-only along their symlink chains, with `HF_HUB_OFFLINE=1`.
  * **VRAM estimate** (conservative, checked against measured usage): weights ×1.05 (FP8 ×2 without native FP8), plus KV for 1.25 × the context, plus 2 GiB and 15% of the weights.
    * 14B-AWQ at 32k needs 20.7 GiB and is allowed; 32B-AWQ is blocked.
    * Start is refused below the requirement and flagged "tight" with under 1.5 GiB spare.
    * `--mem-fraction-static` comes from the VRAM free at launch.
  * **Catalog:** official Qwen coder repos pinned to commits. Downloads resume and are verified against Hugging Face's SHA-256. The HF cache is reused, and custom folders or URLs are checked before downloading.
  * **Registry:** `ghcr.io/chophilip21/openphalanx-backend:<version>`. 0.1.0 is published; 0.2.0 is built but not pushed.
* **Pairing and security:**
  * TLS on `:9090` with a self-signed certificate; clients pin its fingerprint.
  * 8-character single-use codes: 10 minutes, burned after 5 misses, 1 s delay per miss.
  * 256-bit device tokens, stored hashed and revocable.
  * Admin API on `127.0.0.1:9091` with a per-launch token.
  * Prompts are never logged.
* **3.2 GUI:** the Server, Models, Devices and Logs pages build and run, and the `.deb` is 5.9 MiB. The status poll no longer spams the SGLang log. The visual review is still open (Step 5.4).

### Phase 4: Client agent and `oppx`
* **4.1 Inference gateway:**
  * Token-protected OpenAI-compatible `/v1/models` and `/v1/chat/completions`, streaming, with the model pinned.
  * `413` over 4 MiB, `503` while loading.
  * Per-device token counts (usage is requested from SGLang and stripped when the client didn't ask).
  * Verified: 401s, a prompt marker 0 times in the logs, real Aider credited 761/31 tokens.
* **4.2 `oppx` scaffold:**
  * Multiple named servers in a `0600` config with an ssh-style permission warning.
  * Strict URL, fingerprint and name validation.
  * Renamed from `openbase` to avoid clashing with 1Password's `op`/`opx`.
* **4.3 Pairing:**
  * A custom rustls verifier pins the certificate's SHA-256.
  * `pair` asks for confirmation (`[y/N]`, `--fingerprint` or `--yes`) and redeems the code only over the pinned connection.
  * `status` checks certificate, token and model; `unpair` self-revokes via `/v1/unpair`.
  * Verified: an impostor certificate gets no HTTP request.
* **4.4 Local proxy and launcher:**
  * The `axum` loopback proxy adds the device token and requires a random per-run local key; the agent never sees the token.
  * `oppx aider` passes the context metadata and always appends `--no-auto-commits --no-dirty-commits`.
  * Verified: a mid-generation disconnect leaves 0 running requests, and real Aider edited without committing.
* **4.5 Web search:**
  * SearXNG on a private Docker network: no ports, no logs, image pinned by digest.
  * `POST /v1/search` and `oppx search`.
  * Native tool calls don't work on Qwen-14B, so a router decides instead. Results are appended to the end of the latest message as untrusted text, which keeps the cached prefix.
  * **Follow-up (on by default, `--no-web` opts out):**
    * A one-token `yes`/`no` decision (few-shot, cached; 16/16 correct) runs on the whole user turn.
    * The answer starts speculatively alongside it.
    * Cost: +27 ms time to first token (39 → 66 ms). Searches take 1.7–2.3 s, and "newest tokio" was answered correctly.
* **4.6 CLI experience:**
  * Plain `oppx` starts the chat.
  * OPENPHALANX gradient banner (large and compact), spinners, rounded panels, ✓/!/✗ rows.
  * Plain output when piped or with `NO_COLOR`.
  * Aider must use Python ≤ 3.12 (3.13 lacks `audioop`).
  * Checked by you on a real terminal.
* **4.7 Chat frontend:**
  * A Claude-style Python UI over Aider's engine, embedded in `oppx` and run with Aider's interpreter; `--classic` gives Aider's UI.
  * Welcome box, status bar, `✻ Thinking…` spinner, `⏺` answers, `⏺ Update(file)` diffs, no Aider branding.
  * Aider pinned to 0.86.2. Checked by you on a real terminal.
* **4.8 Claude Code parity:**
  * **Keys and commands:** Ctrl-C clears or exits, Esc interrupts, Shift+Tab plan mode, `!cmd`, `@file`, `# note`, `/clear /compact /context /resume …`.
  * **Sessions:** one file per conversation, with `-c`, `-r` and `--resume <id>`.
  * **Ask/edit classifier** (one constrained token, 14/14): questions can't edit files (one used to delete `mul`).
  * **`oppx --update`:** fast-forward, rebuild, pin the engine.
  * Type-ahead is kept during a turn.
* **4.9 Context management and safety bars:**
  * Fixed a real 46k-token request to a 32k model.
  * **Budget:** (context − 4096) / tokenizer factor.
  * **Making room, cheapest first:** summarize (auto-compact), set aside least-recently-used model-requested files, shrink the repo map (8k → 1k), set aside older files. Files too big to fit are refused, nothing is sent over the limit, and `/context` shows the breakdown.
  * **Gateway safety bars:** `n=1`, a `max_tokens` clamp, an early `413`, 4 requests per device and 8 server-wide with a 120 s queue then `503`, 120 requests a minute (`429`), leak-proof slot release.
  * Verified: SGLang survived abuse, the limits held exactly, and this repo fit at 25.5k of a 26.1k budget.
  * **Editing fixes:**
    * Shell suggestions off (the README's `sudo apt` was offered).
    * One whole-file retry when the diff format fails.
    * Accurate diffs from a `write_text` snapshot.
    * Streaming and the spinner forced on (Aider turned both off for non-standard fences, which looked like a freeze).
* **4.10 Model-agnostic follow-ups:**
  * **Catalog data:** `edit_format` and `reasoning_parser` per model.
  * **`/v1/info`:** reports the real model id (shown in `status`, `proxy` and the chat), and `oppx` passes Aider a model settings file with the edit format and the repo map on.
  * **Reasoning:** `<think>` and Aider's THINKING/ANSWER markers are hidden.
  * **`/v1/tokenize`** calibrates the context budget against the server's tokenizer: ratio 1.011, budget 26,065 → 27,541.
  * **`scripts/probe_routing.py`:** router 16/16 and intent check 14/14 on Qwen-14B.

### Phase 5 (completed steps)
* **5.2 Prefix-cache benchmark** (`scripts/bench_session.py`, 10 real turns; `OPPX_DUMP_REQUESTS` shows where requests diverge):
  * **Found:** only 3–7% cached. The repo map was re-ranked every turn, and ask mode used a different system prompt.
  * **Fixed** (`oppx_chat/cache.py`): a whole-repo map cached per (file list, size) whose size only shrinks within a session; questions share the edit prompts plus a prose-only note; a fixed map heading.
  * **Result:** 50% cached (77k of 156k), computed tokens 204k → 78k, time until the second answer starts 4.8 s → 0.6 s. Edit quality unchanged.
* **5.6 Frontend split** (your request): `oppx_chat.py` (1,500 lines) is now the `oppx_chat/` package (`config`, `term`, `render`, `context`, `oppx_io`, `routing`, `cache`, `commands`, `app`) plus `run.py`, layered with no upward imports. A test checks every module is embedded; the pty regression passed.

## Open

* \[ \] **Step 5.1: End-to-end validation from a clean laptop.** A fresh install with only the documented steps (the release installer), pair, `status`, then a real session: a question, a multi-file edit, `!cmd` tests, `/undo`, Esc/Ctrl-C, `-c`/`-r`, web on and off. Revoke mid-session and expect a clear error; restart the server and recover without re-pairing. Compare latency with `CLAUDE.md`'s benchmark.

* \[ \] **Step 5.3: Model matrix.**
  * ✅ **Catalog expanded** from 8 Qwen entries to 28:
    * Qwen3.6 27B and 35B-A3B, plus Qwen3-Coder-Next in BF16;
    * Gemma 4 (E4B, 12B, 26B-A4B, 31B; official QAT 4-bit builds);
    * gpt-oss 20B and 120B;
    * Devstral Small 1.1, Devstral Small 2 and Devstral 2;
    * 4-bit community builds where no official one fits 24 GB.
  * ✅ Every entry is built from Hugging Face by `scripts/catalog_entry.py`, and `--check` passes. Every architecture exists in SGLang 0.5.21.
  * ✅ **VRAM estimate** now models sliding-window, Gemma 4 global and linear-attention layers, the way SGLang sizes its pools. The downloader skips duplicate Mistral weights.
  * At 32k on a 3090 the estimate says these fit: Qwen2.5-Coder 7B/14B AWQ, gpt-oss 20B, and Gemma 4 E4B (tight). Gemma 4 12B QAT fits up to about 29k, and Devstral Small 2 AWQ up to about 16k.
  * Still to do: run each fitting model on hardware. For every catalog model that fits 24 GB at 32k, plus one reasoning model:
  * `probe_routing.py` (at least 90% each);
  * a fixed edit task in `diff` and in `whole`, to set the catalog's `edit_format`;
  * tokenizer ratio, tokens/s, time to first token.

  Record a table here, and fix weak spots only with prompt changes that are re-checked on every model.

* \[ \] **Step 5.4: Server GUI review and hardening** (includes Step 3.2):
  * your visual review of Server, Models, Devices and Logs;
  * show the model id, rate-limit and busy counts, and the web-search state;
  * error paths: Docker stopped, GPU busy, a failed download, a port in use, a revoked device.
  * ✅ **First review round** (your feedback):
    * The Server page is now one centred column: the power button, then a large pairing code under it, then a Grafana-style dashboard instead of side panels.
    * The dashboard has cluster tiles, a nodes table (one row per node and GPU) and six live charts over the last 10 minutes. It's built on a node list (`lib/metrics.svelte.ts`) so a server cluster only adds rows.
    * "Devices" is now "Client" in the nav, right below Server, and the page title is "Client Devices". The server fingerprint moved there from the pairing card.
    * The pairing code has its own centred section (code, copyable `oppx pair` command, New code / Cancel), without fingerprint or web-search text.
    * The nav logo is `assets/openphalanx-shield-green.svg` (copied to `app/src/assets/logo.svg`).
    * Light and dark themes with a toggle in the nav. It follows the system until you choose, and every colour is a token in `app.css`.
    * The nav collapses to icons below 760 px.
    * Checked in headless Chrome with mock data: both themes, running and stopped, and 1400/1280/720 px widths.
  * ✅ **Models page:**
    * The context window is a slider in its own "Server setting" panel. It explains that the window changes memory, not which models are listed, and shows how far the selected model can go on this GPU.
    * Family filter chips and an "only models that fit" toggle.
    * Community badges, and "fits at N or less" hints.

* \[ \] **Step 5.5: Distribution and CI/CD.** Your requirements:

  > We need to package this up and distribute both server and client. Refer to how others distribute packages via `curl` and etc. End-user should not have to install aider-chat, and other dependencies by himself.
  > Build CI/CD pipeline via github actions, and bump version. Versions gets determined by scale (bug, feature, major level), and by PR header (e.g "[bug]Fix ABCD" 0.0.1-> 0.0.2, or "[FEATURE]ASDFF" which would change to 0.1.2)
  > We need automatic documentation generator upon new releases.

  * ✅ **Versioning:**
    * `scripts/bump_version.py`: `[bug]`/`[fix]` patch, `[feature]` minor, `[major]` major. A minor bump resets the patch number (0.0.2 → 0.1.0), confirmed with you.
    * Bumps all five version files, writes `CHANGELOG.md`, and fails PRs into `main` that have no prefix.
  * ✅ **Workflows** (`actionlint` clean):
    * `ci.yml`: app check, clippy `-D warnings`, tests, ruff, shell syntax, PR title.
    * `release.yml`: bump, tag, `oppx` for Linux musl (x86-64/arm64) and macOS (arm64/x86-64), `.deb` and AppImage, `SHA256SUMS`, GitHub release, docs to Pages.
    * The image is built only with a self-hosted `IMAGE_RUNNER`.
    * The static x86-64 build was reproduced locally (8 MB).
  * ✅ **Private engine** (`engine.rs`): uv, Python 3.12 and Aider 0.86.2 in `~/.local/share/oppx/engine`. A fresh machine with no Aider or uv was set up and answering in 7 s.
  * ✅ **Updates and installers:**
    * `oppx --update` release mode: checksum-verified self-replace (says so when no release exists yet).
    * `install.sh` (client) and `install-server.sh` (prerequisite checks plus the `.deb`), both checksum-verified.
  * ✅ **Docs:** `scripts/gen_docs.py` builds README, CLI and API references (every gateway route documented), CLAUDE.md, the roadmap and the changelog with mdBook.
  * **Left for you:**
    * One-time repository settings (Actions write, Pages source "GitHub Actions", public GHCR package).
    * Then the first `[feature]` PR `dev` → `main` runs the pipeline end to end; check it with Step 5.1. macOS and arm64 builds run for the first time there.

## Future Improvement

1. Optimization work for speed and security. 
2. ~~Implement sessions that can be resumed, and make sure we are doing context summarization, etc.~~ Done in Steps 4.8 (sessions, `-c`/`-r`) and 4.9 (auto-compaction and context fitting). 
3. Another important feature that deserves its own section. right now, our codebase assumes there is one server, and N possible clients. But this is not only the case. There can be N servers that can distribute the workload, and load one large model in a distributed way. And we can also imagine multiple clients, that points at cluster of nodes. 

Scaling from a single GPU workstation to a cluster of nodes handling multiple concurrent clients is the exact use case that frameworks like SGLang and vLLM were built to solve for enterprise deployments.To achieve this, the architecture splits into two distinct problems: distributing the model (across N servers) and distributing the traffic (routing N clients).Here is exactly how this is handled in modern LLM infrastructure.Part 1: Distributing One Large Model Across N ServersIf a model is too large to fit on a single machine (e.g., a 70B parameter model or massive Mixture-of-Experts like DeepSeek), you cluster multiple physical servers together. SGLang supports this natively using Ray and NCCL (NVIDIA Collective Communications Library).Tensor Parallelism (TP) & Pipeline Parallelism (PP):TP slices individual matrix math operations across multiple GPUs. If those GPUs are on different servers, SGLang uses Ray to coordinate them over the network.PP slices the model vertically. Server A handles layers 1–20, and Server B handles layers 21–40. Server A computes the first half and passes the intermediate tensors over the network to Server B to finish.   Prefill/Decode (PD) Disaggregation:This is a highly advanced SGLang feature for clusters. You designate some servers strictly as "Prefill nodes" (their only job is reading massive codebases/prompts) and other servers as "Decode nodes" (their only job is generating the output tokens). Once a Prefill node processes an Aider Repo Map, it transfers the KV cache over the network to the Decode node to stream the answer.   Note: Splitting a single model across multiple physical machines requires extremely fast networking (e.g., InfiniBand or 400GbE RoCE). Standard Gigabit Ethernet is too slow for Tensor Parallelism between physical servers.Part 2: Routing N Clients to N Servers (Load Balancing)If you simply want to increase your capacity to handle many developers (N clients) at once, you run identical copies of your model across multiple independent servers (Data Parallelism).To the clients, there should only ever be one API endpoint. You accomplish this using a router.The SGLang Model Gateway (Router):SGLang has a built-in router (sglang-router) that sits in front of all your GPU servers. You launch your GPU workers, and then launch the router on a head node.   Bashpython -m sglang_router.launch_server --host 0.0.0.0 --port 30000 --dp-size 4
All of your Tauri/Aider clients simply point their OPENAI_API_BASE to this single router IP.Cache-Aware Routing (The Secret Weapon):If 10 developers are working simultaneously, their prompts are huge. SGLang's router uses RadixAttention cache-aware load balancing.When Developer A sends their codebase map, the router sends it to Server 1. When Developer A asks a follow-up question, the router remembers that Server 1 already has Developer A's codebase in its KV cache, and routes the request back to Server 1. If Developer B logs in, the router sends them to an idle node, like Server 2.   External API Gateways (LiteLLM):If you don't use the built-in SGLang router, the industry standard for this is LiteLLM. You run an NGINX-like container called LiteLLM Gateway that receives all API calls and load-balances them across your cluster of SGLang servers using round-robin or lowest-latency routing.
