//! Pre-flight checks and the start sequence for the backend container.

use std::path::PathBuf;

use anyhow::{bail, Result};
use serde::Serialize;

use crate::docker::{self, RunSpec};
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
    /// The card a model that fits one GPU runs on: of the selected ones,
    /// the one with the most free memory.
    pub gpu: Option<GpuInfo>,
    pub gpus: Vec<GpuInfo>,
    /// The selected GPUs that are present, by index.
    pub selected_gpus: Vec<u32>,
    /// The model doesn't fit one card and is spread over the selected ones
    /// (experimental): one stage per GPU, in index order.
    pub local_split: Option<Vec<crate::split::Stage>>,
    pub model: Option<ResolvedModel>,
    pub requirement: Option<Requirement>,
    pub fit: Option<FitCheck>,
    pub image_present: bool,
}

/// Of these cards, the one with the most free memory (the lowest index on a tie).
pub fn best_gpu(gpus: &[GpuInfo]) -> Option<GpuInfo> {
    gpus.iter().max_by_key(|g| (g.free_bytes, std::cmp::Reverse(g.index))).cloned()
}

/// One model over several GPUs of this machine, when it doesn't fit the best
/// one alone. `None`: it fits one card (it then runs there, as on a
/// single-GPU machine), or there is only one. Experimental: SGLang's
/// pipeline parallelism with every rank on this node, sized by the planner
/// the cluster split uses; not yet run on a machine with several cards.
pub fn local_split(req: &Requirement, model_dir: &std::path::Path, gpus: &[GpuInfo]) -> Option<Result<Vec<crate::split::Stage>, String>> {
    let best = best_gpu(gpus)?;
    if gpus.len() < 2 || vram::check(req, best.free_bytes).fit != Fit::Insufficient {
        return None;
    }
    Some(crate::split::ModelShape::read(model_dir).map_err(|e| format!("{e:#}")).and_then(|shape| crate::split::plan_gpus(req, &shape, gpus)))
}

/// `--mem-fraction-static` for a model spread over this machine's GPUs: one
/// value for every rank, so the smallest any stage allows (the largest
/// runtime reserve). SGLang sizes the KV cache by the tightest rank anyway.
fn local_split_fraction(stages: &[crate::split::Stage], gpus: &[GpuInfo]) -> Option<f64> {
    stages
        .iter()
        .zip(gpus)
        .map(|(s, g)| vram::mem_fraction_static(&s.requirement, g.free_bytes, g.total_bytes))
        .try_fold(f64::MAX, |least, f| f.map(|f| least.min(f)))
        .filter(|f| *f < f64::MAX)
}

/// Fits, tight or not, over these cards: on the best one alone, or, when it
/// doesn't fit there and there are several, over all of them together
/// (never better than tight: that path is experimental).
pub fn check_over(req: &Requirement, gpus: &[GpuInfo]) -> Option<FitCheck> {
    let alone = vram::check(req, best_gpu(gpus)?.free_bytes);
    if alone.fit != Fit::Insufficient || gpus.len() < 2 {
        return Some(alone);
    }
    let mut pooled = vram::check(&vram::split_across(req, gpus.len() as u32), gpus.iter().map(|g| g.free_bytes).sum());
    if pooled.fit == Fit::Ok {
        pooled.fit = Fit::Tight;
    }
    Some(pooled)
}

/// The longest context a model can start with over these cards (alone on
/// the best one, or over all of them).
pub fn max_context_over(max_context: u32, gpus: &[GpuInfo], need: impl Fn(u32) -> Requirement) -> Option<u32> {
    let alone = vram::max_fitting_context(max_context, best_gpu(gpus)?.free_bytes, &need);
    if gpus.len() < 2 {
        return alone;
    }
    let n = gpus.len() as u32;
    let pooled = vram::max_fitting_context(max_context, gpus.iter().map(|g| g.free_bytes).sum(), |c| vram::split_across(&need(c), n));
    alone.max(pooled)
}

