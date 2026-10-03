//! Model sources (catalog, Hugging Face repo, local directory), local install
//! detection, and how a model directory gets mounted into the backend.

use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::paths;
use crate::vram::ArchSpec;

/// What the user typed into "custom model".
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Source {
    HuggingFace { repo: String, revision: String },
    LocalDir { path: PathBuf },
}

impl Source {
    pub fn parse(input: &str) -> Result<Source> {
        let s = input.trim();
        if s.is_empty() {
            bail!("Enter a local folder or a Hugging Face model URL.");
        }
        if s.starts_with('/') || s.starts_with("~/") || s.starts_with("./") {
            let path = match s.strip_prefix("~/") {
                Some(rest) => dirs::home_dir().context("no home directory")?.join(rest),
                None => PathBuf::from(s),
            };
            return Ok(Source::LocalDir { path });
        }
        if let Some(rest) = s
            .strip_prefix("https://huggingface.co/")
            .or_else(|| s.strip_prefix("http://huggingface.co/"))
            .or_else(|| s.strip_prefix("huggingface.co/"))
        {
            let parts: Vec<&str> = rest.trim_end_matches('/').split('/').collect();
            if parts.len() < 2 || parts[0].is_empty() || parts[1].is_empty() {
                bail!("That Hugging Face URL doesn't name a model repo.");
            }
            if matches!(parts[0], "datasets" | "spaces") {
                bail!("That URL is a Hugging Face {}, not a model.", parts[0]);
            }
            let revision = match parts.get(2) {
                Some(&"tree") | Some(&"blob") | Some(&"resolve") => {
                    parts.get(3).unwrap_or(&"main").to_string()
                }
                _ => "main".to_string(),
            };
            let repo = format!("{}/{}", parts[0], parts[1]);
            validate_repo(&repo)?;
            return Ok(Source::HuggingFace { repo, revision });
        }
        if s.starts_with("http://") || s.starts_with("https://") {
            bail!(
                "Only Hugging Face model URLs are supported. For other sources, download the \
                 model yourself and choose its folder."
            );
        }
        validate_repo(s)?;
        Ok(Source::HuggingFace { repo: s.to_string(), revision: "main".into() })
    }

    /// Stable key used in settings and the UI.
    pub fn key(&self) -> String {
        match self {
            Source::HuggingFace { repo, .. } => format!("hf:{repo}"),
            Source::LocalDir { path } => format!("local:{}", path.display()),
        }
    }
}

fn validate_repo(repo: &str) -> Result<()> {
    let ok = repo.split('/').count() == 2
        && repo.split('/').all(|p| {
            !p.is_empty()
                && p != "."
                && p != ".."
                && p.chars().all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
        });
    if !ok {
        bail!("\"{repo}\" is not a valid Hugging Face repo id (expected owner/name).");
    }
    Ok(())
}

/// What we need to know about a model to size it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ModelInfo {
    pub architecture: Option<String>,
    pub arch: ArchSpec,
    pub max_context: u32,
    pub weight_bytes: u64,
    pub quant: Option<String>,
}

pub fn arch_from_config(config: &Value) -> Result<(ArchSpec, u32, Option<String>, Option<String>)> {
    // Multimodal configs nest the language model.
    let c = config.get("text_config").unwrap_or(config);
    let num = |k: &str| c.get(k).and_then(Value::as_u64);
    let layers = num("num_hidden_layers").context("config.json lacks num_hidden_layers")?;
    let heads = num("num_attention_heads").context("config.json lacks num_attention_heads")?;
    let kv_heads = num("num_key_value_heads").unwrap_or(heads);
    let head_dim = match num("head_dim") {
        Some(d) => d,
        None => num("hidden_size").context("config.json lacks hidden_size")? / heads.max(1),
    };
    // Hybrid models: only full-attention layers keep a KV cache.
    let kv_layers = if let Some(types) = c.get("layer_types").and_then(Value::as_array) {
        types.iter().filter(|t| t.as_str() == Some("full_attention")).count() as u64
    } else if let Some(interval) = num("full_attention_interval").filter(|i| *i > 0) {
        layers / interval
    } else {
        layers
    };
    let max_context = num("max_position_embeddings").unwrap_or(32_768) as u32;
    let architecture = config
        .get("architectures")
        .and_then(|a| a.get(0))
        .and_then(Value::as_str)
        .map(str::to_string);
    let quant = c
        .get("quantization_config")
        .or_else(|| config.get("quantization_config"))
        .and_then(|q| q.get("quant_method"))
        .and_then(Value::as_str)
        .map(str::to_string);
    Ok((
        ArchSpec { kv_layers: kv_layers as u32, kv_heads: kv_heads as u32, head_dim: head_dim as u32 },
        max_context,
        architecture,
        quant,
    ))
}

