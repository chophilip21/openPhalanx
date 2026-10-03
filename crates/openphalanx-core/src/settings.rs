//! Persistent user settings (`~/.config/openphalanx/settings.json`).

use std::path::PathBuf;

use anyhow::Result;
use serde::{Deserialize, Serialize};

use crate::docker::DEFAULT_IMAGE;
use crate::paths;
use crate::vram::ArchSpec;

/// A model added by the user (local folder or Hugging Face repo outside the catalog).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CustomModel {
    /// `local:/path` or `hf:owner/name`.
    pub key: String,
    pub label: String,
    /// Folder holding config.json and weights (for HF: once downloaded).
    pub dir: PathBuf,
    pub repo: Option<String>,
    pub revision: Option<String>,
    pub weight_bytes: u64,
    pub max_context: u32,
    pub arch: ArchSpec,
    pub quant: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    /// `catalog:<repo>` or a custom model key.
    pub selected_model: Option<String>,
    pub context_len: u32,
    pub gpu_index: u32,
    pub agent_port: u16,
    /// Override for the backend image; `None` tracks the app version.
    pub image: Option<String>,
    pub custom_models: Vec<CustomModel>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            selected_model: Some("catalog:Qwen/Qwen2.5-Coder-14B-Instruct-AWQ".into()),
            context_len: 32_768,
            gpu_index: 0,
            agent_port: 9090,
            image: None,
            custom_models: Vec::new(),
        }
    }
}

fn path() -> PathBuf {
    paths::config_dir().join("settings.json")
}

impl Settings {
    pub fn image(&self) -> String {
        self.image.clone().unwrap_or_else(|| DEFAULT_IMAGE.to_string())
    }

    pub fn load() -> Settings {
        std::fs::read(path())
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default()
    }

    pub fn save(&self) -> Result<()> {
        let p = path();
        std::fs::create_dir_all(p.parent().unwrap())?;
        let tmp = p.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        std::fs::rename(tmp, p)?;
        Ok(())
    }
}
