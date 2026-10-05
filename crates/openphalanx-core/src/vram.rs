//! VRAM requirement estimates and the start/no-start decision.
//!
//! SGLang pre-allocates a static pool (`mem_fraction_static` of total VRAM) for
//! weights plus KV cache, and needs extra memory outside that pool for the CUDA
//! context, CUDA graphs and activations. A model is allowed to start only if
//! the GPU's *currently free* memory covers all three.
//!
//! The estimate is deliberately conservative; a model that looks small by its
//! download size can still fail to load once KV cache and runtime are added.
//! Calibration (Qwen2.5-Coder-14B-AWQ, RTX 3090, SGLang 0.5.21, measured):
//! weights 9.43 GiB in VRAM for a 9.29 GiB download, CUDA graphs 1.27 GiB,
//! CUDA context and allocator 1.08 GiB, plus activation peaks during prefill.

use serde::{Deserialize, Serialize};

pub const GIB: u64 = 1024 * 1024 * 1024;

/// Weights in VRAM vs. on disk: quantized kernels repack and pad (+1.5%
/// measured for AWQ); allow 5%.
pub const WEIGHT_LOAD_FACTOR: f64 = 1.05;
/// Without native FP8 (compute capability < 8.9) assume FP8 weights may be
/// held as 16-bit, i.e. twice their download size.
pub const FP8_NATIVE_MIN_CC: f32 = 8.9;
/// KV cache for one full context window, plus 25% so a full-length request
/// can coexist with a cached prefix or a second request.
pub const KV_HEADROOM: f64 = 1.25;
/// Runtime outside the static pool = base + a share of the weights, since CUDA
/// graphs and activation buffers grow with the model. Gives 3.5 GiB for the
/// 14B AWQ model (measured 2.35 GiB before activation peaks).
pub const RUNTIME_BASE: u64 = 2 * GIB;
pub const RUNTIME_PER_WEIGHT: f64 = 0.15;
/// Below this much spare VRAM, starting is allowed but flagged as risky.
pub const TIGHT_MARGIN: u64 = 3 * GIB / 2;
/// Never hand SGLang more than this fraction of the card.
pub const MAX_MEM_FRACTION: f64 = 0.90;

/// SGLang sizes the sliding-window layers' pool at this share of the full
/// layers' tokens (`--swa-full-tokens-ratio`, default 0.8, with the radix cache on).
pub const SWA_POOL_RATIO: f64 = 0.8;
/// Hybrid linear-attention models (Qwen3-Next, Qwen3.5/3.6) keep a recurrent
/// state pool sized at this share of the KV cache (`--mamba-full-memory-ratio`).
pub const LINEAR_STATE_RATIO: f64 = 0.9;

fn is_zero(v: &u32) -> bool {
    *v == 0
}

/// Attention shape needed to size the KV cache. Dense models only set the
/// first three fields; the rest describe mixed-attention models.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchSpec {
    /// Full-attention layers (every layer in a plain dense model).
    pub kv_layers: u32,
    pub kv_heads: u32,
    pub head_dim: u32,
    /// Sliding-window layers (Gemma, gpt-oss), with their own shape.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub swa_layers: u32,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub swa_kv_heads: u32,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub swa_head_dim: u32,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub swa_window: u32,
    /// Linear-attention layers (no KV cache, but a recurrent state pool).
    #[serde(default, skip_serializing_if = "is_zero")]
    pub linear_layers: u32,
}

impl ArchSpec {
    /// A plain dense model: every layer has full attention.
    pub const fn dense(kv_layers: u32, kv_heads: u32, head_dim: u32) -> Self {
        ArchSpec { kv_layers, kv_heads, head_dim, swa_layers: 0, swa_kv_heads: 0, swa_head_dim: 0, swa_window: 0, linear_layers: 0 }
    }

