//! Openphalanx server GUI: Tauri commands plus a background monitor that
//! pushes a status snapshot to the frontend every two seconds.

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use openphalanx_core::admin::{self, AdminClient};
use openphalanx_core::app_update::{self, Install};
use openphalanx_core::cluster::{self, Cluster, ClusterView, Role, Strategy};
use openphalanx_core::server::{self, CheckStatus, StartProgress};
use openphalanx_core::settings::{CustomModel, Settings};
use openphalanx_core::vram::{self, FitCheck, Requirement};
use openphalanx_core::{catalog, docker, download, gpu, model, net, paths, split};
use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, State};
use tokio::io::{AsyncBufReadExt, BufReader};

type CmdResult<T> = Result<T, String>;

fn err(e: impl Into<anyhow::Error>) -> String {
    format!("{:#}", e.into())
}

const MONITOR_INTERVAL: Duration = Duration::from_secs(2);
/// While serving, warn if other processes leave less than this free.
const LOW_VRAM_WARNING: u64 = 300 * 1024 * 1024;

#[derive(Debug, Clone, Default, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum Phase {
    #[default]
    Idle,
    Starting {
        detail: String,
    },
    Stopping,
    Failed {
        message: String,
    },
    /// Stopped on purpose by the cluster (e.g. a server of a split model dropped out).
    Paused {
        message: String,
    },
}

#[derive(Debug, Clone, Serialize)]
struct DownloadView {
    key: String,
    done_bytes: u64,
    total_bytes: u64,
    bytes_per_sec: f64,
    current_file: String,
    error: Option<String>,
    finished: bool,
}

#[derive(Default)]
struct Inner {
    phase: Phase,
    /// Set once we launched or adopted a container; an exit is then a crash.
    expect_running: bool,
    /// Bumped by every user stop. A monitor tick that read the container
    /// before (or during) a stop must not adopt it or call its exit a crash:
    /// `docker stop` takes up to 20 s and ticks run every 2 s.
    stop_epoch: u64,
    /// Request a pairing code as soon as the admin API answers.
    want_pairing: bool,
    /// Free VRAM per GPU, last measured while our container was not running.
    idle_free_vram: HashMap<u32, u64>,
    /// The same for each cluster member's best GPU (by member id): while a
    /// split model runs, its worker holds that memory.
    idle_member_free: HashMap<String, u64>,
    downloads: HashMap<String, DownloadView>,
    cancels: HashMap<String, download::Cancel>,
    log_follower: bool,
    /// The running model is split across the cluster (its plan, for display).
    split: Option<String>,
}

pub struct AppState {
    settings: Mutex<Settings>,
    inner: Mutex<Inner>,
    /// This machine as a cluster head (nodes join it on CLUSTER_PORT); None
    /// if its state couldn't be opened.
    cluster: Option<Arc<Cluster>>,
    /// Why the cluster service isn't available, if it isn't.
    cluster_error: Mutex<Option<String>>,
}

impl AppState {
    fn settings(&self) -> Settings {
        self.settings.lock().unwrap().clone()
    }

    fn update_settings(&self, f: impl FnOnce(&mut Settings)) -> CmdResult<Settings> {
        let mut s = self.settings.lock().unwrap();
        f(&mut s);
        s.save().map_err(err)?;
        Ok(s.clone())
    }
}

/// A split-model cluster pauses when a member hasn't reported for this long
/// (or said goodbye). Reports come every 5 s, so this is two missed reports.
const SPLIT_DROP_SECS: u64 = 12;

// --------------------------------------------------------------------------
// Snapshot
// --------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
struct ServerView {
    /// stopped | starting | running | stopping | error | external
    state: &'static str,
    detail: Option<String>,
    model_key: Option<String>,
    /// What the backend is serving (or loading), for display; the model is
    /// fixed until the server stops.
    model: Option<RunningModel>,
}

