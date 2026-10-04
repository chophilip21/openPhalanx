#!/usr/bin/env python3
"""Builds model catalog entries from Hugging Face, for crates/openphalanx-core/catalog.json.

Everything measurable comes from the repo at a pinned commit: the commit
itself, the size of the weights SGLang loads (root `.safetensors`, minus
Mistral's duplicate `consolidated*` copy), and the attention shape from
config.json, parsed like `model::arch_from_config` (full, sliding-window and
linear-attention layers). Names, parameter labels and notes are editorial and
come from the spec file.

    scripts/catalog_entry.py specs.json > entries.json        # one entry per spec
    scripts/catalog_entry.py --check                          # re-verify catalog.json against HF

A spec: {"id": "openai/gpt-oss-20b", "name": "gpt-oss 20B", "family": "gpt-oss",
         "params": "21B (3.6B active)", "quant": "MXFP4", "revision": "<sha, optional>",
         "quantized_by": "<only for community quantizations>",
         "base_model": "<community quantizations: the original repo, for the release date>", ...any other catalog field}

`released` is the date the model was first published on Hugging Face (the
repo's creation date; for a community quantization, its base model's).
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "crates/openphalanx-core/catalog.json"
HF = "https://huggingface.co"


def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "openphalanx-catalog"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def runtime_weight_bytes(siblings) -> int:
    names = [s["rfilename"] for s in siblings]
    has_shards = any(n.endswith(".safetensors") and "/" not in n and not n.startswith("consolidated") for n in names)
    total = 0
    for s in siblings:
        n = s["rfilename"]
        if "/" in n or not n.endswith(".safetensors"):
            continue
        if has_shards and n.startswith("consolidated"):
            continue
        total += s.get("size", 0)
    return total


def arch(config: dict) -> tuple[dict, int]:
    c = config.get("text_config", config)
    layers = c["num_hidden_layers"]
    heads = c["num_attention_heads"]
    kv_heads = c.get("num_key_value_heads") or heads
    head_dim = c.get("head_dim") or c["hidden_size"] // heads
    types = c.get("layer_types") or []
    if types:
        full, swa, linear = (types.count(k) for k in ("full_attention", "sliding_attention", "linear_attention"))
    elif c.get("full_attention_interval"):
        full = layers // c["full_attention_interval"]
        swa, linear = 0, layers - full
    else:
        full, swa, linear = layers, 0, 0
    spec = {
        "kv_layers": full,
        "kv_heads": c.get("num_global_key_value_heads") or kv_heads,
        "head_dim": c.get("global_head_dim") or head_dim,
    }
    if swa:
        spec.update(swa_layers=swa, swa_kv_heads=kv_heads, swa_head_dim=head_dim, swa_window=c.get("sliding_window", 0))
    if linear:
        spec["linear_layers"] = linear
    return spec, c.get("max_position_embeddings", 32768)


def build(spec: dict) -> dict:
    repo = spec["id"]
    rev = spec.get("revision")
    info = get(f"{HF}/api/models/{repo}{'/revision/' + rev if rev else ''}?blobs=true")
    sha = info["sha"]
    config = get(f"{HF}/{repo}/raw/{sha}/config.json")
    a, max_ctx = arch(config)
    lic = (info.get("cardData") or {}).get("license") or "see repo"
    created = info.get("createdAt")
    if spec.get("base_model"):
        created = get(f"{HF}/api/models/{spec['base_model']}").get("createdAt")
    entry = {
        "id": repo,
        "revision": sha,
        "name": spec["name"],
        "family": spec["family"],
        "params": spec["params"],
        "quant": spec["quant"],
        "weight_bytes": runtime_weight_bytes(info.get("siblings", [])),
        "max_context": max_ctx,
        "license": lic,
        "released": (created or "")[:10] or None,
        "arch": a,
    }
    for k, v in spec.items():
        if k not in entry and k not in ("revision", "base_model"):
            entry[k] = v
    return entry


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("specs", nargs="?", help="JSON list of specs")
    p.add_argument("--check", action="store_true", help="verify catalog.json sizes and shapes against Hugging Face")
    args = p.parse_args()
    if args.check:
        bad = 0
        for e in json.loads(CATALOG.read_text()):
            fresh = build({**e, "revision": e["revision"]})
            for k in ("weight_bytes", "arch", "max_context"):
                if fresh[k] != e[k]:
                    bad += 1
                    print(f"{e['id']}: {k} is {e[k]}, Hugging Face says {fresh[k]}")
        print("catalog matches Hugging Face" if not bad else f"{bad} mismatches")
        sys.exit(1 if bad else 0)
    specs = json.load(open(args.specs))
    json.dump([build(s) for s in specs], sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
