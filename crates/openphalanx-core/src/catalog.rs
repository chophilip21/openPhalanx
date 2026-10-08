//! Curated catalog of coding models. Openphalanx never redistributes weights:
//! every entry points to the publisher's own Hugging Face repo, pinned to a
//! commit, and downloads are verified against that commit's SHA-256 hashes.
//! Sizes and attention shapes come from each repo's file listing and config.json.

use serde::{Deserialize, Serialize};

use crate::vram::ArchSpec;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CatalogEntry {
    /// Hugging Face repo id, e.g. `Qwen/Qwen2.5-Coder-14B-Instruct-AWQ`.
    pub id: String,
    /// Pinned commit; downloads use exactly this revision.
    pub revision: String,
    pub name: String,
    /// Model family for grouping and filtering ("Qwen", "Gemma", "gpt-oss", "Devstral").
    pub family: String,
    /// Set for community quantizations: who made them (the base model's
    /// publisher is in the name). Official repos leave it empty.
    #[serde(default)]
    pub quantized_by: Option<String>,
    pub params: String,
    pub quant: String,
    /// Total size of the `.safetensors` files.
    pub weight_bytes: u64,
    pub max_context: u32,
    pub license: String,
    /// First published on Hugging Face (YYYY-MM-DD; for community
    /// quantizations, the base model's date).
    #[serde(default)]
    pub released: Option<String>,
    pub arch: ArchSpec,
    /// Verified end to end on real hardware.
    #[serde(default)]
    pub tested: bool,
    /// Specialized for code by its publisher (Qwen Coder, Devstral, DeepSeek Coder).
    #[serde(default)]
    pub coding: bool,
    #[serde(default)]
    pub min_compute_capability: Option<f32>,
    #[serde(default)]
    pub notes: Option<String>,
    /// Edit format the coding agent should use with this model ("diff",
    /// "whole", "udiff"…); `None` means the default ("diff").
    #[serde(default)]
    pub edit_format: Option<String>,
    /// SGLang `--reasoning-parser` for models that emit thinking blocks.
    #[serde(default)]
    pub reasoning_parser: Option<String>,
    /// SGLang `--dtype`, when the checkpoint's own is wrong for SGLang. Some
    /// 4-bit builds of hybrid linear-attention models declare float16, while
    /// SGLang keeps their recurrent state in bfloat16, and the first prefill
    /// fails on the mix.
    #[serde(default)]
    pub dtype: Option<String>,
    /// The publisher's long-context mode (YaRN), turned on only when the
    /// server starts with a context above the native window.
    #[serde(default)]
    pub yarn: Option<Yarn>,
}

impl CatalogEntry {
    /// Total parameters in billions, parsed from e.g. "30.5B (3.3B active)".
    /// The leading size in billions: "14.7B" → 14.7, "1T (32B active)" → 1000.
    pub fn params_billions(&self) -> Option<f32> {
        let s = self.params.trim();
        let end = s.find(|c: char| !(c.is_ascii_digit() || c == '.')).unwrap_or(s.len());
        let n: f32 = s[..end].parse().ok()?;
        match s[end..].chars().next() {
            Some('T') => Some(n * 1000.0),
            Some('B') => Some(n),
            _ => None,
        }
    }

    pub fn source_url(&self) -> String {
        format!("https://huggingface.co/{}/tree/{}", self.id, self.revision)
    }
}

/// YaRN rope scaling as a publisher documents it ("add this to config.json
/// for contexts beyond 32k"): the model then reaches `factor` × its native
/// window. Static YaRN can slightly lower quality on short inputs, so it is
/// used only when the chosen context needs it.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Yarn {
    pub factor: f32,
    #[serde(rename = "original_max_position_embeddings")]
    pub original_max: u32,
}

impl Yarn {
    /// The longest context with YaRN on.
    pub fn max_context(&self) -> u32 {
        (self.original_max as f64 * self.factor as f64) as u32
    }

    /// Sane bounds; a cluster member checks the host's numbers with this.
    pub fn check(&self) -> anyhow::Result<()> {
        anyhow::ensure!(self.factor.is_finite() && (1.0..=16.0).contains(&self.factor), "invalid YaRN factor {}", self.factor);
        anyhow::ensure!((1024..=1 << 20).contains(&self.original_max), "invalid YaRN native window {}", self.original_max);
        Ok(())
    }

    /// SGLang's `--json-model-override-args`, built here from the two numbers
    /// (never from text a host or a catalog could slip flags into).
    ///
    /// `max_position_embeddings` is raised too: SGLang 0.5.21 takes the context
    /// limit from it and ignores the factor when `original_max_position_embeddings`
    /// is present (`get_context_length`), so Qwen's snippet alone still capped
    /// at 32k. The YaRN math reads only `factor` and the original window.
    pub fn override_json(&self) -> String {
        serde_json::json!({
          "max_position_embeddings": self.max_context(),
          "rope_scaling": {
            "rope_type": "yarn",
            "type": "yarn",
            "factor": self.factor,
            "original_max_position_embeddings": self.original_max,
        } })
        .to_string()
    }
}

pub fn catalog() -> Vec<CatalogEntry> {
    serde_json::from_str(include_str!("../catalog.json")).expect("embedded catalog.json is valid")
}

pub fn find(id: &str) -> Option<CatalogEntry> {
    catalog().into_iter().find(|e| e.id == id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_is_well_formed() {
        let entries = catalog();
        assert!(!entries.is_empty());
        assert!(entries.iter().any(|e| e.tested));
        let with = |p: &str| CatalogEntry { params: p.into(), ..entries[0].clone() };
        assert_eq!(with("14.7B").params_billions(), Some(14.7));
        assert_eq!(with("1T (32B active)").params_billions(), Some(1000.0));
        assert_eq!(with("2.8T MoE").params_billions(), Some(2800.0));
        for e in &entries {
            assert_eq!(e.revision.len(), 40, "{} revision must be a full commit sha", e.id);
            assert!(e.id.contains('/'), "{}", e.id);
            assert!(e.weight_bytes > 100_000_000, "{}", e.id); // Qwen2.5-Coder 0.5B AWQ is 0.7 GB
            assert!(e.arch.kv_layers > 0 && e.arch.kv_heads > 0 && e.arch.head_dim > 0);
            assert!(e.params_billions().is_some_and(|p| p > 0.0), "{} params", e.id);
            assert!(!e.family.is_empty(), "{} family", e.id);
            // Weights are counted once (no duplicate consolidated copies).
            assert!(e.arch.kv_bytes_per_token() > 0, "{} arch", e.id);
        }
    }
}
