//! Openphalanx server GUI: Tauri commands plus a background monitor that
//! pushes a status snapshot to the frontend every two seconds.

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use openphalanx_core::admin::{self, AdminClient};
use openphalanx_core::server::{self, CheckStatus, StartProgress};
use openphalanx_core::settings::{CustomModel, Settings};
use openphalanx_core::vram::{self, FitCheck, Requirement};
use openphalanx_core::{catalog, docker, download, gpu, model, net, paths};
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
    /// Request a pairing code as soon as the admin API answers.
    want_pairing: bool,
    /// Free VRAM per GPU, last measured while our container was not running.
    idle_free_vram: HashMap<u32, u64>,
    downloads: HashMap<String, DownloadView>,
    cancels: HashMap<String, download::Cancel>,
    log_follower: bool,
}

pub struct AppState {
    settings: Mutex<Settings>,
    inner: Mutex<Inner>,
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

// --------------------------------------------------------------------------
// Snapshot
// --------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize)]
struct ServerView {
    /// stopped | starting | running | stopping | error | external
    state: &'static str,
    detail: Option<String>,
    model_key: Option<String>,
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
}

async fn admin_client() -> Option<AdminClient> {
    let c = docker::inspect().await.ok().flatten()?;
    (c.state.running && c.managed).then(|| c.admin_token.map(AdminClient::new)).flatten()
}

async fn build_snapshot(app: &AppHandle) -> Snapshot {
    let st = app.state::<AppState>();
    let settings = st.settings();
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
        matches!(inner.phase, Phase::Idle) && inner.expect_running && !running
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
    } else if managed {
        inner.expect_running = true; // adopt a container from a previous session
    }
    if let Some(message) = crash_message {
        inner.expect_running = false;
        inner.phase = Phase::Failed { message };
    }