#[derive(Debug, Clone, Serialize)]
struct RunningModel {
    key: String,
    label: String,
    quant: Option<String>,
    context_len: Option<u32>,
    /// Split across the cluster: each server's layers and memory.
    split: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct Snapshot {
    server: ServerView,
    gpus: Vec<gpu::GpuInfo>,
    admin: Option<admin::Status>,
    endpoint: Option<String>,
    downloads: Vec<DownloadView>,
    settings: Settings,
    warnings: Vec<String>,
    cluster: Option<ClusterSnapshot>,
}

#[derive(Debug, Clone, Serialize)]
struct ClusterSnapshot {
    #[serde(flatten)]
    view: ClusterView,
    /// Why the cluster service isn't running, if it isn't.
    error: Option<String>,
}

async fn admin_client() -> Option<AdminClient> {
    let c = docker::inspect().await.ok().flatten()?;
    (c.state.running && c.managed).then(|| c.admin_token.map(AdminClient::new)).flatten()
}

async fn build_snapshot(app: &AppHandle) -> Snapshot {
    let st = app.state::<AppState>();
    let settings = st.settings();
    let epoch = st.inner.lock().unwrap().stop_epoch;
    let gpus = gpu::query().await.unwrap_or_default();
    let container = docker::inspect().await.ok().flatten();
    let running = container.as_ref().is_some_and(|c| c.state.running);
    let managed = container.as_ref().is_some_and(|c| c.managed);
    let client = container
        .as_ref()
        .filter(|c| c.state.running && c.managed)
        .and_then(|c| c.admin_token.clone())
        .map(AdminClient::new);
    let mut admin_status = match &client {
        Some(c) => c.status().await.ok(),
        None => None,
    };

    // Pair-on-start: ask for a code once the admin API is up.
    let want_pairing = st.inner.lock().unwrap().want_pairing;
    if want_pairing {
        if let (Some(c), Some(status)) = (&client, admin_status.as_mut()) {
            if let Ok(p) = c.new_pairing().await {
                status.pairing = p;
                st.inner.lock().unwrap().want_pairing = false;
            }
        }
    }

    let exited_unexpectedly = {
        let inner = st.inner.lock().unwrap();
        matches!(inner.phase, Phase::Idle) && inner.expect_running && !running && inner.stop_epoch == epoch
    };
    let crash_message = if exited_unexpectedly {
        let logs = docker::logs_tail(120).await.unwrap_or_default();
        let code = container.as_ref().map(|c| c.state.exit_code).unwrap_or_default();
        Some(docker::diagnose_crash(&logs).unwrap_or_else(|| {
            format!("The backend stopped unexpectedly (exit code {code}). See Logs for details.")
        }))
    } else {
        None
    };

    let mut inner = st.inner.lock().unwrap();
    if !running {
        for g in &gpus {
            inner.idle_free_vram.insert(g.index, g.free_bytes);
        }
        for m in st.cluster.as_ref().map(|c| c.members()).unwrap_or_default() {
            let best = m.report.as_ref().and_then(|r| r.inventory.gpus.iter().map(|g| g.free_bytes).max());
            if let (true, Some(free)) = (m.online, best) {
                inner.idle_member_free.insert(m.id, free);
            }
        }
    } else if managed && matches!(inner.phase, Phase::Idle) && inner.stop_epoch == epoch {
        inner.expect_running = true; // adopt a container from a previous session
    }
    if let Some(message) = crash_message {
        inner.expect_running = false;
        inner.phase = Phase::Failed { message };
    }

    if let Some(c) = st.cluster.as_ref().filter(|c| !c.is_member()) {
        // Split orders are kept across app restarts: pick a running split
        // back up, and drop orders left from a run that is gone.
        if c.has_workers() && matches!(inner.phase, Phase::Idle) {
            if running && managed && inner.split.is_none() {
                inner.split = c.split_summary();
            } else if !running && !inner.expect_running {
                c.set_workers(HashMap::new());
            }
        }
        // Split model: every server holds part of it, so one dropping out
        // (or its worker failing) breaks inference. Stop instead of failing
        // requests, and say why.
        let serving_here = running && managed && matches!(inner.phase, Phase::Idle);
        if serving_here && inner.split.is_some() {
            let workers = c.worker_states();
            let in_run: Vec<String> = workers.iter().map(|(name, _)| name.clone()).collect();
            let dropped: Vec<String> = c.dropped_members(SPLIT_DROP_SECS).into_iter().filter(|n| in_run.contains(n)).collect();
            let failed = workers.iter().find_map(|(name, w)| {
                w.as_ref().filter(|w| w.state == "failed").map(|w| (name.clone(), w.message.clone().unwrap_or_default()))
            });
            let stop_with = if !dropped.is_empty() {
                let names = dropped.join(", ");
                Some(Phase::Paused {
                    message: format!(
                        "Paused: {names} dropped out of the cluster. The model is split across the servers, so serving \
                         stopped instead of failing requests. Start again once {names} is back, or remove it from the \
                         cluster."
                    ),
                })
            } else {
                failed.map(|(name, why)| Phase::Failed {
                    message: format!("{name}'s share of the split model failed: {why}. See its log on the Logs page."),
                })
            };
            if let Some(phase) = stop_with {
                inner.phase = phase;
                inner.split = None;
                inner.expect_running = false;
                inner.stop_epoch += 1;
                c.set_workers(HashMap::new());
                tauri::async_runtime::spawn(async { let _ = docker::stop().await; });
            }
        }
        // Members see whether the host is serving (and can't start meanwhile).
        let serving = matches!(inner.phase, Phase::Starting { .. }) || (running && managed && matches!(inner.phase, Phase::Idle));
        c.set_serving(serving);
    }

    let (state, detail) = match &inner.phase {
        Phase::Starting { detail } => ("starting", Some(detail.clone())),
        Phase::Stopping => ("stopping", None),
        Phase::Failed { message } => ("error", Some(message.clone())),
        Phase::Paused { message } => ("paused", Some(message.clone())),
        Phase::Idle if running && !managed => (
            "external",
            Some("A backend container was started outside this app. Stop it to manage it from here.".into()),
        ),
        Phase::Idle if running => match &admin_status {
            Some(a) if a.sglang == "ready" => ("running", None),
            Some(_) => ("starting", Some("Loading the model into VRAM…".into())),
            None => ("starting", Some("Starting the agent server…".into())),
        },
        Phase::Idle => ("stopped", None),
    };

    let mut warnings = Vec::new();
    if running {
        if let Some(g) = gpus.iter().find(|g| g.index == settings.gpu_index) {
            if g.free_bytes < LOW_VRAM_WARNING {
                warnings.push(format!(
                    "GPU memory is almost full ({} free). Avoid starting other GPU programs.",
                    vram::fmt_gib(g.free_bytes)
                ));
            }
        }
    }

    let mut downloads: Vec<DownloadView> = inner.downloads.values().cloned().collect();
    downloads.sort_by(|a, b| a.key.cmp(&b.key));
    let need_follower = running && !inner.log_follower;
    if need_follower {
        inner.log_follower = true;
    }
    let split_plan = inner.split.clone();
    drop(inner);
    if need_follower {
        spawn_log_follower(app.clone());
    }

    Snapshot {
        server: ServerView {
            state,
            detail,
            model: container.as_ref().filter(|c| c.state.running).and_then(|c| {
                let key = c.model_key.clone()?;
                let m = server::resolve(&settings, &key);
                Some(RunningModel {
                    label: m.as_ref().map_or_else(|| key.trim_start_matches("catalog:").to_string(), |m| m.label.clone()),
                    quant: m.and_then(|m| m.quant),
                    context_len: c.context_len,
                    split: split_plan.clone(),
                    key,
                })
            }),
            model_key: container.and_then(|c| c.model_key),
        },
        gpus,
        admin: admin_status,
        endpoint: net::lan_ip().map(|ip| format!("{ip}:{}", settings.agent_port)),
        downloads,
        settings,
        warnings,
        cluster: st.cluster.as_ref().map(|c| ClusterSnapshot { view: c.view(), error: st.cluster_error.lock().unwrap().clone() }),
    }
}

fn spawn_log_follower(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        if let Ok(mut child) = docker::follow_logs(300) {
            let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<String>();
            for pipe in [
                child.stdout.take().map(|p| Box::new(p) as Box<dyn tokio::io::AsyncRead + Unpin + Send>),
                child.stderr.take().map(|p| Box::new(p) as Box<dyn tokio::io::AsyncRead + Unpin + Send>),
            ]
            .into_iter()
            .flatten()
            {
                let tx = tx.clone();
                tauri::async_runtime::spawn(async move {
                    let mut lines = BufReader::new(pipe).lines();
                    while let Ok(Some(line)) = lines.next_line().await {
                        if tx.send(line).is_err() {
                            break;
                        }
                    }
                });
            }
            drop(tx);
            // Batch lines so a noisy model load doesn't flood the webview.
            let mut batch = Vec::new();
            loop {
                tokio::select! {
                    line = rx.recv() => match line {
                        Some(l) => batch.push(l),
                        None => break,
                    },
                    _ = tokio::time::sleep(Duration::from_millis(200)), if !batch.is_empty() => {
                        let _ = app.emit("log", std::mem::take(&mut batch));
                    }
                }
            }
            if !batch.is_empty() {
                let _ = app.emit("log", batch);
            }
            let _ = child.wait().await;
        }
        app.state::<AppState>().inner.lock().unwrap().log_follower = false;
    });
}

async fn monitor(app: AppHandle) {
    loop {
        let snap = build_snapshot(&app).await;
        let _ = app.emit("snapshot", &snap);
        tokio::time::sleep(MONITOR_INTERVAL).await;
    }
}

// --------------------------------------------------------------------------
// Commands: status and server lifecycle
// --------------------------------------------------------------------------

#[tauri::command]
async fn get_snapshot(app: AppHandle) -> Snapshot {
    build_snapshot(&app).await
}

#[tauri::command]
async fn get_preflight(state: State<'_, AppState>) -> CmdResult<server::Preflight> {
    let settings = state.settings();
    follow_selection(&state, &settings);
    let mut pf = server::preflight(&settings).await;
    plan_split(&state, &settings, &mut pf);
    Ok(pf)
}