    /// KV cache bytes per context token (K and V, 16-bit), as SGLang
    /// allocates it: sliding-window layers count at `SWA_POOL_RATIO`, and a
    /// hybrid linear-attention model adds its recurrent state pool.
    pub fn kv_bytes_per_token(&self) -> u64 {
        let full = 2 * self.kv_layers as u64 * self.kv_heads as u64 * self.head_dim as u64 * 2;
        let swa = 2 * self.swa_layers as u64 * self.swa_kv_heads as u64 * self.swa_head_dim as u64 * 2;
        let mut bytes = full as f64 + swa as f64 * SWA_POOL_RATIO;
        if self.linear_layers > 0 {
            bytes *= 1.0 + LINEAR_STATE_RATIO;
        }
        bytes.ceil() as u64
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Requirement {
    pub context_len: u32,
    /// Size of the downloaded weight files.
    pub download_bytes: u64,
    /// Estimated weights once loaded into VRAM.
    pub weight_bytes: u64,
    pub kv_bytes: u64,
    pub overhead_bytes: u64,
    pub total_bytes: u64,
    /// FP8 weights counted at 16-bit size because the GPU lacks native FP8.
    pub fp8_upcast: bool,
}

/// Conservative VRAM need for serving `context_len` tokens. `quant` is the
/// model's quantization label (e.g. "AWQ 4-bit", "fp8") and
/// `compute_capability` the target GPU's, when known.
pub fn requirement(
    download_bytes: u64,
    quant: Option<&str>,
    arch: &ArchSpec,
    context_len: u32,
    compute_capability: Option<f32>,
) -> Requirement {
    let is_fp8 = quant.is_some_and(|q| q.to_ascii_lowercase().contains("fp8"));
    let fp8_upcast = is_fp8 && compute_capability.is_none_or(|cc| cc + 0.001 < FP8_NATIVE_MIN_CC);
    let factor = WEIGHT_LOAD_FACTOR * if fp8_upcast { 2.0 } else { 1.0 };
    let weight_bytes = (download_bytes as f64 * factor) as u64;
    let kv_bytes = (arch.kv_bytes_per_token() as f64 * context_len as f64 * KV_HEADROOM) as u64;
    let overhead_bytes = RUNTIME_BASE + (weight_bytes as f64 * RUNTIME_PER_WEIGHT) as u64;
    Requirement {
        context_len,
        download_bytes,
        weight_bytes,
        kv_bytes,
        overhead_bytes,
        total_bytes: weight_bytes + kv_bytes + overhead_bytes,
        fp8_upcast,
    }
}

/// The need when one model is split across `servers` machines (pipeline
/// parallel): weights and KV cache are shared out, but every machine pays its
/// own runtime memory (CUDA context, activations, graphs).
pub fn split_across(req: &Requirement, servers: u32) -> Requirement {
    let extra = req.overhead_bytes * u64::from(servers.max(1) - 1);
    Requirement {
        overhead_bytes: req.overhead_bytes + extra,
        total_bytes: req.total_bytes + extra,
        ..*req
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Fit {
    Ok,
    /// Fits, but with little spare memory; other GPU apps could push it over.
    Tight,
    /// Does not fit; starting is refused.
    Insufficient,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct FitCheck {
    pub fit: Fit,
    pub required_bytes: u64,
    pub free_bytes: u64,
    /// Free minus required; negative when it does not fit.
    pub headroom_bytes: i64,
    pub message: String,
}

/// Smallest context the app offers.
pub const MIN_CONTEXT: u32 = 2048;

/// The longest context (in steps of 1,024 tokens, up to `max_context`) whose
/// requirement still fits in `free_bytes`; `None` if not even `MIN_CONTEXT` does.
pub fn max_fitting_context(max_context: u32, free_bytes: u64, need: impl Fn(u32) -> Requirement) -> Option<u32> {
    let fits = |ctx: u32| need(ctx).total_bytes <= free_bytes;
    if max_context < MIN_CONTEXT || !fits(MIN_CONTEXT) {
        return None;
    }
    // The requirement grows with the context, so binary search the 1k steps.
    let (mut lo, mut hi) = (MIN_CONTEXT / 1024, max_context / 1024);
    if fits(max_context) {
        return Some(max_context);
    }
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        if fits(mid * 1024) {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    Some(lo * 1024)
}

pub fn check(req: &Requirement, free_bytes: u64) -> FitCheck {
    let headroom = free_bytes as i64 - req.total_bytes as i64;
    let (fit, message) = if headroom < 0 {
        (
            Fit::Insufficient,
            format!(
                "Needs {} but only {} of VRAM is free. Pick a smaller model or a shorter context.",
                fmt_gib(req.total_bytes),
                fmt_gib(free_bytes)
            ),
        )
    } else if (headroom as u64) < TIGHT_MARGIN {
        (
            Fit::Tight,
            format!(
                "Fits with only {} to spare. Close other GPU apps before starting.",
                fmt_gib(headroom as u64)
            ),
        )
    } else {
        (Fit::Ok, format!("Fits with {} to spare.", fmt_gib(headroom as u64)))
    };
    FitCheck {
        fit,
        required_bytes: req.total_bytes,
        free_bytes,
        headroom_bytes: headroom,
        message,
    }
}

/// `--mem-fraction-static` for SGLang: everything currently free minus the
/// runtime overhead, so the static pool can never claim memory another process
/// already holds. Returns `None` when the model does not fit.
pub fn mem_fraction_static(req: &Requirement, free_bytes: u64, total_bytes: u64) -> Option<f64> {
    if total_bytes == 0 || free_bytes < req.total_bytes {
        return None;
    }
    let budget = (free_bytes - req.overhead_bytes) as f64;
    let fraction = (budget / total_bytes as f64).min(MAX_MEM_FRACTION);
    // The cap can only bite on cards far larger than the model; still verify.
    let static_needed = (req.weight_bytes + req.kv_bytes) as f64 / total_bytes as f64;
    (fraction >= static_needed).then(|| (fraction * 1000.0).floor() / 1000.0)
}

pub fn fmt_gib(bytes: u64) -> String {
    format!("{:.1} GiB", bytes as f64 / GIB as f64)
}

#[cfg(test)]
mod tests {
    use super::*;

    const QWEN14B: ArchSpec = ArchSpec::dense(48, 8, 128);
    const QWEN14B_AWQ_DOWNLOAD: u64 = 9_980_170_584;
    const RTX3090_TOTAL: u64 = 24_576 * 1024 * 1024;
    const AMPERE: Option<f32> = Some(8.6);

    fn qwen14b_awq() -> Requirement {
        requirement(QWEN14B_AWQ_DOWNLOAD, Some("AWQ 4-bit"), &QWEN14B, 32_768, AMPERE)
    }

    #[test]
    fn kv_size_matches_sglang_measurement() {
        // SGLang reported 51604 tokens -> K 4.72 GB + V 4.72 GB on the 3090.
        let kv = QWEN14B.kv_bytes_per_token() * 51_604;
        let reported = 2.0 * 4.72 * GIB as f64;
        assert!((kv as f64 - reported).abs() / reported < 0.01, "kv={kv}");
    }

    #[test]
    fn estimate_covers_measured_footprint() {
        // Measured: weights 9.43 GiB, graphs 1.27 GiB, context/allocator 1.08 GiB,
        // and one 32k-token window of KV (6.0 GiB).
        let measured = 9.43 + 1.27 + 1.08 + 6.0;
        let r = qwen14b_awq();
        let est = r.total_bytes as f64 / GIB as f64;
        assert!(est > measured + 1.0, "estimate {est:.2} GiB vs measured {measured:.2} GiB");
        assert!(r.weight_bytes as f64 / GIB as f64 > 9.43);
        // Still fits an idle 3090 (desktop using ~1 GiB) without being "tight".
        let free = RTX3090_TOTAL - GIB;
        assert_eq!(check(&r, free).fit, Fit::Ok, "total {:.2} GiB", est);
    }

    #[test]
    fn mem_fraction_leaves_runtime_room() {
        let r = qwen14b_awq();
        let free = RTX3090_TOTAL - GIB;
        let frac = mem_fraction_static(&r, free, RTX3090_TOTAL).unwrap();
        assert!(frac > 0.75 && frac <= MAX_MEM_FRACTION, "frac={frac}");
        let outside_pool = free as f64 - frac * RTX3090_TOTAL as f64;
        assert!(outside_pool >= r.overhead_bytes as f64 * 0.99);
    }

    #[test]
    fn refuses_when_free_memory_is_short() {
        let r = qwen14b_awq();
        let free = r.total_bytes - 1;
        assert_eq!(check(&r, free).fit, Fit::Insufficient);
        assert_eq!(mem_fraction_static(&r, free, RTX3090_TOTAL), None);
    }

    #[test]
    fn tight_band() {
        let r = qwen14b_awq();
        assert_eq!(check(&r, r.total_bytes + GIB).fit, Fit::Tight);
        assert_eq!(check(&r, r.total_bytes + 2 * GIB).fit, Fit::Ok);
    }

    #[test]
    fn fp8_counts_double_without_native_support() {
        let arch = ArchSpec::dense(48, 4, 128);
        let ampere = requirement(10 * GIB, Some("FP8"), &arch, 8192, Some(8.6));
        let ada = requirement(10 * GIB, Some("fp8"), &arch, 8192, Some(8.9));
        let unknown = requirement(10 * GIB, Some("FP8"), &arch, 8192, None);
        assert!(ampere.fp8_upcast && unknown.fp8_upcast && !ada.fp8_upcast);
        assert_eq!(ampere.weight_bytes, 2 * ada.weight_bytes);
    }

    #[test]
    fn qwen32b_awq_does_not_fit_a_24gb_card() {
        let arch = ArchSpec::dense(64, 8, 128);
        let r = requirement(19_328_993_904, Some("AWQ 4-bit"), &arch, 8192, AMPERE);
        assert_eq!(check(&r, RTX3090_TOTAL - GIB).fit, Fit::Insufficient);
    }

    #[test]
    fn mixed_attention_kv() {
        // gpt-oss-20b: 12 full + 12 sliding layers, 8 KV heads of 64.
        let gpt_oss = ArchSpec { swa_layers: 12, swa_kv_heads: 8, swa_head_dim: 64, swa_window: 128, ..ArchSpec::dense(12, 8, 64) };
        assert_eq!(gpt_oss.kv_bytes_per_token(), (24_576.0 * (1.0 + SWA_POOL_RATIO)).ceil() as u64);
        // Qwen3.6-27B: 16 full-attention layers plus the linear-attention state pool.
        let qwen36 = ArchSpec { linear_layers: 48, ..ArchSpec::dense(16, 4, 256) };
        let full = 2 * 16 * 4 * 256 * 2;
        assert_eq!(qwen36.kv_bytes_per_token(), (full as f64 * (1.0 + LINEAR_STATE_RATIO)).ceil() as u64);
        // A dense spec serializes without the optional fields.
        assert_eq!(serde_json::to_string(&ArchSpec::dense(1, 2, 3)).unwrap(), r#"{"kv_layers":1,"kv_heads":2,"head_dim":3}"#);
    }

    #[test]
    fn longest_fitting_context() {
        let need = |ctx| requirement(9_980_170_584, Some("AWQ 4-bit"), &ArchSpec::dense(48, 8, 128), ctx, Some(8.6));
        let free = 24 * GIB;
        let best = max_fitting_context(131_072, free, need).unwrap();
        assert!(need(best).total_bytes <= free && need(best + 1024).total_bytes > free);
        assert_eq!(max_fitting_context(32_768, 100 * GIB, need), Some(32_768));
        assert_eq!(max_fitting_context(32_768, GIB, need), None);
    }

    #[test]
    fn a_split_model_pays_runtime_memory_on_every_server() {
        let arch = ArchSpec { kv_layers: 48, kv_heads: 8, head_dim: 128, ..Default::default() };
        let one = requirement(9 << 30, Some("AWQ 4-bit"), &arch, 32768, Some(8.6));
        assert_eq!(split_across(&one, 1), one);
        let two = split_across(&one, 2);
        assert_eq!(two.weight_bytes, one.weight_bytes);
        assert_eq!(two.kv_bytes, one.kv_bytes);
        assert_eq!(two.total_bytes, one.total_bytes + one.overhead_bytes);
    }
}
