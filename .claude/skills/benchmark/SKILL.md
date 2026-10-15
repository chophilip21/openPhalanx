---
name: benchmark
description: "The speed benchmark behind BENCHMARK.md (scripts/bench_speed.py): how it runs on each machine, the test rig, measurement pitfalls found, and open findings from the runs. Read before running or changing bench_speed.py or filling BENCHMARK.md."
---

# Benchmarks: bench_speed.py and the numbers in BENCHMARK.md

Part of the developer notes; the index is in `CLAUDE.md`. The reader-facing page is `BENCHMARK.md`; this file holds how the numbers are made and what was found on the way.

## How it runs

* `scripts/bench_speed.py --label <machine> --models <id,id,…>`: per model, `oppxs use`, `oppxs context` (the longest that fits when the asked one doesn't, unless `--no-fit`), `oppxs start` (timed: start to serving), then SGLang's `bench_serving` inside the backend container: one user at 2k/8k/32k prompts (512 out, 5 requests after one warm-up, prefix cache flushed), then 1/2/4/8 users (2k in, 256 out). Results: one JSON line each in `<out>/results.jsonl`; `--summary <files…>` prints the BENCHMARK.md rows from any number of them (both machines together).
* **Its own settings and server identity** (`XDG_CONFIG_HOME`, `XDG_DATA_HOME` under `<out>`; `models` linked to the real models folder): the app's settings are untouched, and a machine that is a cluster member doesn't ask its host before each start (the laptop, a member of the 3090's cluster, failed with "can't reach the host … take-over" before this). Web search is turned off in those settings.
* **Split runs** (`--attach [--name …]`): `oppxs` can't host a split model (the split plan lives in the app), so the app on the 3090 is the host and the laptop a member; press Start in the app, then the script measures what serves in the host's container and leaves it running. VRAM used is the host's only.
* The desktop app must be closed (except with `--attach`) (it and `oppxs` can't run on one machine at once); the script refuses otherwise.
* **Raw results are committed** under `benchmarks/results/<date>-<machine>.jsonl` (checked for home paths, addresses and tokens first); `BENCHMARK.md` and the README's "How fast is it?" are written from them. Working copies go to `target/bench/` (ignored).
* Only the standard library: on another server, `scp` the file and run it there. The laptop runs it from `~/oppx-bench/`.

## Test rig

* **3090** (this machine): RTX 3090 24 GB, power limit 350 W (max 375), driver 580.173.02, kernel 7.1.5, oppxs 0.7.4, SGLang 0.5.21.
* **4090 Laptop** (`ssh 4090`): RTX 4090 Laptop GPU 16 GB, power limit 150 W (max 175), on AC, driver 580.126.18, kernel 6.18.7, oppxs 0.7.4.

## Pitfalls found

* **Prompts as text are ~10% longer than asked.** `random-ids` decodes random token ids to text and the server re-tokenizes it longer: a "32k" prompt was 33.5–36k tokens, over a 32k context, every request refused, and `bench_serving` still counted them completed with a first token at 0 ms (it printed 8,614 tok/s). Fixed with `--tokenize-prompt` (exact lengths), and a result whose median first token is 0 ms is now an error. The first run (`run1`) is void for this reason.
* `pkill -f bench_speed.py` from a shell whose own command line contains that text kills the shell too.

## Findings from the runs (open)

* **Long prompts are slow to process on the 3090:** Qwen2.5-Coder-14B AWQ, 32k prompt: 21.6 s to the first token (~1,500 tokens/s), against ~2,000 tokens/s at 8k. SGLang chose `chunked_prefill_size=2048` (in the backend log). A larger chunk may help long prompts; not tried. This is the case a coding agent hits on its first request.
* **`oppxs context` accepts any value when the model fits at no context** (`set_context` in `crates/openphalanx-server/src/main.rs`: `fit` is `None`, so nothing is refused, and it prints "Context window set"); `start` then refuses with the real reason. Seen with gpt-oss-20b on the laptop (needs at least 17.9 GiB, 15.6 free). It should say the model doesn't fit.
* **Devstral-Small-2-24B AWQ (`cyankiwi/…-AWQ-4bit`) runs out of memory while loading on the 3090**, although the estimate says it fits at 21.5k (catalog weights 15.0 GB): `torch.OutOfMemoryError` in `compressed_tensors_wNa16.create_weights` (`ministral3.py`) with 22.1 GiB in use, before any KV cache. The weights take far more memory loaded than the catalog's 15 GB; the catalog entry or the estimate for this build is wrong. Not yet looked into. On the laptop the estimate refuses it (needs at least 24.0 GiB at the minimum context), which suggests the estimate already knows part of it.
* **Qwen2.5-Coder-32B AWQ doesn't fit the 3090 at any context by the estimate** (needs at least 30.4 GiB at the asked 32k, and more than the 21.8 GiB free even at 2k), though it is the model people commonly run on a 3090 (as a 4-bit GGUF). The estimate may be too cautious here; not tried with Force load.
* **On 16 GB (laptop) gpt-oss-20b and Devstral-24B fit at no context** by the estimate (at least 17.9 and 24.0 GiB). Not tried with Force load.
* **Split Qwen3.6-27B AWQ serves about 4 requests at once** (3090 host + 4090 Laptop member, 64k, pp 2): 4 users 65 tok/s total, 8 users also 65 tok/s with the first token at 19.4 s instead of 4.3 s, so the rest queue. Likely the state cache of this hybrid (Mamba-style) model: the app passes `--max-mamba-cache-size 16` (with `--chunked-prefill-size 2048`). Not looked into. Single user: 34.6 tok/s at 2k, 28.6 at 60k; prompt processing ~1,100 tok/s (56 s at 60k).
* **The split results file carried the host's LAN address** (`extra_args`: `--dist-init-addr`); it is masked to `<ip>` in `benchmarks/results/`. Check every new results file before committing.
* The laptop reaches the first token slightly sooner than the 3090 (7B at 8k: 1.75 s vs 1.98 s), the 3090 generates ~40% faster (7B: 148 vs 108 tok/s; 14B: 81 vs 57). Expected: generation is bound by memory bandwidth (936 vs ~576 GB/s), prompt processing by compute.
