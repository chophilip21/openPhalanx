//! VRAM requirement estimates and the start/no-start decision.
//!
//! SGLang pre-allocates a static pool (`mem_fraction_static` of total VRAM) for
//! weights plus KV cache, and needs extra memory outside that pool for the CUDA
//! context, CUDA graphs and activations. A model is allowed to start only if
//! the GPU's *currently free* memory covers weights + KV cache for one full
//! context window + that runtime overhead.

use serde::{Deserialize, Serialize};

pub const GIB: u64 = 1024 * 1024 * 1024;

/// CUDA context, CUDA graphs and activations, outside SGLang's static pool.
/// Measured about 1.5 GiB for a 14B model on an RTX 3090; doubled for safety.
pub const RUNTIME_OVERHEAD: u64 = 3 * GIB;
/// Below this much spare VRAM, starting is allowed but flagged as risky.
pub const TIGHT_MARGIN: u64 = 3 * GIB / 2;
/// Never hand SGLang more than this fraction of the card.
pub const MAX_MEM_FRACTION: f64 = 0.90;

/// Attention shape needed to size the KV cache.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArchSpec {
    /// Layers that keep a KV cache (all layers, except in hybrid-attention models).
    pub kv_layers: u32,
    pub kv_heads: u32,
    pub head_dim: u32,
}

impl ArchSpec {
    /// K and V, fp16/bf16 KV cache.
    pub fn kv_bytes_per_token(&self) -> u64 {
        2 * self.kv_layers as u64 * self.kv_heads as u64 * self.head_dim as u64 * 2
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Requirement {
    pub context_len: u32,
    pub weight_bytes: u64,
    pub kv_bytes: u64,
    pub overhead_bytes: u64,
    pub total_bytes: u64,
}

pub fn requirement(weight_bytes: u64, arch: &ArchSpec, context_len: u32) -> Requirement {
    let kv_bytes = arch.kv_bytes_per_token() * context_len as u64;
    Requirement {
        context_len,
        weight_bytes,
        kv_bytes,
        overhead_bytes: RUNTIME_OVERHEAD,
        total_bytes: weight_bytes + kv_bytes + RUNTIME_OVERHEAD,
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
    let budget = (free_bytes - RUNTIME_OVERHEAD) as f64;
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

    const QWEN14B: ArchSpec = ArchSpec { kv_layers: 48, kv_heads: 8, head_dim: 128 };
    const QWEN14B_AWQ_WEIGHTS: u64 = 9_980_170_584;
    const RTX3090_TOTAL: u64 = 24_576 * 1024 * 1024;

    #[test]
    fn kv_size_matches_sglang_measurement() {
        // SGLang reported 51604 tokens -> K 4.72 GB + V 4.72 GB on the 3090.
        let kv = QWEN14B.kv_bytes_per_token() * 51_604;
        let reported = 2.0 * 4.72 * GIB as f64;
        assert!((kv as f64 - reported).abs() / reported < 0.01, "kv={kv}");
    }

    #[test]
    fn qwen14b_awq_fits_on_idle_3090() {
        let req = requirement(QWEN14B_AWQ_WEIGHTS, &QWEN14B, 32_768);
        // ~9.3 GiB weights + 6 GiB KV + 3 GiB overhead.
        assert!(req.total_bytes > 18 * GIB && req.total_bytes < 19 * GIB);
        let free = RTX3090_TOTAL - GIB / 2; // desktop compositor
        assert_eq!(check(&req, free).fit, Fit::Ok);
        let frac = mem_fraction_static(&req, free, RTX3090_TOTAL).unwrap();
        assert!(frac > 0.8 && frac <= MAX_MEM_FRACTION, "frac={frac}");
    }

    #[test]
    fn refuses_when_free_memory_is_short() {
        let req = requirement(QWEN14B_AWQ_WEIGHTS, &QWEN14B, 32_768);
        let free = req.total_bytes - 1;
        assert_eq!(check(&req, free).fit, Fit::Insufficient);
        assert_eq!(mem_fraction_static(&req, free, RTX3090_TOTAL), None);
    }

    #[test]
    fn tight_band() {
        let req = requirement(QWEN14B_AWQ_WEIGHTS, &QWEN14B, 32_768);
        assert_eq!(check(&req, req.total_bytes + GIB).fit, Fit::Tight);
        assert_eq!(check(&req, req.total_bytes + 2 * GIB).fit, Fit::Ok);
    }

    #[test]
    fn static_pool_covers_weights_and_kv() {
        let req = requirement(QWEN14B_AWQ_WEIGHTS, &QWEN14B, 32_768);
        let free = req.total_bytes; // exactly enough
        let frac = mem_fraction_static(&req, free, RTX3090_TOTAL).unwrap();
        let pool = frac * RTX3090_TOTAL as f64;
        // Flooring to 3 decimals may shave up to 0.1% of the card.
        assert!(pool + RTX3090_TOTAL as f64 * 0.001 >= (req.weight_bytes + req.kv_bytes) as f64);
    }
}
