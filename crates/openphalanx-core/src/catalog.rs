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
}

impl CatalogEntry {
    /// Total parameters in billions, parsed from e.g. "30.5B (3.3B active)".
    pub fn params_billions(&self) -> Option<f32> {
        self.params.split('B').next()?.trim().parse().ok()
    }

    pub fn source_url(&self) -> String {
        format!("https://huggingface.co/{}/tree/{}", self.id, self.revision)
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
        for e in &entries {
            assert_eq!(e.revision.len(), 40, "{} revision must be a full commit sha", e.id);
            assert!(e.id.contains('/'), "{}", e.id);
            assert!(e.weight_bytes > 1_000_000_000, "{}", e.id);
            assert!(e.arch.kv_layers > 0 && e.arch.kv_heads > 0 && e.arch.head_dim > 0);
            assert!(e.params_billions().is_some_and(|p| p > 0.0), "{} params", e.id);
            assert!(!e.family.is_empty(), "{} family", e.id);
            // Weights are counted once (no duplicate consolidated copies).
            assert!(e.arch.kv_bytes_per_token() > 0, "{} arch", e.id);
        }
    }
}