fn gpu_names(gpus: &[GpuInfo]) -> String {
    gpus.iter().map(|g| format!("GPU {}", g.index)).collect::<Vec<_>>().join(", ")
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
    let selected = settings.selected_gpus(&gpus);
    let gpu = best_gpu(&selected);
    match &gpu {
        Some(g) if selected.len() == 1 => {
            checks.push(Check::new("gpu", "GPU", Pass, format!("{} ({})", g.name, vram::fmt_gib(g.total_bytes))))
        }
        Some(_) => checks.push(Check::new(
            "gpu",
            "GPU",
            Pass,
            selected
                .iter()
                .map(|g| format!("GPU {}: {} ({})", g.index, g.name, vram::fmt_gib(g.total_bytes)))
                .collect::<Vec<_>>()
                .join(" · "),
        )),
        None if gpus.is_empty() => checks.push(Check::new("gpu", "GPU", Fail, "No NVIDIA GPU found (nvidia-smi failed).")),
        None => checks.push(Check::new(
            "gpu",
            "GPU",
            Fail,
            format!(
                "None of the selected GPUs ({}) is in this machine. Choose GPUs on the Settings page.",
                settings.gpus.clone().unwrap_or_default().iter().map(|i| i.to_string()).collect::<Vec<_>>().join(", ")
            ),
        )),
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
    let mut fit = match (&requirement, &gpu) {
        (Some(req), Some(g)) if !running => Some(vram::check(req, g.free_bytes)),
        _ => None,
    };
    // Doesn't fit the best card: over all the selected ones, if there are several.
    let mut local = None;
    let dir = model.as_ref().and_then(|m| m.installed_dir.clone());
    if let (Some(req), Some(f), Some(dir)) = (&requirement, fit.as_mut(), dir.as_deref()) {
        match local_split(req, dir, &selected) {
            Some(Ok(stages)) => {
                let free: u64 = selected.iter().map(|g| g.free_bytes).sum();
                let over = vram::split_across(req, selected.len() as u32);
                *f = FitCheck {
                    // It starts, as far as the plan can tell; never "fits comfortably".
                    fit: Fit::Tight,
                    required_bytes: over.total_bytes,
                    free_bytes: free,
                    headroom_bytes: free as i64 - over.total_bytes as i64,
                    message: format!(
                        "Doesn't fit one GPU, so it is spread over {} (experimental, not yet tried on real hardware): {}.",
                        gpu_names(&selected),
                        crate::split::describe(&stages)
                    ),
                };
                local = Some(stages);
            }
            Some(Err(reason)) => f.message = format!("{} Over {} it doesn't fit either: {reason}", f.message, gpu_names(&selected)),
            None => {}
        }
    }
    if let Some(f) = &fit {
        let status = match f.fit {
            Fit::Ok => Pass,
            Fit::Tight => Warn,
            Fit::Insufficient => Fail,
        };
        checks.push(Check::new("vram", "VRAM", status, f.message.clone()));
    }

    if docker_ok && !running {
        for (port, what) in [(settings.agent_port, "agent"), (settings.admin_port, "admin")] {
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
    let selected_gpus = selected.iter().map(|g| g.index).collect();
    Preflight { checks, can_start, running, gpu, gpus, selected_gpus, local_split: local, model, requirement, fit, image_present }
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
    let selected = settings.selected_gpus(&gpu::query().await?);
    let gpu = best_gpu(&selected).ok_or_else(|| anyhow::anyhow!("The selected GPUs disappeared"))?;
    let req = match &split {
        Some(s) => s.stage.requirement,
        None => model.requirement(settings.context_len, gpu.compute_capability),
    };
    let fit = vram::check(&req, gpu.free_bytes);
    // A model that doesn't fit the best card goes over all the selected
    // ones. Not as part of a cluster split: this machine then gives one card.
    let local = match &split {
        None => local_split(&req, &dir, &selected).and_then(|plan| plan.ok()),
        Some(_) => None,
    };
    let local = local.and_then(|stages| local_split_fraction(&stages, &selected).map(|f| (stages, f)));
    let (cards, fraction, partition) = match local {
        Some((stages, f)) => (selected.iter().map(|g| g.index).collect(), f, Some(crate::split::partition(&stages))),
        None => {
            let fraction = match vram::mem_fraction_static(&req, gpu.free_bytes, gpu.total_bytes) {
                Some(f) if fit.fit != Fit::Insufficient => f,
                _ if force => vram::mem_fraction_forced(&req, gpu.free_bytes),
                _ => bail!(fit.message),
            };
            (vec![gpu.index], fraction, None)
        }
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
        gpus: cards,
        agent_port: settings.agent_port,
        admin_port: settings.admin_port,
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
        local_split: partition,
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

#[cfg(test)]
mod tests {
    use super::*;

    const GIB: u64 = 1 << 30;

    fn card(index: u32, free_gib: u64) -> GpuInfo {
        GpuInfo {
            index,
            name: format!("Card {index}"),
            total_bytes: 24 * GIB,
            used_bytes: 24 * GIB - free_gib * GIB,
            free_bytes: free_gib * GIB,
            utilization_pct: None,
            temperature_c: None,
            compute_capability: Some(8.6),
        }
    }

    /// About 28 GiB comfortable: too much for one 24 GB card, fine for two.
    fn big() -> Requirement {
        Requirement {
            context_len: 32768,
            download_bytes: 17 * GIB,
            weight_bytes: 18 * GIB,
            kv_bytes: 8 * GIB,
            overhead_bytes: 2 * GIB,
            total_bytes: 28 * GIB,
            fp8_upcast: false,
        }
    }

    fn small() -> Requirement {
        Requirement { weight_bytes: 9 * GIB, kv_bytes: 4 * GIB, total_bytes: 15 * GIB, ..big() }
    }

    fn model_dir() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("config.json"), r#"{"num_hidden_layers": 64, "vocab_size": 151936, "hidden_size": 4096}"#).unwrap();
        dir
    }

    #[test]
    fn a_model_that_fits_one_card_runs_on_the_one_with_the_most_free_memory() {
        let cards = [card(0, 10), card(1, 23), card(2, 23)];
        assert_eq!(best_gpu(&cards).unwrap().index, 1, "the lowest index on a tie");
        let dir = model_dir();
        assert!(local_split(&small(), dir.path(), &cards).is_none(), "fits card 1: nothing is spread");
        assert_eq!(check_over(&small(), &cards).unwrap().free_bytes, 23 * GIB);
        // One card: as before, whatever the model needs.
        assert!(local_split(&big(), dir.path(), &cards[1..2]).is_none());
        assert_eq!(check_over(&big(), &cards[1..2]).unwrap().fit, Fit::Insufficient);
        assert!(best_gpu(&[]).is_none());
        assert!(check_over(&big(), &[]).is_none());
    }

    #[test]
    fn a_model_too_big_for_one_card_is_spread_over_the_selected_ones() {
        let cards = [card(0, 23), card(1, 16)];
        let dir = model_dir();
        let stages = local_split(&big(), dir.path(), &cards).expect("doesn't fit one").expect("fits two");
        assert_eq!(stages.iter().map(|s| s.layers).sum::<u32>(), 64);
        assert_eq!((stages[0].name.as_str(), stages[1].name.as_str()), ("GPU 0", "GPU 1"));
        assert!(stages[0].layers > stages[1].layers, "the card with more free memory takes more layers");
        // One fraction for every rank: the smallest any stage allows.
        let fraction = local_split_fraction(&stages, &cards).unwrap();
        for (s, g) in stages.iter().zip(&cards) {
            assert!(fraction <= vram::mem_fraction_static(&s.requirement, g.free_bytes, g.total_bytes).unwrap());
        }
        // It starts, but is never called a comfortable fit.
        assert_eq!(check_over(&big(), &cards).unwrap().fit, Fit::Tight);
        // Not enough together: the reason names GPUs, not servers.
        let tight = [card(0, 12), card(1, 8)];
        let why = local_split(&big(), dir.path(), &tight).unwrap().unwrap_err();
        assert!(why.contains("2 GPUs"), "{why}");
        assert_eq!(check_over(&big(), &tight).unwrap().fit, Fit::Insufficient);
        // No config.json to read the layers from: said, not a panic.
        let empty = tempfile::tempdir().unwrap();
        assert!(local_split(&big(), empty.path(), &cards).unwrap().is_err());
    }

    #[test]
    fn the_longest_context_counts_all_selected_cards() {
        let need = |c: u32| Requirement { context_len: c, kv_bytes: u64::from(c) * 400_000, ..small() };
        let one = max_context_over(131_072, &[card(0, 16)], need).unwrap();
        let two = max_context_over(131_072, &[card(0, 16), card(1, 16)], need).unwrap();
        assert!(two > one, "{two} vs {one}");
        assert!(max_context_over(131_072, &[], need).is_none());
    }
}
