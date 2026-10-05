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
/// Private bridge network shared by the backend and SearXNG (no published ports for SearXNG).
pub const NETWORK: &str = "openphalanx";
pub const SEARXNG_CONTAINER: &str = "openphalanx-searxng";
/// Pinned multi-arch index digest (SearXNG 2026.10.2).
pub const SEARXNG_IMAGE: &str =
    "searxng/searxng@sha256:c642712fcedcdaa78fac44f71eada86aff510745826ba1bd1a368211fea2ce7f";
pub const SEARXNG_SETTINGS: &str = include_str!("../searxng/settings.yml");
const SEARXNG_INTERNAL_URL: &str = "http://openphalanx-searxng:8080";
/// SearXNG on the host's loopback, for a backend on the host's network (split
/// model); still unreachable from other machines.
pub const SEARXNG_LOOPBACK_PORT: u16 = 9098;
/// Prefill chunk for every rank of a split model (see `SplitRank::args`).
pub const SPLIT_PREFILL_CHUNK: u32 = 2048;
/// A member's share of a split model (a headless SGLang rank, no gateway).
pub const WORKER_CONTAINER: &str = "openphalanx-worker";
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
    /// The context window it was started with (`CONTEXT_LENGTH`).
    pub context_len: Option<u32>,
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
    let env_var = |name: &str| {
        let prefix = format!("{name}=");
        c["Config"]["Env"].as_array().and_then(|env| {
            env.iter().filter_map(|e| e.as_str()).find_map(|e| e.strip_prefix(prefix.as_str()).map(str::to_string))
        })
    };
    let admin_token = env_var("ADMIN_TOKEN");
    let context_len = env_var("CONTEXT_LENGTH").and_then(|v| v.parse().ok());
    Ok(Some(ContainerInfo {
        state,
        image: c["Config"]["Image"].as_str().unwrap_or_default().to_string(),
        managed: labels[MANAGED_LABEL].as_str() == Some("true"),
        model_key: labels[MODEL_LABEL].as_str().map(str::to_string),
        context_len,
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
    /// Point the gateway at the SearXNG container.
    pub web_search: bool,
    /// What clients are told about the model (GET /v1/info).
    pub model_id: String,
    pub edit_format: String,
    pub reasoning_parser: Option<String>,
    /// SGLang `--dtype` override.
    pub dtype: Option<String>,
    /// Days a client pairing lasts (`None`: never).
    pub pairing_ttl_days: Option<u32>,
    /// Rank 0 of a model split across servers.
    pub split: Option<SplitRank>,
}

/// One rank of a pipeline-parallel SGLang run across machines. These
/// containers share the host's network: NCCL and torch connect back to the
/// address each rank announces, which a Docker bridge would hide.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SplitRank {
    pub rank: u32,
    pub nnodes: u32,
    /// `host:port` of rank 0's rendezvous.
    pub dist_init_addr: String,
    /// `SGLANG_PP_LAYER_PARTITION`, e.g. "40,24".
    pub partition: String,
    /// Interface for NCCL and gloo (the LAN one), if known.
    pub interface: Option<String>,
}

impl SplitRank {
    fn env(&self) -> Vec<(&'static str, String)> {
        let mut env = vec![
            ("SGLANG_PP_LAYER_PARTITION", self.partition.clone()),
            // Plain TCP between the machines; no InfiniBand on a LAN.
            ("NCCL_IB_DISABLE", "1".into()),
        ];
        if let Some(i) = &self.interface {
            env.push(("NCCL_SOCKET_IFNAME", i.clone()));
            env.push(("GLOO_SOCKET_IFNAME", i.clone()));
        }
        env
    }

    fn args(&self) -> String {
        // Every rank must chunk prefills the same way: SGLang otherwise picks
        // a size per GPU (4096 on a 24 GB card, 2048 on 16 GB), and a rank
        // handed a bigger chunk than its own fails to reshape it. 2048 is
        // the smaller default, so activations fit the smaller cards.
        format!(
            "--pp-size {n} --nnodes {n} --node-rank {r} --dist-init-addr {a} --chunked-prefill-size {SPLIT_PREFILL_CHUNK}",
            n = self.nnodes,
            r = self.rank,
            a = self.dist_init_addr
        )
    }
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
    ];
    if spec.split.is_some() {
        // The gateway then binds the agent port itself, and the admin API
        // and SGLang only on loopback (ADMIN_HOST, --host 127.0.0.1).
        a.extend(["--network".into(), "host".into()]);
    } else {
        a.extend([
            "--network".into(),
            NETWORK.into(),
            "-p".into(),
            format!("{0}:{0}", spec.agent_port),
            // Admin API is reachable from this machine only.
            "-p".into(),
            format!("127.0.0.1:{ADMIN_PORT}:{ADMIN_PORT}"),
        ]);
    }
    a.extend([
        "-v".into(),
        format!("{}:/state", spec.state_dir.display()),
        "--label".into(),
        format!("{MANAGED_LABEL}=true"),
        "--label".into(),
        format!("{MODEL_LABEL}={}", spec.model_key),
    ]);
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
        ("DEVICE_TTL_DAYS", spec.pairing_ttl_days.map_or_else(|| "never".to_string(), |d| d.to_string())),
        // Weights are always fetched by the GUI; never let SGLang download.
        ("HF_HUB_OFFLINE", "1".into()),
    ] {
        a.push("-e".into());
        a.push(format!("{k}={v}"));
    }
    if spec.web_search {
        a.push("-e".into());
        a.push(match spec.split {
            Some(_) => format!("SEARXNG_URL=http://127.0.0.1:{SEARXNG_LOOPBACK_PORT}"),
            None => format!("SEARXNG_URL={SEARXNG_INTERNAL_URL}"),
        });
    }
    let mut extra: Vec<String> = Vec::new();
    if let Some(split) = &spec.split {
        for (k, v) in split.env() {
            a.push("-e".into());
            a.push(format!("{k}={v}"));
        }
        for (k, v) in [("ADMIN_HOST", "127.0.0.1".to_string()), ("SGLANG_PORT", crate::split::SGLANG_HOST_PORT.to_string())] {
            a.push("-e".into());
            a.push(format!("{k}={v}"));
        }
        extra.push(split.args());
    }
    for (k, v) in [("MODEL_ID", spec.model_id.clone()), ("EDIT_FORMAT", spec.edit_format.clone())] {
        a.push("-e".into());
        a.push(format!("{k}={v}"));
    }
    if let Some(dtype) = &spec.dtype {
        extra.push(format!("--dtype {dtype}"));
    }
    if let Some(parser) = &spec.reasoning_parser {
        extra.push(format!("--reasoning-parser {parser}"));
        // The gateway drives reasoning models differently (see gateway.classify).
        a.push("-e".into());
        a.push(format!("REASONING_PARSER={parser}"));
    }
    if !extra.is_empty() {
        a.push("-e".into());
        a.push(format!("SGLANG_EXTRA_ARGS={}", extra.join(" ")));
    }
    a.push(spec.image.clone());
    a
}

