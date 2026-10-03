//! Docker Engine control through the `docker` CLI.

use std::path::{Path, PathBuf};
use std::process::Stdio;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};

use crate::model::ModelMount;

pub const CONTAINER_NAME: &str = "openphalanx-backend";
/// Image tag follows the app version, so each release runs its matching backend.
pub const DEFAULT_IMAGE: &str =
    concat!("ghcr.io/chophilip21/openphalanx-backend:", env!("CARGO_PKG_VERSION"));
pub const ADMIN_PORT: u16 = 9091;
const MANAGED_LABEL: &str = "io.openphalanx.managed";
const MODEL_LABEL: &str = "io.openphalanx.model";

async fn docker(args: &[&str]) -> Result<std::process::Output> {
    Command::new("docker")
        .args(args)
        .stdin(Stdio::null())
        .output()
        .await
        .context("the docker CLI is not installed")
}

fn stderr(out: &std::process::Output) -> String {
    String::from_utf8_lossy(&out.stderr).trim().to_string()
}

/// Docker daemon version, with an actionable error when unreachable.
pub async fn daemon_version() -> Result<String> {
    let out = docker(&["version", "--format", "{{.Server.Version}}"]).await?;
    if out.status.success() {
        return Ok(String::from_utf8_lossy(&out.stdout).trim().to_string());
    }
    let err = stderr(&out);
    if err.contains("permission denied") {
        bail!("No permission to use Docker. Add your user to the docker group: sudo usermod -aG docker $USER, then log out and back in.");
    }
    if err.contains("Cannot connect") || err.contains("Is the docker daemon running") {
        bail!("The Docker daemon is not running. Start it with: sudo systemctl start docker");
    }
    bail!("Docker is not usable: {err}");
}

pub async fn has_nvidia_runtime() -> Result<bool> {
    let out = docker(&["info", "--format", "{{json .Runtimes}}"]).await?;
    Ok(out.status.success() && String::from_utf8_lossy(&out.stdout).contains("nvidia"))
}

pub async fn image_exists(image: &str) -> Result<bool> {
    Ok(docker(&["image", "inspect", "--format", "{{.Id}}", image]).await?.status.success())
}

/// Run a docker command, streaming each output line to `on_line`.
async fn stream(args: &[&str], mut on_line: impl FnMut(String)) -> Result<()> {
    let mut child = Command::new("docker")
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("the docker CLI is not installed")?;
    let mut out = BufReader::new(child.stdout.take().unwrap()).lines();
    let mut err = BufReader::new(child.stderr.take().unwrap()).lines();
    let mut tail = Vec::new();
    let (mut out_done, mut err_done) = (false, false);
    while !(out_done && err_done) {
        tokio::select! {
            line = out.next_line(), if !out_done => match line? {
                Some(l) => on_line(l),
                None => out_done = true,
            },
            line = err.next_line(), if !err_done => match line? {
                Some(l) => { tail.push(l.clone()); on_line(l) }
                None => err_done = true,
            },
        }
    }
    if !child.wait().await?.success() {
        let last = tail.iter().rev().take(3).rev().cloned().collect::<Vec<_>>().join("\n");
        bail!("docker {} failed: {last}", args[0]);
    }
    Ok(())
}

pub async fn pull(image: &str, on_line: impl FnMut(String)) -> Result<()> {
    stream(&["pull", image], on_line).await
}

/// Build from a local checkout (development fallback when the image has not
/// been published yet).
pub async fn build(context_dir: &Path, image: &str, on_line: impl FnMut(String)) -> Result<()> {
    let dockerfile = context_dir.join("Dockerfile.server");
    let (df, ctx) = (dockerfile.to_string_lossy(), context_dir.to_string_lossy());
    stream(&["build", "--progress=plain", "-f", &df, "-t", image, &ctx], on_line).await
}

