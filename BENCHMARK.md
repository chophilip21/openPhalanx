# Benchmarks

Openphalanx is a local coding setup: an agent on your laptop, the model on your own GPUs. This page answers the questions people ask before they set one up:

1. **Is it fast enough to code with?** How quickly answers stream, and how long you wait before the first word on a large prompt.
2. **What can I run on the card I have?** Which models fit a 24 GB or 16 GB card, and with how much context.
3. **What does splitting a model across two machines buy, and what does it cost?**
4. **Does the agent actually get work done?** Real coding tasks, checked by compiling and running tests rather than by reading the reply.
5. **How does it compare** with llama.cpp / Ollama on the same card.

Sections 1, 2 and 5 are measured, and section 3 for one model; the rest is marked **not run yet**. Every number comes from the hardware below, the raw results are in [`benchmarks/results/`](benchmarks/results/), and **How to reproduce** at the end has the commands.

## Test rig

| Machine | GPU | VRAM | Power limit | Driver |
|---|---|---|---|---|
| Desktop | RTX 3090 (Ampere) | 24 GB | 350 W | 580.173.02 |
| Laptop | RTX 4090 Laptop GPU (Ada) | 16 GB | 150 W (plugged in) | 580.126.18 |

Openphalanx 0.7.4 (SGLang 0.5.21), measured 2026-10-14. Each model is started the way the app starts it (`oppxs start`: same memory settings, same catalog arguments), with nothing else on the GPU and web search off. One user at a time unless the test says otherwise.

## What we measure

| Metric | What it means for you |
|---|---|
| **Generation speed** (tokens/s, one user) | How fast the answer streams. Below about 20 tokens/s, you read faster than the model writes |
| **Time to first token** at 2k / 8k / 32k prompt tokens | How long you wait before the answer starts. Coding agents send 5–30k tokens per request, so this matters more than generation speed |
| **Prompt processing** (tokens/s) | The same, as a rate |
| **Context that fits** | How much of a repo the model can see at once |
| **Several users at once** | Whether one server can be shared by a team |

Speed is measured with SGLang's own `bench_serving` inside the backend container: random prompts of an exact length (so nothing comes from the prefix cache), the cache flushed first, 512 tokens generated, the median of 5 requests after one warm-up. Not measured: general quality benchmarks (HumanEval, MMLU…). Model makers publish those, and they say little about how a model behaves as an agent in your repo.

## Results

### 1. Speed on one card

One user, prompt not cached.

| Model | GPU | Context | Generation | First token, 2k prompt | 8k | 32k | Prompt processing (8k) |
|---|---|---|---|---|---|---|---|
| Qwen2.5-Coder-7B AWQ | 3090 | 32k | **148 tok/s** | 0.46 s | 1.98 s | 9.9 s | 4,140 tok/s |
| Qwen2.5-Coder-14B AWQ (default) | 3090 | 32k | **81 tok/s** | 0.91 s | 3.99 s | 21.6 s | 2,060 tok/s |
| gpt-oss-20b | 3090 | 32k | **107 tok/s** | 0.86 s | 3.27 s | 14.5 s | 2,510 tok/s |
| Devstral-Small-2-24B AWQ | 3090 | — | fails to load (runs out of memory while loading the weights) | | | | |
| Qwen2.5-Coder-32B AWQ | 3090 | — | doesn't fit 24 GB by the app's estimate | | | | |
| Qwen3.6-27B AWQ | 3090 | — | doesn't fit 24 GB by the app's estimate (runs split: section 3) | | | | |
| Qwen2.5-Coder-7B AWQ | 4090 Laptop | 32k | **108 tok/s** | 0.41 s | 1.75 s | 9.0 s | 4,670 tok/s |
| Qwen2.5-Coder-14B AWQ (default) | 4090 Laptop | 16k | **57 tok/s** | 0.80 s | 3.53 s | 7.7 s (15.8k prompt) | 2,320 tok/s |
| gpt-oss-20b | 4090 Laptop | — | doesn't fit 16 GB by the app's estimate | | | | |
| Devstral-Small-2-24B AWQ | 4090 Laptop | — | doesn't fit 16 GB by the app's estimate | | | | |

* The 32k column is a 31.4k-token prompt (the context minus room for the answer).
* Generation slows as the context fills: the 14B on the 3090 writes 81 tok/s after a 2k prompt and 52 tok/s after a 31k one.
* The laptop starts answering slightly sooner, the 3090 writes about 40% faster. Writing is limited by memory bandwidth (936 GB/s against about 576), reading the prompt by compute.
* Time from Start to serving: 110–190 s, with the backend image already built.
* "Doesn't fit by the app's estimate": the app refuses to start a model that needs more memory than is free, even at the shortest context. The estimate is deliberately cautious, so some of these might load with **Force load**; not tried.

### 2. What fits

The longest context each model starts with (32k was the most asked for).