/// A member's rank of a split model: SGLang alone (the image's launch
/// script, no gateway), on the host's network, reading the weights read-only.
#[derive(Debug, Clone)]
pub struct WorkerSpec {
    pub image: String,
    pub gpu_index: u32,
    pub model: ModelMount,
    pub model_key: String,
    pub mem_fraction_static: f64,
    pub context_len: u32,
    pub split: SplitRank,
    /// Must match rank 0's.
    pub dtype: Option<String>,
    pub run_id: String,
}

pub const RUN_LABEL: &str = "io.openphalanx.run";

pub fn worker_run_args(spec: &WorkerSpec) -> Vec<String> {
    let mut a: Vec<String> = vec![
        "run".into(),
        "-d".into(),
        "--name".into(),
        WORKER_CONTAINER.into(),
        "--gpus".into(),
        format!("\"device={}\"", spec.gpu_index),
        "--ipc=host".into(),
        "--network".into(),
        "host".into(),
        "--entrypoint".into(),
        "/opt/openphalanx/start-sglang.sh".into(),
        "--label".into(),
        format!("{MANAGED_LABEL}=true"),
        "--label".into(),
        format!("{MODEL_LABEL}={}", spec.model_key),
        "--label".into(),
        format!("{RUN_LABEL}={}", spec.run_id),
    ];
    for dir in &spec.model.dirs {
        a.push("-v".into());
        a.push(format!("{0}:{0}:ro", dir.display()));
    }
    let mut env = vec![
        ("MODEL_PATH", spec.model.model_path.clone()),
        ("SERVED_MODEL_NAME", "worker".to_string()),
        ("SGLANG_PORT", crate::split::SGLANG_HOST_PORT.to_string()),
        ("MEM_FRACTION_STATIC", format!("{}", spec.mem_fraction_static)),
        ("CONTEXT_LENGTH", spec.context_len.to_string()),
        ("HF_HUB_OFFLINE", "1".into()),
        (
            "SGLANG_EXTRA_ARGS",
            match &spec.dtype {
                Some(d) => format!("{} --dtype {d}", spec.split.args()),
                None => spec.split.args(),
            },
        ),
    ];
    env.extend(spec.split.env());
    for (k, v) in env {
        a.push("-e".into());
        a.push(format!("{k}={v}"));
    }
    a.push(spec.image.clone());
    a
}

