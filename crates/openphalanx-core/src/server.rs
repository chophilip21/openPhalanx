//! Pre-flight checks and the start sequence for the backend container.

use std::path::PathBuf;

use anyhow::{bail, Result};
use serde::Serialize;

use crate::docker::{self, RunSpec, ADMIN_PORT};
use crate::gpu::{self, GpuInfo};
use crate::model;
use crate::settings::Settings;
use crate::vram::{self, ArchSpec, Fit, FitCheck, Requirement};
use crate::{admin, catalog, net, paths};

/// The selected model, from the catalog or the user's custom list.
#[derive(Debug, Clone, Serialize)]
pub struct ResolvedModel {
    pub key: String,
    pub label: String,
    pub quant: Option<String>,
    pub repo: Option<String>,
    pub revision: Option<String>,
    pub installed_dir: Option<PathBuf>,
    pub weight_bytes: u64,
    pub arch: ArchSpec,
    pub max_context: u32,
    pub min_compute_capability: Option<f32>,
}

impl ResolvedModel {
    pub fn context_len(&self, wanted: u32) -> u32 {
        wanted.min(self.max_context)
    }

    /// Conservative VRAM need on a GPU with the given compute capability.
    pub fn requirement(&self, wanted_context: u32, compute_capability: Option<f32>) -> Requirement {
        vram::requirement(
            self.weight_bytes,
            self.quant.as_deref(),
            &self.arch,
            self.context_len(wanted_context),
            compute_capability,
        )
    }
}

pub fn catalog_key(id: &str) -> String {
    format!("catalog:{id}")
}