    let (state, detail) = match &inner.phase {
        Phase::Starting { detail } => ("starting", Some(detail.clone())),
        Phase::Stopping => ("stopping", None),
        Phase::Failed { message } => ("error", Some(message.clone())),
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
    drop(inner);
    if need_follower {
        spawn_log_follower(app.clone());
    }

    Snapshot {
        server: ServerView {
            state,
            detail,
            model_key: container.and_then(|c| c.model_key),
        },
        gpus,
        admin: admin_status,
        endpoint: net::lan_ip().map(|ip| format!("{ip}:{}", settings.agent_port)),
        downloads,
        settings,
        warnings,
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
    Ok(server::preflight(&state.settings()).await)
}

#[tauri::command]
async fn start_server(app: AppHandle, state: State<'_, AppState>) -> CmdResult<()> {
    {
        let inner = state.inner.lock().unwrap();
        if matches!(inner.phase, Phase::Starting { .. } | Phase::Stopping) {
            return Err("The backend is already starting or stopping.".into());
        }
    }
    let settings = state.settings();
    let pf = server::preflight(&settings).await;
    if !pf.can_start {
        return Err(pf
            .checks
            .iter()
            .find(|c| c.status == CheckStatus::Fail)
            .map(|c| c.detail.clone())
            .unwrap_or_else(|| "The backend is already running.".into()));
    }
    state.inner.lock().unwrap().phase = Phase::Starting { detail: "Preparing…".into() };

    tauri::async_runtime::spawn(async move {
        let progress_app = app.clone();
        let result = server::start(&settings, move |p| {
            let detail = match &p {
                StartProgress::Checking => "Checking GPU memory…".to_string(),
                StartProgress::PullingImage { line } => format!("Downloading backend image… {line}"),
                StartProgress::BuildingImage { .. } => "Building backend image…".to_string(),
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
                inner.want_pairing = true;
            }
            Err(e) => inner.phase = Phase::Failed { message: err(e) },
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
    }
    let result = docker::stop().await;
    state.inner.lock().unwrap().phase = Phase::Idle;
    result.map_err(err)
}

#[tauri::command]
fn dismiss_error(state: State<'_, AppState>) {
    let mut inner = state.inner.lock().unwrap();
    if matches!(inner.phase, Phase::Failed { .. }) {
        inner.phase = Phase::Idle;
    }
}

#[tauri::command]
async fn get_logs(tail: u32) -> CmdResult<String> {
    docker::logs_tail(tail).await.map_err(err)
}

// --------------------------------------------------------------------------
// Commands: pairing and devices
// --------------------------------------------------------------------------

async fn require_admin() -> CmdResult<AdminClient> {
    admin_client().await.ok_or_else(|| "The backend is not running.".to_string())
}

#[tauri::command]
async fn new_pairing_code() -> CmdResult<admin::Pairing> {
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
    repo: Option<String>,
    source_url: Option<String>,
    revision: Option<String>,
    params: Option<String>,
    quant: Option<String>,
    license: Option<String>,
    notes: Option<String>,
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
}

fn is_app_managed(dir: &Path) -> bool {
    dir.starts_with(paths::models_dir())
}

#[tauri::command]
async fn get_models(state: State<'_, AppState>) -> CmdResult<ModelsView> {
    let settings = state.settings();
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

    let mut keys: Vec<String> = catalog::catalog().iter().map(|e| server::catalog_key(&e.id)).collect();
    keys.extend(settings.custom_models.iter().map(|c| c.key.clone()));
    let mut rows: Vec<ModelRow> = keys
        .iter()
        .filter_map(|key| {
            let m = server::resolve(&settings, key)?;
            let entry = key.strip_prefix("catalog:").and_then(catalog::find);
            let requirement = m.requirement(settings.context_len, gpu.as_ref().and_then(|g| g.compute_capability));
            Some(ModelRow {
                key: key.clone(),
                name: m.label.clone(),
                repo: m.repo.clone(),
                source_url: entry.as_ref().map(|e| e.source_url()).or_else(|| {
                    m.repo.as_ref().map(|r| format!("https://huggingface.co/{r}"))
                }),
                revision: m.revision.clone(),
                params: entry.as_ref().map(|e| e.params.clone()),
                quant: m.quant.clone(),
                license: entry.as_ref().map(|e| e.license.clone()),
                notes: entry.as_ref().and_then(|e| e.notes.clone()),
                tested: entry.as_ref().is_some_and(|e| e.tested),
                best_fit: false,
                custom: entry.is_none(),
                weight_bytes: m.weight_bytes,
                max_context: m.max_context,
                fit: available.map(|free| vram::check(&requirement, free)),
                requirement,
                app_managed: m.installed_dir.as_deref().is_some_and(is_app_managed),
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
    })
}

#[tauri::command]
fn select_model(state: State<'_, AppState>, key: String) -> CmdResult<Settings> {
    state.update_settings(|s| s.selected_model = Some(key))
}

#[tauri::command]
fn set_context_len(state: State<'_, AppState>, context_len: u32) -> CmdResult<Settings> {
    if !(2048..=262_144).contains(&context_len) {
        return Err("Context length must be between 2,048 and 262,144 tokens.".into());
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
        if inner.downloads.get(&key).is_some_and(|d| !d.finished && d.error.is_none()) {
            return Err("Already downloading.".into());
        }
        inner.cancels.insert(key.clone(), cancel.clone());
        inner.downloads.insert(
            key.clone(),
            DownloadView {
                key: key.clone(),
                done_bytes: 0,
                total_bytes: 0,
                bytes_per_sec: 0.0,
                current_file: "Listing files…".into(),
                error: None,
                finished: false,
            },
        );
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

/// Deletes weights that Openphalanx downloaded. Never touches the Hugging
/// Face cache or user-provided folders.
#[tauri::command]
async fn delete_model(state: State<'_, AppState>, key: String) -> CmdResult<()> {
    if model_in_use(&key).await {
        return Err("Stop the backend before deleting the model it is serving.".into());
    }
    let settings = state.settings();
    let dir = server::resolve(&settings, &key)
        .and_then(|m| m.installed_dir)
        .ok_or("That model is not downloaded.")?;
    if !is_app_managed(&dir) {
        return Err("This copy was not downloaded by Openphalanx, so it is left alone.".into());
    }
    tokio::fs::remove_dir_all(&dir).await.map_err(err)
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            settings: Mutex::new(Settings::load()),
            inner: Mutex::new(Inner::default()),
        })
        .setup(|app| {
            tauri::async_runtime::spawn(monitor(app.handle().clone()));
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
        ])
        .run(tauri::generate_context!())
        .expect("error while running Openphalanx");
}
