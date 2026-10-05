# Milestones

Roadmap and progress. Details of everything finished are in `CLAUDE.md` and the git history.

## Proof of concept: complete (v0.3.0)

Server on an RTX 3090, client validated end to end from a separate laptop over the network (Step 5.1).

* **Backend:** SGLang 0.5.21 (pinned) behind a TLS gateway in one image:
  * Pairing codes, hashed device tokens, admin API on loopback.
  * OpenAI-compatible API, `/v1/info`, `/v1/tokenize`.
  * Safety bars: concurrency, rate and size limits.
  * Prompts never logged; the server stores no code and runs nothing for clients.
* **Server app** (Tauri 2 + Svelte 5, Linux):
  * Docker and VRAM safety: never starts a model that won't fit.
  * Server page: power button, pairing section, cluster-ready dashboard.
  * Models page: 28 catalog models (Qwen, Gemma 4, gpt-oss, Devstral), a context-window slider, a fit estimate that models sliding-window and linear-attention layers, and release years.
  * Client page and Logs; light/dark themes.
* **Client `oppx`:**
  * Certificate-pinned pairing and a loopback proxy (the agent never sees the token).
  * A Claude Code-style chat over Aider's engine, with sessions (`-c`/`-r`), `/undo`, Esc/Ctrl-C, plan mode and memory.
  * Context management with auto-compaction, and prefix-cache-friendly requests (3% → 50% cached).
  * Private coding engine: users never install Aider.
  * `oppx --update`; plain error messages for revoked, starting, unreachable and certificate problems.
* **Web search:** private SearXNG, on by default (`--no-web`). Speculative routing (+27 ms) on the user's own words, with date-safe queries.
* **Model-agnostic:**
  * Per-model differences live in catalog data (`edit_format`, `reasoning_parser`).
  * One-word decisions are regex-constrained, or a short free answer on reasoning models.
  * Reasoning is hidden and never saved.
  * `scripts/probe_routing.py` checks any model: 16/16 on Qwen2.5-Coder-14B and on gpt-oss-20b.
* **Measured:** 25.5k of a 26.1k context budget used on a real repo; 50% prefix-cache hits over 10 turns; one-shot questions in 3–5 s from the laptop.
* **Distribution:** `install.sh` / `install-server.sh`, version bumps from PR title prefixes, CI and release workflows, docs generated on each release.

## Next version

* \[ \] **Cluster** (`feature/multi-gpu`), in steps:
  * ✅ **1. Membership:**
    * Servers discover each other on the LAN and are listed only when running and reachable.
    * The user picks members and the host, with approval on the joining side; clients pair with the host only.
    * Members report hardware and serving stats.
    * Per-machine charts via a dropdown.
    * Headless `openphalanx-server`.
    * Strategy switch (split by default, colour-coded with a tooltip).
    * Members download the host's model when it starts (verified on the 4090).
    * Per-machine logs on the Logs page.
    * Remove and re-add members; servers listed about 0.3 s after they start.
    * Start guard: one controller per cluster (a member's Start takes the host role over, refused while the host serves); members locked out show a violet button.
    * Split strategy pauses serving when a member drops out (goodbye on exit, or 12 s without a report).
    * Strategy locked while the server runs; Models page pools VRAM across a split cluster (donut per server).
    * Verified between the 3090 and the 4090 laptop: invite, approve, hand-over, dissolve, and the drop-off of stopped servers.
  * 2\. Members run backends: the host starts or stops a model on a member, a router on the host spreads requests, and clients see one server.
  * 3\. One model split across servers (pipeline parallel), wired networks only. In progress:
    * Done: layer plan by free VRAM (`split.rs`), rank 0 on the host network, member workers through report replies, split-aware pre-flight, pause/fail handling, per-model `dtype` in the catalog.
    * Verified by hand on the 3090 + 4090: Qwen3-8B (about 100 tokens/s) and Qwen3.6-27B (42/22 layers, 32k context, a 16.8k-token prompt answered correctly).
    * Full app run works (Start on the 3090 host, the 4090 as a member). Fixed after it: runtime memory per stage (the KV cache came out shorter than the context), the context the gateway reports, and the client's handling of over-long requests.
    * To do: the backend image on GHCR so members can pull it.
  * Setup on the 4090 still needs sudo: the GUI build libraries, Node.js and the NVIDIA runtime for Docker.
* \[ \] **Model matrix** (was Step 5.3): run every catalog model that fits 24 GB on hardware. For each: `probe_routing.py`, an edit task in `diff` and `whole` to set its `edit_format`, tokenizer ratio, tokens/s and time to first token. Record a table here.
  * Known from Step 5.1: gpt-oss-20b once nested a new function inside another, and misread clear search results ("Rust 1.99.0" answered as "1.116").
* \[ \] **GUI hardening** (was Steps 3.2/5.4):
  * show rate-limit and busy counts;
  * error paths: Docker stopped, GPU busy, failed download, port in use.
* \[ \] **First release follow-ups** (was Step 5.5):
  * check the first release's artifacts on a clean machine (`install.sh`, `install-server.sh`, macOS and arm64 builds);
  * publish the backend image to GHCR (self-hosted `IMAGE_RUNNER` or `scripts/publish-image.sh`);
  * check the docs site.

## Future Improvement

1. Optimization work for speed and security. 