pub fn resolve(settings: &Settings, key: &str) -> Option<ResolvedModel> {
    if let Some(id) = key.strip_prefix("catalog:") {
        let e = catalog::find(id)?;
        let installed_dir = model::find_installed(&e.id, Some(&e.revision))
            .or_else(|| model::find_installed(&e.id, None));
        return Some(ResolvedModel {
            key: key.to_string(),
            label: e.name.clone(),
            quant: Some(e.quant.clone()),
            repo: Some(e.id.clone()),
            revision: Some(e.revision.clone()),
            installed_dir,
            weight_bytes: e.weight_bytes,
            arch: e.arch,
            max_context: e.max_context,
            min_compute_capability: e.min_compute_capability,
        });
    }
    let c = settings.custom_models.iter().find(|c| c.key == key)?;
    let installed = match &c.repo {
        Some(_) => c.dir.join(model::COMPLETE_MARKER).is_file(),
        None => c.dir.join("config.json").is_file(),
    };
    Some(ResolvedModel {
        key: c.key.clone(),
        label: c.label.clone(),
        quant: c.quant.clone(),
        repo: c.repo.clone(),
        revision: c.revision.clone(),
        installed_dir: installed.then(|| c.dir.clone()),
        weight_bytes: c.weight_bytes,
        arch: c.arch,
        max_context: c.max_context,
        min_compute_capability: None,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CheckStatus {
    Pass,
    Warn,
    Fail,
}

#[derive(Debug, Clone, Serialize)]
pub struct Check {
    pub id: &'static str,
    pub label: &'static str,
    pub status: CheckStatus,
    pub detail: String,
}

impl Check {
    fn new(id: &'static str, label: &'static str, status: CheckStatus, detail: impl Into<String>) -> Self {
        Self { id, label, status, detail: detail.into() }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Preflight {
    pub checks: Vec<Check>,
    pub can_start: bool,
    pub gpu: Option<GpuInfo>,
    pub gpus: Vec<GpuInfo>,
    pub model: Option<ResolvedModel>,
    pub requirement: Option<Requirement>,
    pub fit: Option<FitCheck>,
    pub image_present: bool,
}

pub async fn preflight(settings: &Settings) -> Preflight {
    use CheckStatus::*;
    let mut checks = Vec::new();

    let docker_ok = match docker::daemon_version().await {
        Ok(v) => {
            checks.push(Check::new("docker", "Docker engine", Pass, format!("Docker {v}")));
            true
        }
        Err(e) => {
            checks.push(Check::new("docker", "Docker engine", Fail, e.to_string()));
            false
        }
    };

    if docker_ok {
        match docker::has_nvidia_runtime().await {
            Ok(true) => checks.push(Check::new("nvidia_runtime", "NVIDIA container toolkit", Pass, "nvidia runtime registered")),
            _ => checks.push(Check::new(
                "nvidia_runtime",
                "NVIDIA container toolkit",
                Fail,
                "Docker has no nvidia runtime. Install nvidia-container-toolkit and run: sudo nvidia-ctk runtime configure --runtime=docker",
            )),
        }
    }

    let gpus = gpu::query().await.unwrap_or_default();
    let gpu = gpus.iter().find(|g| g.index == settings.gpu_index).cloned();
    match &gpu {
        Some(g) => checks.push(Check::new("gpu", "GPU", Pass, format!("{} ({})", g.name, vram::fmt_gib(g.total_bytes)))),
        None if gpus.is_empty() => checks.push(Check::new("gpu", "GPU", Fail, "No NVIDIA GPU found (nvidia-smi failed).")),
        None => checks.push(Check::new("gpu", "GPU", Fail, format!("GPU {} not found.", settings.gpu_index))),
    }

    let image_present = docker_ok && docker::image_exists(&settings.image()).await.unwrap_or(false);
    if docker_ok {
        checks.push(if image_present {
            Check::new("image", "Backend image", Pass, settings.image())
        } else {
            Check::new("image", "Backend image", Warn, format!("{} will be downloaded on start (about 16 GB).", settings.image()))
        });
        if settings.web_search {
            let present = docker::image_exists(docker::SEARXNG_IMAGE).await.unwrap_or(false);
            checks.push(if present {
                Check::new("searxng", "Web search", Pass, "SearXNG ready (private to the backend)")
            } else {
                Check::new("searxng", "Web search", Warn, "SearXNG will be downloaded on start (about 0.4 GB).")
            });
        }
    }

    let container = if docker_ok { docker::inspect().await.ok().flatten() } else { None };
    let running = container.as_ref().is_some_and(|c| c.state.running);

    let model = settings.selected_model.as_deref().and_then(|k| resolve(settings, k));
    match &model {
        None => checks.push(Check::new("model", "Model", Fail, "Choose a model on the Models page.")),
        Some(m) if m.installed_dir.is_none() => {
            checks.push(Check::new("model", "Model", Fail, format!("{} is not downloaded yet.", m.label)))
        }
        Some(m) => checks.push(Check::new("model", "Model", Pass, format!("{} · {}", m.label, m.quant.clone().unwrap_or_default()))),
    }

    if let (Some(m), Some(g), Some(min)) = (&model, &gpu, model.as_ref().and_then(|m| m.min_compute_capability)) {
        if g.compute_capability.is_some_and(|cc| cc + 0.001 < min) {
            checks.push(Check::new(
                "compute",
                "GPU architecture",
                Warn,
                format!("{} is tuned for compute capability {min}+; this GPU is {:.1}.", m.label, g.compute_capability.unwrap_or(0.0)),
            ));
        }
    }

    let cc = gpu.as_ref().and_then(|g| g.compute_capability);
    let requirement = model.as_ref().map(|m| m.requirement(settings.context_len, cc));
    let fit = match (&requirement, &gpu) {
        (Some(req), Some(g)) if !running => Some(vram::check(req, g.free_bytes)),
        _ => None,
    };
    if let Some(f) = &fit {
        let status = match f.fit {
            Fit::Ok => Pass,
            Fit::Tight => Warn,
            Fit::Insufficient => Fail,
        };
        checks.push(Check::new("vram", "VRAM", status, f.message.clone()));
    }

    if docker_ok && !running {
        for (port, what) in [(settings.agent_port, "agent"), (ADMIN_PORT, "admin")] {
            if !net::port_is_free(port) {
                checks.push(Check::new(
                    if what == "agent" { "agent_port" } else { "admin_port" },
                    "Network port",
                    Fail,
                    format!("Port {port} ({what} API) is already in use by another program."),
                ));
            }
        }
    }

    let can_start = !running && checks.iter().all(|c| c.status != Fail);
    Preflight { checks, can_start, gpu, gpus, model, requirement, fit, image_present }
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "stage", rename_all = "snake_case")]
pub enum StartProgress {
    Checking,
    PullingImage { line: String },
    BuildingImage { line: String },
    Launching,
}

pub async fn start(settings: &Settings, mut on_progress: impl FnMut(StartProgress)) -> Result<()> {
    on_progress(StartProgress::Checking);
    let pf = preflight(settings).await;
    if !pf.can_start {
        let reason = pf
            .checks
            .iter()
            .find(|c| c.status == CheckStatus::Fail)
            .map(|c| c.detail.clone())
            .unwrap_or_else(|| "The backend is already running.".into());
        bail!(reason);
    }

    if !pf.image_present {
        let pulled = docker::pull(&settings.image(), |line| {
            on_progress(StartProgress::PullingImage { line })
        })
        .await;
        if let Err(pull_err) = pulled {
            let Some(ctx) = docker::local_build_context() else {
                bail!("Could not download the backend image: {pull_err}");
            };
            docker::build(&ctx, &settings.image(), |line| on_progress(StartProgress::BuildingImage { line }))
                .await?;
        }
    }

    // The pull can take minutes; re-measure VRAM right before launching.
    on_progress(StartProgress::Checking);
    let model = pf.model.expect("checked by preflight");
    let dir = model.installed_dir.clone().expect("checked by preflight");
    let gpu = gpu::query()
        .await?
        .into_iter()
        .find(|g| g.index == settings.gpu_index)
        .ok_or_else(|| anyhow::anyhow!("GPU {} disappeared", settings.gpu_index))?;
    let req = model.requirement(settings.context_len, gpu.compute_capability);
    let fit = vram::check(&req, gpu.free_bytes);
    if fit.fit == Fit::Insufficient {
        bail!(fit.message);
    }
    let fraction = vram::mem_fraction_static(&req, gpu.free_bytes, gpu.total_bytes)
        .ok_or_else(|| anyhow::anyhow!(fit.message.clone()))?;

    let state_dir = paths::backend_state_dir();
    std::fs::create_dir_all(&state_dir)?;
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&state_dir, std::fs::Permissions::from_mode(0o700))?;
    }

    on_progress(StartProgress::Launching);
    docker::ensure_network().await?;
    if settings.web_search {
        if !docker::image_exists(docker::SEARXNG_IMAGE).await? {
            docker::pull(docker::SEARXNG_IMAGE, |line| on_progress(StartProgress::PullingImage { line })).await?;
        }
        let settings_file = write_searxng_settings()?;
        docker::run_searxng(&settings_file, &admin::new_admin_token()).await?;
    }
    docker::run(&RunSpec {
        image: settings.image(),
        gpu_index: settings.gpu_index,
        agent_port: settings.agent_port,
        model: model::mount_for(&dir)?,
        model_key: model.key.clone(),
        mem_fraction_static: fraction,
        context_len: req.context_len,
        state_dir,
        admin_token: admin::new_admin_token(),
        web_search: settings.web_search,
    })
    .await
}

/// Writes the SearXNG settings (no secrets) where the container can read them.
fn write_searxng_settings() -> Result<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    let dir = paths::data_dir().join("searxng");
    std::fs::create_dir_all(&dir)?;
    let file = dir.join("settings.yml");
    std::fs::write(&file, docker::SEARXNG_SETTINGS)?;
    // SearXNG runs as its own unprivileged user inside the container.
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755))?;
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o644))?;
    Ok(file)
}
