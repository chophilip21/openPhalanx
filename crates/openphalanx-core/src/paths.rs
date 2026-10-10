//! Where Openphalanx keeps its files (XDG locations on Linux).

use std::path::{Path, PathBuf};

fn home() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from("/tmp"))
}

/// `~/.config/openphalanx`: settings.
pub fn config_dir() -> PathBuf {
    dirs::config_dir().unwrap_or_else(|| home().join(".config")).join("openphalanx")
}

/// `~/.local/share/openphalanx`: downloaded models and backend state.
pub fn data_dir() -> PathBuf {
    dirs::data_dir().unwrap_or_else(|| home().join(".local/share")).join("openphalanx")
}

pub fn models_dir() -> PathBuf {
    data_dir().join("models")
}

/// Mounted into the container at `/state`: TLS cert and paired devices.
pub fn backend_state_dir() -> PathBuf {
    data_dir().join("backend-state")
}

/// The Hugging Face hub cache, so models downloaded by other tools are reused.
/// The head's cluster state: node registry and its TLS certificate.
pub fn cluster_dir() -> PathBuf {
    data_dir().join("cluster")
}

pub fn hf_hub_dir() -> PathBuf {
    if let Ok(cache) = std::env::var("HF_HUB_CACHE") {
        return PathBuf::from(cache);
    }
    std::env::var("HF_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| home().join(".cache/huggingface"))
        .join("hub")
}

/// The Hugging Face cache folder of the model `dir` belongs to
/// (`<hub>/models--org--name`), when `dir` is a snapshot inside `hub`.
pub fn hf_cache_repo_dir(dir: &Path, hub: &Path) -> Option<PathBuf> {
    dir.ancestors()
        .find(|a| a.parent() == Some(hub) && a.file_name().is_some_and(|n| n.to_string_lossy().starts_with("models--")))
        .map(Path::to_path_buf)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_the_cache_folder_of_a_snapshot() {
        let hub = Path::new("/home/u/.cache/huggingface/hub");
        let snap = hub.join("models--Qwen--Qwen3-8B-AWQ/snapshots/abc123");
        assert_eq!(hf_cache_repo_dir(&snap, hub), Some(hub.join("models--Qwen--Qwen3-8B-AWQ")));
        assert_eq!(hf_cache_repo_dir(Path::new("/home/u/models/x"), hub), None);
        assert_eq!(hf_cache_repo_dir(&hub.join("datasets--x/snapshots/1"), hub), None);
        assert_eq!(hf_cache_repo_dir(hub, hub), None);
    }
}
