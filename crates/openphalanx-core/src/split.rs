//! One model across several servers (pipeline parallel, cluster step 3).
//!
//! SGLang runs one process per machine (`--pp-size N --nnodes N`), each
//! holding a contiguous slice of the layers (`SGLANG_PP_LAYER_PARTITION`).
//! The host is rank 0: it also serves HTTP behind the gateway. Members run
//! headless workers. This module decides the slices; `docker` launches them.

use std::path::Path;

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::vram::{fmt_gib, runtime_overhead, Requirement, RUNTIME_BASE, RUNTIME_PER_WEIGHT};

/// torch's rendezvous on the host (rank 0). SGLang also uses the next few
/// ports for its own sockets, so `DIST_PORT..DIST_PORT+16` must be free.
pub const DIST_PORT: u16 = 9100;
/// SGLang's HTTP port when the backend shares the host's network.
pub const SGLANG_HOST_PORT: u16 = 9096;

/// What the plan needs from a model's `config.json`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelShape {
    pub layers: u32,
    /// One embedding matrix (vocab × hidden, 16-bit). The first stage holds
    /// the input embedding and the last the output head, whether or not the
    /// model ties them.
    pub embed_bytes: u64,
}

impl ModelShape {
    pub fn read(model_dir: &Path) -> Result<Self> {
        let path = model_dir.join("config.json");
        let text = std::fs::read_to_string(&path).with_context(|| format!("cannot read {}", path.display()))?;
        Self::parse(&text)
    }

    fn parse(config: &str) -> Result<Self> {
        let c: serde_json::Value = serde_json::from_str(config)?;
        // Multimodal models nest the language model's settings.
        let t = if c.get("text_config").is_some_and(|t| t.is_object()) { &c["text_config"] } else { &c };
        let field = |name: &str| t[name].as_u64().or_else(|| c[name].as_u64());
        let layers = field("num_hidden_layers").context("config.json has no num_hidden_layers")? as u32;
        let vocab = field("vocab_size").unwrap_or(0);
        let hidden = field("hidden_size").unwrap_or(0);
        Ok(Self { layers, embed_bytes: vocab * hidden * 2 })
    }
}

/// A server that can take part, and the VRAM it can give.
#[derive(Debug, Clone)]
pub struct Capacity {
    pub id: String,
    pub name: String,
    pub free_bytes: u64,
}

/// One server's slice of the model.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Stage {
    pub id: String,
    pub name: String,
    pub rank: u32,
    pub layers: u32,
    pub free_bytes: u64,
    /// This stage's share, sized like a single-server requirement.
    pub requirement: Requirement,
}

/// Splits `shape.layers` across `nodes` (in rank order, the host first) in
/// proportion to the VRAM each can give after its fixed costs: the fixed
/// runtime memory on every server, the embedding on the first, the output
/// head on the last. Runtime memory that grows with the weights is charged
/// per server for the weights it holds (charging every server for the whole
/// model left SGLang a KV cache shorter than the context window, with
/// gigabytes unused). Every server gets at least one layer.
pub fn plan(req: &Requirement, shape: &ModelShape, nodes: &[Capacity]) -> Result<Vec<Stage>, String> {
    let n = nodes.len();
    let total_layers = shape.layers.max(1);
    if n == 0 {
        return Err("No servers to split the model across.".into());
    }
    if (n as u32) > total_layers {
        return Err(format!("The model has {total_layers} layers, too few for {n} servers."));
    }
    let ends = |i: usize| shape.embed_bytes * (u64::from(i == 0) + u64::from(i == n - 1));
    let body_weights = req.weight_bytes.saturating_sub(2 * shape.embed_bytes);
    let layer_weights = body_weights.div_ceil(u64::from(total_layers));
    let layer_kv = req.kv_bytes.div_ceil(u64::from(total_layers));
    let with_runtime = |w: u64| w + (w as f64 * RUNTIME_PER_WEIGHT) as u64;
    let per_layer = (with_runtime(layer_weights) + layer_kv).max(1);
    let fixed = |i: usize| RUNTIME_BASE + with_runtime(ends(i));

    let caps: Vec<u64> = nodes.iter().enumerate().map(|(i, c)| c.free_bytes.saturating_sub(fixed(i)) / per_layer).collect();
    if let Some((i, _)) = caps.iter().enumerate().find(|(_, c)| **c == 0) {
        return Err(format!(
            "{} has {} of VRAM free, not enough for even one layer of this model next to its runtime memory.",
            nodes[i].name,
            fmt_gib(nodes[i].free_bytes)
        ));
    }
    let cap_sum: u64 = caps.iter().sum();
    if cap_sum < u64::from(total_layers) {
        let need: u64 = (0..n).map(fixed).sum::<u64>() + per_layer * u64::from(total_layers);
        let free: u64 = nodes.iter().map(|c| c.free_bytes).sum();
        return Err(format!(
            "Split across {n} servers it needs about {} but they have {} free together. Pick a smaller model or a shorter context.",
            fmt_gib(need),
            fmt_gib(free)
        ));
    }

    // Proportional shares (at least one each), then hand out the rest to the
    // servers with the most room left.
    let mut layers: Vec<u64> =
        caps.iter().map(|c| (u64::from(total_layers) * c / cap_sum).clamp(1, *c)).collect();
    while layers.iter().sum::<u64>() > u64::from(total_layers) {
        let i = (0..n).filter(|&i| layers[i] > 1).max_by_key(|&i| layers[i]).expect("more layers than servers");
        layers[i] -= 1;
    }
    while layers.iter().sum::<u64>() < u64::from(total_layers) {
        let i = (0..n).max_by_key(|&i| caps[i] - layers[i]).expect("capacity checked");
        layers[i] += 1;
    }

    Ok(nodes
        .iter()
        .enumerate()
        .map(|(i, c)| {
            let weight_bytes = ends(i) + layers[i] * layer_weights;
            let kv_bytes = layers[i] * layer_kv;
            let overhead_bytes = runtime_overhead(weight_bytes);
            Stage {
                id: c.id.clone(),
                name: c.name.clone(),
                rank: i as u32,
                layers: layers[i] as u32,
                free_bytes: c.free_bytes,
                requirement: Requirement {
                    weight_bytes,
                    kv_bytes,
                    overhead_bytes,
                    total_bytes: weight_bytes + kv_bytes + overhead_bytes,
                    ..*req
                },
            }
        })
        .collect())
}

