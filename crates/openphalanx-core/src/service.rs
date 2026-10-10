//! The server without a user interface: start, stop and watch the backend.
//!
//! `oppxs` runs this; the desktop app has the same logic in its own command
//! layer and is to move onto this module. One `Service` per machine.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::admin::{self, AdminClient};
use crate::cluster::Cluster;
use crate::server::{self, CheckStatus, StartProgress};
use crate::settings::Settings;
use crate::{docker, net};

/// How often the monitor looks at the backend. Each look also calls the
/// gateway's admin API, which keeps its watchdog from stopping the backend.
const TICK: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, Default)]
enum Phase {
    #[default]
    Idle,
    Starting(String),
    Stopping,
    Failed(String),
}

#[derive(Default)]
struct Inner {
    phase: Phase,
    /// A backend we started or adopted: its exit is then a crash.
    expect_running: bool,
    /// Bumped by every stop, so a look taken during it isn't read as a crash.
    stop_epoch: u64,
}

/// What `oppxs status` shows about the backend.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ServerStatus {
    /// "stopped", "starting", "running", "stopping", "error" or "external".
    pub state: String,
    pub detail: Option<String>,
    /// The model being served, or the selected one while stopped.
    pub model: Option<String>,
    pub context_len: Option<u32>,
    /// `ip:port` clients pair with.
    pub endpoint: Option<String>,
    pub fingerprint: Option<String>,
    pub pairing: Option<admin::Pairing>,
    pub devices: u32,
}

pub struct Service {
    cluster: Option<Arc<Cluster>>,
    inner: Mutex<Inner>,
}

impl Service {
    pub fn new(cluster: Option<Arc<Cluster>>) -> Arc<Self> {
        Arc::new(Self { cluster, inner: Mutex::new(Inner::default()) })
    }

    /// Starts the selected model. Returns once it is launching; follow
    /// `status` for progress. Refused when a pre-flight check fails.
    pub async fn start(self: &Arc<Self>) -> Result<()> {
        {
            let mut inner = self.inner.lock().unwrap();
            if matches!(inner.phase, Phase::Starting(_) | Phase::Stopping) {
                bail!("The server is already starting or stopping.");
            }
            inner.phase = Phase::Starting("Checking…".into());
        }
        let result = self.begin_start().await;
        if result.is_err() {
            self.inner.lock().unwrap().phase = Phase::Idle;
            if let Some(c) = &self.cluster {
                c.set_serving(false);
            }
        }
        result
    }

    async fn begin_start(self: &Arc<Self>) -> Result<()> {
        // One controller per cluster: a member takes the host role over first.
        if let Some(c) = &self.cluster {
            c.claim_start().await?;
        }
        let settings = Settings::load();
        let pf = server::preflight(&settings).await;
        if pf.running {
            bail!("The server is already running.");
        }
        if let Some(check) = pf.checks.iter().find(|c| c.status == CheckStatus::Fail) {
            // Splitting a model across a cluster is planned by the desktop app.
            let hint = if check.id == "vram" && self.cluster.as_ref().is_some_and(|c| !c.members().is_empty()) {
                " To split it across this cluster, start it from the desktop app on the host."
            } else {
                ""
            };
            bail!("{}{hint}", check.detail);
        }
        let me = self.clone();
        tokio::spawn(async move {
            let progress = me.clone();
            let result = server::start(&settings, None, move |p| {
                let detail = match p {
                    StartProgress::Checking => "Checking GPU memory…".to_string(),
                    StartProgress::PullingImage { .. } => "Downloading the backend image…".to_string(),
                    StartProgress::BuildingImage { .. } => {
                        "Building the backend image (the first start downloads the SGLang base, about 16 GB)…".to_string()
                    }
                    StartProgress::Launching => "Launching the backend…".to_string(),
                };
                progress.inner.lock().unwrap().phase = Phase::Starting(detail);
            })
            .await;
            let mut inner = me.inner.lock().unwrap();
            match result {
                Ok(()) => {
                    inner.phase = Phase::Idle;
                    inner.expect_running = true;
                }
                Err(e) => {
                    inner.phase = Phase::Failed(format!("{e:#}"));
                    if let Some(c) = &me.cluster {
                        c.set_serving(false);
                    }
                }
            }
        });
        Ok(())
    }

    /// Stops the backend this service manages (and SearXNG).
    pub async fn stop(&self) -> Result<()> {
        {
            let mut inner = self.inner.lock().unwrap();
            if matches!(inner.phase, Phase::Starting(_)) {
                bail!("Still preparing the backend. Stop it once it has launched.");
            }
            inner.phase = Phase::Stopping;
            inner.expect_running = false;
            inner.stop_epoch += 1;
        }
        let result = docker::stop().await;
        let mut inner = self.inner.lock().unwrap();
        inner.phase = Phase::Idle;
        inner.stop_epoch += 1;
        result
    }

