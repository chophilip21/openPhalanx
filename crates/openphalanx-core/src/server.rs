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
    /// The installed copy's files are damaged (what's wrong); it can't be started.
    pub broken: Option<String>,
    pub weight_bytes: u64,
    pub arch: ArchSpec,
    pub max_context: u32,
    pub min_compute_capability: Option<f32>,
    /// Shown to clients so they know exactly which model they talk to.
    pub model_id: String,
    pub edit_format: Option<String>,
    pub reasoning_parser: Option<String>,
    /// SGLang `--dtype` override (catalog data).
    pub dtype: Option<String>,
    /// Long-context mode, when the user turned it on (`settings.long_context`);
    /// `max_context` already counts it.
    pub yarn: Option<catalog::Yarn>,
}

/// Edit format when the catalog doesn't name one.
pub const DEFAULT_EDIT_FORMAT: &str = "diff";

impl ResolvedModel {
    pub fn context_len(&self, wanted: u32) -> u32 {
        wanted.min(self.max_context)
    }

    /// YaRN for this context: only above the native window (it can slightly
    /// lower quality on short inputs, so it stays off when not needed).
    pub fn rope_override(&self, context_len: u32) -> Option<catalog::Yarn> {
        self.yarn.filter(|y| context_len > y.original_max)
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

#[cfg(test)]
mod yarn_tests {
    use super::*;

    #[test]
    fn yarn_only_above_the_native_window() {
        let mut settings = Settings::default();
        let key = "catalog:Qwen/Qwen2.5-Coder-14B-Instruct-AWQ";
        // Off unless the user asks for it.
        let off = resolve(&settings, key).unwrap();
        assert_eq!(off.max_context, 32_768, "native window only");
        assert!(off.yarn.is_none() && off.rope_override(65_536).is_none());
        settings.long_context = true;
        let m = resolve(&settings, key).unwrap();
        assert_eq!(m.max_context, 65_536, "native × factor");
        assert!(m.rope_override(32_768).is_none(), "off at the native window");
        let y = m.rope_override(36_864).expect("on above it");
        assert_eq!((y.factor, y.rope_theta), (2.0, 1e6));
        let plain = resolve(&settings, "catalog:openai/gpt-oss-20b").unwrap();
        assert!(plain.yarn.is_none() && plain.rope_override(131_072).is_none());
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
        // Opt-in: without the setting the model keeps its native window.
        let yarn = e.yarn.filter(|_| settings.long_context);
        return Some(ResolvedModel {
            key: key.to_string(),
            label: e.name.clone(),
            quant: Some(e.quant.clone()),
            repo: Some(e.id.clone()),
            revision: Some(e.revision.clone()),
            broken: installed_dir.as_deref().and_then(model::broken),
            installed_dir,
            weight_bytes: e.weight_bytes,
            arch: e.arch,
            max_context: yarn.map_or(e.max_context, |y| y.max_context().max(e.max_context)),
            min_compute_capability: e.min_compute_capability,
            model_id: e.id.clone(),
            edit_format: e.edit_format.clone(),
            reasoning_parser: e.reasoning_parser.clone(),
            dtype: e.dtype.clone(),
            yarn,
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
        broken: installed.then(|| model::broken(&c.dir)).flatten(),
        installed_dir: installed.then(|| c.dir.clone()),
        weight_bytes: c.weight_bytes,
        arch: c.arch,
        max_context: c.max_context,
        min_compute_capability: None,
        model_id: c.repo.clone().unwrap_or_else(|| c.label.clone()),
        edit_format: None,
        reasoning_parser: None,
        dtype: None,
        yarn: None,
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
    /// The backend is already running (`can_start` is false for that alone).
    pub running: bool,
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
            Check::new("image", "Backend image", Warn, if settings.image().starts_with(&format!("{}:", docker::IMAGE_NAME)) {
                "Built on the first start: Docker downloads the official SGLang image (about 16 GB, once) and adds \
                 OpenPhalanx's gateway."
                    .to_string()
            } else {
                format!("{} will be downloaded on start.", settings.image())
            })
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
        Some(m) if m.broken.is_some() => checks.push(Check::new(
            "model",
            "Model",
            Fail,
            format!(
                "{}'s files are broken ({}). Delete it on the Models page and download it again.",
                m.label,
                m.broken.as_deref().unwrap_or_default()
            ),
        )),
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
    Preflight { checks, can_start, running, gpu, gpus, model, requirement, fit, image_present }
}

#[derive(Debug, Clone, Serialize)]
#[serde(tag = "stage", rename_all = "snake_case")]
pub enum StartProgress {
    Checking,
    PullingImage { line: String },
    BuildingImage { line: String },
    Launching,
}

/// This machine's part of a model split across servers: rank 0 (the host).
#[derive(Debug, Clone)]
pub struct SplitStart {
    pub rank: docker::SplitRank,
    pub stage: crate::split::Stage,
}

pub async fn start(settings: &Settings, split: Option<SplitStart>, on_progress: impl FnMut(StartProgress)) -> Result<()> {
    start_with(settings, split, false, on_progress).await
}

/// `start`, with `force`: start although the model doesn't fit the free
/// VRAM. The user accepted that it may fail to load or crash under load.
pub async fn start_with(
    settings: &Settings,
    split: Option<SplitStart>,
    force: bool,
    mut on_progress: impl FnMut(StartProgress),
) -> Result<()> {
    on_progress(StartProgress::Checking);
    let pf = preflight(settings).await;
    // Split: this GPU only holds its stage, checked below instead of the
    // whole model. Forced: the VRAM check is the one being overridden.
    let blocking = pf
        .checks
        .iter()
        .find(|c| c.status == CheckStatus::Fail && !((split.is_some() || force) && c.id == "vram"));
    if let Some(check) = blocking {
        bail!(check.detail.clone());
    }
    if pf.running {
        bail!("The backend is already running.");
    }
    if pf.model.as_ref().and_then(|m| m.installed_dir.as_ref()).is_none() {
        bail!("No model is selected, or it isn't downloaded yet.");
    }

    if !pf.image_present {
        // First start (or a new app version): build the backend from the
        // official SGLang image; a custom image setting is pulled instead.
        let image = settings.image();
        let building = image.starts_with(&format!("{}:", docker::IMAGE_NAME));
        docker::ensure_image(&image, |line| {
            on_progress(if building {
                StartProgress::BuildingImage { line }
            } else {
                StartProgress::PullingImage { line }
            })
        })
        .await?;
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
    let req = match &split {
        Some(s) => s.stage.requirement,
        None => model.requirement(settings.context_len, gpu.compute_capability),
    };
    let fit = vram::check(&req, gpu.free_bytes);
    let fraction = match vram::mem_fraction_static(&req, gpu.free_bytes, gpu.total_bytes) {
        Some(f) if fit.fit != Fit::Insufficient => f,
        _ if force => vram::mem_fraction_forced(&req, gpu.free_bytes),
        _ => bail!(fit.message),
    };

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
        docker::run_searxng(&settings_file, &admin::new_admin_token(), split.is_some()).await?;
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
        model_id: model.model_id.clone(),
        edit_format: model.edit_format.clone().unwrap_or_else(|| DEFAULT_EDIT_FORMAT.to_string()),
        reasoning_parser: model.reasoning_parser.clone(),
        dtype: model.dtype.clone(),
        yarn: model.rope_override(req.context_len),
        pairing_ttl_days: settings.pairing_ttl_days,
        split: split.map(|s| s.rank),
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
