#!/usr/bin/env python3
"""Speed benchmark on one machine, for BENCHMARK.md.

For each model: select it, set the context window (or the longest that fits
this GPU, when the asked one doesn't), start it through `oppxs` (the same
start path as the app: memory fraction, dtype, catalog arguments), then run
SGLang's own `bench_serving` inside the backend container:

* one user at each prompt length (2k, 8k, 32k by default): time to first
  token, prompt processing and generation speed. Prompts are random token ids
  and the prefix cache is flushed first, so nothing is served from cache;
* several users at once (1, 2, 4, 8): total and per-user tokens/s.

It also records the VRAM in use while serving and the time from start to
serving. Every result is a line in `<out>/results.jsonl`; at the end the rows
for BENCHMARK.md are printed. `--summary` prints them again from one or more
results files (e.g. the 3090's and the laptop's together).

    scripts/bench_speed.py --label 3090 --models Qwen2.5-Coder-14B-Instruct-AWQ,gpt-oss-20b
    scripts/bench_speed.py --label "3090 + 4090 Laptop" --attach --name "Qwen3.8-27B (split)"   # after Start in the app
    scripts/bench_speed.py --summary target/bench/*/results.jsonl

Needs `oppxs` and Docker, and the desktop app closed (the two can't run on
one machine at once). Settings and the server's identity go to a scratch
folder under `<out>`, so the app's settings (selected model, context) and its
cluster membership are left as they were, and the run is standalone; models
come from the usual models folder and the Hugging Face cache, and downloads
land there, shared with the app. Only the standard
library: copy this file to another server and run it there.
"""

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

