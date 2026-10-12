---
name: model-catalog
description: "catalog.json and the VRAM estimate (vram.rs): which builds are listed, adding models, dtype overrides, FP8 KV cache, how the KV cache is sized per architecture, YaRN long context and its measurements, what was validated on hardware. Read before adding a model or changing the VRAM maths."
---

# Model catalog and VRAM estimate

Part of the developer notes; the index is in `CLAUDE.md`.

## Model catalog and VRAM estimate

* **Which build:** for each size, the smallest build the pinned SGLang runs on Ampere and Ada: official 4-bit (AWQ, GPTQ, W4A16) first, then a community 4-bit build, then FP8, then the publisher's 16-bit build. A community build must come from a known quantizer (TheBloke, casperhansen, cyankiwi, QuantTrio, Red Hat AI, TechxGenus, stelterlab, hugging-quants) or be widely downloaded; the smallest wins when several qualify. Code Llama comes from the ungated `codellama` org (the `meta-llama` copies are gated). Not used: NVFP4 (refused on the 3090: "Current platform does not support w4a4 nvfp4"), dense MXFP4 in compressed-tensors ("CompressedTensorsW4A16Sparse24 is not supported"), bitsandbytes, GGUF, and gated repos (the downloader has no Hugging Face token, and every official Llama is gated). Left out for now: Mistral Large 3 (NVFP4, or Mistral's own format without `config.json`), DeepSeek V4.1 (`DeepseekV41ForCausalLM` isn't in SGLang 0.5.21) and the 1.5B R1 distill (no usable 4-bit build).
* **Adding models:** write a spec (id, name, family, params, quant, and `quantized_by` for community builds), run `scripts/catalog_entry.py specs.json`, and paste the entries into `catalog.json`. `scripts/catalog_entry.py --check` verifies every entry's weight size, attention shape and context against Hugging Face.
* **Release date** (`released`, shown as a year on the Models page; the full date orders "Newest first"): the repo's creation date on Hugging Face; community quantizations take their base model's date (`base_model` in the spec).
* **`dtype`** (optional, passed as SGLang `--dtype` to every rank): for checkpoints whose declared dtype SGLang can't run. The 4-bit Qwen3.6 builds (cyankiwi 27B, QuantTrio 35B-A3B) declare float16, but SGLang keeps the Gated-DeltaNet state in bfloat16 and the first prefill failed with "Index put requires the source and destination dtypes match"; they use `bfloat16` (verified on the 27B; the 35B-A3B by analogy).
* **FP8 KV cache** (`--kv-cache-dtype`), measured with `scripts/probe_kv.py`: not enabled for any model. On the 3090 (Ampere) only `fp8_e5m2` runs (`fp8_e4m3` fails to compile in Triton on sm_86). It gives gpt-oss-20b a 128k context on one card (199k KV tokens), but costs accuracy: recall at 24k tokens 1/3 against 3/3 with 16-bit KV, coding 4/5 against 5/5. Qwen2.5-Coder-14B gains nothing: its native window is 32k. Worth re-checking `fp8_e4m3` on Ada (4090) and on Qwen3.6, as a per-model catalog field only if it passes.
* **Check the architecture first:** the pinned SGLang must have the model class (`sglang/srt/models/` in the image). All current entries were checked against SGLang 0.5.21.
* **Weights counted:** only root-level `.safetensors` (subfolders like gpt-oss's `original/` and `metal/` are skipped), and Mistral's duplicate `consolidated*.safetensors` are dropped when HF shards exist. The downloader applies the same rule.
* **KV cache** (`vram::ArchSpec`), as SGLang allocates it:
  * full-attention layers × context;
  * sliding-window layers (Gemma 4, gpt-oss) × 0.8 of the context (`--swa-full-tokens-ratio`);
  * Gemma 4's full layers use their own shape (`global_head_dim`, `num_global_key_value_heads`);
  * hybrid linear-attention models (Qwen3-Next, Qwen3.6) × 1.9, for the recurrent-state pool (`--mamba-full-memory-ratio` 0.9);
  * MLA (DeepSeek V2/V3, GLM-4.7-Flash, GLM-5, Mistral Small 4): one latent of `kv_lora_rank + qk_rope_head_dim` per token and layer, stored as one head of half that width (`catalog_entry.py` and `model::arch_from_config` do the same);
  * Kimi-Linear and K3 list their KV-cache layers in `linear_attn_config.full_attn_layers` (the rest are linear); GPTBigCode configs (Granite Code 20B/34B) use `n_layer`/`n_head`/`n_embd`, and `multi_query` means one KV head;
  * every `layer_types` attention kind other than sliding-window and linear counts as full (GLM-5's `deepseek_sparse_attention`). DeepSeek V4's compressed attention is counted as one 512-wide KV head on every layer, an overestimate.
* **YaRN (`yarn` catalog field) is opt-in, off by default** (`settings.long_context`). Qwen2.5-Coder 7B, 14B and 32B carry it at factor 2 (32k → 64k). The switch is a checkbox under the context slider (`ContextSlider.svelte`, shown only for a model that has the mode; `set_long_context`, locked like the slider, and turning it off brings the context back to the native window), or `oppxs long-context on|off`. With it off, `server::resolve` drops the field, so the model keeps its native maximum and nothing is passed to SGLang. With it on, it raises a model's maximum to native × factor and passes `--json-model-override-args` (rope scaling plus `max_position_embeddings`, which SGLang 0.5.21 derives the limit from) only above the native window, to every split rank.
  * **Why it failed:** measured on Qwen2.5-Coder-7B-AWQ (3090, needle recall, `start-sglang.sh` as the app runs it): without YaRN 3/3 at 26k tokens; with YaRN 3/3 at 3k, 2/3 at 8k, 0/3 at 26k and 44k (Qwen's own snippet alone the same). The cause was the override, not YaRN: with transformers 5 `rope_scaling` is an alias of `rope_parameters`, which also holds `rope_theta`, so replacing it dropped the model's base and SGLang's `get_rope_config` fell back to 10000 (Qwen2.5 uses 1,000,000). Checked on CPU with SGLang's own `get_config` + `get_rope_config` in the backend image: theta 1e6 without an override, 10000 with ours or Qwen's snippet, 1e6 with `rope_theta` inside the block.
  * **Fix:** `catalog::Yarn` has a required `rope_theta`, sent inside `rope_scaling`; `scripts/catalog_entry.py` fills it from the model's `config.json` and `--check` verifies it.
  * **Measured with the fix** (3090, needle recall, 10 positions per size, real prompt tokens, `start-sglang.sh` as a worker runs it; the old "26k" sizes were nominal, about 0.68 of that in real tokens):

    | Real tokens | 7B no YaRN | 7B old override ×4 | 7B fixed ×4 | 7B fixed ×2 | 14B no YaRN | 14B fixed ×2 |
    |---|---|---|---|---|---|---|
    | 4k | 8/10 | 7/10 | 9/10 | 8/10 | 10/10 | 10/10 |
    | 8k | 7/10 | 2/10 | 7/10 | – | – | – |
    | 16k | 7/10 | 0/10 | 4/10 | 7/10 | 8/10 | 6/10 |
    | 25k | 9/10 | – | 8/10 | – | 4/10 | 4/10 |
    | 31k | 9/10 | 0/10 | 7/10 | 6/10 | 8/10 | 8/10 |
    | 41k | | | – | 3/10 | | 5/10 |
    | 47–50k | | | 4/10 | 5/10 | | 6/10 |
    | 61–67k | | | 0/10 | 5/10 | | |
    | 101k / 127k | | | 1/10, 2/10 | | | |

    The fix restores recall inside the native window (7B: 35/50 against 40/50 without YaRN and 9/40 with the old override). Beyond it, factor 4 is unusable on the 7B above ~60k; factor 2 holds about half the needles up to 61k on the 7B and 5–6/10 at 41–47k on the 14B (whose in-window scores are 4–10/10 on this probe, so the probe is noisy). A 24 GB card holds about 48.6k KV tokens for the 14B, so YaRN would take it from 32k to ~48k, not 64k.
  * **Why factor 2 and opt-in:** Qwen documents factor 4, which the table rules out. The 32B and the 16-bit builds are untested, and the app's own start with the box ticked hasn't been run (the override it sends is the one measured here by hand). `rope_theta` in a spec is filled in by `catalog_entry.py`.
* **FP8 on Ampere** is counted at 16-bit size (`fp8_upcast`), but SGLang 0.5.21 switches FP8 to Marlin on SM 8.0–8.8 (`can_auto_enable_marlin_fp8`), which keeps 8-bit weights. Worth measuring on the 3090 (dense and MoE) and dropping the upcast if it holds.
* **Validated on hardware:** Qwen2.5-Coder-14B-AWQ and gpt-oss-20b (MXFP4) on the 3090, and Qwen3.6-27B-AWQ split across the 3090 and the 4090 laptop. Qwen3.8-27B (Red Hat AI's W4A16 only, the smallest build) has the same `config.json` as Qwen3.6-27B apart from the transformers version, and it declares bfloat16, so it needs no `dtype` override; it hasn't been started yet. The rest is not yet validated; a model matrix run (routing probes, `scripts/eval_edits.py`, tokens/s) is still to do. MXFP4 (gpt-oss) is counted at 4 bits, since SGLang's Marlin and Triton kernels keep it packed on Ampere.
