//! Persistent user settings (`~/.config/openphalanx/settings.json`).

use std::path::PathBuf;

use anyhow::Result;
use serde::{Deserialize, Serialize};

use crate::docker::default_image;
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
    /// The first selected GPU. Kept for versions that know only one card;
    /// `gpus` is what decides.
    pub gpu_index: u32,
    /// The GPUs the server may use, by index; `None`: every GPU in the
    /// machine (the default). A model that fits one of them runs on one;
    /// only a model that doesn't is spread over them (experimental).
    pub gpus: Option<Vec<u32>>,
    pub agent_port: u16,
    /// The admin API (this app ↔ the backend, loopback only).
    pub admin_port: u16,
    /// The `oppxs` control API (loopback only).
    pub control_port: u16,
    /// Override for the backend image; `None` tracks the app version.
    pub image: Option<String>,
    pub custom_models: Vec<CustomModel>,
    /// Run a private SearXNG next to the backend for client web search.
    pub web_search: bool,
    /// Days a client's pairing lasts before it must pair again; `None`:
    /// never expires. Default one week.
    pub pairing_ttl_days: Option<u32>,
    /// Let models with a long-context mode (the catalog's `yarn`) go past
    /// their native window. Off by default: recall beyond the window is
    /// clearly worse than inside it (measured; see CLAUDE.md).
    pub long_context: bool,
    /// Privacy: show IP addresses with their first digits hidden
    /// (192.168.1.74 as xxx.xx8.1.74) everywhere the app shows one. Display
    /// only; what is sent or copied is unchanged.
    pub hide_ips: bool,
    /// The app's theme: "light" or "dark"; None follows the system.
    pub theme: Option<String>,
    /// Keys this version doesn't know (written by a newer one): kept, so a
    /// downgrade and upgrade doesn't lose them.
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

/// The choices the app offers for `pairing_ttl_days`.
pub const PAIRING_TTL_CHOICES: [Option<u32>; 5] = [Some(1), Some(7), Some(30), Some(365), None];

impl Default for Settings {
    fn default() -> Self {
        Self {
            selected_model: Some("catalog:Qwen/Qwen2.5-Coder-14B-Instruct-AWQ".into()),
            context_len: 32_768,
            gpu_index: 0,
            gpus: None,
            agent_port: 9090,
            admin_port: crate::docker::ADMIN_PORT,
            control_port: crate::ports::CONTROL_PORT,
            image: None,
            custom_models: Vec::new(),
            web_search: true,
            pairing_ttl_days: Some(7),
            long_context: false,
            hide_ips: false,
            theme: None,
            extra: serde_json::Map::new(),
        }
    }
}

fn path() -> PathBuf {
    paths::config_dir().join("settings.json")
}

impl Settings {
    pub fn image(&self) -> String {
        self.image.clone().unwrap_or_else(default_image)
    }

    /// The saved settings. They must stick across restarts and updates, so a
    /// value that doesn't read (a wrong type, written by hand or by another
    /// version) costs only that value, never the rest; a file that isn't
    /// JSON at all is kept beside it as `settings.json.unreadable` before
    /// anything overwrites it.
    pub fn load() -> Settings {
        let p = path();
        let Ok(bytes) = std::fs::read(&p) else { return Settings::default() };
        match Settings::from_json(&bytes) {
            Some(s) => s,
            None => {
                let _ = std::fs::copy(&p, p.with_extension("json.unreadable"));
                Settings::default()
            }
        }
    }

    /// Settings from a file's bytes, field by field (None: not a JSON object).
    pub fn from_json(bytes: &[u8]) -> Option<Settings> {
        let serde_json::Value::Object(saved) = serde_json::from_slice(bytes).ok()? else { return None };
        // A file from before several GPUs could be chosen: it keeps the one
        // card it names, so an update doesn't move a working setup onto
        // other cards. Only a new install starts with all of them.
        let one_card = saved.contains_key("gpu_index") && !saved.contains_key("gpus");
        let mut s = Self::read_fields(saved)?;
        if one_card {
            s.gpus = Some(vec![s.gpu_index]);
        }
        Some(s)
    }

