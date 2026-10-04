//! Hugging Face downloads into the app's models folder.
//!
//! Each file streams to `<name>.part` (resumable with HTTP Range), is checked
//! against the SHA-256 that Hugging Face publishes for LFS files, then renamed
//! into place. A marker file is written only once every file has verified.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::model::{self, ModelInfo, COMPLETE_MARKER};

const HF: &str = "https://huggingface.co";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RemoteFile {
    pub path: String,
    pub size: u64,
    /// SHA-256 for LFS files (all weights); small text files have none.
    pub sha256: Option<String>,
}

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .user_agent(concat!("openphalanx/", env!("CARGO_PKG_VERSION")))
        .connect_timeout(Duration::from_secs(15))
        .build()
        .expect("reqwest client")
}

/// Files SGLang needs: configs, tokenizer and safetensors weights.
fn wanted(path: &str) -> bool {
    if path.contains('/') {
        return false; // e.g. original/ checkpoints, onnx/ exports
    }
    let ext = path.rsplit('.').next().unwrap_or("");
    matches!(ext, "json" | "safetensors" | "txt" | "model" | "tiktoken" | "jinja")
}

/// Mistral repos ship each checkpoint twice: Hugging Face shards
/// (`model-*.safetensors`, what SGLang loads) and Mistral's own
/// `consolidated*.safetensors`. Keep only the shards when both exist.
pub fn is_duplicate_weight(path: &str, all: &[&str]) -> bool {
    path.starts_with("consolidated")
        && path.ends_with(".safetensors")
        && all.iter().any(|p| p.ends_with(".safetensors") && !p.starts_with("consolidated"))
}

fn drop_duplicate_weights(files: Vec<RemoteFile>) -> Vec<RemoteFile> {
    let names: Vec<String> = files.iter().map(|f| f.path.clone()).collect();
    let refs: Vec<&str> = names.iter().map(String::as_str).collect();
    files.into_iter().filter(|f| !is_duplicate_weight(&f.path, &refs)).collect()
}

pub async fn list_files(client: &reqwest::Client, repo: &str, revision: &str) -> Result<Vec<RemoteFile>> {
    let url = format!("{HF}/api/models/{repo}/tree/{revision}?recursive=false");
    let resp = client.get(&url).send().await.context("cannot reach huggingface.co")?;
    match resp.status().as_u16() {
        200 => {}
        401 | 403 => bail!("{repo} is gated or private; Openphalanx only downloads public models."),
        404 => bail!("{repo} (revision {revision}) was not found on Hugging Face."),
        s => bail!("Hugging Face returned HTTP {s} for {repo}."),
    }
    let entries: Vec<Value> = resp.json().await?;
    let files: Vec<RemoteFile> = entries
        .iter()
        .filter(|e| e.get("type").and_then(Value::as_str) == Some("file"))
        .filter_map(|e| {
            let path = e.get("path")?.as_str()?.to_string();
            wanted(&path).then(|| RemoteFile {
                size: e.get("size").and_then(Value::as_u64).unwrap_or(0),
                sha256: e.pointer("/lfs/oid").and_then(Value::as_str).map(str::to_string),
                path,
            })
        })
        .collect();
    let files = drop_duplicate_weights(files);
    if !files.iter().any(|f| f.path.ends_with(".safetensors")) {
        bail!("{repo} has no .safetensors weights, which SGLang needs.");
    }
    if !files.iter().any(|f| f.path == "config.json") {
        bail!("{repo} has no config.json.");
    }
    Ok(files)
}

