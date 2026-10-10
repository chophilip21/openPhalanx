#!/usr/bin/env python3
"""Measures where a model's VRAM goes under different SGLang settings, to
check the estimates in `vram.rs` and to compare memory-saving flags.

For each named configuration it starts SGLang alone (no gateway) from the
backend image, then reports: weights as loaded, KV-cache tokens, start-up
time, free VRAM when idle, the lowest free VRAM during a heavy load (a
near-full-context prompt, a long generation, and concurrent requests), and
prefill and decode speed.

    scripts/measure_vram.py --model Qwen/Qwen2.5-Coder-14B-Instruct-AWQ \\
        [--context 32768] [--fraction 0.85] [--configs base,graphs8] [--concurrency 4]

Run on the server node with the GPU otherwise idle. The memory fraction is
the same for every configuration, so the lowest free VRAM is comparable:
more left free means less runtime memory used.
"""

import argparse
import json
import os
import re
import subprocess
import threading
import time

NAME = "opx-measure"
# Extra SGLang arguments per configuration.
CONFIGS = {
    "base": "",
    # The gateway admits at most 8 requests at once (MAX_ACTIVE_REQUESTS).
    "graphs8": "--cuda-graph-max-bs-decode 8 --max-running-requests 8",
    "graphs8-noprefill": "--cuda-graph-max-bs-decode 8 --max-running-requests 8 --disable-prefill-cuda-graph",
    "graphs8-chunk2k": "--cuda-graph-max-bs-decode 8 --max-running-requests 8 --chunked-prefill-size 2048",
    "graphs8-noprefill-chunk2k": "--cuda-graph-max-bs-decode 8 --max-running-requests 8 --disable-prefill-cuda-graph --chunked-prefill-size 2048",
}


def sh(*args: str, **kw) -> str:
    return subprocess.run(args, capture_output=True, text=True, **kw).stdout


def free_mib() -> int:
    return int(sh("nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits").split()[0])


def image() -> str:
    tags = sh("docker", "images", "--format", "{{.Repository}}:{{.Tag}}").split()
    return next(t for t in tags if t.startswith("openphalanx-backend:"))


def ask(prompt: str, max_tokens: int) -> dict:
    """One chat request from inside the container (SGLang listens on loopback)."""
    code = (
        "import json,sys,time,urllib.request\n"
        "b=json.dumps({'model':'m','messages':[{'role':'user','content':sys.stdin.read()}],"
        f"'max_tokens':{max_tokens},'temperature':0}}).encode()\n"
        "t=time.time()\n"
        "try:\n"
        "    r=json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/v1/chat/completions',b,"
        "{'Content-Type':'application/json'}),timeout=900))\n"
        "    print(json.dumps({'prompt':r['usage']['prompt_tokens'],'completion':r['usage']['completion_tokens'],'secs':time.time()-t}))\n"
        "except Exception as e:\n"
        "    print(json.dumps({'error':str(e)[:200]}))\n"
    )
    out = subprocess.run(["docker", "exec", "-i", NAME, "python3", "-c", code], input=prompt, capture_output=True, text=True).stdout
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": out[-200:]}


def measure(name: str, extra: str, args) -> dict:
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    before = free_mib()
    t0 = time.time()
    subprocess.run([
        "docker", "run", "-d", "--name", NAME, "--gpus", "all", "--ipc=host",
        "--entrypoint", "/opt/openphalanx/start-sglang.sh",
        "-v", f"{args.hf}:/root/.cache/huggingface:ro",
        # A model folder is mounted at its own path; a repo id comes from the cache.
        *(["-v", f"{args.model}:{args.model}:ro"] if os.path.isdir(args.model) else []),
        "-e", f"MODEL_PATH={args.model}", "-e", "SERVED_MODEL_NAME=m", "-e", "SGLANG_PORT=8080",
        "-e", f"MEM_FRACTION_STATIC={args.fraction}", "-e", f"CONTEXT_LENGTH={args.context}",
        "-e", "HF_HUB_OFFLINE=1", "-e", f"SGLANG_EXTRA_ARGS={extra}", image(),
    ], capture_output=True, check=True)
    while True:
        log = sh("docker", "logs", NAME) + subprocess.run(["docker", "logs", NAME], capture_output=True, text=True).stderr
        if "fired up" in log:
            break
        if "exited with code" in log or time.time() - t0 > 1200:
            tail = [ln for ln in log.splitlines() if "Error" in ln or "error" in ln][-2:]
            subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
            return {"config": name, "failed": " | ".join(tail)[:300] or "did not start"}
        time.sleep(3)
    startup = time.time() - t0
    weights = re.search(r"Load weight end.*?mem usage=([\d.]+) GB", log)
    kv = re.search(r"max_total_num_tokens=(\d+)", log)
    idle = free_mib()

    # Lowest free VRAM while the model works.
    lowest = [idle]
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            lowest[0] = min(lowest[0], free_mib())
            time.sleep(0.2)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    # ~1.75 characters a token for this synthetic code.
    filler = "\n".join(f"def f{i}(x):\n    return x * {i} + {i * i}" for i in range(20000))
    big = filler[: int((args.context - 1500) * 1.7)]
    prefill = ask(big + "\n\nWhat does f7(3) return? Number only.", 8)
    decode = ask("Write a detailed 700-word essay about memory management on GPUs.", 900)
    results = []
    part = filler[: int(args.context * 1.7 / max(args.concurrency, 1) * 0.6)]
    threads = [threading.Thread(target=lambda: results.append(ask(part + "\n\nSummarize this file in 200 words.", 300)))
               for _ in range(args.concurrency)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stop.set()
    watcher.join()
    errors = [r["error"] for r in [prefill, decode, *results] if "error" in r]
    log = sh("docker", "logs", NAME) + subprocess.run(["docker", "logs", NAME], capture_output=True, text=True).stderr
    oom = bool(re.search(r"out of memory|OutOfMemory", log, re.I))
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    return {
        "config": name,
        "free_before_mib": before,
        "weights_gb": float(weights.group(1)) if weights else None,
        "kv_tokens": int(kv.group(1)) if kv else None,
        "startup_s": round(startup),
        "idle_free_mib": idle,
        "lowest_free_mib": lowest[0],
        # SGLang leaves free × (1 − fraction) outside the weights and KV pool.
        "reserve_mib": round(before * (1 - args.fraction)),
        "runtime_idle_mib": round(before * (1 - args.fraction)) - idle,
        "runtime_peak_mib": round(before * (1 - args.fraction)) - lowest[0],
        "prefill_tok_s": round(prefill["prompt"] / prefill["secs"]) if "prompt" in prefill else None,
        "prefill_tokens": prefill.get("prompt"),
        "decode_tok_s": round(decode["completion"] / decode["secs"], 1) if "completion" in decode else None,
        "oom": oom,
        "errors": errors,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="a Hugging Face repo id (in the cache) or a model folder")
    ap.add_argument("--hf", default=os.path.expanduser("~/.cache/huggingface"))
    ap.add_argument("--context", type=int, default=32768)
    ap.add_argument("--fraction", type=float, default=0.85)
    ap.add_argument("--configs", default="base,graphs8")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--json")
    args = ap.parse_args()
    rows = []
    for name in args.configs.split(","):
        row = measure(name, CONFIGS[name], args)
        rows.append(row)
        print(json.dumps(row), flush=True)
    if args.json:
        json.dump(rows, open(args.json, "w"), indent=2)


if __name__ == "__main__":
    main()