/// Inspect a model folder on disk (config.json + safetensors).
pub fn inspect_local(dir: &Path) -> Result<ModelInfo> {
    if !dir.is_dir() {
        bail!("{} is not a folder.", dir.display());
    }
    let config_path = dir.join("config.json");
    let config: Value = serde_json::from_slice(
        &std::fs::read(&config_path)
            .with_context(|| format!("{} has no config.json", dir.display()))?,
    )
    .context("config.json is not valid JSON")?;
    let (arch, max_context, architecture, quant) = arch_from_config(&config)?;
    let mut weight_bytes = 0;
    for entry in std::fs::read_dir(dir)? {
        let path = entry?.path();
        if path.extension().is_some_and(|e| e == "safetensors") {
            // metadata() follows symlinks (HF cache snapshots are symlinks).
            weight_bytes += std::fs::metadata(&path)
                .with_context(|| format!("broken file {}", path.display()))?
                .len();
        }
    }
    if weight_bytes == 0 {
        bail!("{} has no .safetensors weights (SGLang needs safetensors).", dir.display());
    }
    Ok(ModelInfo { architecture, arch, max_context, weight_bytes, quant })
}

/// App-managed download location for a Hugging Face repo.
pub fn app_model_dir(repo: &str) -> PathBuf {
    paths::models_dir().join(repo)
}

pub const COMPLETE_MARKER: &str = ".openphalanx-complete.json";

/// Where an installed copy of a repo lives, preferring our own verified
/// download, then a complete snapshot in the Hugging Face cache.
pub fn find_installed(repo: &str, revision: Option<&str>) -> Option<PathBuf> {
    let ours = app_model_dir(repo);
    if ours.join(COMPLETE_MARKER).is_file() {
        return Some(ours);
    }
    find_in_hf_cache(&paths::hf_hub_dir(), repo, revision)
}

pub fn find_in_hf_cache(hub: &Path, repo: &str, revision: Option<&str>) -> Option<PathBuf> {
    let snapshots = hub.join(format!("models--{}", repo.replace('/', "--"))).join("snapshots");
    let mut candidates: Vec<PathBuf> = match revision {
        Some(rev) => vec![snapshots.join(rev)],
        None => std::fs::read_dir(&snapshots).ok()?.flatten().map(|e| e.path()).collect(),
    };
    candidates.sort();
    candidates.into_iter().find(|dir| hf_snapshot_complete(dir))
}

/// A snapshot is complete when config.json and every shard named in the
/// safetensors index resolve to real files.
fn hf_snapshot_complete(dir: &Path) -> bool {
    if !dir.join("config.json").is_file() {
        return false;
    }
    let index = dir.join("model.safetensors.index.json");
    if let Ok(bytes) = std::fs::read(&index) {
        let Ok(v) = serde_json::from_slice::<Value>(&bytes) else { return false };
        let Some(map) = v.get("weight_map").and_then(Value::as_object) else { return false };
        let mut shards: Vec<&str> = map.values().filter_map(Value::as_str).collect();
        shards.dedup();
        return shards.iter().all(|s| dir.join(s).is_file());
    }
    dir.join("model.safetensors").is_file()
}

/// A bind mount that makes a model folder visible inside the container.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ModelMount {
    pub host_dir: PathBuf,
    pub container_dir: String,
    /// Value for `MODEL_PATH` inside the container.
    pub model_path: String,
}

