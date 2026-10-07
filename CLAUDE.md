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
* **Branching and releases:** work happens on `dev`. A PR from `dev` into `main` is a release: its title prefix sets the version bump (`[bug]`/`[fix]` patch, `[feature]` minor, `[major]` major; `[docs]`, `[ci]`, `[chore]`, `[test]`, `[refactor]` don't release). See "Releasing" below.
* **Toolchain on the server node:** `cargo` is in `~/.cargo/bin` (not on the non-interactive `PATH`), so use `export PATH=$HOME/.cargo/bin:$PATH`. `uv`/`uvx` are in `~/.local/bin`.
* **Versioning:** the backend image tag equals the workspace version in `Cargo.toml`. The release workflow bumps `Cargo.toml`, `Cargo.lock`, `app/package.json`, `app/package-lock.json` and `app/src-tauri/tauri.conf.json` together (`scripts/bump_version.py`); don't bump by hand.
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
| `oppx` client CLI | Working: `pair` (certificate pinning), `status`, `unpair`, `aider` (local Aider through a pinned loopback proxy), `proxy`, `servers`, `use` |
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
sudo apt install ../target/release/bundle/deb/Openphalanx_0.3.0_amd64.deb
```

### 2. Choose a model

Open **Models**:

* **Context window** (slider at the top) is a server setting: the most tokens one request can use, passed to SGLang when the server starts. It doesn't filter models; it changes each model's KV-cache need, so the VRAM column and badges follow it. The panel shows how far the selected model can go on this GPU.
* The table lists Qwen (2.5 Coder, 3 Coder, 3.6), Gemma 4, gpt-oss and Devstral, filterable by family or by "only models that fit". Official repos, plus a few widely used 4-bit **Community** quantizations (labelled with who made them) where the publisher ships no build that fits 24 GB.
* The table shows each model's VRAM need at the chosen context window, with a **Fits / Tight / Won't fit** badge measured against your GPU's free memory, and the longest context that would fit. The need is deliberately conservative and much larger than the download size. It covers weights as loaded, KV cache for the full context plus 25%, and runtime memory. **Best fit for this GPU** marks the largest model that fits with headroom.
* Click **Download** on a model that fits. The default, Qwen2.5-Coder-14B-Instruct-AWQ, is already selected, and it is reused if it is in `~/.cache/huggingface`.
* To use your own model, enter a local folder (with `config.json` and `.safetensors`) or a Hugging Face URL under **Add your own model**. Its VRAM need is checked before anything downloads.

### 3. Start

On **Server**, check that the pre-flight list is green, then press the power button:

* The first start builds the backend image: Docker downloads the official SGLang image (about 16 GB, once) and the app adds its gateway on top (about a minute). Loading the model then takes about 3–4 minutes.
* The running model (name, quantization, context) shows under the headline with a lock, and as **Running** on Models; it's fixed until the server stops (the tooltip says so). The context comes from the container's `CONTEXT_LENGTH`.
* The button turns green when SGLang is ready, and a **pairing code** appears. Use it to pair a client. Codes are single-use and expire after 10 minutes, but paired devices stay paired.
* **Logs** shows live SGLang output. **Devices** lists paired clients and lets you revoke them.

Start is disabled whenever free VRAM is below what the selected model needs. Close other GPU programs, or choose a smaller model or context length.

## Run the backend without the GUI

Useful on a headless machine or while developing the backend. Build the image from the repo (the app does the same from the copy it carries), then run it; the container serves the model in `MODEL_PATH` (a Hugging Face repo id or a path inside the container):

```bash
docker build -f docker/Dockerfile.server -t openphalanx-backend:dev docker/
export ADMIN_TOKEN=$(openssl rand -hex 32)
mkdir -p ~/.local/share/openphalanx/backend-state

docker run -d --name openphalanx-backend --gpus all --ipc=host \
  -p 9090:9090 -p 127.0.0.1:9091:9091 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v ~/.local/share/openphalanx/backend-state:/state \
  -e ADMIN_TOKEN \
  openphalanx-backend:dev

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
oppx --update                                 # update oppx (release download, or source pull + rebuild) and its engine
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
* **Chat frontend** (`client/oppx/frontend/`: the `oppx_chat` package plus `run.py`, embedded with `include_str!` via `agent::FRONTEND`):
  * **Modules**, lowest layer first; each imports only from the ones above it (the map is in `oppx_chat/__init__.py`): `config` (environment from oppx), `term` (output, spinner, Esc), `render` (streamed answers, diffs), `context` (`ContextManager`), `agent` (the agent engine; no UI imports), `oppx_io` (Aider's IO, prompt, keys), `routing` (ask/edit check), `cache` (prefix-cache patches), `commands` (slash commands, memory), `app` (Aider patches, turn loop, `main`). A new module must also be listed in `agent::FRONTEND`; a unit test checks this.
  * **Engines** (`oppx --engine agent|aider`, `OPPX_ENGINE`; default `agent`):
    * **`agent`** (`oppx_chat/agent.py`, no UI code; `app.make_agent`/`agent_turn` connect it): the model reads the repository on demand, like Claude Code, instead of being sent whole files and a repo map with every request. Each step it picks one action as **JSON constrained by a schema** (model-agnostic, no tool-call parser): `list`, `grep` (`git grep`), `outline` (Aider's tree-sitter tags, regex fallback), `read` (line ranges; files up to 300 lines whole, ±6 lines around a range), `edit`, `create`, `run` (asks y/N), `web_search` (`/v1/search`), `answer`. Text payloads are then written free-form in a second call: edits as Aider SEARCH/REPLACE blocks applied with Aider's fuzzy `do_replace`, the reply streamed. Calls carry `oppx_utility`, so the gateway's search router stays out.
    * **Guards for small models** (each found in the evaluation): exact repeats and re-reads of lines already shown are refused, and after two only `answer` is allowed; reading a big file page by page gets a "grep instead" hint; answering from grep hits alone, or ending a change request with nothing changed, gets one nudge; before answering a change, the model is told which files it actually changed (it claimed edits it never made); a file must be read before it can be edited; an ambiguous SEARCH (matching several places) is refused with the line numbers; a failed SEARCH returns the closest real lines; an edit that changes the `{}()[]` balance gets a warning; an empty edit from a reasoning model is recovered from `reasoning_content`. Files named in the message are opened up front (small whole, big as an outline); `/add` and `@file` files are listed for the model.
    * **Context:** the conversation only grows by appending, so SGLang's prefix cache serves most of each request; near the limit, old tool output is cut first (the latest 6 kept), then the oldest turns. The system prompt (rules, top-level folders, `OPENPHALANX.md`/`AGENTS.md`) is built once per session.
    * **`aider`**: Aider's own loop, with everything below (context manager, cache patches, ask/edit routing). Slash commands, `/undo`, sessions, the status bar and Esc work in both.
    * **Measured** (RTX 3090, this repo at v0.4.1): a typical Aider request was ~25k tokens, ~60% whole files the model had mentioned and ~30% repo map, with 8–19 s to the first answer and a 42% cache hit rate. The agent's questions use 2–14k-token requests with 76–93% served from cache, answering in 2–6 s. Edits (`scripts/eval_edits.py`, 6 tasks checked by compiling and testing, 3 runs): Qwen2.5-Coder-14B-AWQ **agent 18/18, Aider 15/18** (Aider can't do the multi-file rename); gpt-oss-20b **agent 5/6 three times, Aider 4/6**.
  * **How it runs:** `oppx` writes the package to `~/.cache/oppx/frontend-<version>-<hash>/` and runs `run.py` with the Python of the Aider venv (the `python` next to the real launcher, or its shebang), so `aider`, `prompt_toolkit` and `rich` are importable.
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
    * Commands: `/clear /compact /cost /context /status /model /init /memory /review /export /doctor /vim /resume /undo /exit`, (`/undo` restores the files the last editing turn changed, once; nothing is committed), plus Aider's `/add /drop /run /test /lint /web` and our `/search`. Other Claude-only commands report "not available".
  * **Esc interrupt:** `EscWatcher` holds the terminal in cbreak mode during a turn and turns a lone Esc into SIGINT. It pauses whenever a question needs an answer. Keystrokes typed during a turn are kept: finished lines are queued as the next messages, and a partial line is pre-filled.
  * **Interrupts:** Aider's `keyboard_interrupt` (which exits on a double press) is replaced. A mid-stream interrupt is detected from the "I see that you interrupted…" note Aider records; check the coder returned after `SwitchCoder` too.
  * **Ask/edit routing:** each normal message goes through a one-word `ask`/`edit` decision by the server's model through the proxy (`routing.ask_choice`). It's one regex-constrained token on regular models (about 40 ms, 16/16 on probes, including text-only requests such as essays). On **reasoning models** (`/v1/info` `reasoning`, passed as `OPPX_REASONING`), SGLang applies the regex only after the reasoning, so it's a short free answer at low effort (`chat_template_kwargs` `reasoning_effort: low`, `enable_thinking: false`) instead, and the first allowed word counts. That scores 16/16 on gpt-oss-20b in about 260 ms. These calls carry `oppx_utility: true`, so the gateway never routes or searches them. Questions run as `/ask`, so they can't edit; a file named in an answer is added to the chat but doesn't trigger Aider's "I added these files" follow-up request (it made the model answer twice). Without it, Qwen-14B in diff mode deleted `mul` when asked "what does mul return?". The gateway skips the web-search router for requests that carry `regex` or `response_format`.
  * **Context manager** (`oppx_chat/context.py`):
    * **Hooks:** it wraps `Coder.format_messages`, so every request Aider builds is fitted first.
    * **Budget:** `(context − 4096 reserved for the answer) / factor`. The factor covers the gap between Aider's generic token count and the model's real tokenizer: 1.10 until calibrated (26,065 tokens at 32k), then the measured ratio × 1.03. After each answer, `ContextManager.calibrate` counts the request it just sent with the server's tokenizer (`POST /v1/tokenize` via the proxy, in a background thread) and keeps a running average. On Qwen2.5-Coder the ratio is about 1.01, giving 27,541 tokens.
    * **Making room, cheapest loss first:**
      1. summarize the conversation (`summarizer.summarize_all`);
      2. set aside files the model requested that are least recently used;
      3. halve the repo map down to 1,024 tokens;
      4. set aside older files you added or that were edited.

      Files used in the current turn are never set aside. Set-aside files stay visible as signatures in the repo map.
    * **Repo map:** capped at 8,192 tokens (`map_mul_no_files=1`). Aider alone lets it grow to about 28k when no files are in the chat, which caused a 46k-token request on this repo.
    * **File requests:** the model's requests go through `confirm_ask`. A single file over 60% of the budget is refused with a reason.
    * **Calibrating early:** an uncalibrated request above 60% of the budget is measured with `/v1/tokenize` before it's sent. The 1.10 guess was 20% short on Qwen3.6, so its first large request failed every time and nothing ever calibrated.
    * **Server refusals:** SGLang's "Input length (N tokens) exceeds the maximum allowed length (M tokens)" (and "longer than the model's context length") is classified as a context overflow (`_get_ex_info`), not a connection error, which Aider retried for about a minute with nothing on screen. The numbers set the ratio and the server's limit, and `run_turn` refits and resends once. `--allow-auto-truncate` is deliberately not used: SGLang would cut the prompt's start (system prompt, repo map) without telling the agent.
    * **Final guard:** `check_tokens` is replaced, so a request is never sent over the limit, and Aider's "proceed anyway / providers won't charge" text is suppressed.
    * **Visibility:** `/context` shows the breakdown, and the status bar shows the percentage used.
  * **Progress display:** one `TurnSpinner` runs from Enter to the end of the turn. It's paused by any output (`out()` and the spinner share `OUT_LOCK`) and restarted on each wait; Aider's `WaitingSpinner` is mapped onto it. `Coder.show_pretty` is forced to True. Aider disables streaming and its spinner whenever it picks a non-```` ``` ```` fence (any Markdown file with code blocks in the chat), which looked like a freeze. The edit-block hider understands every fence Aider may pick: ```` ``` ````, four backticks, and `<source>`, `<code>` and `<pre>` tags.
  * **Editing safeguards:**
    * Aider's `--no-suggest-shell-commands` is always set. Aider otherwise offers to run any `bash` block in a reply, even quoted file content.
    * A change request that yields no edit gets one model-agnostic retry in `whole` format (`_whole_file_retry`; small files only, output hidden, diff shown), then a "No changes were made" notice.
    * The content before an edit comes from `OppxIO.write_text`.
  * **Questions on strict models:** questions reuse the edit prompts (for the cache), so `cache.QUESTION_RULE` adds a question exception to the system prompt and reminder. gpt-oss ranks system rules over the note in the message and otherwise answered questions with edit blocks. Aider's few-shot examples go in the system prompt (`examples_as_sys_msg`); sent as turns, gpt-oss took them for the real conversation.
  * **Server errors** (`oppx_io.server_problem`): litellm's errors and Aider's provider hints ("Check your API key") become one plain message per turn: revoked (pair again), starting up or busy, unreachable, or certificate changed. A refused turn skips the whole-file retry, and Aider's "Retrying in N s" lines are hidden.
  * **Model info:** `oppx` reads `GET /v1/info` at start-up and passes `OPPX_MODEL_ID` (shown in the welcome box, status bar and `/status`) and a `--model-settings-file` with the server's `edit_format` and `use_repo_map: true` (Aider otherwise assumes `whole` and no repo map for our model name). Reasoning output is hidden by `hide_reasoning`, and the spinner keeps going while the model reasons. That covers `<think>` spans, a lone `</think>`, Aider's "► THINKING / ► ANSWER" markers, and Aider's internal `<thinking-content-…>` tag: with SGLang's separate `reasoning_content`, Aider 0.86 opens with that tag but closes with the configured one. `OppxIO.ai_output` strips reasoning from the session file too, so `-c`/`-r` don't resend it.
  * **Memory:** `OPENPHALANX.md` (and `AGENTS.md` if present) in the repo root is loaded read-only every turn. `/init` asks the model to write it.
  * **Sessions:** one file per conversation in `~/.local/state/oppx/history/<repo>-<id>/<YYYYmmdd-HHMMSS>.md`, with a shared `input.history`. The old single per-repo file is migrated as session `00000000-000000`. `-c` and `-r` pass `--restore-chat-history`.
  * **Prefix-cache friendliness** (`oppx_chat/cache.py`; measured with `scripts/bench_session.py`, milestone Step 5.2). SGLang only reuses an unchanged prompt prefix, and two things used to change near the start of every request, giving 3–7% cache hits over 10 turns:
    * **Repo map:** Aider re-ranked it around the chat files and the names each message mentions. It's now ranked once for the whole repo and cached per (file list, map size). The context manager's map size only shrinks within a session (reset by `/clear`).
    * **System prompt:** questions ran in Aider's ask mode, which has its own prompts. `AskCoder` now gets the editing coder's prompts, and the question carries `ASK_NOTE`; ask mode never applies edits. The map heading's `{other}` word is fixed too.

    Result: 50% of prompt tokens served from cache over the same 10 turns, and follow-up questions start answering in 0.6 s instead of 4.8 s. The rest is inherent: new or edited files, files moving into the history after the first edit, and compaction. `OPPX_DUMP_REQUESTS=<file>` writes every request as JSON lines to find where consecutive requests diverge.
  * **Testing:** drive it in a pty (Python `pty.fork`, 120×40 via `TIOCSWINSZ`). The driver must **answer cursor-position requests** (`ESC[6n` → `ESC[30;1R`), or prompt_toolkit never draws the status bar. Render the raw bytes with `pyte` to see the real screen.
* **Coding engine** (`client/oppx/src/engine.rs`): users never install Aider.
  * `oppx` keeps a private engine in `~/.local/share/oppx/engine/` (`OPPX_ENGINE_DIR` overrides): `bin/uv`, a uv-managed Python 3.12 (`UV_PYTHON_PREFERENCE=only-managed`, so a system 3.13 can't break it), and `aider-chat==agent::AIDER_VERSION` installed with `uv tool install` into `tools/`. About 740 MB.
  * **Lookup order:** the private engine at the pinned version, then an `aider` on `PATH` at exactly the pinned version (handy on dev machines), otherwise install the private one (`engine::ensure`). Versions are read from the venv's `aider_chat-X.Y.Z.dist-info` folder, not by importing Aider (2 s).
  * uv is reused when present (the private copy, `PATH`, `~/.local/bin`, `~/.cargo/bin`); otherwise Astral's installer runs with `UV_UNMANAGED_INSTALL` into `engine/bin`, with no shell-profile changes.
  * The frontend runs on the venv's `python` next to the real launcher. uv writes a `#!/bin/sh` trampoline instead of a Python shebang when the path is long, so the shebang alone isn't reliable.
  * To test a first install, run with `HOME` pointing to an empty folder and `PATH=/usr/bin:/bin` (verified: about 7 s on this network, uv download included).
* **`--update`** (`client/oppx/src/update.rs`) has two modes:
  * **Release builds** (`OPPX_RELEASE_TARGET` set at build time by the release workflow): read GitHub's latest release, download `oppx-<target>.tar.gz`, check it against `SHA256SUMS`, unpack next to the current binary and rename over it. A 404 means no release yet.
  * **Source builds:** work on the checkout `oppx` was built from (`CARGO_MANIFEST_DIR/../..`), and refuse if it has local changes. Fetch and fast-forward, then rebuild with `cargo install --locked --path client/oppx` when anything was pulled, or when the commit embedded at build time (`build.rs`, shown in `oppx --version`) differs from HEAD. To test in isolation, use a scratch clone with a local bare remote and `CARGO_INSTALL_ROOT` set to a scratch folder.
  * Both modes then install or refresh the private engine.
* **Stand-in agents:** a fake `aider` script on `PATH` (printing its args and env) is a quick way to test `oppx aider` without the real agent.

## Cluster

Servers on one network form a cluster: one **host** and its **members**. They don't share inference yet (see `milestone.md`, "Next version"). The service is `cluster::Cluster` in core. It runs inside the app, or headless as `openphalanx-server run` (`crates/openphalanx-server`); both use `~/.local/share/openphalanx/cluster/`, so run one per machine.

* **Discovery:** every server broadcasts a beacon (id, name, role, certificate fingerprint) on UDP `9093` every 3 s. A server is listed under **Servers on this network** only when its beacon is under 10 s old *and* a TLS health check, pinned to the fingerprint it announced, succeeds. Stopped or unreachable servers drop off within about 10 s. Broadcast only crosses one LAN segment, not Tailscale.
  * **Fast join:** a server that starts (app opened, or `openphalanx-server run`) sends a query beacon twice; the others answer by unicast at once and check it immediately, so it is listed in about 0.3 s (measured on the 4090) instead of waiting for the next 3 s round.
* **Roles** (`state.json`): standalone, host or member. A host with no members is standalone again.
  * **Clients pair with the host only:** members hide the pairing section, refuse `new_pairing_code`, and skip the start-up pairing code.
* **Joining needs consent on both sides:**
  1. **Add to cluster:** the host sends an invitation (a single-use secret bound to that server, valid for 10 minutes).
  2. **Approve on the invited server:** in its app, or with `openphalanx-server approve`, after checking the host's fingerprint. It then joins over a connection pinned to that certificate and gets a 256-bit member token; the host stores only its hash.
  3. **Trusted hosts:** a server remembers the hosts it approved, and accepts their invitations without asking next time.
* **Make host (hand-over):**
  * The current host asks a server (on the network, or one of its members) to take over. That server approves ("Become host") and invites everyone in the request.
  * The old host and its members accept automatically: the old host asked for it, and members learn the new host's fingerprint from the old host in their report replies.
  * Clients must pair with the new host.
* **Strategy** (`cluster::Strategy`, the host decides; members show the host's choice from report replies):
  * **Split model** (default): one model across the servers. Bigger models, about single-GPU speed, needs every server up and a wired network.
  * **Replicas:** a full copy per server. More concurrent users and resilience, no bigger models.

  It's colour-coded in the cluster panel (violet / blue) with an ⓘ tooltip; `openphalanx-server strategy split|replicas` does the same.
  * **Locked while serving:** `set_strategy` refuses a change while this server is serving ("Stop the server before changing the strategy"), and the panel shows the other option faded with a lock.
  * **Pooled VRAM (split):** on a host with online members, the Models page measures fits against the sum of each server's free VRAM (its GPU with the most free memory, as last reported), and every server's runtime memory is added to the need (`vram::split_across`). A donut (`VramPool.svelte`) shows each server's share and the selected model's need. The pre-flight VRAM check follows the split plan (`plan_split` in the app).
* **Split serving** (cluster step 3, `split.rs`; pipeline parallel with SGLang `--pp-size N --nnodes N`):
  * **When:** the host, with the split strategy and online members, starts a model that doesn't fit its own GPU. A model that fits runs on the host alone: faster, and it keeps serving if a member drops out.
  * **Plan** (`split::plan`): layers are shared out in proportion to each server's free VRAM after its fixed costs (runtime memory on every server, the input embedding on the first, the output head on the last), at least one each. Layer count and embedding size come from the model's `config.json` (`text_config` for multimodal models). Passed as `SGLANG_PP_LAYER_PARTITION`. SGLang takes the minimum KV capacity across ranks, so each server sizes its own `--mem-fraction-static` from its stage.
  * **Rank 0 is the host's normal backend** on the host's network (`--network host`; NCCL and torch connect back to the address each rank announces, which a bridge would hide). The gateway then binds the admin API to loopback itself (`ADMIN_HOST=127.0.0.1`), SGLang moves to `9096`, and SearXNG is published on `127.0.0.1:9098` only.
  * **Members run a worker:** the host puts a `WorkerOrder` (run id, model, rank, partition, `host:9100` rendezvous, stage) in each member's report reply. The member starts `openphalanx-worker` (the same image with `start-sglang.sh` as entrypoint, no gateway, host network, weights read-only) with its own GPU and LAN interface (`NCCL_SOCKET_IFNAME`/`GLOO_SOCKET_IFNAME`), and reports `starting`/`running`/`failed`. An order without a worker, or leaving the cluster, stops it. Its log goes to the host's Logs page prefixed `[split worker]`.
  * **Before starting** the host checks every member: NVIDIA runtime present, and the model synced ("still downloading (45%)" otherwise). Members need the backend image too; the worker fails with a clear message if it's missing.
  * **While serving:** a member dropping out pauses serving; a failed worker stops it with that member's reason. Stop clears the orders, so workers stop too. The Server page's model pill shows the split ("rig-3090: 40 layers (…) · laptop-4090: 24 layers (…)").
  * **Runtime memory per stage:** each server is charged runtime memory for the weights it holds (`vram::runtime_overhead`), not the whole model's. Charging the whole model's to each left Qwen3.6-27B with a 30.9k-token KV cache at a 32k context and 5–6 GB unused per GPU. `/v1/info` also reports `min(context, SGLang's KV tokens)` as `context_length`, so clients budget against what the server can actually hold.
  * **Same settings on every rank:** `--chunked-prefill-size 2048` (`docker::SPLIT_PREFILL_CHUNK`; SGLang otherwise picks 4096 on a 24 GB card and 2048 on 16 GB, and the smaller rank crashed reshaping a 4096-token chunk), and the model's `dtype` override if the catalog has one.
  * **Ports between the servers:** `9100` and the next 16 on the host, plus NCCL's ephemeral ports both ways; keep the servers on one LAN without a firewall between them.
  * **Trusted LAN only:** the rendezvous (`9100`) and NCCL/gloo traffic aren't authenticated, and SGLang passes objects between ranks with `pickle` (`broadcast_pyobj` in `sglang/srt/utils/common.py`). Anyone who can reach those ports while a split model starts or runs can potentially execute code on the servers. That's inherent to multi-node SGLang; the cluster panel's Split tooltip says so.
  * **Planning by hand:** `cargo run -p openphalanx-core --example split_plan -- rig-3090=22.1 laptop-4090=15.5` prints the plan for the selected model and context (the host first, free GiB per server).
  * **Verified by hand** (containers with the app's arguments, 3090 + 4090 laptop on gigabit Ethernet): Qwen3-8B-AWQ 20/16 layers, about 100 tokens/s, a 6.1k-token prompt in 2.3 s; Qwen3.6-27B-AWQ 42/22 layers at 32k context (18.6 of 24 GB and 11.0 of 16 GB used), correct on a 16.8k-token prompt in 13.7 s. The full app flow (Start on the host, worker orders, pause) is still to be run.
* **Model sync:**
  * The cluster's model follows the host's selected model (`set_desired_model` on select, on every pre-flight check and on Start; repo plus pinned commit; local-folder models are skipped). The host sends it to members in every report reply.
  * A member that already has it (the app's models folder or the HF cache) reports "ready"; otherwise "missing". **It downloads only after the user agrees on the host:** the Server page asks "laptop-4090 doesn't have … (20.4 GB). Download it there?", and `approve_download` lets members fetch it (verified, resumable), reporting progress and errors (retried). Choosing another model resets the approval.
  * Start waits up to 8 s for members to report on a newly chosen model before checking the split, so a fresh selection doesn't fail on a stale report.
  * The Machines table shows each member's status; headless, use `openphalanx-server model <catalog-id>|none`.
  * Verified: the 4090 downloaded Qwen2.5-Coder-7B-AWQ (5.2 GB, about 92 MB/s, verified, marker written), and reported "already on this machine" after a restart.
* **Member logs:** members queue log lines (their backend's `docker logs` since the last report, plus cluster events such as joins and downloads) and send up to 400 per report. The host keeps 3,000 per member. The Logs page has a machine dropdown; headless, use `openphalanx-server logs <member>`.
* **One controller (start guard):** whoever presses Start first becomes the controller; the other side can't start too.
  * `Cluster::claim_start` runs before pre-flight. A standalone server or host just marks itself serving (refused while it is handing control to someone). A member first takes the host role over through `POST /cluster/v1/take-over`, which the host refuses with 409 while it is serving or already handing over. Both checks happen under one `Control` mutex on the host, so two simultaneous presses can't both win.
  * The app's monitor keeps `set_serving` in step with the backend (starting or running). The host sends `host_serving` in report replies; a member then has `locked_by_host` in its view, and its power button turns violet with a lock and "<host> is running the cluster". All it can do is leave the cluster (or close the app).
* **Split pause:** with the split strategy, a host that is serving stops its backend when a member has said goodbye or hasn't reported for 12 s (`dropped_members`), and the Server page shows "Paused: <member> dropped out of the cluster…" (state `paused`, red). Start again once it's back, or remove it. Members send `POST /cluster/v1/bye` when the app exits (`RunEvent::Exit`) or `openphalanx-server` gets Ctrl-C, so a clean exit pauses at once.
* **Members report** to the host every 5 s (`/cluster/v1/report`): inventory (CPUs, RAM, GPUs with load and temperature, Docker/NVIDIA runtime) and, if their backend runs, what it serves (model, gateway and inference metrics). The host shows a member offline after 20 s without a report. A member that gets 401 (removed, or the cluster was dissolved) becomes standalone.
* **App:**
  * **Cluster panel** on the Server page: pending invitations and host requests (Approve/Decline, with the sender's fingerprint), this server's role with Rename/Leave/Dissolve, and the servers on the network (Add to cluster / Make host).
  * **In this cluster** cards: the host has a red **Remove** on each member, a member has **Leave** on its own card. A removed server shows up under "Not in this cluster" again, and **Add to cluster** brings it back without asking there (it trusts the host it approved before).
  * **Machines table:** every server, with Make host/Remove for members.
  * **Charts** show **one machine at a time** (a dropdown); the tiles sum the cluster.
* **Headless control:** `openphalanx-server status | servers | invite <name> | make-host <name> | approve [<name>] | decline <name> | remove <name> | leave | dissolve`. These talk to the running server on `127.0.0.1:9094` with a per-start token in `control.token` (0600). Use `run --name <name>` to set the display name; it's kept.
* **Verified** between the 3090 (app and headless) and the 4090 laptop (headless), on the wired LAN:
  * discovery in both directions; a non-server machine (the HP laptop) never listed;
  * invite and approve; membership surviving restarts of both;
  * reports with the 4090's GPU;
  * hand-over (the 4090 became host, and the 3090 moved over by itself);
  * dissolve; a stopped server gone from the list within 12 s.

## Web search

* **SearXNG:** the `openphalanx-searxng` container is pinned by digest and runs on the private `openphalanx` Docker network with **no published ports**. Its settings live in `crates/openphalanx-core/searxng/settings.yml` and are written to `~/.local/share/openphalanx/searxng/settings.yml` (0644, read-only mount, `FORCE_OWNERSHIP=false`). The secret is passed as `SEARXNG_SECRET`, and `--log-driver none` keeps queries out of logs. The GUI starts it before the backend when `settings.web_search` is on (the default; toggle on the Server page). The backend gets `SEARXNG_URL=http://openphalanx-searxng:8080`.
* **Gateway:**
  * `POST /v1/search {query, max_results}` returns `{results: [{title, url, snippet}]}`.
  * A chat request with `X-Oppx-Web-Search: auto` (sent by `oppx` unless `--no-web`) goes through the **router**, in two steps:
    1. **Decide:** a `yes`/`no` call to the same model (`gateway.classify`: one regex-constrained token, or a short free answer on reasoning models; see the ask/edit routing above). It reads the user's **whole latest turn**, every user message since the last assistant message, minus any paragraph that repeats the system prompt: Aider appends its editing rules to each message, and the router read those instead of the question. The few-shot examples sit in a fixed system prompt, so they're cached. Median about 50 ms (about 260 ms on gpt-oss); 16/16 correct on mixed phrasings for both.
    2. **Query:** only on `yes`, a call writes the search query (constrained JSON, or free text on reasoning models). It's told today's date, and `drop_invented_dates` removes dates and years the model added that the user didn't write. gpt-oss stamped queries with "2024-10-04" or today's date and found stale pages.
    3. **Results** say when they were retrieved, that they beat training data for things that change (versions, releases), and that links can't be opened. gpt-oss otherwise tried to browse and returned nothing.
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
| `9090/tcp` (all interfaces) | Public gateway API, TLS only. `/health` and `/v1/pair` are open. `/v1/whoami`, `/v1/unpair` (self-revoke), `/v1/info` (model id, context, edit format), `/v1/tokenize`, `/v1/search`, `/v1/models` and `/v1/chat/completions` (OpenAI-compatible, streaming) need a device token |
| `9091/tcp` (`127.0.0.1` only) | Admin API for the GUI; needs the per-launch admin token |
| `9092/tcp` (all interfaces, while the app or `openphalanx-server` runs) | Cluster API between servers (TLS, the server's own certificate). `health`, `invite`, `host-request` and `join` (needs an invitation secret) are open; `report` and `leave` need a member token |
| `9093/udp` (all interfaces) | Discovery beacons (LAN broadcast) |
| `9094/tcp` (`127.0.0.1` only) | `openphalanx-server` control API; needs `control.token` |
| `9096`, `9098` (`127.0.0.1` only), `9100`–`9116/tcp` and NCCL's ephemeral ports | Split model only: SGLang and SearXNG on the host's network, and the rendezvous and traffic between the ranks |
| `~/.local/share/openphalanx/cluster/` | This server's certificate (`head.crt`, `head.key`, 0600), identity, role and trusted hosts (`state.json`), and members as host (`members.json`, hashed tokens). Deleting it resets the server's cluster identity |
| `~/.config/openphalanx/settings.json` | Selected model, context length, GPU index, custom models |
| `~/.local/share/openphalanx/models/` | Models downloaded by the GUI (verified, pinned to a commit) |
| `~/.local/share/openphalanx/backend-state/` | Backend TLS certificate and paired devices (hashed tokens). Deleting it unpairs every client and changes the fingerprint |
| `~/.local/share/openphalanx/searxng/settings.yml` | SearXNG settings (no secrets), mounted read-only |
| `~/.cache/huggingface/hub/` | Existing Hugging Face cache; reused read-only when a model is already there |

Open `9090/tcp` in your firewall for the clients' network, and `9092/tcp` plus `9093/udp` between cluster servers. Never expose `9091` or `9094`.

## Security model

* **TLS:** the public API uses a self-signed certificate created on first start. Clients pin its SHA-256 fingerprint when they pair (shown in the GUI), so later connections can't be intercepted.
* **Pairing codes:** 8 characters, about 39 bits. Each is single-use, valid for 10 minutes, and burned after 5 wrong attempts, with a 1 s delay after each miss.
* **Device tokens:** each client trades a pairing code for its own random 256-bit device token. The server stores only the SHA-256 hash, and you can revoke any device from the GUI.
* **Pairing expiry:** pairings last 1 day, 1 week (default), 1 month, 1 year or never (Client tab; `settings.pairing_ttl_days`). The app passes it as `DEVICE_TTL_DAYS` at start and changes it live with `PUT /admin/pairing-policy`; it applies to every device, counted from when it paired. Devices paired before expiry existed count from the upgrade (`since` in `backend-state/pairing_policy.json`). An expired token gets a 401 "pairing expired"; `oppx status`, the chat and `oppx search` say to pair again, and `oppx status` shows how long the pairing has left.
* **Admin API:** the admin API is reachable from the server machine only, and requires a random token generated at each launch.
* **Web search:** SearXNG is reachable only from the gateway (private network, no ports) and keeps no logs. Automatic search is on by default (the user's choice; `--no-web` opts out). Router-written queries leave the server for public search engines, so they can carry fragments of a request. Results are injected as untrusted reference text.
* **Safety bars** (gateway, any client):
  * forces `n=1` and drops `best_of`, and clamps `max_tokens` to half the context;
  * answers `413` for bodies over 8 characters per context token, before SGLang sees them;
  * allows at most 4 requests in flight per device and 8 server-wide, queueing for up to 120 s before a `503` with `retry-after`;
  * limits each device to 120 requests a minute (`429`).

  The settings live in `gateway.py`, and the `MAX_ACTIVE_REQUESTS`, `MAX_DEVICE_REQUESTS`, `QUEUE_WAIT_S` and `RATE_PER_MINUTE` variables override them. Slots are released through an idempotent cleanup that also runs as the response's background task, so a client that disconnects before streaming starts can't leak one. SGLang itself rejects over-length input with a clean 400 and queues overload: 12 concurrent 20k-token requests all completed.
* **Split model:** needs a trusted LAN; the traffic between ranks is unauthenticated and pickle-based (see Cluster → Split serving).
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

docker build -f docker/Dockerfile.server -t openphalanx-backend:dev docker/   # the backend image by hand (the app builds its own; see below)
scripts/bench_session.py --oppx target/debug/oppx               # prefix-cache benchmark (in a scratch copy of a repo)
scripts/gen_docs.py --oppx target/debug/oppx --out target/docs  # docs sources; then: mdbook build target/docs
OPENAI_API_BASE=… OPENAI_API_KEY=… scripts/probe_routing.py     # via `oppx proxy --no-web`; run after any model change
OPENAI_API_BASE=… OPENAI_API_KEY=… scripts/probe_kv.py          # long-context recall + coding tasks run against tests (e.g. KV dtype checks)
scripts/eval_edits.py --oppx target/debug/oppx --engine agent   # 6 edit tasks in fresh clones, checked by compiling/testing; compare engines
~/.local/share/oppx/engine/tools/aider-chat/bin/python scripts/bench_agent.py --repo <scratch clone>   # agent turns headless, steps and tokens
```

The backend image tag follows the version in the root `Cargo.toml`, so bump both together when changing `docker/`. The base SGLang image is pinned by digest in `docker/Dockerfile.server`.

## Model catalog and VRAM estimate

* **Adding models:** write a spec (id, name, family, params, quant, and `quantized_by` for community builds), run `scripts/catalog_entry.py specs.json`, and paste the entries into `catalog.json`. `scripts/catalog_entry.py --check` verifies every entry's weight size, attention shape and context against Hugging Face.
* **Release date** (`released`, shown as a year on the Models page): the repo's creation date on Hugging Face; community quantizations take their base model's date (`base_model` in the spec).
* **`dtype`** (optional, passed as SGLang `--dtype` to every rank): for checkpoints whose declared dtype SGLang can't run. The 4-bit Qwen3.6 builds (cyankiwi 27B, QuantTrio 35B-A3B) declare float16, but SGLang keeps the Gated-DeltaNet state in bfloat16 and the first prefill failed with "Index put requires the source and destination dtypes match"; they use `bfloat16` (verified on the 27B; the 35B-A3B by analogy).
* **FP8 KV cache** (`--kv-cache-dtype`), measured with `scripts/probe_kv.py`: not enabled for any model. On the 3090 (Ampere) only `fp8_e5m2` runs (`fp8_e4m3` fails to compile in Triton on sm_86). It gives gpt-oss-20b a 128k context on one card (199k KV tokens), but costs accuracy: recall at 24k tokens 1/3 against 3/3 with 16-bit KV, coding 4/5 against 5/5. Qwen2.5-Coder-14B gains nothing: its native window is 32k. Worth re-checking `fp8_e4m3` on Ada (4090) and on Qwen3.6, as a per-model catalog field only if it passes.
* **Check the architecture first:** the pinned SGLang must have the model class (`sglang/srt/models/` in the image). All current entries were checked against SGLang 0.5.21.
* **Weights counted:** only root-level `.safetensors` (subfolders like gpt-oss's `original/` and `metal/` are skipped), and Mistral's duplicate `consolidated*.safetensors` are dropped when HF shards exist. The downloader applies the same rule.
* **KV cache** (`vram::ArchSpec`), as SGLang allocates it:
  * full-attention layers × context;
  * sliding-window layers (Gemma 4, gpt-oss) × 0.8 of the context (`--swa-full-tokens-ratio`);
  * Gemma 4's full layers use their own shape (`global_head_dim`, `num_global_key_value_heads`);
  * hybrid linear-attention models (Qwen3-Next, Qwen3.6) × 1.9, for the recurrent-state pool (`--mamba-full-memory-ratio` 0.9).
* **Validated on hardware:** Qwen2.5-Coder-14B-AWQ, and gpt-oss-20b (MXFP4 on the 3090, the whole Step 5.1 run). The rest is not yet validated. MXFP4 (gpt-oss) is counted at 4 bits, since SGLang's Marlin and Triton kernels keep it packed on Ampere. Milestone Step 5.3 runs the matrix.

## Releasing

* **CI** (`.github/workflows/ci.yml`, every PR and push to `dev`/`main`): app build and type check, `cargo clippy -D warnings`, `cargo test` for core and `oppx`, ruff (syntax and undefined names) for the gateway, frontend and scripts, shell syntax, and the PR title check for PRs into `main`. The GPU lifecycle test stays manual.
* **Release** (`.github/workflows/release.yml`, a merged PR into `main` with a release prefix, or run by hand with a bump and a changelog line): `scripts/bump_version.py` bumps every version file and adds the `CHANGELOG.md` entry. The workflow commits "Release vX.Y.Z" to `main`, tags it, fast-forwards `dev` when possible, then starts **Publish** for the tag.
* **Publish** (`.github/workflows/publish.yml`, `workflow_dispatch` with a tag; redo a release with `gh workflow run publish.yml --ref main -f tag=vX.Y.Z`). It always runs on `main` (the `github-pages` environment only deploys from `main`) and checks out the tag for everything it builds:
  1. `oppx` for `x86_64`/`aarch64-unknown-linux-musl` (static) and `aarch64`/`x86_64-apple-darwin`, with `OPPX_RELEASE_TARGET` set; the app as `.deb` and AppImage.
  2. A GitHub release with the archives, `SHA256SUMS`, `install.sh`, `install-server.sh`, and notes from `main`'s `CHANGELOG.md`.
  3. The docs (`scripts/gen_docs.py` + mdBook) on GitHub Pages.
* **No backend image is published.** The app carries the build files (`docker::BUILD_FILES`: the Dockerfile, `gateway.py`, `start-sglang.sh`, `supervisord.conf`, embedded with `include_str!`) and builds `openphalanx-backend:<version>-<hash of those files>` on the first start (`docker::ensure_image`): Docker pulls the official SGLang image, pinned by digest in the Dockerfile, and adds ~45 MB on top. A changed gateway or Dockerfile gets a new tag and is rebuilt, so neither users nor developers run a stale image; older tags of ours are removed after a build. Cluster members build the same way when a split worker starts. A custom `image` setting is pulled instead.
* **Why two workflows:** a tag pushed with `GITHUB_TOKEN` starts no workflow, but a dispatch does. A `pull_request` run can't deploy Pages, because its ref is the PR's merge ref, not `main`.
* **One-time repository settings:** Actions → workflow permissions "Read and write"; if `main` is protected, allow GitHub Actions to push to it; Pages → source "GitHub Actions".
* Lint the workflows locally with `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No permission to use Docker" | `sudo usermod -aG docker $USER`, then log out and back in |
| "Docker has no nvidia runtime" | Install NVIDIA Container Toolkit, then `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| "Port 9090 is already in use" | Another program, or an old backend container, holds the port: `docker ps`, then `docker rm -f openphalanx-backend` |
| Start disabled with "Needs X but only Y of VRAM is free" | Close other GPU programs (`nvidia-smi` lists them), or choose a smaller model or context |
| Backend image build fails | The first start pulls `lmsysorg/sglang` from Docker Hub: check the network and disk space (about 55 GB free). Docker Hub allows 100 anonymous pulls per 6 hours per IP; `docker login` raises that. The full build output is in the app's start error and `docker build` can be rerun by hand (see "Run the backend without the GUI") |
| `oppx aider` crashes with `No module named 'audioop'` / `'pyaudioop'` | An `aider` on `PATH` was installed with Python 3.13, which removed `audioop`. `oppx` only uses a `PATH` Aider at the pinned version; otherwise it uses its private 3.12 engine. Run `oppx --update`, or remove the old Aider |
| Backend stops during start-up | The GUI shows the reason; the full output is on **Logs** or in `docker logs openphalanx-backend` |

## Repository layout

```
app/                     Tauri 2 + Svelte 5 server GUI (Linux)
  src/                   frontend: pages, components, typed command bindings
  src-tauri/             Rust shell: Tauri commands, 2 s status monitor, log streaming
crates/openphalanx-core/ Docker, GPU, VRAM, model catalog, downloads, pre-flight, cluster, split plan (no Tauri, unit-tested)
crates/openphalanx-server/ openphalanx-server: headless server (cluster service + command-line control)
crates/pinned-tls/       TLS pinned to a certificate fingerprint (shared by oppx and the cluster)
  catalog.json           curated models pinned to Hugging Face commits (built with scripts/catalog_entry.py); optional per-model
                         edit_format and reasoning_parser (passed as EDIT_FORMAT, --reasoning-parser)
docker/                  backend image: SGLang + gateway under supervisord (no agent code)
  server/gateway.py      TLS gateway: pairing, device tokens, admin API
client/oppx/             client CLI: config.rs (paired servers, 0600 file), tls.rs (fingerprint pinning),
                         api.rs (gateway calls), engine.rs (private Aider), update.rs, main.rs (clap commands)
  frontend/              chat frontend: oppx_chat/ package + run.py (embedded in the binary)
.github/workflows/       ci.yml (every PR/push), release.yml (merged PR into main -> release)
scripts/probe_routing.py score the search router and ask/edit check against the loaded model
scripts/catalog_entry.py catalog entries from Hugging Face (pinned commit, weight size, attention shape); --check re-verifies
scripts/bench_session.py 10-turn prefix-cache benchmark through a real oppx session
scripts/bump_version.py  version bump from a PR title, plus the CHANGELOG.md entry
scripts/gen_docs.py      docs site sources (README, CLI and API reference, CLAUDE.md, roadmap)
scripts/install*.sh      client and server installers (attached to each release)
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