    fn read_fields(saved: serde_json::Map<String, serde_json::Value>) -> Option<Settings> {
        if let Ok(s) = serde_json::from_value(serde_json::Value::Object(saved.clone())) {
            return Some(s);
        }
        let serde_json::Value::Object(mut kept) = serde_json::to_value(Settings::default()).ok()? else { return None };
        for (key, value) in saved {
            let mut trial = kept.clone();
            trial.insert(key.clone(), value.clone());
            if serde_json::from_value::<Settings>(serde_json::Value::Object(trial)).is_ok() {
                kept.insert(key, value);
            }
        }
        serde_json::from_value(serde_json::Value::Object(kept)).ok()
    }

    /// The selected GPUs that are in the machine, in index order.
    pub fn selected_gpus(&self, present: &[crate::gpu::GpuInfo]) -> Vec<crate::gpu::GpuInfo> {
        let mut out: Vec<_> =
            present.iter().filter(|g| self.gpus.as_ref().is_none_or(|sel| sel.contains(&g.index))).cloned().collect();
        out.sort_by_key(|g| g.index);
        out
    }

    /// Selects GPUs by index (`None`: every GPU). An empty list is refused.
    pub fn set_gpus(&mut self, gpus: Option<Vec<u32>>) -> std::result::Result<(), String> {
        let gpus = gpus.map(|mut g| {
            g.sort_unstable();
            g.dedup();
            g
        });
        if gpus.as_ref().is_some_and(|g| g.is_empty()) {
            return Err("At least one GPU has to stay selected.".into());
        }
        self.gpu_index = gpus.as_ref().and_then(|g| g.first().copied()).unwrap_or(0);
        self.gpus = gpus;
        Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;

    fn card(index: u32) -> crate::gpu::GpuInfo {
        crate::gpu::GpuInfo {
            index,
            name: format!("GPU {index}"),
            total_bytes: 24 << 30,
            used_bytes: 0,
            free_bytes: 24 << 30,
            utilization_pct: None,
            temperature_c: None,
            compute_capability: Some(8.6),
        }
    }

    #[test]
    fn every_gpu_unless_some_are_chosen() {
        let present = [card(2), card(0), card(1)];
        let mut s = Settings::default();
        assert_eq!(s.selected_gpus(&present).iter().map(|g| g.index).collect::<Vec<_>>(), [0, 1, 2]);
        s.set_gpus(Some(vec![2, 0, 2])).unwrap();
        assert_eq!(s.gpus, Some(vec![0, 2]));
        assert_eq!(s.gpu_index, 0);
        assert_eq!(s.selected_gpus(&present).iter().map(|g| g.index).collect::<Vec<_>>(), [0, 2]);
        // A chosen card that is gone is simply not there; nothing else is picked for it.
        s.set_gpus(Some(vec![1, 7])).unwrap();
        assert_eq!(s.selected_gpus(&present).iter().map(|g| g.index).collect::<Vec<_>>(), [1]);
        assert!(s.set_gpus(Some(vec![])).is_err());
        s.set_gpus(None).unwrap();
        assert_eq!(s.selected_gpus(&present).len(), 3);
        // Saved and read back, the choice holds (and "all" stays "all").
        let again = Settings::from_json(&serde_json::to_vec(&s).unwrap()).unwrap();
        assert_eq!(again.gpus, None);
    }

    #[test]
    fn a_bad_value_costs_only_that_value() {
        let s = Settings::from_json(br#"{"context_len": 65536, "agent_port": "oops", "hide_ips": true, "theme": "light"}"#).unwrap();
        assert_eq!(s.context_len, 65536);
        assert_eq!(s.agent_port, 9090, "the unreadable value falls back alone");
        assert!(s.hide_ips);
        assert_eq!(s.theme.as_deref(), Some("light"));
        assert!(Settings::from_json(b"not json").is_none());
    }

    #[test]
    fn keys_from_a_newer_version_are_kept() {
        let s = Settings::from_json(br#"{"gpu_index": 1, "future_toggle": {"on": true}}"#).unwrap();
        assert_eq!(s.gpu_index, 1);
        let again = serde_json::to_value(&s).unwrap();
        assert_eq!(again["future_toggle"]["on"], true, "written back when saved");
        assert_eq!(s.gpus, Some(vec![1]), "a file from before several GPUs keeps its one card");
        let old = Settings::from_json(br#"{"web_search": false}"#).unwrap();
        assert_eq!(old.gpus, None, "no card named: every GPU");
        assert!(!old.web_search);
        assert!(!old.hide_ips, "a field this file predates gets its default");
    }
}