CONTAINER = "openphalanx-backend"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd, env=None, timeout=None, check=False):
    """A command's exit code and combined output."""
    try:
        p = subprocess.run(cmd, env=env, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} s"
    out = (p.stdout + p.stderr).strip()
    if check and p.returncode:
        sys.exit(f"{' '.join(cmd)} failed:\n{out}")
    return p.returncode, out


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Bench:
    def __init__(self, args):
        self.args = args
        self.out = args.out
        os.makedirs(self.out, exist_ok=True)
        # Its own settings and server identity: a machine that is a cluster
        # member would otherwise ask its host before every start, and a
        # single-card number shouldn't involve the cluster. The models folder
        # is the real one, so downloads are shared with the app.
        data = os.path.join(self.out, "data", "openphalanx")
        models = os.path.expanduser("~/.local/share/openphalanx/models")
        os.makedirs(models, exist_ok=True)
        os.makedirs(data, exist_ok=True)
        if not os.path.lexists(os.path.join(data, "models")):
            os.symlink(models, os.path.join(data, "models"))
        self.env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(self.out, "config"),
                        XDG_DATA_HOME=os.path.join(self.out, "data"))
        self.daemon = None
        self.results = open(os.path.join(self.out, "results.jsonl"), "a")

    def oppxs(self, *a, timeout=120):
        return run([self.args.oppxs, *a], env=self.env, timeout=timeout)

    def record(self, **row):
        row = {"label": self.args.label, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **row}
        self.results.write(json.dumps(row) + "\n")
        self.results.flush()

    # ---- the server ----

    def ensure_daemon(self):
        if self.oppxs("status")[0] == 0:
            log("oppxs run is already running; using it")
            return
        self.oppxs("web-search", "off")  # no SearXNG next to the model: nothing here searches
        logf = open(os.path.join(self.out, "oppxs.log"), "a")
        self.daemon = subprocess.Popen([self.args.oppxs, "run", "--name", self.args.label], env=self.env,
                                       stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(60):
            time.sleep(1)
            if self.oppxs("status")[0] == 0:
                log("started oppxs run")
                return
            if self.daemon.poll() is not None:
                break
        sys.exit(f"oppxs run didn't come up; see {logf.name}")

    def shutdown(self):
        self.oppxs("stop", timeout=120)
        if self.daemon and self.daemon.poll() is None:
            self.daemon.send_signal(signal.SIGTERM)
            try:
                self.daemon.wait(60)
            except subprocess.TimeoutExpired:
                self.daemon.kill()

    def set_context(self, want):
        """Set the context window; when it doesn't fit, the longest that does
        (with --fit). Returns the context set, or None and the reason."""
        code, out = self.oppxs("context", str(want))
        if code == 0:
            return want, None
        m = re.search(r"(?:fits|supports) up to (\d+) tokens", out)
        if m and self.args.fit and int(m.group(1)) >= 2048:
            got = int(m.group(1)) // 1024 * 1024  # a round number, a little under the edge
            code, out2 = self.oppxs("context", str(got))
            if code == 0:
                return got, None
            out = out2
        return None, out

    def vram_used(self):
        code, out = run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
        return sum(int(x) for x in out.split()) * 2**20 if code == 0 else None

    # ---- bench_serving inside the container ----

    def bench(self, env, input_len, output_len, concurrency, prompts):
        cmd = [
            "python3", "-m", "sglang.bench_serving", "--backend", "sglang",
            "--host", "127.0.0.1", "--port", env["SGLANG_PORT"], "--model", env["MODEL_PATH"],
            "--dataset-name", "random-ids", "--random-input-len", str(input_len),
            "--random-output-len", str(output_len), "--random-range-ratio", "1",
            "--num-prompts", str(prompts), "--max-concurrency", str(concurrency),
            # token ids, not text: text re-tokenizes about 10% longer, so a
            # "32k" prompt went over a 32k context and every request was refused
            "--tokenize-prompt",
            "--warmup-requests", "1", "--flush-cache", "--disable-tqdm", "--seed", "1",
            "--output-file", "/tmp/bench.jsonl",
        ]
        run(["docker", "exec", CONTAINER, "rm", "-f", "/tmp/bench.jsonl"])
        code, out = run(["docker", "exec", CONTAINER, *cmd], timeout=self.args.test_timeout)
        if code != 0:
            return None, out[-2000:]
        code, raw = run(["docker", "exec", CONTAINER, "cat", "/tmp/bench.jsonl"])
        lines = [l for l in raw.splitlines() if l.strip().startswith("{")]
        if code != 0 or not lines:
            return None, out[-2000:]
        r = json.loads(lines[-1])
        # Refused requests still count as completed, with a first token at 0 ms.
        if not r.get("completed") or not r.get("median_ttft_ms"):
            return None, "requests failed (no first token): " + out[-1500:]
        keep = ["completed", "duration", "median_ttft_ms", "p99_ttft_ms", "median_tpot_ms", "median_itl_ms",
                "input_throughput", "output_throughput", "total_input_tokens", "total_output_tokens"]
        return {k: r.get(k) for k in keep}, None

    def model(self, name):
        a = self.args
        log(f"== {name}")
        if not a.no_download:
            log("download (no-op when already there)")
            code, out = self.oppxs("download", name, timeout=4 * 3600)
            if code:
                self.record(model=name, test="error", error=f"download: {out[-1500:]}")
                return log(f"download failed: {out[-300:]}")
        code, out = self.oppxs("use", name)
        if code:
            self.record(model=name, test="error", error=f"use: {out}")
            return log(f"can't select it: {out}")
        ctx, why = self.set_context(a.context)
        if ctx is None:
            self.record(model=name, test="fit", context=None, error=why)
            return log(f"doesn't fit: {why}")
        log(f"context {ctx}; starting")
        t0 = time.time()
        code, out = self.oppxs("start", timeout=a.start_timeout)
        load_s = round(time.time() - t0, 1)
        if code:
            tail = self.oppxs("logs", "-n", "40")[1]
            self.record(model=name, test="start", context=ctx, error=f"{out[-1500:]}\n--- log ---\n{tail[-3000:]}")
            self.oppxs("stop")
            return log(f"start failed after {load_s} s: {out[-300:]}")
        code, env_out = run(["docker", "exec", CONTAINER, "printenv"])
        env = dict(l.split("=", 1) for l in env_out.splitlines() if "=" in l)
        self.record(model=name, test="start", context=ctx, load_s=load_s, vram_used=self.vram_used(),
                    mem_fraction=env.get("MEM_FRACTION_STATIC"), served=env.get("SERVED_MODEL_NAME"))
        log(f"serving after {load_s} s")
        try:
            self.measure(name, ctx, env)
        finally:
            self.oppxs("stop")

    def attach(self):
        """Measure the model already serving (a split run the app started):
        nothing is selected, started or stopped."""
        code, env_out = run(["docker", "exec", CONTAINER, "printenv"])
        if code:
            sys.exit(f"no backend is running here ({CONTAINER}): {env_out}")
        env = dict(l.split("=", 1) for l in env_out.splitlines() if "=" in l)
        name = self.args.name or env["MODEL_PATH"]
        ctx = int(env.get("CONTEXT_LENGTH") or self.args.context)
        log(f"== {name} (already serving, context {ctx})")
        self.record(model=name, test="start", context=ctx, load_s=None, vram_used=self.vram_used(),
                    mem_fraction=env.get("MEM_FRACTION_STATIC"), served=env.get("SERVED_MODEL_NAME"),
                    extra_args=env.get("SGLANG_EXTRA_ARGS"))
        self.measure(name, ctx, env)

    def measure(self, name, ctx, env):
        a = self.args
        for want in a.inputs:
            n = min(want, ctx - a.output_len - 64)  # room for the answer
            r, err = self.bench(env, n, a.output_len, 1, a.runs)
            self.record(model=name, test="single", context=ctx, input_len=n, output_len=a.output_len,
                        concurrency=1, **(r or {"error": err}))
            log(f"1 user, {n} in: " + (f"TTFT {r['median_ttft_ms']:.0f} ms, {1000 / r['median_tpot_ms']:.1f} tok/s"
                                      if r else f"failed: {err[-300:]}"))
        for c in a.users:
            n = min(a.users_input, ctx - a.users_output - 64)
            r, err = self.bench(env, n, a.users_output, c, max(2 * c, a.runs))
            self.record(model=name, test="users", context=ctx, input_len=n, output_len=a.users_output,
                        concurrency=c, **(r or {"error": err}))
            log(f"{c} users: " + (f"{r['output_throughput']:.0f} tok/s total" if r else f"failed: {err[-300:]}"))

    def machine(self):
        q = "name,memory.total,driver_version,enforced.power.limit,power.max_limit"
        info = {"gpus": run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader"])[1].splitlines(),
                "oppxs": self.oppxs("--version")[1], "kernel": os.uname().release}
        self.record(test="machine", **info)
        log(f"machine: {info}")


# ---- the tables ----

def summary(paths):
    rows = [json.loads(l) for p in paths for l in open(p) if l.strip()]
    gib = lambda b: f"{b / 2**30:.1f} GiB" if b else "—"
    labels = list(dict.fromkeys(r["label"] for r in rows))
    for label in labels:
        for r in rows:
            if r["label"] == label and r["test"] == "machine":
                print(f"<!-- {label}: {'; '.join(r['gpus'])} · {r['oppxs']} · kernel {r['kernel']} -->")
    # the latest result per (label, model, test, input, concurrency)
    last = {}
    for r in rows:
        last[(r["label"], r.get("model"), r["test"], r.get("input_len"), r.get("concurrency"))] = r
    rows = list(last.values())
    models = list(dict.fromkeys(r["model"] for r in rows if r.get("model")))

    print("\n### One user\n")
    print("| Model | GPU | Context | Gen tok/s | TTFT by prompt length | Prompt tok/s (longest) | VRAM used | Start to serving |")
    print("|---|---|---|---|---|---|---|---|")
    for label in labels:
        for m in models:
            start = next((r for r in rows if r["label"] == label and r.get("model") == m and r["test"] in ("start", "fit")), None)
            if not start:
                continue
            if start.get("error"):
                print(f"| {m} | {label} | {start.get('context') or '—'} | won't run: {start['error'].splitlines()[0][:80]} | | | | |")
                continue
            single = sorted((r for r in rows if r["label"] == label and r.get("model") == m and r["test"] == "single"
                             and r.get("median_ttft_ms")), key=lambda r: r["input_len"])
            gen = f"{1000 / single[0]['median_tpot_ms']:.1f}" if single else "—"
            ttft = ", ".join(f"{r['input_len'] // 1024}k: {r['median_ttft_ms'] / 1000:.2f} s" for r in single) or "—"
            pp = f"{single[-1]['input_len'] / (single[-1]['median_ttft_ms'] / 1000):,.0f}" if single else "—"
            print(f"| {m} | {label} | {start['context'] // 1024}k | {gen} | {ttft} | {pp} | {gib(start.get('vram_used'))} | {f"{start['load_s']:.0f} s" if start.get('load_s') else '—'} |")

    print("\n### Several users\n")
    print("| Model | GPU | Users | Total tok/s | Per-user tok/s | Median TTFT |")
    print("|---|---|---|---|---|---|")
    for label in labels:
        for m in models:
            for r in sorted((r for r in rows if r["label"] == label and r.get("model") == m and r["test"] == "users"
                             and r.get("output_throughput")), key=lambda r: r["concurrency"]):
                print(f"| {m} | {label} | {r['concurrency']} | {r['output_throughput']:.0f} | "
                      f"{1000 / r['median_tpot_ms']:.1f} | {r['median_ttft_ms'] / 1000:.2f} s |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", help="this machine in the tables, e.g. 3090 or '4090 Laptop'")
    ap.add_argument("--models", help="comma-separated catalog ids, or a unique part of one")
    ap.add_argument("--attach", action="store_true",
                    help="measure the model already serving (a split run started in the app), and leave it running")
    ap.add_argument("--name", help="with --attach: the model's name in the tables (default: its path)")
    ap.add_argument("--context", type=int, default=32768, help="context window to serve (default 32768)")
    ap.add_argument("--no-fit", dest="fit", action="store_false",
                    help="skip a model that doesn't fit the context, instead of using the longest that does")
    ap.add_argument("--inputs", default="2048,8192,32768", help="prompt lengths for one user (cut to the context)")
    ap.add_argument("--output-len", type=int, default=512, help="tokens generated per request, one user")
    ap.add_argument("--runs", type=int, default=5, help="requests per measurement, after one warm-up")
    ap.add_argument("--users", default="1,2,4,8", help="concurrency levels; empty to skip")
    ap.add_argument("--users-input", type=int, default=2048)
    ap.add_argument("--users-output", type=int, default=256)
    ap.add_argument("--no-download", action="store_true", help="don't download models that aren't there")
    ap.add_argument("--start-timeout", type=int, default=1800, help="seconds to wait for a model to serve")
    ap.add_argument("--test-timeout", type=int, default=1800)
    ap.add_argument("--oppxs", default="oppxs")
    ap.add_argument("--out", help="results folder (default target/bench/<label>-<date>)")
    ap.add_argument("--summary", nargs="+", metavar="RESULTS", help="only print the tables from these results files")
    args = ap.parse_args()

    if args.summary:
        return summary([p for s in args.summary for p in glob.glob(s)])
    if not args.label or not (args.models or args.attach):
        ap.error("--label and --models (or --attach) are needed, or --summary")
    if not args.attach and run(["pgrep", "-x", "openphalanx"])[0] == 0:
        sys.exit("The Openphalanx app is running; close it first (it and oppxs can't run on one machine at once).")
    args.inputs = [int(x) for x in args.inputs.split(",") if x]
    args.users = [int(x) for x in args.users.split(",") if x]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", args.label).strip("-").lower()
    args.out = os.path.abspath(args.out or os.path.join(ROOT, "target", "bench", f"{slug}-{datetime.now():%Y%m%d-%H%M}"))

    b = Bench(args)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    try:
        if args.attach:
            b.machine()
            b.attach()
        else:
            b.ensure_daemon()
            b.machine()
            for m in [m.strip() for m in args.models.split(",") if m.strip()]:
                b.model(m)
    finally:
        if not args.attach:
            b.shutdown()
        b.results.close()
    log(f"results: {b.results.name}")
    summary([b.results.name])


if __name__ == "__main__":
    main()