/// The repo's `docker/` folder, when running from a source checkout.
pub fn local_build_context() -> Option<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../docker");
    dir.join("Dockerfile.server").is_file().then(|| dir.canonicalize().unwrap_or(dir))
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ContainerState {
    #[serde(rename = "Status")]
    pub status: String,
    #[serde(rename = "Running")]
    pub running: bool,
    #[serde(rename = "ExitCode")]
    pub exit_code: i64,
    #[serde(rename = "OOMKilled")]
    pub oom_killed: bool,
    #[serde(rename = "Error", default)]
    pub error: String,
    #[serde(rename = "StartedAt", default)]
    pub started_at: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ContainerInfo {
    pub state: ContainerState,
    pub image: String,
    pub managed: bool,
    pub model_key: Option<String>,
    /// Recovered from the container's environment, so the GUI can reattach
    /// after a restart without storing the secret anywhere else.
    #[serde(skip)]
    pub admin_token: Option<String>,
}

pub async fn inspect() -> Result<Option<ContainerInfo>> {
    let out = docker(&["inspect", "--type", "container", CONTAINER_NAME]).await?;
    if !out.status.success() {
        return Ok(None);
    }
    let v: serde_json::Value = serde_json::from_slice(&out.stdout)?;
    let c = &v[0];
    let state: ContainerState = serde_json::from_value(c["State"].clone())?;
    let labels = &c["Config"]["Labels"];
    let admin_token = c["Config"]["Env"].as_array().and_then(|env| {
        env.iter()
            .filter_map(|e| e.as_str())
            .find_map(|e| e.strip_prefix("ADMIN_TOKEN=").map(str::to_string))
    });
    Ok(Some(ContainerInfo {
        state,
        image: c["Config"]["Image"].as_str().unwrap_or_default().to_string(),
        managed: labels[MANAGED_LABEL].as_str() == Some("true"),
        model_key: labels[MODEL_LABEL].as_str().map(str::to_string),
        admin_token,
    }))
}

#[derive(Debug, Clone)]
pub struct RunSpec {
    pub image: String,
    pub gpu_index: u32,
    pub agent_port: u16,
    pub model: ModelMount,
    pub model_key: String,
    pub mem_fraction_static: f64,
    pub context_len: u32,
    pub state_dir: PathBuf,
    pub admin_token: String,
}

pub fn run_args(spec: &RunSpec) -> Vec<String> {
    let mut a: Vec<String> = vec![
        "run".into(),
        "-d".into(),
        "--name".into(),
        CONTAINER_NAME.into(),
        "--gpus".into(),
        format!("\"device={}\"", spec.gpu_index),
        "--ipc=host".into(),
        "-p".into(),
        format!("{0}:{0}", spec.agent_port),
        // Admin API is reachable from this machine only.
        "-p".into(),
        format!("127.0.0.1:{ADMIN_PORT}:{ADMIN_PORT}"),
        "-v".into(),
        format!("{}:/state", spec.state_dir.display()),
        "--label".into(),
        format!("{MANAGED_LABEL}=true"),
        "--label".into(),
        format!("{MODEL_LABEL}={}", spec.model_key),
    ];
    for dir in &spec.model.dirs {
        a.push("-v".into());
        a.push(format!("{0}:{0}:ro", dir.display()));
    }
    for (k, v) in [
        ("MODEL_PATH", spec.model.model_path.clone()),
        ("MEM_FRACTION_STATIC", format!("{}", spec.mem_fraction_static)),
        ("CONTEXT_LENGTH", spec.context_len.to_string()),
        ("AGENT_PORT", spec.agent_port.to_string()),
        ("ADMIN_PORT", ADMIN_PORT.to_string()),
        ("ADMIN_TOKEN", spec.admin_token.clone()),
        // Weights are always fetched by the GUI; never let SGLang download.
        ("HF_HUB_OFFLINE", "1".into()),
    ] {
        a.push("-e".into());
        a.push(format!("{k}={v}"));
    }
    a.push(spec.image.clone());
    a
}

pub async fn run(spec: &RunSpec) -> Result<()> {
    remove().await?;
    let args = run_args(spec);
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    let out = docker(&refs).await?;
    if !out.status.success() {
        bail!("docker run failed: {}", stderr(&out));
    }
    Ok(())
}

pub async fn stop() -> Result<()> {
    docker(&["stop", "-t", "20", CONTAINER_NAME]).await?;
    remove().await
}

pub async fn remove() -> Result<()> {
    docker(&["rm", "-f", CONTAINER_NAME]).await?;
    Ok(())
}

pub async fn logs_tail(lines: u32) -> Result<String> {
    let out = docker(&["logs", "--tail", &lines.to_string(), CONTAINER_NAME]).await?;
    Ok(format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    ))
}

/// `docker logs -f`; the caller reads stdout and stderr.
pub fn follow_logs(tail: u32) -> Result<Child> {
    Command::new("docker")
        .args(["logs", "-f", "--tail", &tail.to_string(), CONTAINER_NAME])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .context("the docker CLI is not installed")
}

/// Explains a crash from the tail of the container log, when recognizable.
pub fn diagnose_crash(logs: &str) -> Option<String> {
    let l = logs.to_lowercase();
    if l.contains("cuda out of memory") || l.contains("outofmemoryerror") {
        return Some("The GPU ran out of memory. Close other GPU apps or pick a smaller model or context.".into());
    }
    if l.contains("not enough memory") {
        return Some("SGLang could not fit the model in the memory it was given. Pick a smaller model or context.".into());
    }
    if l.contains("incomplete download") || l.contains("missing from") {
        return Some("Some model files are missing or unreadable inside the backend. Re-download the model.".into());
    }
    if l.contains("no such file or directory") && l.contains("config.json") {
        return Some("The model folder is missing config.json or is not readable.".into());
    }
    if l.contains("address already in use") {
        return Some("A port the backend needs is already in use.".into());
    }
    if l.contains("sglang exited with code") {
        return Some("The inference engine (SGLang) failed. See Logs for the error.".into());
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_args_publish_admin_on_loopback_only() {
        let spec = RunSpec {
            image: DEFAULT_IMAGE.into(),
            gpu_index: 0,
            agent_port: 9090,
            model: ModelMount { dirs: vec!["/m".into(), "/blobs".into()], model_path: "/m".into() },
            model_key: "catalog:Qwen/X".into(),
            mem_fraction_static: 0.812,
            context_len: 32768,
            state_dir: "/s".into(),
            admin_token: "secret".into(),
        };
        let a = run_args(&spec).join(" ");
        assert!(a.contains("-p 9090:9090"));
        assert!(a.contains("-p 127.0.0.1:9091:9091"));
        assert!(a.contains("-v /m:/m:ro") && a.contains("-v /blobs:/blobs:ro"));
        assert!(a.contains("-e MEM_FRACTION_STATIC=0.812"));
        assert!(a.contains("-e HF_HUB_OFFLINE=1"));
        assert!(a.ends_with(DEFAULT_IMAGE));
    }

    #[test]
    fn diagnoses_oom() {
        assert!(diagnose_crash("torch.OutOfMemoryError: CUDA out of memory. Tried").is_some());
        assert!(diagnose_crash("all good").is_none());
    }
}