pub async fn run_worker(spec: &WorkerSpec) -> Result<()> {
    remove_worker().await?;
    let args = worker_run_args(spec);
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    let out = docker(&refs).await?;
    if !out.status.success() {
        bail!("docker run failed: {}", stderr(&out));
    }
    Ok(())
}

pub async fn remove_worker() -> Result<()> {
    docker(&["rm", "-f", WORKER_CONTAINER]).await?;
    Ok(())
}

/// The worker's run id and state ("running", "exited", …), if it exists.
pub async fn worker_state() -> Result<Option<(String, ContainerState)>> {
    let out = docker(&["inspect", "--type", "container", WORKER_CONTAINER]).await?;
    if !out.status.success() {
        return Ok(None);
    }
    let v: serde_json::Value = serde_json::from_slice(&out.stdout)?;
    let state: ContainerState = serde_json::from_value(v[0]["State"].clone())?;
    let run = v[0]["Config"]["Labels"][RUN_LABEL].as_str().unwrap_or_default().to_string();
    Ok(Some((run, state)))
}

pub async fn worker_logs_tail(lines: u32) -> Result<String> {
    let out = docker(&["logs", "--tail", &lines.to_string(), WORKER_CONTAINER]).await?;
    Ok(format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr)))
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

/// Stops the backend and SearXNG.
pub async fn stop() -> Result<()> {
    docker(&["stop", "-t", "20", CONTAINER_NAME]).await?;
    remove().await?;
    docker(&["rm", "-f", SEARXNG_CONTAINER]).await?;
    Ok(())
}

pub async fn remove() -> Result<()> {
    docker(&["rm", "-f", CONTAINER_NAME]).await?;
    Ok(())
}

pub async fn ensure_network() -> Result<()> {
    if docker(&["network", "inspect", NETWORK]).await?.status.success() {
        return Ok(());
    }
    let out = docker(&["network", "create", "--label", &format!("{MANAGED_LABEL}=true"), NETWORK]).await?;
    if !out.status.success() {
        bail!("cannot create Docker network {NETWORK}: {}", stderr(&out));
    }
    Ok(())
}

pub fn searxng_run_args(settings_file: &Path, secret: &str, loopback: bool) -> Vec<String> {
    let mut a: Vec<String> = vec![
        "run".into(),
        "-d".into(),
        "--name".into(),
        SEARXNG_CONTAINER.into(),
        "--network".into(),
        NETWORK.into(),
        "--label".into(),
        format!("{MANAGED_LABEL}=true"),
        // SearXNG logs full engine URLs (including the query) when an engine
        // fails; queries can carry code context, so its output is not kept.
        "--log-driver".into(),
        "none".into(),
        // Never chown the host's settings file; it is mounted read-only.
        "-e".into(),
        "FORCE_OWNERSHIP=false".into(),
        "-e".into(),
        format!("SEARXNG_SECRET={secret}"),
        "-v".into(),
        format!("{}:/etc/searxng/settings.yml:ro", settings_file.display()),
    ];
    if loopback {
        a.extend(["-p".into(), format!("127.0.0.1:{SEARXNG_LOOPBACK_PORT}:8080")]);
    }
    a.push(SEARXNG_IMAGE.into());
    a
}