/// A model split across this host and its members (cluster step 3).
struct SplitPlan {
    stages: Vec<split::Stage>,
    model: cluster::ModelSpec,
    dtype: Option<String>,
    /// Rank 0's YaRN setting; every rank must use it.
    yarn: Option<openphalanx_core::catalog::Yarn>,
}

/// The model members are asked to keep on disk (repo and pinned commit).
/// Local-folder models can't be fetched by members, so they have none.
fn model_spec(settings: &Settings) -> Option<cluster::ModelSpec> {
    let m = settings.selected_model.as_deref().and_then(|k| server::resolve(settings, k))?;
    Some(cluster::ModelSpec {
        key: m.key.clone(),
        label: format!("{}{}", m.label, m.quant.as_deref().map(|q| format!(" · {q}")).unwrap_or_default()),
        repo: m.repo.clone()?,
        revision: m.revision.clone()?,
        weight_bytes: m.weight_bytes,
    })
}

/// As host: the model members are asked for is the one selected here (also
/// after a restart, or a selection made before this machine was host).
fn follow_selection(state: &AppState, settings: &Settings) {
    if let Some(c) = state.cluster.as_ref().filter(|c| c.role() == Role::Host) {
        let _ = c.set_desired_model(model_spec(settings));
    }
}

/// Split strategy, as host with online members: a model that doesn't fit
/// this GPU is split across the servers instead. A model that fits runs
/// here alone (faster, and it keeps serving if a member drops out).
/// Adjusts the VRAM check to match and returns the plan; `None` when this
/// start doesn't split.
fn plan_split(state: &AppState, settings: &Settings, pf: &mut server::Preflight) -> Option<SplitPlan> {
    let (Some(req), Some(fit), Some(model)) = (pf.requirement, pf.fit.clone(), pf.model.clone()) else { return None };
    if fit.fit != vram::Fit::Insufficient {
        return None;
    }
    let c = state.cluster.as_ref()?;
    let pool = cluster_pool(state, pf.gpu.as_ref(), Some(fit.free_bytes), pf.running)?;
    let names = pool.iter().filter(|n| !n.this).map(|n| n.name.clone()).collect::<Vec<_>>().join(", ");

    let result = (|| -> Result<SplitPlan, String> {
        let spec = model_spec(settings).ok_or_else(|| {
            format!("{} is a local folder, which {names} can't fetch, so it can't be split across the cluster.", model.label)
        })?;
        let members = c.members();
        for n in pool.iter().filter(|n| !n.this) {
            let Some(r) = members.iter().find(|m| m.id == n.id).and_then(|m| m.report.as_ref()) else { continue };
            if !r.inventory.nvidia_runtime {
                return Err(format!(
                    "{} can't run GPU containers yet: install the NVIDIA Container Toolkit there \
                     (sudo nvidia-ctk runtime configure --runtime=docker, then restart Docker).",
                    n.name
                ));
            }
            match &r.model_sync {
                Some(s) if s.repo == spec.repo && s.revision == spec.revision && s.state == "ready" => {}
                Some(s) if s.repo == spec.repo && s.revision == spec.revision && s.state == "missing" => {
                    return Err(format!(
                        "{} doesn't have {} ({}). Download it there first; Start offers to.",
                        n.name,
                        spec.label,
                        vram::fmt_gib(spec.weight_bytes)
                    ));
                }
                Some(s) if s.repo == spec.repo && s.revision == spec.revision && s.state == "downloading" => {
                    let pct = (s.done_bytes * 100).checked_div(s.total_bytes).unwrap_or(0);
                    return Err(format!("{} is still downloading {} ({pct}%). Start again when it's done.", n.name, spec.label));
                }
                Some(s) if s.repo == spec.repo && s.state == "error" => {
                    return Err(format!(
                        "{} couldn't download {}: {}",
                        n.name,
                        spec.label,
                        s.error.clone().unwrap_or_default()
                    ));
                }
                _ => return Err(format!("checking whether {} has {}…", n.name, spec.label)),
            }
        }
        let dir = model.installed_dir.as_deref().ok_or("The model isn't downloaded on this machine.")?;
        let shape = split::ModelShape::read(dir).map_err(|e| format!("{e:#}"))?;
        let caps: Vec<split::Capacity> = pool
            .iter()
            .map(|n| split::Capacity { id: n.id.clone(), name: n.name.clone(), free_bytes: n.available_bytes })
            .collect();
        let stages = split::plan(&req, &shape, &caps)?;
        Ok(SplitPlan { stages, model: spec, dtype: model.dtype.clone(), yarn: model.rope_override(req.context_len) })
    })();

    let check = pf.checks.iter_mut().find(|c| c.id == "vram")?;
    match result {
        Ok(plan) => {
            check.status = CheckStatus::Pass;
            check.detail = format!(
                "Doesn't fit this GPU alone ({} free), so it's split across the cluster: {}.",
                vram::fmt_gib(fit.free_bytes),
                split::describe(&plan.stages)
            );
            pf.can_start = !pf.running && pf.checks.iter().all(|c| c.status != CheckStatus::Fail);
            Some(plan)
        }
        Err(reason) => {
            check.detail = format!("Fits the cluster's pooled VRAM with {names}, but can't be split yet: {reason}");
            None
        }
    }
}