/// `SGLANG_PP_LAYER_PARTITION`: the layer count of each rank.
pub fn partition(stages: &[Stage]) -> String {
    stages.iter().map(|s| s.layers.to_string()).collect::<Vec<_>>().join(",")
}

/// "rig-3090: 40 layers (14.2 GiB) · laptop-4090: 24 layers (9.1 GiB)".
pub fn describe(stages: &[Stage]) -> String {
    stages
        .iter()
        .map(|s| format!("{}: {} layers ({})", s.name, s.layers, fmt_gib(s.requirement.total_bytes)))
        .collect::<Vec<_>>()
        .join(" · ")
}

/// The network interface that holds `ip`, for NCCL and gloo (otherwise they
/// may pick Docker's bridge or a VPN).
pub fn interface_for(ip: std::net::IpAddr) -> Option<String> {
    let out = std::process::Command::new("ip").args(["-o", "addr", "show"]).output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let needle = format!(" {ip}/");
    text.lines().find(|l| l.contains(&needle)).and_then(|l| l.split_whitespace().nth(1)).map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

    const GIB: u64 = 1 << 30;

    fn req() -> Requirement {
        Requirement {
            context_len: 32768,
            download_bytes: 17 * GIB,
            weight_bytes: 18 * GIB,
            kv_bytes: 8 * GIB,
            overhead_bytes: 2 * GIB,
            total_bytes: 28 * GIB,
            fp8_upcast: false,
        }
    }
    const SHAPE: ModelShape = ModelShape { layers: 64, embed_bytes: GIB };
    fn node(name: &str, free: u64) -> Capacity {
        Capacity { id: name.into(), name: name.into(), free_bytes: free }
    }

    #[test]
    fn reads_layers_from_plain_and_nested_configs() {
        let plain = r#"{"num_hidden_layers": 36, "vocab_size": 151936, "hidden_size": 4096}"#;
        assert_eq!(ModelShape::parse(plain).unwrap(), ModelShape { layers: 36, embed_bytes: 151936 * 4096 * 2 });
        let nested = r#"{"architectures": ["X"], "text_config": {"num_hidden_layers": 64, "vocab_size": 10, "hidden_size": 4}}"#;
        assert_eq!(ModelShape::parse(nested).unwrap().layers, 64);
        assert!(ModelShape::parse("{}").is_err());
    }

    #[test]
    fn layers_follow_free_vram_and_every_stage_fits() {
        let stages = plan(&req(), &SHAPE, &[node("3090", 22 * GIB), node("4090", 15 * GIB)]).unwrap();
        assert_eq!(stages.iter().map(|s| s.layers).sum::<u32>(), 64);
        assert!(stages[0].layers > stages[1].layers, "the bigger card takes more: {}", describe(&stages));
        for s in &stages {
            assert!(s.requirement.total_bytes <= s.free_bytes, "{} over: {}", s.name, describe(&stages));
        }
        assert_eq!(stages[1].rank, 1);
        assert_eq!(partition(&stages), format!("{},{}", stages[0].layers, stages[1].layers));
    }

    #[test]
    fn refuses_when_the_pool_is_too_small_or_a_server_cant_hold_a_layer() {
        let small = plan(&req(), &SHAPE, &[node("a", 12 * GIB), node("b", 10 * GIB)]).unwrap_err();
        assert!(small.contains("free together"), "{small}");
        let tiny = plan(&req(), &SHAPE, &[node("a", 40 * GIB), node("b", 2 * GIB)]).unwrap_err();
        assert!(tiny.starts_with("b has"), "{tiny}");
    }

    #[test]
    fn one_server_gets_everything() {
        let stages = plan(&req(), &SHAPE, &[node("a", 40 * GIB)]).unwrap();
        assert_eq!(stages[0].layers, 64);
        let r = stages[0].requirement;
        assert!(r.weight_bytes.abs_diff(req().weight_bytes) < GIB / 8, "all the weights, rounding aside");
        assert_eq!(r.overhead_bytes, runtime_overhead(r.weight_bytes));
        assert_eq!(r.total_bytes, r.weight_bytes + r.kv_bytes + r.overhead_bytes);
    }
}
