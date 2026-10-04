//! Where Openphalanx keeps its files (XDG locations on Linux).

use std::path::PathBuf;

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
pub fn hf_hub_dir() -> PathBuf {
    if let Ok(cache) = std::env::var("HF_HUB_CACHE") {
        return PathBuf::from(cache);
    }
    std::env::var("HF_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| home().join(".cache/huggingface"))
        .join("hub")
}