#[tauri::command]
async fn start_server(app: AppHandle, state: State<'_, AppState>) -> CmdResult<()> {
    {
        let inner = state.inner.lock().unwrap();
        if matches!(inner.phase, Phase::Starting { .. } | Phase::Stopping) {
            return Err("The backend is already starting or stopping.".into());
        }
    }
    // One controller per cluster: claim it (a member takes the host role
    // over first; the host refuses while its own server runs). Shown as
    // starting meanwhile, so the monitor keeps the claim.
    state.inner.lock().unwrap().phase = Phase::Starting { detail: "Checking the cluster…".into() };
    let release = |state: &AppState| {
        state.inner.lock().unwrap().phase = Phase::Idle;
        if let Some(c) = &state.cluster {
            c.set_serving(false);
        }
    };
    if let Some(c) = state.cluster.clone() {
        if let Err(e) = c.claim_start().await {
            release(&state);
            return Err(format!("{e:#}"));
        }
    }
    let settings = state.settings();
    // A host asks its members to have the same model on disk (they download
    // it from the same pinned commit if they don't).
    if let Some(c) = state.cluster.as_ref().filter(|c| !c.is_member()) {
        follow_selection(&state, &settings);
        let spec = model_spec(&settings);
        // Members answer on their next report (every 5 s): wait for each
        // online one to report on this model, so the split check below sees
        // whether it's there instead of the previous model.
        if let Some(spec) = spec.filter(|_| c.role() == Role::Host && c.strategy() == Strategy::Split) {
            for _ in 0..16 {
                let pending = c.members().iter().filter(|m| m.online).any(|m| {
                    !m.report.as_ref().and_then(|r| r.model_sync.as_ref()).is_some_and(|s| {
                        s.repo == spec.repo && s.revision == spec.revision && s.state != "checking"
                    })
                });
                if !pending {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
        }
    }
    let mut pf = server::preflight(&settings).await;
    let plan = plan_split(&state, &settings, &mut pf);
    if !pf.can_start {
        release(&state);
        return Err(pf
            .checks
            .iter()
            .find(|c| c.status == CheckStatus::Fail)
            .map(|c| c.detail.clone())
            .unwrap_or_else(|| "The backend is already running.".into()));
    }
    state.inner.lock().unwrap().phase = Phase::Starting { detail: "Preparing…".into() };

    // Split: members get their orders in the next report reply (within 5 s)
    // and start their workers; rank 0 waits for them to connect.
    let split_start = match (&plan, &state.cluster) {
        (Some(plan), Some(c)) => {
            let Some(ip) = net::lan_ip() else {
                release(&state);
                return Err("This machine has no LAN address to split the model over.".into());
            };
            // Unique per start, so members replace any older worker.
            let run_id = format!(
                "{:x}",
                std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_nanos()
            );
            let rank = |r: u32, interface: Option<String>| docker::SplitRank {
                rank: r,
                nnodes: plan.stages.len() as u32,
                dist_init_addr: format!("{ip}:{}", split::DIST_PORT),
                partition: split::partition(&plan.stages),
                interface,
            };
            let orders = plan.stages[1..]
                .iter()
                .map(|stage| {
                    (stage.id.clone(), cluster::WorkerOrder {
                        run_id: run_id.clone(),
                        model: plan.model.clone(),
                        image: settings.image(),
                        context_len: settings.context_len,
                        dtype: plan.dtype.clone(),
                        yarn: plan.yarn,
                        rank: rank(stage.rank, None),
                        stage: stage.clone(),
                    })
                })
                .collect();
            c.set_workers(orders);
            state.inner.lock().unwrap().split = Some(split::describe(&plan.stages));
            Some(server::SplitStart { rank: rank(0, split::interface_for(ip)), stage: plan.stages[0].clone() })
        }
        _ => {
            state.inner.lock().unwrap().split = None;
            None
        }
    };

    tauri::async_runtime::spawn(async move {
        let progress_app = app.clone();
        let result = server::start(&settings, split_start, move |p| {
            let detail = match &p {
                StartProgress::Checking => "Checking GPU memory…".to_string(),
                StartProgress::PullingImage { line } => format!("Downloading backend image… {line}"),
                StartProgress::BuildingImage { line } => {
                    // Docker reports each layer of the base download as "1.05GB / 3.29GB".
                    let size = line
                        .split_whitespace()
                        .collect::<Vec<_>>()
                        .windows(3)
                        .find(|w| w[1] == "/" && w[0].ends_with('B') && w[2].ends_with('B'))
                        .map(|w| format!(" · layer {} / {}", w[0], w[2]))
                        .unwrap_or_default();
                    format!("Building the backend image (first start: downloads the SGLang base, about 16 GB, once){size}…")
                }
                StartProgress::Launching => "Launching the backend…".to_string(),
            };
            let _ = progress_app.emit("start-progress", &p);
            progress_app.state::<AppState>().inner.lock().unwrap().phase = Phase::Starting { detail };
        })
        .await;
        let st = app.state::<AppState>();
        let mut inner = st.inner.lock().unwrap();
        match result {
            Ok(()) => {
                inner.phase = Phase::Idle;
                inner.expect_running = true;
                // Clients pair with the cluster's host only.
                inner.want_pairing = !st.cluster.as_ref().is_some_and(|c| c.is_member());
            }
            Err(e) => {
                inner.phase = Phase::Failed { message: err(e) };
                inner.split = None;
                if let Some(c) = &st.cluster {
                    c.set_serving(false);
                    c.set_workers(HashMap::new());
                }
            }
        }
    });
    Ok(())
}

#[tauri::command]
async fn stop_server(state: State<'_, AppState>) -> CmdResult<()> {
    {
        let mut inner = state.inner.lock().unwrap();
        if matches!(inner.phase, Phase::Starting { .. }) {
            return Err("Still preparing the backend. Stop it once it has launched.".into());
        }
        inner.phase = Phase::Stopping;
        inner.expect_running = false;
        inner.want_pairing = false;
        inner.stop_epoch += 1;
    }
    if let Some(c) = &state.cluster {
        c.set_workers(HashMap::new()); // members stop their shares too
    }
    let result = docker::stop().await;
    let mut inner = state.inner.lock().unwrap();
    inner.split = None;
    inner.phase = Phase::Idle;
    inner.expect_running = false;
    inner.stop_epoch += 1; // ticks that overlapped the stop are stale too
    result.map_err(err)
}

#[tauri::command]
fn dismiss_error(state: State<'_, AppState>) {
    let mut inner = state.inner.lock().unwrap();
    if matches!(inner.phase, Phase::Failed { .. } | Phase::Paused { .. }) {
        inner.phase = Phase::Idle;
    }
}

#[tauri::command]
async fn get_logs(tail: u32) -> CmdResult<String> {
    docker::logs_tail(tail).await.map_err(err)
}

// --------------------------------------------------------------------------
// Commands: app updates
// --------------------------------------------------------------------------

#[tauri::command]
async fn check_update() -> CmdResult<app_update::Check> {
    app_update::check(env!("CARGO_PKG_VERSION")).await.map_err(err)
}

#[derive(Clone, Serialize)]
struct UpdateProgress {
    stage: &'static str,
    done: u64,
    total: u64,
}

/// Downloads and installs the latest release, then relaunches the app. The
/// backend container keeps running meanwhile; the new app adopts it.
#[tauri::command]
async fn install_update(app: AppHandle) -> CmdResult<()> {
    let install = Install::detect();
    let emit = |stage: &'static str, done: u64, total: u64| {
        let _ = app.emit("update-progress", UpdateProgress { stage, done, total });
    };
    let last_pct = std::sync::atomic::AtomicU64::new(u64::MAX);
    let (_, file) = app_update::download(&install, |done, total| {
        let pct = (done * 100).checked_div(total).unwrap_or(0);
        if last_pct.swap(pct, Ordering::Relaxed) != pct {
            emit("downloading", done, total);
        }
    })
    .await
    .map_err(err)?;
    emit("installing", 0, 0);
    app_update::install(&install, &file).await.map_err(err)?;
    emit("restarting", 0, 0);
    // The new copy starts once this one has exited and freed its ports
    // (Tauri's restart() starts it first, and both would bind 9092).
    let exe = match &install {
        Install::AppImage { path } => path.clone(),
        _ => {
            let exe = std::env::current_exe().map_err(err)?;
            // apt replaced the file this process runs from.
            std::path::PathBuf::from(exe.to_string_lossy().trim_end_matches(" (deleted)").to_string())
        }
    };
    std::process::Command::new("sh")
        .args(["-c", "sleep 2; exec \"$0\""])
        .arg(&exe)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(err)?;
    app.exit(0);
    Ok(())
}

// --------------------------------------------------------------------------
// Commands: pairing and devices
// --------------------------------------------------------------------------

async fn require_admin() -> CmdResult<AdminClient> {
    admin_client().await.ok_or_else(|| "The backend is not running.".to_string())
}

#[tauri::command]
async fn new_pairing_code(state: State<'_, AppState>) -> CmdResult<admin::Pairing> {
    // Clients pair with the cluster's host only.
    if let Some(Role::Member { host, .. }) = state.cluster.as_ref().map(|c| c.role()) {
        return Err(format!("This server is a member of {}'s cluster. Pair clients with the host ({}).", host.name, host.url));
    }
    require_admin().await?.new_pairing().await.map_err(err)
}

#[tauri::command]
async fn clear_pairing_code() -> CmdResult<admin::Pairing> {
    require_admin().await?.clear_pairing().await.map_err(err)
}

#[tauri::command]
async fn list_devices() -> CmdResult<Vec<admin::Device>> {
    require_admin().await?.devices().await.map_err(err)
}

/// How long client pairings last (`None`: never). Saved, and applied at once
/// when the backend runs (it also gets it at every start).
#[tauri::command]
async fn set_pairing_ttl(state: State<'_, AppState>, days: Option<u32>) -> CmdResult<Settings> {
    if !openphalanx_core::settings::PAIRING_TTL_CHOICES.contains(&days) {
        return Err("Choose 1 day, 1 week, 1 month, 1 year or never.".into());
    }
    let settings = state.update_settings(|s| s.pairing_ttl_days = days)?;
    if let Some(admin) = admin_client().await {
        admin.set_pairing_ttl(days).await.map_err(err)?;
    }
    Ok(settings)
}

#[tauri::command]
async fn revoke_device(id: String) -> CmdResult<()> {
    require_admin().await?.revoke(&id).await.map(|_| ()).map_err(err)
}

// --------------------------------------------------------------------------
// Commands: models
// --------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
struct ModelRow {
    key: String,
    name: String,
    /// Catalog family ("Qwen", "Gemma", …); none for custom models.
    family: Option<String>,
    /// Who made a community quantization; none for official repos.
    quantized_by: Option<String>,
    /// Longest context that fits the available VRAM (none: not even 2k fits).
    max_fit_context: Option<u32>,
    repo: Option<String>,
    source_url: Option<String>,
    revision: Option<String>,
    params: Option<String>,
    quant: Option<String>,
    license: Option<String>,
    /// Year the model was published (catalog), e.g. "2025".
    released: Option<String>,
    notes: Option<String>,
    /// The full release date, for sorting (`released` is the year shown).
    released_on: Option<String>,
    /// Verified end to end on real hardware (catalog flag).
    tested: bool,
    /// Largest installed-or-downloadable model that fits comfortably right now.
    best_fit: bool,
    custom: bool,
    weight_bytes: u64,
    max_context: u32,
    requirement: Requirement,
    fit: Option<FitCheck>,
    installed_dir: Option<String>,
    /// The installed copy's files are damaged (what's wrong): delete and download again.
    broken: Option<String>,
    /// Downloaded by Openphalanx (so it can also be deleted from here).
    app_managed: bool,
}

#[derive(Debug, Clone, Serialize)]
struct ModelsView {
    rows: Vec<ModelRow>,
    selected: Option<String>,
    context_len: u32,
    gpu: Option<gpu::GpuInfo>,
    /// VRAM the backend can count on; measured before start when running.
    available_bytes: Option<u64>,
    available_basis: &'static str,
    /// Split cluster (host with online members): the VRAM each server adds.
    /// `available_bytes` is then their sum, and needs include every server's
    /// runtime memory (`vram::split_across`).
    pool: Option<Vec<PoolNode>>,
}

#[derive(Debug, Clone, Serialize)]
struct PoolNode {
    id: String,
    name: String,
    this: bool,
    gpu: Option<String>,
    available_bytes: u64,
    total_bytes: u64,
}

fn is_app_managed(dir: &Path) -> bool {
    dir.starts_with(paths::models_dir())
}

/// Split strategy: the model is shared out across the host and its online
/// members, so it can use their VRAM too (one GPU per machine, the one with
/// the most free memory, as reported every 5 s). `None` unless this is a
/// split cluster's host with an online member.
fn cluster_pool(state: &AppState, gpu: Option<&gpu::GpuInfo>, available: Option<u64>, running: bool) -> Option<Vec<PoolNode>> {
    let idle = state.inner.lock().unwrap().idle_member_free.clone();
    state
        .cluster
        .as_ref()
        .filter(|c| c.role() == Role::Host && c.strategy() == Strategy::Split)
        .map(|c| {
            let mut nodes = vec![PoolNode {
                id: c.id(),
                name: c.name(),
                this: true,
                gpu: gpu.map(|g| g.name.clone()),
                available_bytes: available.unwrap_or(0),
                total_bytes: gpu.map_or(0, |g| g.total_bytes),
            }];
            for m in c.members().into_iter().filter(|m| m.online) {
                let best = m.report.as_ref().and_then(|r| r.inventory.gpus.iter().max_by_key(|g| g.free_bytes).cloned());
                if let Some(g) = best {
                    // While serving, what it had before its worker started.
                    let available = if running { idle.get(&m.id).copied().unwrap_or(g.free_bytes) } else { g.free_bytes };
                    nodes.push(PoolNode {
                        id: m.id,
                        name: m.name,
                        this: false,
                        gpu: Some(g.name),
                        available_bytes: available,
                        total_bytes: g.total_bytes,
                    });
                }
            }
            nodes
        })
        .filter(|nodes| nodes.len() > 1)
}

/// The VRAM a model can use: this GPU's free memory (as it was before the
/// backend started, while it runs), or the cluster's pool on a split host
/// with online members. Fits, the context cap and `set_context_len` all use it.
struct VramBudget {
    gpu: Option<gpu::GpuInfo>,
    available: Option<u64>,
    basis: &'static str,
    pool: Option<Vec<PoolNode>>,
    servers: u32,
}

async fn vram_budget(state: &AppState, settings: &Settings) -> VramBudget {
    let gpus = gpu::query().await.unwrap_or_default();
    let gpu = gpus.into_iter().find(|g| g.index == settings.gpu_index);
    let running = docker::inspect().await.ok().flatten().is_some_and(|c| c.state.running);
    let (available, basis) = match &gpu {
        Some(g) if running => {
            let idle = state.inner.lock().unwrap().idle_free_vram.get(&g.index).copied();
            (Some(idle.unwrap_or(g.total_bytes)), "measured before the backend started")
        }
        Some(g) => (Some(g.free_bytes), "free right now"),
        None => (None, "no GPU detected"),
    };
    let pool = cluster_pool(state, gpu.as_ref(), available, running);
    let servers = pool.as_ref().map_or(1, |p| p.len() as u32);
    let (available, basis) = match &pool {
        Some(p) => (Some(p.iter().map(|n| n.available_bytes).sum()), "pooled across the cluster (split)"),
        None => (available, basis),
    };
    VramBudget { gpu, available, basis, pool, servers }
}

impl VramBudget {
    /// The longest context `m` fits in, or `None` when not even the minimum does.
    fn max_fit(&self, m: &server::ResolvedModel) -> Option<u32> {
        let cc = self.gpu.as_ref().and_then(|g| g.compute_capability);
        let need = |c: u32| vram::split_across(&m.requirement(c, cc), self.servers);
        self.available.and_then(|free| vram::max_fitting_context(m.max_context, free, need))
    }
}

/// The selected model's context ceiling, for the slider.
#[derive(Debug, Clone, Serialize)]
struct ContextLimit {
    model: Option<String>,
    /// The longest context that fits the VRAM; `None`: not even 2k fits (or no GPU).
    max_fit: Option<u32>,
    model_max: Option<u32>,
    /// The cap is the model's own maximum context, not the VRAM (which allows more).
    by_model: bool,
    /// Above this the model runs in its long-context mode (YaRN); `None`: it has none.
    native_max: Option<u32>,
    available_bytes: Option<u64>,
    basis: &'static str,
    servers: u32,
}

async fn context_limit(state: &AppState, settings: &Settings) -> ContextLimit {
    let budget = vram_budget(state, settings).await;
    let model = settings.selected_model.as_deref().and_then(|k| server::resolve(settings, k));
    let max_fit = model.as_ref().and_then(|m| budget.max_fit(m));
    let model_max = model.as_ref().map(|m| m.max_context);
    ContextLimit {
        model: model.as_ref().map(|m| m.label.clone()),
        max_fit,
        model_max,
        by_model: matches!((max_fit, model_max), (Some(f), Some(m)) if f >= m),
        native_max: model.as_ref().and_then(|m| m.yarn).map(|y| y.original_max),
        available_bytes: budget.available,
        basis: budget.basis,
        servers: budget.servers,
    }
}

#[tauri::command]
async fn get_context_limit(state: State<'_, AppState>) -> CmdResult<ContextLimit> {
    let settings = state.settings();
    Ok(context_limit(&state, &settings).await)
}

#[tauri::command]
async fn get_models(state: State<'_, AppState>) -> CmdResult<ModelsView> {
    let settings = state.settings();
    let VramBudget { gpu, available, basis, pool, servers } = vram_budget(&state, &settings).await;

    let mut keys: Vec<String> = catalog::catalog().iter().map(|e| server::catalog_key(&e.id)).collect();
    keys.extend(settings.custom_models.iter().map(|c| c.key.clone()));
    let mut rows: Vec<ModelRow> = keys
        .iter()
        .filter_map(|key| {
            let m = server::resolve(&settings, key)?;
            let entry = key.strip_prefix("catalog:").and_then(catalog::find);
            let cc = gpu.as_ref().and_then(|g| g.compute_capability);
            let need = |c: u32| vram::split_across(&m.requirement(c, cc), servers);
            let requirement = need(settings.context_len);
            let max_fit_context = available.and_then(|free| vram::max_fitting_context(m.max_context, free, need));
            Some(ModelRow {
                key: key.clone(),
                name: m.label.clone(),
                family: entry.as_ref().map(|e| e.family.clone()),
                quantized_by: entry.as_ref().and_then(|e| e.quantized_by.clone()),
                max_fit_context,
                repo: m.repo.clone(),
                source_url: entry.as_ref().map(|e| e.source_url()).or_else(|| {
                    m.repo.as_ref().map(|r| format!("https://huggingface.co/{r}"))
                }),
                revision: m.revision.clone(),
                params: entry.as_ref().map(|e| e.params.clone()),
                quant: m.quant.clone(),
                license: entry.as_ref().map(|e| e.license.clone()),
                released: entry.as_ref().and_then(|e| e.released.as_ref()).map(|d| d.chars().take(4).collect()),
                notes: entry.as_ref().and_then(|e| e.notes.clone()),
                released_on: entry.as_ref().and_then(|e| e.released.clone()),
                tested: entry.as_ref().is_some_and(|e| e.tested),
                best_fit: false,
                custom: entry.is_none(),
                weight_bytes: m.weight_bytes,
                max_context: m.max_context,
                fit: available.map(|free| vram::check(&requirement, free)),
                requirement,
                app_managed: m.installed_dir.as_deref().is_some_and(is_app_managed),
                broken: m.broken.clone(),
                installed_dir: m.installed_dir.map(|d| d.display().to_string()),
            })
        })
        .collect();
    // Recommend the catalog model with the most parameters that fits with
    // headroom ("Ok", not "Tight"); among equal sizes, prefer higher precision.
    let params = |r: &ModelRow| {
        r.key.strip_prefix("catalog:").and_then(catalog::find).and_then(|e| e.params_billions())
    };
    if let Some(best) = rows
        .iter_mut()
        .filter(|r| r.fit.as_ref().is_some_and(|f| f.fit == vram::Fit::Ok))
        .filter_map(|r| params(r).map(|p| ((p * 10.0) as u64, r.requirement.weight_bytes, r)))
        .max_by_key(|(p, w, _)| (*p, *w))
        .map(|(_, _, r)| r)
    {
        best.best_fit = true;
    }
    Ok(ModelsView {
        rows,
        selected: settings.selected_model,
        context_len: settings.context_len,
        gpu,
        available_bytes: available,
        available_basis: basis,
        pool,
    })
}

#[tauri::command]
fn select_model(state: State<'_, AppState>, key: String) -> CmdResult<Settings> {
    let settings = state.update_settings(|s| s.selected_model = Some(key))?;
    // As host: members start fetching it now, so a split start doesn't wait.
    if let Some(c) = state.cluster.as_ref().filter(|c| c.role() == Role::Host) {
        let _ = c.set_desired_model(model_spec(&settings));
    }
    Ok(settings)
}

/// Locked while the server starts or runs: the running model keeps the context
/// it started with, and the setting must say what it serves.
#[tauri::command]
async fn set_context_len(state: State<'_, AppState>, context_len: u32) -> CmdResult<Settings> {
    if !(2048..=262_144).contains(&context_len) {
        return Err("Context length must be between 2,048 and 262,144 tokens.".into());
    }
    let starting = matches!(state.inner.lock().unwrap().phase, Phase::Starting { .. } | Phase::Stopping);
    let running = docker::inspect().await.ok().flatten().is_some_and(|c| c.state.running);
    if starting || running {
        return Err("Stop the server to change the context window; it applies at the next start.".into());
    }
    // Never above what the selected model fits in the VRAM free right now
    // (or pooled across the cluster): a longer context runs out of memory.
    let limit = context_limit(&state, &state.settings()).await;
    if let (Some(max), Some(model)) = (limit.max_fit, &limit.model) {
        if context_len > max && limit.by_model {
            return Err(format!("{model} supports up to {max} tokens of context; that's the model's own maximum."));
        }
        if context_len > max {
            return Err(format!(
                "{model} fits up to {} tokens of context in the VRAM {} ({}). A longer context would run out of GPU memory.",
                max,
                if limit.servers > 1 { "pooled across the cluster" } else { "free on this GPU" },
                vram::fmt_gib(limit.available_bytes.unwrap_or(0)),
            ));
        }
    }
    state.update_settings(|s| s.context_len = context_len)
}

#[tauri::command]
fn set_gpu(state: State<'_, AppState>, index: u32) -> CmdResult<Settings> {
    state.update_settings(|s| s.gpu_index = index)
}

/// Takes effect on the next start (SearXNG starts alongside the backend).
#[tauri::command]
fn set_web_search(state: State<'_, AppState>, enabled: bool) -> CmdResult<Settings> {
    state.update_settings(|s| s.web_search = enabled)
}

/// Where a model key downloads from and to.
fn download_target(settings: &Settings, key: &str) -> CmdResult<(String, String, std::path::PathBuf)> {
    if let Some(id) = key.strip_prefix("catalog:") {
        let e = catalog::find(id).ok_or("Unknown catalog model.")?;
        return Ok((e.id.clone(), e.revision.clone(), model::app_model_dir(&e.id)));
    }
    let c = settings
        .custom_models
        .iter()
        .find(|c| c.key == key)
        .ok_or("Unknown model.")?;
    match (&c.repo, &c.revision) {
        (Some(repo), Some(rev)) => Ok((repo.clone(), rev.clone(), c.dir.clone())),
        _ => Err("Local models don't need downloading.".into()),
    }
}

#[tauri::command]
fn download_model(app: AppHandle, state: State<'_, AppState>, key: String) -> CmdResult<()> {
    let (repo, revision, dest) = download_target(&state.settings(), &key)?;
    let cancel: download::Cancel = Arc::new(AtomicBool::new(false));
    {
        let mut inner = state.inner.lock().unwrap();
        // A second click while it runs is harmless: the download keeps going.
        if inner.downloads.get(&key).is_some_and(|d| !d.finished && d.error.is_none()) {
            return Ok(());
        }
        inner.cancels.insert(key.clone(), cancel.clone());
        let view = DownloadView {
            key: key.clone(),
            done_bytes: 0,
            total_bytes: 0,
            bytes_per_sec: 0.0,
            current_file: "Listing files…".into(),
            error: None,
            finished: false,
        };
        // Shown at once; the first byte can be seconds away (listing, checking files on disk).
        let _ = app.emit("download", &view);
        inner.downloads.insert(key.clone(), view);
    }

    tauri::async_runtime::spawn(async move {
        let client = download::client();
        let progress_app = app.clone();
        let progress_key = key.clone();
        let result = async {
            let files = download::list_files(&client, &repo, &revision).await?;
            download::download(&client, &repo, &revision, &files, &dest, cancel, move |p| {
                let view = DownloadView {
                    key: progress_key.clone(),
                    done_bytes: p.done_bytes,
                    total_bytes: p.total_bytes,
                    bytes_per_sec: p.bytes_per_sec,
                    current_file: p.current_file,
                    error: None,
                    finished: false,
                };
                let _ = progress_app.emit("download", &view);
                progress_app.state::<AppState>().inner.lock().unwrap().downloads.insert(view.key.clone(), view);
            })
            .await
        }
        .await;

        let st = app.state::<AppState>();
        let mut inner = st.inner.lock().unwrap();
        inner.cancels.remove(&key);
        if let Some(view) = inner.downloads.get_mut(&key) {
            match result {
                Ok(_) => view.finished = true,
                Err(e) => view.error = Some(err(e)),
            }
            let _ = app.emit("download", &view.clone());
        }
    });
    Ok(())
}

#[tauri::command]
fn cancel_download(state: State<'_, AppState>, key: String) {
    if let Some(c) = state.inner.lock().unwrap().cancels.get(&key) {
        c.store(true, Ordering::Relaxed);
    }
}

#[tauri::command]
fn clear_download(state: State<'_, AppState>, key: String) {
    let mut inner = state.inner.lock().unwrap();
    if inner.downloads.get(&key).is_some_and(|d| d.finished || d.error.is_some()) {
        inner.downloads.remove(&key);
    }
}

async fn model_in_use(key: &str) -> bool {
    docker::inspect()
        .await
        .ok()
        .flatten()
        .is_some_and(|c| c.state.running && c.model_key.as_deref() == Some(key))
}

/// Deletes a downloaded model: the app's own copy, or its folder in the
/// Hugging Face cache (the UI warns that the cache may be shared). Never
/// touches user-provided folders.
#[tauri::command]
async fn delete_model(state: State<'_, AppState>, key: String) -> CmdResult<()> {
    if model_in_use(&key).await {
        return Err("Stop the backend before deleting the model it is serving.".into());
    }
    let settings = state.settings();
    let dir = server::resolve(&settings, &key)
        .and_then(|m| m.installed_dir)
        .ok_or("That model is not downloaded.")?;
    // Downloaded by the app: its folder. Found in the Hugging Face cache: that
    // model's whole cache folder (the UI says the cache may be shared).
    let target = if is_app_managed(&dir) {
        dir
    } else if let Some(repo) = paths::hf_cache_repo_dir(&dir, &paths::hf_hub_dir()) {
        repo
    } else {
        return Err("This copy is outside the app's models folder and the Hugging Face cache, so it is left alone.".into());
    };
    tokio::fs::remove_dir_all(&target).await.map_err(err)
}

#[derive(Debug, Clone, Serialize)]
struct CustomInspect {
    key: String,
    label: String,
    local: bool,
    dir: String,
    repo: Option<String>,
    revision: Option<String>,
    info: model::ModelInfo,
    requirement: Requirement,
    fit: Option<FitCheck>,
}

async fn inspect_source(state: &AppState, input: &str) -> CmdResult<CustomInspect> {
    let source = model::Source::parse(input).map_err(err)?;
    let settings = state.settings();
    let (label, local, dir, repo, revision, info) = match &source {
        model::Source::LocalDir { path } => {
            let info = model::inspect_local(path).map_err(err)?;
            let label = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
            (label, true, path.clone(), None, None, info)
        }
        model::Source::HuggingFace { repo, revision } => {
            let client = download::client();
            let sha = download::resolve_revision(&client, repo, revision).await.map_err(err)?;
            let (info, _) = download::inspect_remote(&client, repo, &sha).await.map_err(err)?;
            let label = repo.split('/').nth(1).unwrap_or(repo).to_string();
            (label, false, model::app_model_dir(repo), Some(repo.clone()), Some(sha), info)
        }
    };
    let ctx = settings.context_len.min(info.max_context);
    let gpu = gpu::query().await.ok().and_then(|g| g.into_iter().find(|g| g.index == settings.gpu_index));
    let requirement = vram::requirement(
        info.weight_bytes,
        info.quant.as_deref(),
        &info.arch,
        ctx,
        gpu.as_ref().and_then(|g| g.compute_capability),
    );
    let free = gpu.map(|g| g.free_bytes);
    Ok(CustomInspect {
        key: source.key(),
        label,
        local,
        dir: dir.display().to_string(),
        repo,
        revision,
        fit: free.map(|f| vram::check(&requirement, f)),
        requirement,
        info,
    })
}

#[tauri::command]
async fn inspect_custom(state: State<'_, AppState>, input: String) -> CmdResult<CustomInspect> {
    inspect_source(&state, &input).await
}

#[tauri::command]
async fn add_custom(app: AppHandle, state: State<'_, AppState>, input: String) -> CmdResult<String> {
    let i = inspect_source(&state, &input).await?;
    let custom = CustomModel {
        key: i.key.clone(),
        label: i.label.clone(),
        dir: i.dir.clone().into(),
        repo: i.repo.clone(),
        revision: i.revision.clone(),
        weight_bytes: i.info.weight_bytes,
        max_context: i.info.max_context,
        arch: i.info.arch,
        quant: i.info.quant.clone(),
    };
    state.update_settings(|s| {
        s.custom_models.retain(|c| c.key != custom.key);
        s.custom_models.push(custom);
    })?;
    if !i.local {
        download_model(app, state, i.key.clone())?;
    }
    Ok(i.key)
}

#[tauri::command]
async fn remove_custom(state: State<'_, AppState>, key: String) -> CmdResult<Settings> {
    if model_in_use(&key).await {
        return Err("Stop the backend before removing the model it is serving.".into());
    }
    let settings = state.settings();
    if let Some(c) = settings.custom_models.iter().find(|c| c.key == key) {
        if c.repo.is_some() && is_app_managed(&c.dir) && c.dir.exists() {
            tokio::fs::remove_dir_all(&c.dir).await.map_err(err)?;
        }
    }
    state.update_settings(|s| {
        s.custom_models.retain(|c| c.key != key);
        if s.selected_model.as_deref() == Some(key.as_str()) {
            s.selected_model = None;
        }
    })
}

// --------------------------------------------------------------------------
// Cluster: servers on this network, and the cluster this machine is in
// --------------------------------------------------------------------------

/// Runs the cluster service (API, discovery, reports) for as long as the app runs.
async fn serve_cluster(app: AppHandle) {
    let st = app.state::<AppState>();
    let Some(c) = st.cluster.clone() else { return };
    if let Err(e) = c.run().await {
        *st.cluster_error.lock().unwrap() = Some(format!(
            "The cluster service couldn't start ({e:#}). Is openphalanx-server or another Openphalanx app running on this machine?"
        ));
    }
}

fn require_cluster(state: &AppState) -> CmdResult<Arc<Cluster>> {
    state.cluster.clone().ok_or_else(|| "The cluster service isn't available.".to_string())
}

/// Invites a server on this network into this machine's cluster.
#[tauri::command]
async fn cluster_invite(state: State<'_, AppState>, id: String) -> CmdResult<()> {
    require_cluster(&state)?.invite(&id).await.map_err(err)
}

/// Asks a server (on the network, or a member) to host this machine's cluster.
#[tauri::command]
async fn cluster_make_host(state: State<'_, AppState>, id: String) -> CmdResult<()> {
    require_cluster(&state)?.request_host(&id).await.map_err(err)
}

#[tauri::command]
async fn cluster_approve_invite(state: State<'_, AppState>, host_id: String) -> CmdResult<()> {
    require_cluster(&state)?.approve_invite(&host_id).await.map_err(err)
}

/// Becomes host for a request; returns servers that couldn't be invited.
#[tauri::command]
async fn cluster_approve_host(state: State<'_, AppState>, from_id: String) -> CmdResult<Vec<String>> {
    require_cluster(&state)?.approve_host_request(&from_id).await.map_err(err)
}

#[tauri::command]
fn cluster_decline(state: State<'_, AppState>, id: String) -> CmdResult<()> {
    let c = require_cluster(&state)?;
    c.decline_invite(&id);
    c.decline_host_request(&id);
    Ok(())
}

/// Removes a member; its token stops working at once.
#[tauri::command]
fn cluster_remove(state: State<'_, AppState>, id: String) -> CmdResult<()> {
    match require_cluster(&state)?.remove_member(&id).map_err(err)? {
        true => Ok(()),
        false => Err("That server is no longer in the cluster.".into()),
    }
}

#[tauri::command]
async fn cluster_leave(state: State<'_, AppState>) -> CmdResult<()> {
    require_cluster(&state)?.leave().await.map_err(err)
}

#[tauri::command]
fn cluster_dissolve(state: State<'_, AppState>) -> CmdResult<()> {
    require_cluster(&state)?.dissolve().map_err(err)
}

/// How the cluster uses its GPUs ("split" or "replicas"); the host decides.
#[tauri::command]
fn cluster_set_strategy(state: State<'_, AppState>, strategy: Strategy) -> CmdResult<()> {
    require_cluster(&state)?.set_strategy(strategy).map_err(err)
}

/// As host: members that lack the cluster's model may download it.
#[tauri::command]
fn cluster_approve_download(state: State<'_, AppState>) -> CmdResult<()> {
    require_cluster(&state)?.approve_download().map_err(err)
}

/// A member's recent log lines (its backend and cluster events), as host.
#[tauri::command]
fn cluster_member_logs(state: State<'_, AppState>, id: String) -> CmdResult<Vec<String>> {
    Ok(require_cluster(&state)?.member_logs(&id))
}

/// Renames this server (as other servers and the app show it).
#[tauri::command]
fn cluster_rename(state: State<'_, AppState>, name: String) -> CmdResult<()> {
    require_cluster(&state)?.set_name(&name).map_err(err)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage({
            let (cluster, cluster_error) = match Cluster::open(&paths::cluster_dir()) {
                Ok(c) => (Some(c), None),
                Err(e) => (None, Some(format!("cluster state unavailable: {e:#}"))),
            };
            AppState {
                settings: Mutex::new(Settings::load()),
                inner: Mutex::new(Inner::default()),
                cluster,
                cluster_error: Mutex::new(cluster_error),
            }
        })
        .setup(|app| {
            tauri::async_runtime::spawn(monitor(app.handle().clone()));
            tauri::async_runtime::spawn(serve_cluster(app.handle().clone()));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_snapshot,
            get_preflight,
            start_server,
            stop_server,
            dismiss_error,
            get_logs,
            new_pairing_code,
            clear_pairing_code,
            list_devices,
            set_pairing_ttl,
            revoke_device,
            get_models,
            select_model,
            set_context_len,
            set_gpu,
            set_web_search,
            download_model,
            cancel_download,
            clear_download,
            delete_model,
            inspect_custom,
            add_custom,
            remove_custom,
            cluster_invite,
            cluster_make_host,
            cluster_approve_invite,
            cluster_approve_host,
            cluster_decline,
            cluster_remove,
            cluster_leave,
            cluster_dissolve,
            cluster_rename,
            cluster_set_strategy,
            cluster_approve_download,
            check_update,
            install_update,
            get_context_limit,
            cluster_member_logs,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Openphalanx")
        .run(|app, event| {
            // Tell the host this member is going away, so a split model pauses
            // at once instead of after a timeout.
            if let tauri::RunEvent::Exit = event {
                if let Some(c) = app.state::<AppState>().cluster.clone() {
                    tauri::async_runtime::block_on(c.goodbye());
                }
            }
        });
}