| Model | Weights | 4090 Laptop (16 GB) | 3090 (24 GB) | Split, both (40 GB) |
|---|---|---|---|---|
| Qwen2.5-Coder-7B AWQ | 5.2 GB | 32k | 32k | — |
| Qwen2.5-Coder-14B AWQ | 9.3 GB | 16k | 32k | not run yet |
| gpt-oss-20b | 12.8 GB | doesn't fit | 32k | — |
| Devstral-Small-2-24B AWQ | 15.0 GB | doesn't fit | fails to load | — |
| Qwen2.5-Coder-32B AWQ | 18.0 GB | doesn't fit | doesn't fit | not run yet |
| Qwen3.6-27B AWQ | 19.0 GB | doesn't fit | doesn't fit | **64k** |
| Qwen3.8-27B W4A16 | 18.1 GB | doesn't fit | doesn't fit | 64k (earlier run) |
| Qwen3.6-35B-A3B AWQ | 23.7 GB | doesn't fit | doesn't fit | not run yet |

### 3. Splitting across machines

The app on the 3090 as the cluster host, the laptop as a member, over gigabit Ethernet; the model's layers are split between the two (pipeline parallel), started from the app's Start button.

**What it unlocks:** Qwen3.6-27B AWQ fits neither card alone and runs split at a 64k context (18.8 GiB used on the 3090, 12.3 GiB on the laptop).

| Model | Context | Generation | First token, 2k prompt | 8k | 32k | 60k | Prompt processing |
|---|---|---|---|---|---|---|---|
| Qwen3.6-27B AWQ (split) | 64k | **35 tok/s** | 1.9 s | 6.7 s | 28.0 s | 56.4 s | ~1,100–1,200 tok/s |

Generation holds up as the context fills (35 tok/s after a 2k prompt, 29 after 60k). A cold long prompt is a long wait (nearly a minute at 60k), which is where the prefix cache earns its keep: an agent session pays it once, and later turns reuse the cached part.

**Not run yet:** the 14B on one card against split (what crossing the network costs), Qwen3.8-27B, and Qwen3.6-35B-A3B.

From an earlier hand run (gigabit Ethernet, 3090 + 4090 Laptop): Qwen3-8B AWQ split 20/16 layers wrote about 100 tok/s, and Qwen3.6-27B AWQ at 32k answered a 16.8k-token prompt in 13.7 s.

### 4. Multi-turn sessions and the prefix cache

**Not run yet** with this method (`scripts/bench_session.py`: 10 real turns, cache hit rate and time to first token per turn).

An earlier measurement on the 3090 with the default model: a 7.2k-token request took 3.7 s to the first token cold and **0.2 s** when its prefix was already cached. Agent sessions resend the same prefix every turn, so most turns get the cached time.

### 5. Several users on one server

2k-token prompts, 256 tokens out each. Total is the output tokens/s of the whole server; per user is each request's writing speed.

| Model | GPU | Users | Total | Per user | First token |
|---|---|---|---|---|---|
| Qwen2.5-Coder-14B AWQ | 3090 | 1 | 61 tok/s | 78 tok/s | 0.97 s |
| | | 2 | 86 tok/s | 74 tok/s | 1.86 s |
| | | 4 | 136 tok/s | 60 tok/s | 3.24 s |
| | | 8 | **179 tok/s** | 42 tok/s | 5.47 s |
| Qwen2.5-Coder-14B AWQ | 4090 Laptop | 1 | 49 tok/s | 58 tok/s | 0.80 s |
| | | 8 | 142 tok/s | 37 tok/s | 4.31 s |
| Qwen2.5-Coder-7B AWQ | 3090 | 8 | 369 tok/s | 77 tok/s | 2.22 s |
| gpt-oss-20b | 3090 | 8 | 171 tok/s | 33 tok/s | 4.08 s |
| Qwen3.6-27B AWQ | split | 1 | 27 tok/s | 34 tok/s | 1.99 s |
| | | 4 | 65 tok/s | 22 tok/s | 4.33 s |
| | | 8 | 65 tok/s | 22 tok/s | 19.4 s |

Eight people sharing the 3090 with the default model each still get 42 tok/s, about the speed of a hosted model. The split Qwen3.6-27B serves about four requests at a time: with eight, the total stays at 65 tok/s and the rest wait their turn (19 s to the first token). Every level for every model is in the raw results.

### 6. Does the agent get work done?

**Not run yet** for this page. From the developer notes, on split Qwen3.8-27B (3090 + 4090 Laptop, 64k context): `scripts/eval_edits.py` 9 of 9 edit tasks (three of which name no file), `scripts/eval_nav.py` 10 of 10 questions about the repository.

### 7. Compared with llama.cpp / Ollama

**Not run yet.** Planned: the same model on the same 3090 (AWQ against GGUF Q4_K_M, roughly the same size), one user and four.

## How to reproduce

```bash
# speed and "what fits", one machine (close the desktop app first; downloads what's missing)
scripts/bench_speed.py --label 3090 --models Qwen/Qwen2.5-Coder-14B-Instruct-AWQ,openai/gpt-oss-20b

# a split run: press Start in the app (host), then measure what it serves
scripts/bench_speed.py --label "3090 + 4090 Laptop" --attach --name "Qwen3.8-27B (split)"

# the tables, from any number of results files
scripts/bench_speed.py --summary benchmarks/results/*.jsonl
```

`bench_speed.py` uses only the Python standard library, so it can be copied to another server and run there. It uses its own settings, so the app's selected model and context are left alone.