/// Resolve a branch or tag (e.g. `main`) to its commit, so a download is
/// pinned even when the user typed a moving reference.
pub async fn resolve_revision(client: &reqwest::Client, repo: &str, revision: &str) -> Result<String> {
    let resp = client
        .get(format!("{HF}/api/models/{repo}/revision/{revision}"))
        .send()
        .await
        .context("cannot reach huggingface.co")?;
    match resp.status().as_u16() {
        200 => {}
        401 | 403 => bail!("{repo} is gated or private; Openphalanx only downloads public models."),
        404 => bail!("{repo} (revision {revision}) was not found on Hugging Face."),
        s => bail!("Hugging Face returned HTTP {s} for {repo}."),
    }
    let v: Value = resp.json().await?;
    v.get("sha").and_then(Value::as_str).map(str::to_string).context("no commit sha in response")
}

/// Size a remote model before downloading anything large.
pub async fn inspect_remote(
    client: &reqwest::Client,
    repo: &str,
    revision: &str,
) -> Result<(ModelInfo, Vec<RemoteFile>)> {
    let files = list_files(client, repo, revision).await?;
    let config: Value = client
        .get(format!("{HF}/{repo}/resolve/{revision}/config.json"))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await
        .context("config.json is not valid JSON")?;
    let (arch, max_context, architecture, quant) = model::arch_from_config(&config)?;
    let weight_bytes = files.iter().filter(|f| f.path.ends_with(".safetensors")).map(|f| f.size).sum();
    Ok((ModelInfo { architecture, arch, max_context, weight_bytes, quant }, files))
}

#[derive(Debug, Clone, Serialize)]
pub struct Progress {
    pub repo: String,
    pub total_bytes: u64,
    pub done_bytes: u64,
    pub current_file: String,
    pub bytes_per_sec: f64,
}

/// Cooperative cancellation flag shared with the UI.
pub type Cancel = Arc<AtomicBool>;

pub async fn available_space(dir: &Path) -> Result<u64> {
    let mut probe = dir.to_path_buf();
    while !probe.exists() {
        probe = probe.parent().context("no existing parent")?.to_path_buf();
    }
    let out = tokio::process::Command::new("df")
        .args(["-B1", "--output=avail"])
        .arg(&probe)
        .output()
        .await?;
    let text = String::from_utf8_lossy(&out.stdout);
    text.lines().nth(1).and_then(|l| l.trim().parse().ok()).context("cannot read free disk space")
}