pub fn mount_for(model_dir: &Path) -> ModelMount {
    // HF cache snapshots are symlinks into ../../blobs, so mount the whole repo
    // folder or the links dangle inside the container.
    let parent = model_dir.parent();
    let repo_dir = parent.and_then(Path::parent);
    if let (Some(parent), Some(repo_dir), Some(rev)) = (parent, repo_dir, model_dir.file_name()) {
        let is_snapshot = parent.file_name().is_some_and(|n| n == "snapshots")
            && repo_dir
                .file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with("models--"));
        if is_snapshot {
            return ModelMount {
                host_dir: repo_dir.to_path_buf(),
                container_dir: "/model-repo".into(),
                model_path: format!("/model-repo/snapshots/{}", rev.to_string_lossy()),
            };
        }
    }
    ModelMount {
        host_dir: model_dir.to_path_buf(),
        container_dir: "/model".into(),
        model_path: "/model".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_sources() {
        assert_eq!(
            Source::parse("Qwen/Qwen2.5-Coder-7B-Instruct").unwrap(),
            Source::HuggingFace { repo: "Qwen/Qwen2.5-Coder-7B-Instruct".into(), revision: "main".into() }
        );
        assert_eq!(
            Source::parse("https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct/tree/abc123").unwrap(),
            Source::HuggingFace { repo: "Qwen/Qwen2.5-Coder-7B-Instruct".into(), revision: "abc123".into() }
        );
        assert_eq!(
            Source::parse("https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct/").unwrap(),
            Source::HuggingFace { repo: "Qwen/Qwen2.5-Coder-7B-Instruct".into(), revision: "main".into() }
        );
        assert_eq!(
            Source::parse("/data/models/x").unwrap(),
            Source::LocalDir { path: "/data/models/x".into() }
        );
        assert!(Source::parse("https://example.com/model.tar").is_err());
        assert!(Source::parse("https://huggingface.co/datasets/foo/bar").is_err());
        assert!(Source::parse("../etc/passwd").is_err());
        assert!(Source::parse("a/b/c").is_err());
    }

    #[test]
    fn reads_dense_and_hybrid_configs() {
        let qwen = json!({"architectures":["Qwen2ForCausalLM"],"num_hidden_layers":48,
            "num_attention_heads":40,"num_key_value_heads":8,"hidden_size":5120,
            "max_position_embeddings":32768,"quantization_config":{"quant_method":"awq"}});
        let (arch, ctx, name, quant) = arch_from_config(&qwen).unwrap();
        assert_eq!(arch, ArchSpec { kv_layers: 48, kv_heads: 8, head_dim: 128 });
        assert_eq!((ctx, name.as_deref(), quant.as_deref()), (32768, Some("Qwen2ForCausalLM"), Some("awq")));

        let next = json!({"num_hidden_layers":48,"num_attention_heads":16,"num_key_value_heads":2,
            "head_dim":256,"hidden_size":2048,"full_attention_interval":4});
        assert_eq!(arch_from_config(&next).unwrap().0, ArchSpec { kv_layers: 12, kv_heads: 2, head_dim: 256 });
    }

    #[test]
    fn inspects_local_dir_and_hf_snapshot() {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("models--Org--M");
        let snap = repo.join("snapshots/rev1");
        std::fs::create_dir_all(&snap).unwrap();
        std::fs::create_dir_all(repo.join("blobs")).unwrap();
        std::fs::write(repo.join("blobs/w"), vec![0u8; 1000]).unwrap();
        std::os::unix::fs::symlink("../../blobs/w", snap.join("model.safetensors")).unwrap();
        std::fs::write(
            snap.join("config.json"),
            r#"{"num_hidden_layers":2,"num_attention_heads":4,"hidden_size":256}"#,
        )
        .unwrap();

        let info = inspect_local(&snap).unwrap();
        assert_eq!(info.weight_bytes, 1000);
        assert_eq!(info.arch, ArchSpec { kv_layers: 2, kv_heads: 4, head_dim: 64 });

        assert_eq!(find_in_hf_cache(tmp.path(), "Org/M", None), Some(snap.clone()));
        assert_eq!(find_in_hf_cache(tmp.path(), "Org/M", Some("other")), None);

        let m = mount_for(&snap);
        assert_eq!(m.host_dir, repo);
        assert_eq!(m.model_path, "/model-repo/snapshots/rev1");
        assert_eq!(mount_for(Path::new("/data/plain")).model_path, "/model");
    }
}