    /// The service is ending: a backend it manages must not outlive it.
    pub async fn shutdown(&self) {
        if let Some(c) = &self.cluster {
            c.goodbye().await;
        }
        let container = docker::inspect().await.ok().flatten();
        if container.is_some_and(|c| c.managed) {
            let _ = docker::stop().await;
        }
    }

    /// Looks at the backend: notices a crash, adopts a backend that is
    /// already running, tells the cluster whether this server is serving.
    pub async fn status(&self) -> ServerStatus {
        let settings = Settings::load();
        let epoch = self.inner.lock().unwrap().stop_epoch;
        let container = docker::inspect().await.ok().flatten();
        let running = container.as_ref().is_some_and(|c| c.state.running);
        let managed = container.as_ref().is_some_and(|c| c.managed);
        let admin_status = match Self::admin_of(container.as_ref()) {
            Some(client) => client.status().await.ok(),
            None => None,
        };

        let crashed = {
            let inner = self.inner.lock().unwrap();
            matches!(inner.phase, Phase::Idle) && inner.expect_running && !running && inner.stop_epoch == epoch
        };
        let crash = if crashed {
            let logs = docker::logs_tail(120).await.unwrap_or_default();
            let code = container.as_ref().map(|c| c.state.exit_code).unwrap_or_default();
            Some(docker::diagnose_crash(&logs).unwrap_or_else(|| {
                format!("The backend stopped unexpectedly (exit code {code}). See `oppxs logs`.")
            }))
        } else {
            None
        };

        let mut inner = self.inner.lock().unwrap();
        if let Some(message) = crash {
            inner.expect_running = false;
            inner.phase = Phase::Failed(message);
        } else if running && managed && matches!(inner.phase, Phase::Idle) && inner.stop_epoch == epoch {
            inner.expect_running = true;
        }
        let serving = matches!(inner.phase, Phase::Starting(_)) || (running && managed && matches!(inner.phase, Phase::Idle));
        if let Some(c) = self.cluster.as_ref().filter(|c| !c.is_member()) {
            c.set_serving(serving);
        }

        let (state, detail) = match &inner.phase {
            Phase::Starting(detail) => ("starting", Some(detail.clone())),
            Phase::Stopping => ("stopping", None),
            Phase::Failed(message) => ("error", Some(message.clone())),
            Phase::Idle if running && !managed => {
                ("external", Some("A backend container was started by hand. Stop it with `oppxs stop`.".into()))
            }
            Phase::Idle if running => match &admin_status {
                Some(a) if a.sglang == "ready" => ("running", None),
                Some(_) => ("starting", Some("Loading the model into VRAM…".into())),
                None => ("starting", Some("Starting the gateway…".into())),
            },
            Phase::Idle => ("stopped", None),
        };
        let key = container.as_ref().filter(|_| running).and_then(|c| c.model_key.clone()).or(settings.selected_model.clone());
        let model = key.and_then(|k| server::resolve(&settings, &k)).map(|m| match &m.quant {
            Some(q) => format!("{} · {q}", m.label),
            None => m.label,
        });
        ServerStatus {
            state: state.into(),
            detail,
            model,
            context_len: container.as_ref().filter(|_| running).and_then(|c| c.context_len).or(Some(settings.context_len)),
            endpoint: net::lan_ip().map(|ip| format!("{ip}:{}", settings.agent_port)),
            fingerprint: admin_status.as_ref().map(|a| a.tls_fingerprint.clone()),
            pairing: admin_status.as_ref().map(|a| a.pairing.clone()),
            devices: admin_status.as_ref().map_or(0, |a| a.devices),
        }
    }

    /// Watches the backend until the task is dropped.
    pub async fn monitor(self: Arc<Self>) {
        loop {
            self.status().await;
            tokio::time::sleep(TICK).await;
        }
    }

    fn admin_of(container: Option<&docker::ContainerInfo>) -> Option<AdminClient> {
        container.filter(|c| c.state.running && c.managed).and_then(|c| c.admin_token.clone()).map(AdminClient::new)
    }

    async fn admin(&self) -> Result<AdminClient> {
        let container = docker::inspect().await.ok().flatten();
        Self::admin_of(container.as_ref()).context("The server isn't running; start it with `oppxs start`.")
    }

    /// A new single-use pairing code. Clients pair with a cluster's host only.
    pub async fn new_pairing(&self) -> Result<admin::Pairing> {
        if let Some(c) = self.cluster.as_ref().filter(|c| c.is_member()) {
            if let crate::cluster::Role::Member { host, .. } = c.role() {
                bail!("This server is a member of {}'s cluster. Pair clients with the host ({}).", host.name, host.url);
            }
        }
        self.admin().await?.new_pairing().await
    }

    /// Paired clients with their usage.
    pub async fn devices(&self) -> Result<Vec<admin::Device>> {
        self.admin().await?.devices().await
    }

    /// Revokes a paired client; its token stops working at once.
    pub async fn revoke(&self, id: &str) -> Result<()> {
        self.admin().await?.revoke(id).await.map(|_| ())
    }
}
