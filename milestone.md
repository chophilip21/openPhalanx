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
2. ~~Implement sessions that can be resumed, and make sure we are doing context summarization, etc.~~ Done in Steps 4.8 (sessions, `-c`/`-r`) and 4.9 (auto-compaction and context fitting). 
3. Another important feature that deserves its own section. right now, our codebase assumes there is one server, and N possible clients. But this is not only the case. There can be N servers that can distribute the workload, and load one large model in a distributed way. And we can also imagine multiple clients, that points at cluster of nodes. 

Scaling from a single GPU workstation to a cluster of nodes handling multiple concurrent clients is the exact use case that frameworks like SGLang and vLLM were built to solve for enterprise deployments.To achieve this, the architecture splits into two distinct problems: distributing the model (across N servers) and distributing the traffic (routing N clients).Here is exactly how this is handled in modern LLM infrastructure.Part 1: Distributing One Large Model Across N ServersIf a model is too large to fit on a single machine (e.g., a 70B parameter model or massive Mixture-of-Experts like DeepSeek), you cluster multiple physical servers together. SGLang supports this natively using Ray and NCCL (NVIDIA Collective Communications Library).Tensor Parallelism (TP) & Pipeline Parallelism (PP):TP slices individual matrix math operations across multiple GPUs. If those GPUs are on different servers, SGLang uses Ray to coordinate them over the network.PP slices the model vertically. Server A handles layers 1–20, and Server B handles layers 21–40. Server A computes the first half and passes the intermediate tensors over the network to Server B to finish.   Prefill/Decode (PD) Disaggregation:This is a highly advanced SGLang feature for clusters. You designate some servers strictly as "Prefill nodes" (their only job is reading massive codebases/prompts) and other servers as "Decode nodes" (their only job is generating the output tokens). Once a Prefill node processes an Aider Repo Map, it transfers the KV cache over the network to the Decode node to stream the answer.   Note: Splitting a single model across multiple physical machines requires extremely fast networking (e.g., InfiniBand or 400GbE RoCE). Standard Gigabit Ethernet is too slow for Tensor Parallelism between physical servers.Part 2: Routing N Clients to N Servers (Load Balancing)If you simply want to increase your capacity to handle many developers (N clients) at once, you run identical copies of your model across multiple independent servers (Data Parallelism).To the clients, there should only ever be one API endpoint. You accomplish this using a router.The SGLang Model Gateway (Router):SGLang has a built-in router (sglang-router) that sits in front of all your GPU servers. You launch your GPU workers, and then launch the router on a head node.   Bashpython -m sglang_router.launch_server --host 0.0.0.0 --port 30000 --dp-size 4
All of your Tauri/Aider clients simply point their OPENAI_API_BASE to this single router IP.Cache-Aware Routing (The Secret Weapon):If 10 developers are working simultaneously, their prompts are huge. SGLang's router uses RadixAttention cache-aware load balancing.When Developer A sends their codebase map, the router sends it to Server 1. When Developer A asks a follow-up question, the router remembers that Server 1 already has Developer A's codebase in its KV cache, and routes the request back to Server 1. If Developer B logs in, the router sends them to an idle node, like Server 2.   External API Gateways (LiteLLM):If you don't use the built-in SGLang router, the industry standard for this is LiteLLM. You run an NGINX-like container called LiteLLM Gateway that receives all API calls and load-balances them across your cluster of SGLang servers using round-robin or lowest-latency routing.