/// (Re)starts SearXNG on the private network. No ports are published, except
/// on loopback for a backend on the host's network (`loopback`).
pub async fn run_searxng(settings_file: &Path, secret: &str, loopback: bool) -> Result<()> {
    docker(&["rm", "-f", SEARXNG_CONTAINER]).await?;
    let args = searxng_run_args(settings_file, secret, loopback);
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    let out = docker(&refs).await?;
    if !out.status.success() {
        bail!("cannot start SearXNG: {}", stderr(&out));
    }
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

    fn spec() -> RunSpec {
        RunSpec {
            image: DEFAULT_IMAGE.into(),
            gpu_index: 0,
            agent_port: 9090,
            model: ModelMount { dirs: vec!["/m".into(), "/blobs".into()], model_path: "/m".into() },
            model_key: "catalog:Qwen/X".into(),
            mem_fraction_static: 0.812,
            context_len: 32768,
            state_dir: "/s".into(),
            admin_token: "secret".into(),
            web_search: true,
            model_id: "Qwen/X".into(),
            edit_format: "diff".into(),
            reasoning_parser: Some("qwen3".into()),
            dtype: None,
            pairing_ttl_days: Some(7),
            split: None,
        }
    }

    fn split_rank(rank: u32) -> SplitRank {
        SplitRank {
            rank,
            nnodes: 2,
            dist_init_addr: "192.168.1.77:9100".into(),
            partition: "40,24".into(),
            interface: Some("eno1".into()),
        }
    }

    #[test]
    fn run_args_publish_admin_on_loopback_only() {
        let a = run_args(&spec()).join(" ");
        assert!(a.contains("-p 9090:9090"));
        assert!(a.contains("-p 127.0.0.1:9091:9091"));
        assert!(a.contains("-v /m:/m:ro") && a.contains("-v /blobs:/blobs:ro"));
        assert!(a.contains("-e MEM_FRACTION_STATIC=0.812"));
        assert!(a.contains("-e HF_HUB_OFFLINE=1"));
        assert!(a.contains("-e DEVICE_TTL_DAYS=7"));
        assert!(run_args(&RunSpec { pairing_ttl_days: None, ..spec() }).join(" ").contains("-e DEVICE_TTL_DAYS=never"));
        assert!(a.contains("--network openphalanx"));
        assert!(a.contains("-e SEARXNG_URL=http://openphalanx-searxng:8080"));
        assert!(a.contains("-e MODEL_ID=Qwen/X") && a.contains("-e EDIT_FORMAT=diff"));
        assert!(a.contains("-e SGLANG_EXTRA_ARGS=--reasoning-parser qwen3"));
        assert!(a.ends_with(DEFAULT_IMAGE));
    }

    #[test]
    fn split_rank_zero_shares_the_host_network_but_keeps_admin_on_loopback() {
        let a = run_args(&RunSpec { split: Some(split_rank(0)), ..spec() }).join(" ");
        assert!(a.contains("--network host") && !a.contains(" -p "), "{a}");
        assert!(a.contains("-e ADMIN_HOST=127.0.0.1"), "host network: the gateway must bind admin to loopback");
        assert!(a.contains("-e SEARXNG_URL=http://127.0.0.1:9098"));
        assert!(a.contains("-e SGLANG_PP_LAYER_PARTITION=40,24"));
        assert!(a.contains("-e NCCL_SOCKET_IFNAME=eno1") && a.contains("-e GLOO_SOCKET_IFNAME=eno1"));
        assert!(a.contains(
            "-e SGLANG_EXTRA_ARGS=--pp-size 2 --nnodes 2 --node-rank 0 --dist-init-addr 192.168.1.77:9100 \
             --chunked-prefill-size 2048 --reasoning-parser qwen3"
        ));
    }

    #[test]
    fn workers_run_sglang_alone() {
        let w = WorkerSpec {
            image: DEFAULT_IMAGE.into(),
            gpu_index: 0,
            model: ModelMount { dirs: vec!["/m".into()], model_path: "/m".into() },
            model_key: "catalog:Qwen/X".into(),
            mem_fraction_static: 0.7,
            context_len: 32768,
            split: split_rank(1),
            dtype: Some("bfloat16".into()),
            run_id: "r1".into(),
        };
        let a = worker_run_args(&w).join(" ");
        assert!(a.contains("--name openphalanx-worker") && a.contains("--network host"));
        assert!(a.contains("--entrypoint /opt/openphalanx/start-sglang.sh"), "no gateway on a worker");
        assert!(a.contains("--label io.openphalanx.run=r1"));
        assert!(a.contains("--node-rank 1") && a.contains("-v /m:/m:ro"));
        assert!(a.contains("--chunked-prefill-size 2048 --dtype bfloat16"), "same dtype as rank 0: {a}");
        assert!(!a.contains("ADMIN_TOKEN"));
    }

    #[test]
    fn searxng_is_private_and_never_chowns() {
        let a = searxng_run_args(Path::new("/d/settings.yml"), "k", false).join(" ");
        assert!(!a.contains(" -p "), "SearXNG must not publish ports");
        let lo = searxng_run_args(Path::new("/d/settings.yml"), "k", true).join(" ");
        assert!(lo.contains("-p 127.0.0.1:9098:8080"), "only on loopback");
        assert!(a.contains("--network openphalanx"));
        assert!(a.contains("FORCE_OWNERSHIP=false"));
        assert!(a.contains("--log-driver none"), "queries must not be logged");
        assert!(a.contains("-v /d/settings.yml:/etc/searxng/settings.yml:ro"));
        assert!(a.contains("searxng/searxng@sha256:"));
        assert!(SEARXNG_SETTINGS.contains("formats: [json]"));
    }

    #[test]
    fn diagnoses_oom() {
        assert!(diagnose_crash("torch.OutOfMemoryError: CUDA out of memory. Tried").is_some());
        assert!(diagnose_crash("all good").is_none());
    }
}