pub async fn download(
    client: &reqwest::Client,
    repo: &str,
    revision: &str,
    files: &[RemoteFile],
    dest: &Path,
    cancel: Cancel,
    mut on_progress: impl FnMut(Progress),
) -> Result<PathBuf> {
    tokio::fs::create_dir_all(dest).await?;
    let total: u64 = files.iter().map(|f| f.size).sum();
    let already: u64 = futures_util::future::join_all(files.iter().map(|f| async {
        let done = tokio::fs::metadata(dest.join(&f.path)).await.map(|m| m.len()).unwrap_or(0);
        let part = tokio::fs::metadata(part_path(dest, &f.path)).await.map(|m| m.len()).unwrap_or(0);
        done.max(part)
    }))
    .await
    .into_iter()
    .sum();
    let needed = total.saturating_sub(already);
    let free = available_space(dest).await?;
    if free < needed + 1024 * 1024 * 1024 {
        bail!(
            "Not enough disk space: need {:.1} GiB, {:.1} GiB free in {}.",
            needed as f64 / 1073741824.0,
            free as f64 / 1073741824.0,
            dest.display()
        );
    }

    let mut done_bytes = 0u64;
    let started = Instant::now();
    let mut transferred = 0u64;
    let mut last_emit = Instant::now() - Duration::from_secs(1);

    for file in files {
        let final_path = dest.join(&file.path);
        if verified_complete(&final_path, file).await? {
            done_bytes += file.size;
            continue;
        }
        let part = part_path(dest, &file.path);
        let mut hasher = Sha256::new();
        let mut have = 0u64;
        if let Ok(mut existing) = tokio::fs::File::open(&part).await {
            // Re-hash what we already have so resumed files still verify.
            let mut buf = vec![0u8; 1 << 20];
            loop {
                let n = existing.read(&mut buf).await?;
                if n == 0 {
                    break;
                }
                hasher.update(&buf[..n]);
                have += n as u64;
            }
        }
        if have > file.size {
            tokio::fs::remove_file(&part).await?;
            hasher = Sha256::new();
            have = 0;
        }

        let url = format!("{HF}/{repo}/resolve/{revision}/{}", file.path);
        let mut req = client.get(&url);
        if have > 0 {
            req = req.header(reqwest::header::RANGE, format!("bytes={have}-"));
        }
        let resp = req.send().await?.error_for_status()?;
        if have > 0 && resp.status() != reqwest::StatusCode::PARTIAL_CONTENT {
            // Server ignored the range; start this file over.
            hasher = Sha256::new();
            have = 0;
        }
        let mut out = tokio::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .append(have > 0)
            .truncate(have == 0)
            .open(&part)
            .await?;
        done_bytes += have;

        let mut stream = resp.bytes_stream();
        while let Some(chunk) = stream.next().await {
            if cancel.load(Ordering::Relaxed) {
                out.flush().await?;
                bail!("Download cancelled.");
            }
            let chunk = chunk.context("download interrupted")?;
            hasher.update(&chunk);
            out.write_all(&chunk).await?;
            done_bytes += chunk.len() as u64;
            transferred += chunk.len() as u64;
            if last_emit.elapsed() >= Duration::from_millis(250) {
                last_emit = Instant::now();
                on_progress(Progress {
                    repo: repo.to_string(),
                    total_bytes: total,
                    done_bytes,
                    current_file: file.path.clone(),
                    bytes_per_sec: transferred as f64 / started.elapsed().as_secs_f64().max(0.001),
                });
            }
        }
        out.flush().await?;
        drop(out);

        let size = tokio::fs::metadata(&part).await?.len();
        if size != file.size {
            bail!("{}: expected {} bytes, got {size}.", file.path, file.size);
        }
        if let Some(expected) = &file.sha256 {
            let actual = hex::encode(hasher.finalize());
            if &actual != expected {
                tokio::fs::remove_file(&part).await?;
                bail!("{} failed its SHA-256 check and was deleted; try again.", file.path);
            }
        }
        tokio::fs::rename(&part, &final_path).await?;
    }

    let marker = serde_json::json!({
        "repo": repo,
        "revision": revision,
        "files": files,
        "completed_at": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0),
    });
    tokio::fs::write(dest.join(COMPLETE_MARKER), serde_json::to_vec_pretty(&marker)?).await?;
    on_progress(Progress {
        repo: repo.to_string(),
        total_bytes: total,
        done_bytes: total,
        current_file: String::new(),
        bytes_per_sec: 0.0,
    });
    Ok(dest.to_path_buf())
}

fn part_path(dest: &Path, file: &str) -> PathBuf {
    dest.join(format!("{file}.part"))
}

/// A finished file is trusted only if its size matches; hashes were checked
/// before it was renamed into place.
async fn verified_complete(path: &Path, file: &RemoteFile) -> Result<bool> {
    Ok(matches!(tokio::fs::metadata(path).await, Ok(m) if m.len() == file.size))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selects_only_runtime_files() {
        for keep in ["config.json", "model-00001-of-00002.safetensors", "merges.txt", "tokenizer.model"] {
            assert!(wanted(keep), "{keep}");
        }
        for skip in ["pytorch_model.bin", "model.gguf", "original/consolidated.pth", "README.md", "onnx/model.json"] {
            assert!(!wanted(skip), "{skip}");
        }
        // Mistral layout: keep the HF shards, drop the consolidated copy.
        let all = ["consolidated.safetensors", "model-00001-of-00002.safetensors", "config.json"];
        assert!(is_duplicate_weight("consolidated.safetensors", &all));
        assert!(!is_duplicate_weight("model-00001-of-00002.safetensors", &all));
        // A repo with only consolidated weights keeps them.
        assert!(!is_duplicate_weight("consolidated.safetensors", &["consolidated.safetensors"]));
    }
}
