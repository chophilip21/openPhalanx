//! Clusters of Openphalanx servers on one network.
//!
//! Every server (the app, or the headless `openphalanx-server`) runs this
//! service. It announces itself on the LAN, lists the other servers it can
//! actually reach, and can form a cluster with them:
//!
//! * **Discovery:** a UDP broadcast beacon on `DISCOVERY_PORT` every few
//!   seconds. A beacon only makes a *candidate*; a server is listed once a TLS
//!   health check, pinned to the certificate it announced, succeeds. Servers
//!   that stop, or that this machine can't reach, drop off within seconds.
//! * **Roles:** standalone, host, or member. Clients pair only with the host;
//!   members hide pairing.
//! * **Joining needs consent on both sides:** the host *invites* a candidate,
//!   and the candidate's owner *approves* it after checking the host's
//!   fingerprint. A machine remembers hosts it approved, and auto-approves
//!   them later. Anyone on the LAN can send an invitation, but it only ever
//!   becomes a pending request.
//! * **Choosing another host:** a "host request" asks a candidate to become
//!   host and invite this machine and its members. Members learn the new
//!   host's fingerprint from the old host, so they move over without asking.
//! * **Members report** every `HEARTBEAT_SECS`: hardware, GPU load, and what
//!   they serve, so the host's dashboard can chart every machine.
//!
//! Trust is the same as client pairing: self-signed certificates pinned by
//! SHA-256, single-use invitation secrets, 256-bit member tokens stored only
//! as hashes, and removal that takes effect immediately.

use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use axum::extract::{ConnectInfo, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use rand::Rng;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::admin::{AdminClient, GatewayMetrics, InferenceMetrics};
use crate::gpu::{self, GpuInfo};

/// TLS API between servers (open it on the cluster's network).
pub const CLUSTER_PORT: u16 = 9092;
/// UDP broadcast beacons (same network only).
pub const DISCOVERY_PORT: u16 = 9093;
/// How often a member reports to its host.
pub const HEARTBEAT_SECS: u64 = 5;
/// A member that hasn't reported for this long is offline.
pub const OFFLINE_AFTER_SECS: u64 = 20;

const BEACON_SECS: u64 = 3;
/// A server drops off the candidate list this long after its last beacon.
const CANDIDATE_TTL_SECS: u64 = 10;
/// Reachability is re-checked this often per candidate.
const RECHECK_SECS: u64 = 8;
const INVITE_TTL_SECS: u64 = 600;
const MAX_PENDING: usize = 8;
const MAGIC: &str = "openphalanx-cluster-v1";
const MAX_NAME: usize = 64;

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn sha256_hex(s: &str) -> String {
    hex::encode(Sha256::digest(s.as_bytes()))
}

fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    rand::rng().fill(&mut buf[..]);
    hex::encode(buf)
}

fn clean_name(s: &str) -> String {
    s.trim().chars().filter(|c| !c.is_control()).take(MAX_NAME).collect()
}

// --------------------------------------------------------------------------
// What a server reports
// --------------------------------------------------------------------------

/// A server's hardware.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Inventory {
    pub hostname: String,
    pub os: String,
    pub arch: String,
    pub cpus: u32,
    pub memory_bytes: u64,
    #[serde(default)]
    pub gpus: Vec<GpuInfo>,
    pub docker_version: Option<String>,
    pub docker_error: Option<String>,
    pub nvidia_runtime: bool,
    pub version: String,
}

/// What a server is serving right now (from its backend's admin API).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Serving {
    pub model: String,
    pub sglang: String,
    pub gateway: GatewayMetrics,
    pub inference: InferenceMetrics,
}

/// The model the host serves, so members can have it on disk too.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ModelSpec {
    pub key: String,
    pub label: String,
    /// Hugging Face repo and the exact commit (members download this one).
    pub repo: String,
    pub revision: String,
    pub weight_bytes: u64,
}

/// A member's copy of the host's model.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct ModelSync {
    pub label: String,
    pub repo: String,
    pub revision: String,
    /// "checking", "downloading", "ready" or "error".
    pub state: String,
    pub done_bytes: u64,
    pub total_bytes: u64,
    pub bytes_per_sec: f64,
    pub error: Option<String>,
}

/// As host: what a member should run for a model split across servers.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkerOrder {
    /// New for every start; a member restarts its worker when it changes.
    pub run_id: String,
    pub model: ModelSpec,
    pub image: String,
    pub context_len: u32,
    /// SGLang `--dtype` override; every rank must use rank 0's.
    #[serde(default)]
    pub dtype: Option<String>,
    /// Its rank; the member fills in its own network interface.
    pub rank: crate::docker::SplitRank,
    pub stage: crate::split::Stage,
}

/// A member's worker for the host's current run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkerStatus {
    pub run_id: String,
    /// "starting", "running" or "failed".
    pub state: String,
    pub message: Option<String>,
}

/// A member's report to its host.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Report {
    pub inventory: Inventory,
    pub serving: Option<Serving>,
    /// Its copy of the host's model, once the host has asked for one.
    #[serde(default)]
    pub model_sync: Option<ModelSync>,
    /// New log lines since the last report (its backend, and cluster events).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub logs: Vec<String>,
    /// Its share of a split model, if the host asked for one.
    #[serde(default)]
    pub worker: Option<WorkerStatus>,
}

/// Log lines a host keeps per member, for the Logs page.
const MEMBER_LOG_LINES: usize = 3000;
/// Log lines a member queues while its host is unreachable.
const OUTBOX_LOG_LINES: usize = 1000;
/// Most log lines sent in one report.
const LOG_LINES_PER_REPORT: usize = 400;

pub async fn collect_inventory() -> Inventory {
    let docker = crate::docker::daemon_version().await.map_err(|e| format!("{e:#}"));
    let nvidia_runtime = docker.is_ok() && crate::docker::has_nvidia_runtime().await.unwrap_or(false);
    let (docker_version, docker_error) = match docker {
        Ok(v) => (Some(v), None),
        Err(e) => (None, Some(e)),
    };
    Inventory {
        hostname: gethostname::gethostname().to_string_lossy().to_string(),
        os: std::env::consts::OS.to_string(),
        arch: std::env::consts::ARCH.to_string(),
        cpus: std::thread::available_parallelism().map(|n| n.get() as u32).unwrap_or(1),
        memory_bytes: memory_bytes().unwrap_or(0),
        gpus: gpu::query().await.unwrap_or_default(),
        docker_version,
        docker_error,
        nvidia_runtime,
        version: env!("CARGO_PKG_VERSION").to_string(),
    }
}

/// The local backend's stats, when it is running and managed by Openphalanx.
pub async fn collect_serving() -> Option<Serving> {
    let c = crate::docker::inspect().await.ok().flatten()?;
    if !(c.state.running && c.managed) {
        return None;
    }
    let status = AdminClient::new(c.admin_token?).status().await.ok()?;
    Some(Serving { model: status.model, sglang: status.sglang, gateway: status.gateway, inference: status.inference })
}

fn memory_bytes() -> Option<u64> {
    let meminfo = std::fs::read_to_string("/proc/meminfo").ok()?;
    let kb: u64 = meminfo.lines().find(|l| l.starts_with("MemTotal:"))?.split_whitespace().nth(1)?.parse().ok()?;
    Some(kb * 1024)
}

// --------------------------------------------------------------------------
// Persistent state
// --------------------------------------------------------------------------

/// Another server, as one server knows it.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Peer {
    pub id: String,
    pub name: String,
    pub url: String,
    pub fingerprint: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
#[serde(tag = "role", rename_all = "snake_case")]
pub enum Role {
    #[default]
    Standalone,
    Host,
    Member {
        host: Peer,
        #[serde(skip_serializing_if = "String::is_empty", default)]
        token: String,
    },
}

/// How a cluster uses its GPUs (chosen by the host).
#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Strategy {
    /// One model, its layers spread across the servers (pipeline parallel):
    /// bigger models than any one machine holds, at about single-GPU speed.
    /// Needs every server up and a fast wired network.
    #[default]
    Split,
    /// Every server runs its own full copy; a router spreads requests:
    /// more concurrent users and resilience, but no bigger models.
    Replicas,
}

/// `state.json`: who this server is and what it belongs to.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Persisted {
    id: String,
    /// Display name (default: the hostname).
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    role: Role,
    /// Host fingerprints this server approved; their invitations are accepted.
    #[serde(default)]
    trusted_hosts: Vec<String>,
    /// As host (or standalone): how this cluster uses its GPUs.
    #[serde(default)]
    strategy: Strategy,
    /// As host: the model members should have on disk (set when the host starts serving).
    #[serde(default)]
    desired_model: Option<ModelSpec>,
}

/// A member, as its host stores it (`members.json`).
#[derive(Debug, Clone, Serialize, Deserialize)]
struct MemberRecord {
    peer: Peer,
    token_sha256: String,
    joined_at: u64,
}

// --------------------------------------------------------------------------
// In-memory state
// --------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
struct Live {
    last_seen: Option<u64>,
    report: Option<Report>,
    /// Closed its app or stopped its server; offline until it reports again.
    gone: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Beacon {
    magic: String,
    id: String,
    name: String,
    port: u16,
    fingerprint: String,
    role: String,
    #[serde(default)]
    host_id: Option<String>,
    version: String,
    /// "Who's there?": receivers answer right away (sent when a server starts).
    #[serde(default)]
    query: bool,
}

#[derive(Debug, Clone)]
struct Candidate {
    beacon: Beacon,
    address: IpAddr,
    last_seen: u64,
    /// Result of the last pinned health check, and when it ran.
    reachable: Option<bool>,
    checked_at: u64,
}

/// An invitation this host sent.
#[derive(Debug, Clone)]
struct OutgoingInvite {
    invite_id: String,
    secret_sha256: String,
    for_id: String,
    expires_at: u64,
}

/// An invitation waiting for approval here.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PendingInvite {
    pub host: Peer,
    pub invite_id: String,
    #[serde(skip_serializing, default)]
    pub secret: String,
    pub received_at: u64,
}

/// A request for this server to become host, waiting for approval here.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HostRequest {
    pub from: Peer,
    pub members: Vec<Peer>,
    pub received_at: u64,
}

/// How a member's link to its host is doing.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct HostLink {
    pub connected: bool,
    pub last_ok: Option<u64>,
    pub error: Option<String>,
}

// --------------------------------------------------------------------------
// Views for the app
// --------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemberView {
    pub id: String,
    pub name: String,
    pub url: String,
    pub fingerprint: String,
    pub joined_at: u64,
    pub last_seen: Option<u64>,
    pub online: bool,
    pub report: Option<Report>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandidateView {
    pub id: String,
    pub name: String,
    pub url: String,
    pub fingerprint: String,
    pub role: String,
    /// Already in a cluster hosted elsewhere.
    pub busy: bool,
    pub version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClusterView {
    pub id: String,
    pub name: String,
    pub url: Option<String>,
    pub fingerprint: String,
    pub port: u16,
    pub role: Role,
    /// The cluster's strategy: this machine's setting, or (as member) the host's.
    pub strategy: Strategy,
    pub members: Vec<MemberView>,
    pub candidates: Vec<CandidateView>,
    pub invites: Vec<PendingInvite>,
    pub host_requests: Vec<HostRequest>,
    pub host_link: Option<HostLink>,
    /// Discovery isn't working (e.g. the UDP port is taken).
    pub discovery_error: Option<String>,
    /// As host: the model members are asked to have.
    pub desired_model: Option<ModelSpec>,
    /// As member: this machine's copy of the host's model.
    pub model_sync: Option<ModelSync>,
    /// As member: the host is running the cluster's server, so this machine
    /// can't start one (it may only leave the cluster).
    pub locked_by_host: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct InviteMsg {
    host: Peer,
    invite_id: String,
    secret: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct HostRequestMsg {
    from: Peer,
    members: Vec<Peer>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct JoinMsg {
    invite_id: String,
    secret: String,
    peer: Peer,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct JoinReply {
    token: String,
}

/// The host's answer to a report; `expect_host` announces a hand-over.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct ReportReply {
    #[serde(default)]
    expect_host: Option<String>,
    /// The host's strategy, so members show it.
    #[serde(default)]
    strategy: Option<Strategy>,
    /// The model members should have on disk.
    #[serde(default)]
    ensure_model: Option<ModelSpec>,
    /// The host's server is running or starting: members can't start theirs.
    #[serde(default)]
    host_serving: bool,
    /// Run this share of a split model (`None`: run none).
    #[serde(default)]
    worker: Option<WorkerOrder>,
}

// --------------------------------------------------------------------------
// The service
// --------------------------------------------------------------------------

pub struct Cluster {
    dir: PathBuf,
    fingerprint: String,
    state: Mutex<Persisted>,
    members: Mutex<Vec<MemberRecord>>,
    live: Mutex<HashMap<String, Live>>,
    candidates: Mutex<HashMap<String, Candidate>>,
    outgoing: Mutex<Vec<OutgoingInvite>>,
    pending: Mutex<Vec<PendingInvite>>,
    host_requests: Mutex<Vec<HostRequest>>,
    /// Accept an invitation from this fingerprint without asking, until a time
    /// (after asking a server to become host, or when the old host says so).
    expect_host: Mutex<Option<(String, u64)>>,
    /// The fingerprint a hand-over goes to; told to members in report replies.
    handover: Mutex<Option<(String, u64)>>,
    host_link: Mutex<HostLink>,
    discovery_error: Mutex<Option<String>>,
    /// As member: the host's strategy, from its report replies.
    host_strategy: Mutex<Option<Strategy>>,
    /// As member: its copy of the host's model, and the download's cancel flag.
    sync: Mutex<Option<ModelSync>>,
    sync_cancel: Mutex<Option<crate::download::Cancel>>,
    /// As member: log lines waiting for the next report.
    outbox: Mutex<std::collections::VecDeque<String>>,
    /// As member: where the backend log was read up to (unix seconds).
    backend_log_since: Mutex<u64>,
    /// As host: recent log lines per member.
    member_logs: Mutex<HashMap<String, std::collections::VecDeque<String>>>,
    /// Wakes the reachability checker when a new server shows up.
    check_now: tokio::sync::Notify,
    /// Who may start the cluster's server (see `claim_start`).
    control: Mutex<Control>,
    /// As member: whether the host is serving (from its report replies).
    host_serving: Mutex<bool>,
    /// As host: each member's share of the split model being served.
    workers: Mutex<HashMap<String, WorkerOrder>>,
    /// As member: the order it runs, and how that is going.
    worker: Mutex<Option<WorkerOrder>>,
    worker_status: Mutex<Option<WorkerStatus>>,
}

/// One controller per cluster: the host. Checked and set under one lock, so
/// two starts (or a start and a take-over) can't both win.
#[derive(Debug, Default)]
struct Control {
    /// This machine's server is running or starting.
    serving: bool,
    /// As host: control is being handed to (fingerprint, name) until a time.
    handing_over: Option<(String, String, u64)>,
}

impl Cluster {
    /// Opens (or creates) this server's cluster state in `dir`.
    pub fn open(dir: &Path) -> Result<Arc<Self>> {
        std::fs::create_dir_all(dir).with_context(|| format!("cannot create {}", dir.display()))?;
        let der = ensure_certificate(dir)?;
        let state: Persisted = match std::fs::read(dir.join("state.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes).context("state.json is not valid")?,
            Err(_) => Persisted {
                id: random_hex(8),
                name: None,
                role: Role::Standalone,
                trusted_hosts: Vec::new(),
                strategy: Strategy::default(),
                desired_model: None,
            },
        };
        let members = match std::fs::read(dir.join("members.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes).context("members.json is not valid")?,
            Err(_) => Vec::new(),
        };
        let c = Arc::new(Cluster {
            dir: dir.to_path_buf(),
            fingerprint: pinned_tls::fingerprint(&der),
            state: Mutex::new(state),
            members: Mutex::new(members),
            live: Mutex::new(HashMap::new()),
            candidates: Mutex::new(HashMap::new()),
            outgoing: Mutex::new(Vec::new()),
            pending: Mutex::new(Vec::new()),
            host_requests: Mutex::new(Vec::new()),
            expect_host: Mutex::new(None),
            handover: Mutex::new(None),
            host_link: Mutex::new(HostLink::default()),
            discovery_error: Mutex::new(None),
            host_strategy: Mutex::new(None),
            sync: Mutex::new(None),
            sync_cancel: Mutex::new(None),
            outbox: Mutex::new(std::collections::VecDeque::new()),
            backend_log_since: Mutex::new(now()),
            member_logs: Mutex::new(HashMap::new()),
            check_now: tokio::sync::Notify::new(),
            control: Mutex::new(Control::default()),
            host_serving: Mutex::new(false),
            workers: Mutex::new(HashMap::new()),
            worker: Mutex::new(None),
            worker_status: Mutex::new(None),
        });
        c.save_state()?;
        Ok(c)
    }

    pub fn id(&self) -> String {
        self.state.lock().unwrap().id.clone()
    }

    /// The display name: set by the user, or the hostname.
    pub fn name(&self) -> String {
        self.state.lock().unwrap().name.clone().unwrap_or_else(|| clean_name(&gethostname::gethostname().to_string_lossy()))
    }

    /// Renames this server (shown to other servers and in the app).
    pub fn set_name(&self, name: &str) -> Result<()> {
        let name = clean_name(name);
        if name.is_empty() {
            bail!("a name can't be empty");
        }
        self.state.lock().unwrap().name = Some(name);
        self.save_state()
    }

    pub fn fingerprint(&self) -> &str {
        &self.fingerprint
    }

    pub fn role(&self) -> Role {
        self.state.lock().unwrap().role.clone()
    }

    /// The cluster's strategy: this machine's own setting, or the host's when
    /// this machine is a member.
    pub fn strategy(&self) -> Strategy {
        if self.is_member() {
            if let Some(s) = *self.host_strategy.lock().unwrap() {
                return s;
            }
        }
        self.state.lock().unwrap().strategy
    }

    /// Sets the strategy (the host decides; members follow it).
    pub fn set_strategy(&self, strategy: Strategy) -> Result<()> {
        if self.is_member() {
            bail!("the cluster's host decides its strategy");
        }
        // How the model is laid out across servers can't change under a
        // running server; it takes effect on the next start.
        if self.control.lock().unwrap().serving && self.strategy() != strategy {
            bail!("Stop the server before changing the strategy; it applies when the server starts.");
        }
        self.state.lock().unwrap().strategy = strategy;
        self.save_state()
    }

    /// As host: the model members should have on disk. Set when the host
    /// starts serving; members that lack it download it (verified, from the
    /// same pinned commit) and report progress. `None` stops asking.
    pub fn set_desired_model(&self, model: Option<ModelSpec>) -> Result<()> {
        self.state.lock().unwrap().desired_model = model;
        self.save_state()
    }

    /// Claims the right to start the cluster's server; call before starting.
    /// Standalone or host: granted unless control is being handed over. A
    /// member first takes the host role over (the host refuses while its own
    /// server runs), so only one machine in a cluster ever serves.
    pub async fn claim_start(&self) -> Result<()> {
        match self.role() {
            Role::Standalone | Role::Host => {
                let mut ctl = self.control.lock().unwrap();
                if let Some((_, name, until)) = &ctl.handing_over {
                    if *until > now() {
                        bail!("Control is being handed to {name}; it starts the cluster's server.");
                    }
                }
                ctl.serving = true;
                Ok(())
            }
            Role::Member { host, token } => {
                if *self.host_serving.lock().unwrap() {
                    bail!(
                        "{} is running the cluster's server, so this machine can't start one. \
                         Leave the cluster to run on your own.",
                        host.name
                    );
                }
                self.take_over(&host, &token).await?;
                self.control.lock().unwrap().serving = true;
                Ok(())
            }
        }
    }

    /// The app reports whether its server is running or starting (every
    /// monitor tick), and releases control when it stops.
    pub fn set_serving(&self, serving: bool) {
        self.control.lock().unwrap().serving = serving;
    }

    /// As member: becomes the host, with the old host's consent; everyone
    /// else (the old host included) moves over without asking.
    async fn take_over(&self, host: &Peer, token: &str) -> Result<()> {
        let me = self.me().context("this machine has no network address")?;
        let client = pinned_tls::pinned_client(&host.fingerprint)?;
        let resp = client
            .post(format!("{}/cluster/v1/take-over", host.url))
            .bearer_auth(token)
            .json(&me)
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .with_context(|| format!("can't reach the host, {}", host.name))?;
        if !resp.status().is_success() {
            bail!("{} refused: {}", host.name, error_text(resp).await);
        }
        let peers: Vec<Peer> = resp.json().await?;
        self.set_role(Role::Host)?;
        *self.host_link.lock().unwrap() = HostLink::default();
        *self.host_serving.lock().unwrap() = false;
        self.log(format!("took over as host from {}", host.name));
        for peer in peers.iter().filter(|p| p.id != me.id) {
            let (invite_id, secret) = (random_hex(8), random_hex(32));
            self.outgoing.lock().unwrap().push(OutgoingInvite {
                invite_id: invite_id.clone(),
                secret_sha256: sha256_hex(&secret),
                for_id: peer.id.clone(),
                expires_at: now() + INVITE_TTL_SECS,
            });
            let _ = send_invite(peer, InviteMsg { host: me.clone(), invite_id, secret }).await;
        }
        Ok(())
    }

    /// As host: agrees to hand control to a member, unless this machine's
    /// server is running or control is already going elsewhere.
    fn grant_take_over(&self, requester_id: &str, requester: &Peer) -> Result<Vec<Peer>, (StatusCode, String)> {
        {
            let mut ctl = self.control.lock().unwrap();
            if ctl.serving {
                return Err((StatusCode::CONFLICT, format!("{} is running the cluster's server", self.name())));
            }
            if let Some((fp, name, until)) = &ctl.handing_over {
                if *until > now() && fp != &requester.fingerprint {
                    return Err((StatusCode::CONFLICT, format!("control is already going to {name}")));
                }
            }
            ctl.handing_over = Some((requester.fingerprint.clone(), requester.name.clone(), now() + 60));
        }
        let until = now() + INVITE_TTL_SECS;
        // This machine and the other members accept the new host's invitation.
        *self.expect_host.lock().unwrap() = Some((requester.fingerprint.clone(), until));
        *self.handover.lock().unwrap() = Some((requester.fingerprint.clone(), until));
        let mut peers: Vec<Peer> = self.me().into_iter().collect();
        peers.extend(self.members.lock().unwrap().iter().filter(|m| m.peer.id != requester_id).map(|m| m.peer.clone()));
        Ok(peers)
    }

    /// As member: tells the host this machine is going away (app closed), so
    /// it shows offline at once instead of after a timeout.
    pub async fn goodbye(&self) {
        if let Role::Member { host, token } = self.role() {
            if let Ok(client) = pinned_tls::pinned_client(&host.fingerprint) {
                let _ = client
                    .post(format!("{}/cluster/v1/bye", host.url))
                    .bearer_auth(token)
                    .timeout(Duration::from_secs(2))
                    .send()
                    .await;
            }
        }
    }

    /// As host: members that are gone (said goodbye, or no report for
    /// `after_secs`; a new member counts from when it joined).
    pub fn dropped_members(&self, after_secs: u64) -> Vec<String> {
        let members = self.members.lock().unwrap();
        let live = self.live.lock().unwrap();
        let t = now();
        members
            .iter()
            .filter(|m| {
                let l = live.get(&m.peer.id);
                let seen = l.and_then(|l| l.last_seen).unwrap_or(m.joined_at);
                l.is_some_and(|l| l.gone) || t.saturating_sub(seen) > after_secs
            })
            .map(|m| m.peer.name.clone())
            .collect()
    }

    /// As host: a member's recent log lines, oldest first.
    pub fn member_logs(&self, id: &str) -> Vec<String> {
        self.member_logs.lock().unwrap().get(id).map(|l| l.iter().cloned().collect()).unwrap_or_default()
    }

    /// As member: a line for the host's Logs page.
    fn log(&self, line: impl Into<String>) {
        let mut out = self.outbox.lock().unwrap();
        out.push_back(format!("[cluster] {}", line.into()));
        while out.len() > OUTBOX_LOG_LINES {
            out.pop_front();
        }
    }

    /// Members hide client pairing: clients pair with the host only.
    pub fn is_member(&self) -> bool {
        matches!(self.role(), Role::Member { .. })
    }

    /// This server as others reach it (LAN address).
    pub fn me(&self) -> Option<Peer> {
        let ip = crate::net::lan_ip()?;
        Some(Peer { id: self.id(), name: self.name(), url: url_for(ip, CLUSTER_PORT), fingerprint: self.fingerprint.clone() })
    }

    fn save_state(&self) -> Result<()> {
        let state = self.state.lock().unwrap().clone();
        write_private(&self.dir.join("state.json"), &serde_json::to_vec_pretty(&state)?)
    }

    fn save_members(&self) -> Result<()> {
        let members = self.members.lock().unwrap().clone();
        write_private(&self.dir.join("members.json"), &serde_json::to_vec_pretty(&members)?)
    }

    fn set_role(&self, role: Role) -> Result<()> {
        self.state.lock().unwrap().role = role;
        self.save_state()
    }

    // ---- host side --------------------------------------------------------

    /// Invites a discovered server into this machine's cluster (this machine
    /// becomes the host if it was standalone). The candidate must approve.
    pub async fn invite(&self, candidate_id: &str) -> Result<()> {
        if self.is_member() {
            bail!("this machine is a member of another cluster; leave it first");
        }
        let target = self.reachable_candidate(candidate_id)?;
        let me = self.me().context("this machine has no network address")?;
        let (invite_id, secret) = (random_hex(8), random_hex(32));
        self.outgoing.lock().unwrap().push(OutgoingInvite {
            invite_id: invite_id.clone(),
            secret_sha256: sha256_hex(&secret),
            for_id: target.id.clone(),
            expires_at: now() + INVITE_TTL_SECS,
        });
        send_invite(&target, InviteMsg { host: me, invite_id, secret }).await?;
        if self.role() == Role::Standalone {
            self.set_role(Role::Host)?;
        }
        Ok(())
    }

    /// Asks a discovered server to become the host of this machine and its
    /// current members. It must approve; this machine and its members then
    /// move over without asking (they know the new host's fingerprint).
    pub async fn request_host(&self, candidate_id: &str) -> Result<()> {
        if self.is_member() {
            bail!("this machine is a member of another cluster; ask its host instead");
        }
        let target = self.reachable_candidate(candidate_id)?;
        let me = self.me().context("this machine has no network address")?;
        let mut members = vec![me.clone()];
        members.extend(self.members.lock().unwrap().iter().map(|m| m.peer.clone()));
        let client = pinned_tls::pinned_client(&target.fingerprint)?;
        let resp = client
            .post(format!("{}/cluster/v1/host-request", target.url))
            .json(&HostRequestMsg { from: me, members })
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .with_context(|| format!("cannot reach {}", target.name))?;
        if !resp.status().is_success() {
            bail!("{} declined: {}", target.name, error_text(resp).await);
        }
        let until = now() + INVITE_TTL_SECS;
        *self.expect_host.lock().unwrap() = Some((target.fingerprint.clone(), until));
        *self.handover.lock().unwrap() = Some((target.fingerprint, until));
        Ok(())
    }

    /// Removes a member; its token stops working at once.
    pub fn remove_member(&self, id: &str) -> Result<bool> {
        let removed = {
            let mut members = self.members.lock().unwrap();
            let before = members.len();
            members.retain(|m| m.peer.id != id);
            members.len() != before
        };
        if !removed {
            return Ok(false);
        }
        self.save_members()?;
        self.live.lock().unwrap().remove(id);
        if self.members.lock().unwrap().is_empty() && self.role() == Role::Host {
            self.set_role(Role::Standalone)?;
        }
        Ok(true)
    }

    /// Ends this cluster: every member is removed (each notices on its next report).
    pub fn dissolve(&self) -> Result<()> {
        self.members.lock().unwrap().clear();
        self.save_members()?;
        self.live.lock().unwrap().clear();
        self.outgoing.lock().unwrap().clear();
        if self.role() == Role::Host {
            self.set_role(Role::Standalone)?;
        }
        Ok(())
    }

    fn redeem_invite(&self, invite_id: &str, secret: &str, peer_id: &str) -> bool {
        let mut out = self.outgoing.lock().unwrap();
        let t = now();
        out.retain(|i| i.expires_at > t);
        let hash = sha256_hex(secret);
        let Some(pos) = out.iter().position(|i| i.invite_id == invite_id) else { return false };
        let ok = out[pos].secret_sha256 == hash && out[pos].for_id == peer_id;
        if ok {
            out.remove(pos);
        }
        ok
    }

    fn add_member(&self, peer: Peer) -> Result<String> {
        let token = format!("phx-member-{}", random_hex(32));
        {
            let mut members = self.members.lock().unwrap();
            members.retain(|m| m.peer.id != peer.id);
            members.push(MemberRecord { peer, token_sha256: sha256_hex(&token), joined_at: now() });
        }
        self.save_members()?;
        if self.role() != Role::Host {
            self.set_role(Role::Host)?;
        }
        Ok(token)
    }

    fn authenticate(&self, headers: &HeaderMap) -> Option<String> {
        let token = headers.get("authorization")?.to_str().ok()?.strip_prefix("Bearer ")?.trim();
        let hash = sha256_hex(token);
        self.members.lock().unwrap().iter().find(|m| m.token_sha256 == hash).map(|m| m.peer.id.clone())
    }

    fn record_report(&self, id: &str, mut report: Report) {
        let logs = std::mem::take(&mut report.logs);
        if !logs.is_empty() {
            let mut all = self.member_logs.lock().unwrap();
            let buf = all.entry(id.to_string()).or_default();
            buf.extend(logs);
            while buf.len() > MEMBER_LOG_LINES {
                buf.pop_front();
            }
        }
        let mut live = self.live.lock().unwrap();
        let e = live.entry(id.to_string()).or_default();
        e.last_seen = Some(now());
        e.report = Some(report);
        e.gone = false;
    }

    // ---- candidate side ---------------------------------------------------

    fn receive_invite(&self, msg: InviteMsg) -> Result<(), (StatusCode, String)> {
        let handover = self.is_handover_target(&msg.host.fingerprint);
        if self.is_member() && !self.trusted(&msg.host.fingerprint) {
            return Err((StatusCode::CONFLICT, "already a member of another cluster".into()));
        }
        if !self.members.lock().unwrap().is_empty() && !handover {
            return Err((StatusCode::CONFLICT, "this server hosts its own cluster; it has to dissolve it first".into()));
        }
        let mut host = msg.host;
        host.name = clean_name(&host.name);
        let mut pending = self.pending.lock().unwrap();
        pending.retain(|p| p.host.id != host.id);
        if pending.len() >= MAX_PENDING {
            return Err((StatusCode::TOO_MANY_REQUESTS, "too many pending invitations".into()));
        }
        pending.push(PendingInvite { host, invite_id: msg.invite_id, secret: msg.secret, received_at: now() });
        Ok(())
    }

    /// Whether this host asked `fingerprint` to take over its cluster.
    fn is_handover_target(&self, fingerprint: &str) -> bool {
        self.handover.lock().unwrap().as_ref().is_some_and(|(fp, until)| fp == fingerprint && *until > now())
    }

    /// Whether a host's invitations are accepted here without asking.
    fn trusted(&self, fingerprint: &str) -> bool {
        let expected = self.expect_host.lock().unwrap().clone();
        if expected.is_some_and(|(fp, until)| fp == fingerprint && until > now()) {
            return true;
        }
        self.state.lock().unwrap().trusted_hosts.iter().any(|f| f == fingerprint)
    }

    /// Accepts an invitation: joins the host and becomes its member. A member
    /// moving to a new host (a hand-over) leaves its old host first.
    pub async fn approve_invite(&self, host_id: &str) -> Result<()> {
        let invite = self
            .pending
            .lock()
            .unwrap()
            .iter()
            .find(|p| p.host.id == host_id)
            .cloned()
            .context("that invitation is no longer pending")?;
        if !self.members.lock().unwrap().is_empty() {
            if !self.is_handover_target(&invite.host.fingerprint) {
                bail!("this machine hosts a cluster; dissolve it before joining another");
            }
            // Hand-over: the members were told the new host, and move by themselves.
            self.members.lock().unwrap().clear();
            self.save_members()?;
            self.live.lock().unwrap().clear();
        }
        if let Role::Member { host, token } = self.role() {
            if host.id != invite.host.id {
                if let Ok(client) = pinned_tls::pinned_client(&host.fingerprint) {
                    let _ = client.post(format!("{}/cluster/v1/leave", host.url)).bearer_auth(token).timeout(Duration::from_secs(5)).send().await;
                }
            }
        }
        let me = self.me().context("this machine has no network address")?;
        let client = pinned_tls::pinned_client(&invite.host.fingerprint)?;
        let resp = client
            .post(format!("{}/cluster/v1/join", invite.host.url))
            .json(&JoinMsg { invite_id: invite.invite_id.clone(), secret: invite.secret.clone(), peer: me })
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .with_context(|| format!("cannot reach {}", invite.host.name))?;
        if !resp.status().is_success() {
            let msg = error_text(resp).await;
            self.pending.lock().unwrap().retain(|p| p.host.id != host_id);
            bail!("{} refused: {msg}", invite.host.name);
        }
        let reply: JoinReply = resp.json().await?;
        {
            let mut state = self.state.lock().unwrap();
            if !state.trusted_hosts.contains(&invite.host.fingerprint) {
                state.trusted_hosts.push(invite.host.fingerprint.clone());
            }
            state.role = Role::Member { host: invite.host.clone(), token: reply.token };
        }
        self.save_state()?;
        self.log(format!("joined {}'s cluster", invite.host.name));
        self.pending.lock().unwrap().clear();
        self.host_requests.lock().unwrap().clear();
        *self.expect_host.lock().unwrap() = None;
        *self.handover.lock().unwrap() = None;
        self.control.lock().unwrap().handing_over = None;
        *self.host_link.lock().unwrap() = HostLink::default();
        Ok(())
    }

    pub fn decline_invite(&self, host_id: &str) {
        self.pending.lock().unwrap().retain(|p| p.host.id != host_id);
    }

    fn receive_host_request(&self, mut msg: HostRequestMsg) -> Result<(), (StatusCode, String)> {
        // A member may only be asked by its own host (a hand-over).
        if let Role::Member { host, .. } = self.role() {
            if host.id != msg.from.id || host.fingerprint != msg.from.fingerprint {
                return Err((StatusCode::CONFLICT, "already a member of another cluster".into()));
            }
        }
        msg.from.name = clean_name(&msg.from.name);
        let mut reqs = self.host_requests.lock().unwrap();
        reqs.retain(|r| r.from.id != msg.from.id);
        if reqs.len() >= MAX_PENDING {
            return Err((StatusCode::TOO_MANY_REQUESTS, "too many pending requests".into()));
        }
        reqs.push(HostRequest { from: msg.from, members: msg.members, received_at: now() });
        Ok(())
    }

    /// Becomes host for a request: invites everyone it lists (who join
    /// without asking, since they asked for this host). Returns the servers
    /// that couldn't be invited.
    pub async fn approve_host_request(&self, from_id: &str) -> Result<Vec<String>> {
        let req = self
            .host_requests
            .lock()
            .unwrap()
            .iter()
            .find(|r| r.from.id == from_id)
            .cloned()
            .context("that request is no longer pending")?;
        self.host_requests.lock().unwrap().retain(|r| r.from.id != from_id);
        let me = self.me().context("this machine has no network address")?;
        if matches!(self.role(), Role::Standalone | Role::Member { .. }) {
            // A member taking over stops reporting to its old host, which
            // joins this machine (it asked for this).
            self.set_role(Role::Host)?;
            *self.host_link.lock().unwrap() = HostLink::default();
        }
        let mut problems = Vec::new();
        for peer in req.members.iter().filter(|p| p.id != me.id) {
            let (invite_id, secret) = (random_hex(8), random_hex(32));
            self.outgoing.lock().unwrap().push(OutgoingInvite {
                invite_id: invite_id.clone(),
                secret_sha256: sha256_hex(&secret),
                for_id: peer.id.clone(),
                expires_at: now() + INVITE_TTL_SECS,
            });
            if let Err(e) = send_invite(peer, InviteMsg { host: me.clone(), invite_id, secret }).await {
                problems.push(format!("{e:#}"));
            }
        }
        Ok(problems)
    }

    pub fn decline_host_request(&self, from_id: &str) {
        self.host_requests.lock().unwrap().retain(|r| r.from.id != from_id);
    }

    /// Leaves the cluster this machine is a member of.
    pub async fn leave(&self) -> Result<()> {
        let Role::Member { host, token } = self.role() else { bail!("this machine isn't a member of a cluster") };
        let client = pinned_tls::pinned_client(&host.fingerprint)?;
        // Best effort: the host may be gone; leaving locally still counts.
        let _ = client.post(format!("{}/cluster/v1/leave", host.url)).bearer_auth(token).timeout(Duration::from_secs(5)).send().await;
        self.set_role(Role::Standalone)
    }

    // ---- views ------------------------------------------------------------

    fn reachable_candidate(&self, id: &str) -> Result<Peer> {
        let cands = self.candidates.lock().unwrap();
        let c = cands.get(id).context("that server is no longer on the network")?;
        if c.reachable != Some(true) || now().saturating_sub(c.last_seen) > CANDIDATE_TTL_SECS {
            bail!("{} isn't reachable from this machine right now", c.beacon.name);
        }
        Ok(Peer {
            id: c.beacon.id.clone(),
            name: c.beacon.name.clone(),
            url: url_for(c.address, c.beacon.port),
            fingerprint: c.beacon.fingerprint.clone(),
        })
    }

    pub fn members(&self) -> Vec<MemberView> {
        let members = self.members.lock().unwrap();
        let live = self.live.lock().unwrap();
        let t = now();
        members
            .iter()
            .map(|m| {
                let l = live.get(&m.peer.id).cloned().unwrap_or_default();
                MemberView {
                    id: m.peer.id.clone(),
                    name: m.peer.name.clone(),
                    url: m.peer.url.clone(),
                    fingerprint: m.peer.fingerprint.clone(),
                    joined_at: m.joined_at,
                    online: !l.gone && l.last_seen.is_some_and(|s| t.saturating_sub(s) <= OFFLINE_AFTER_SECS),
                    last_seen: l.last_seen,
                    report: l.report,
                }
            })
            .collect()
    }

    /// Reachable servers on this network that aren't in this cluster.
    pub fn candidates(&self) -> Vec<CandidateView> {
        let t = now();
        let member_ids: Vec<String> = self.members.lock().unwrap().iter().map(|m| m.peer.id.clone()).collect();
        let my_host = match self.role() {
            Role::Member { host, .. } => Some(host.id),
            _ => None,
        };
        let my_id = self.id();
        let mut out: Vec<CandidateView> = self
            .candidates
            .lock()
            .unwrap()
            .values()
            .filter(|c| t.saturating_sub(c.last_seen) <= CANDIDATE_TTL_SECS && c.reachable == Some(true))
            .filter(|c| !member_ids.contains(&c.beacon.id) && Some(&c.beacon.id) != my_host.as_ref())
            .map(|c| CandidateView {
                id: c.beacon.id.clone(),
                name: c.beacon.name.clone(),
                url: url_for(c.address, c.beacon.port),
                fingerprint: c.beacon.fingerprint.clone(),
                role: c.beacon.role.clone(),
                busy: c.beacon.role == "member" && c.beacon.host_id.as_deref() != Some(my_id.as_str()),
                version: c.beacon.version.clone(),
            })
            .collect();
        out.sort_by(|a, b| a.name.cmp(&b.name));
        out
    }

    pub fn view(&self) -> ClusterView {
        let mut role = self.role();
        if let Role::Member { token, .. } = &mut role {
            token.clear(); // never leaves the service
        }
        let host_link = matches!(role, Role::Member { .. }).then(|| self.host_link.lock().unwrap().clone());
        // Read these before building the view: a lock taken inside the struct
        // literal is held until the literal ends.
        let desired_model = self.state.lock().unwrap().desired_model.clone();
        let locked_by_host = self.is_member() && *self.host_serving.lock().unwrap();
        ClusterView {
            id: self.id(),
            name: self.name(),
            url: self.me().map(|p| p.url),
            fingerprint: self.fingerprint.clone(),
            port: CLUSTER_PORT,
            strategy: self.strategy(),
            role,
            members: self.members(),
            candidates: self.candidates(),
            invites: self.pending.lock().unwrap().clone(),
            host_requests: self.host_requests.lock().unwrap().clone(),
            host_link,
            discovery_error: self.discovery_error.lock().unwrap().clone(),
            desired_model,
            model_sync: self.sync.lock().unwrap().clone(),
            locked_by_host,
        }
    }

    // ---- running ----------------------------------------------------------

    pub fn router(self: Arc<Self>) -> Router {
        Router::new()
            .route("/cluster/v1/health", get(health))
            .route("/cluster/v1/invite", post(invite_handler))
            .route("/cluster/v1/host-request", post(host_request_handler))
            .route("/cluster/v1/join", post(join_handler))
            .route("/cluster/v1/report", post(report_handler))
            .route("/cluster/v1/leave", post(leave_handler))
            .route("/cluster/v1/take-over", post(take_over_handler))
            .route("/cluster/v1/bye", post(bye_handler))
            .with_state(self)
    }

    /// Runs the whole service: the TLS API, discovery, reachability checks,
    /// and (as a member) reports to the host. Returns only on a fatal error.
    pub async fn run(self: Arc<Self>) -> Result<()> {
        tokio::spawn(self.clone().discovery());
        tokio::spawn(self.clone().check_candidates());
        tokio::spawn(self.clone().report_loop());
        let config = tls_config(&self.dir)?;
        let addr = SocketAddr::from(([0, 0, 0, 0], CLUSTER_PORT));
        let app = self.router().into_make_service_with_connect_info::<SocketAddr>();
        axum_server::bind_rustls(addr, config).serve(app).await.with_context(|| format!("cluster service on {addr}"))
    }

    fn beacon(&self) -> Beacon {
        let name = self.name();
        let state = self.state.lock().unwrap();
        let (role, host_id) = match &state.role {
            Role::Standalone => ("standalone", None),
            Role::Host => ("host", None),
            Role::Member { host, .. } => ("member", Some(host.id.clone())),
        };
        Beacon {
            magic: MAGIC.into(),
            id: state.id.clone(),
            name,
            port: CLUSTER_PORT,
            fingerprint: self.fingerprint.clone(),
            role: role.into(),
            host_id,
            version: env!("CARGO_PKG_VERSION").into(),
            query: false,
        }
    }

    /// Sends a beacon every few seconds and records everyone else's.
    async fn discovery(self: Arc<Self>) {
        let socket = match bind_discovery() {
            Ok(s) => Arc::new(s),
            Err(e) => {
                *self.discovery_error.lock().unwrap() =
                    Some(format!("Can't use UDP port {DISCOVERY_PORT} for discovery ({e:#}); other servers won't be listed."));
                return;
            }
        };
        let sender = socket.clone();
        let me = self.clone();
        tokio::spawn(async move {
            let target = SocketAddr::from(([255, 255, 255, 255], DISCOVERY_PORT));
            // On start, ask who's there: everyone answers at once, so this
            // server's list fills within a second instead of a beacon period.
            for _ in 0..2 {
                if let Ok(bytes) = serde_json::to_vec(&Beacon { query: true, ..me.beacon() }) {
                    let _ = sender.send_to(&bytes, target).await;
                }
                tokio::time::sleep(Duration::from_millis(400)).await;
            }
            loop {
                if let Ok(bytes) = serde_json::to_vec(&me.beacon()) {
                    let _ = sender.send_to(&bytes, target).await;
                }
                tokio::time::sleep(Duration::from_secs(BEACON_SECS)).await;
            }
        });
        let mut buf = vec![0u8; 4096];
        loop {
            let Ok((n, from)) = socket.recv_from(&mut buf).await else { continue };
            let Ok(beacon) = serde_json::from_slice::<Beacon>(&buf[..n]) else { continue };
            if beacon.magic != MAGIC || beacon.id == self.id() || beacon.fingerprint.len() < 64 {
                continue;
            }
            if beacon.query {
                // Answer the newcomer directly.
                if let Ok(bytes) = serde_json::to_vec(&self.beacon()) {
                    let _ = socket.send_to(&bytes, SocketAddr::new(from.ip(), DISCOVERY_PORT)).await;
                }
            }
            let mut new = false;
            let mut cands = self.candidates.lock().unwrap();
            let entry = cands.entry(beacon.id.clone()).or_insert(Candidate {
                beacon: beacon.clone(),
                address: from.ip(),
                last_seen: 0,
                reachable: None,
                checked_at: 0,
            });
            // New, back after a gap, or a new address or certificate: check now.
            let t = now();
            if entry.last_seen == 0
                || t.saturating_sub(entry.last_seen) > CANDIDATE_TTL_SECS
                || entry.address != from.ip()
                || entry.beacon.fingerprint != beacon.fingerprint
            {
                entry.reachable = None;
                entry.checked_at = 0;
                new = true;
            }
            entry.address = from.ip();
            entry.beacon = beacon;
            entry.last_seen = t;
            drop(cands);
            if new {
                self.check_now.notify_one();
            }
        }
    }

    /// Lists a candidate only after a TLS health check pinned to the
    /// certificate it announced: it is running, and reachable from here.
    async fn check_candidates(self: Arc<Self>) {
        loop {
            let t = now();
            let due: Vec<(String, String, String)> = {
                let mut cands = self.candidates.lock().unwrap();
                cands.retain(|_, c| t.saturating_sub(c.last_seen) <= CANDIDATE_TTL_SECS * 6);
                cands
                    .values()
                    .filter(|c| t.saturating_sub(c.checked_at) >= RECHECK_SECS)
                    .map(|c| (c.beacon.id.clone(), url_for(c.address, c.beacon.port), c.beacon.fingerprint.clone()))
                    .collect()
            };
            for (id, url, fp) in due {
                let ok = async {
                    let client = pinned_tls::pinned_client(&fp)?;
                    let v: serde_json::Value =
                        client.get(format!("{url}/cluster/v1/health")).timeout(Duration::from_secs(3)).send().await?.json().await?;
                    anyhow::Ok(v["id"].as_str() == Some(id.as_str()))
                }
                .await
                .unwrap_or(false);
                if let Some(c) = self.candidates.lock().unwrap().get_mut(&id) {
                    c.reachable = Some(ok);
                    c.checked_at = now();
                }
            }
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(2)) => {}
                _ = self.check_now.notified() => {}
            }
        }
    }

    /// As a member: report to the host every few seconds. Also accepts
    /// pending invitations from hosts this machine already trusts.
    async fn report_loop(self: Arc<Self>) {
        loop {
            if !self.is_member() && self.worker.lock().unwrap().is_some() {
                // Left or removed while running a share of the host's model.
                self.clone().follow_worker_order(None).await;
            }
            if let Role::Member { host, token } = self.role() {
                self.collect_backend_logs().await;
                let logs: Vec<String> = {
                    let out = self.outbox.lock().unwrap();
                    out.iter().take(LOG_LINES_PER_REPORT).cloned().collect()
                };
                // Awaited before the literal: a lock taken inside it would be
                // held across these awaits.
                let inventory = collect_inventory().await;
                let serving = collect_serving().await;
                let worker = self.check_worker().await;
                let model_sync = self.sync.lock().unwrap().clone();
                let report = Report { inventory, serving, model_sync, logs: logs.clone(), worker };
                let result = async {
                    let client = pinned_tls::pinned_client(&host.fingerprint)?;
                    let resp = client
                        .post(format!("{}/cluster/v1/report", host.url))
                        .bearer_auth(&token)
                        .json(&report)
                        .timeout(Duration::from_secs(10))
                        .send()
                        .await?;
                    anyhow::Ok(resp)
                }
                .await;
                match result {
                    Ok(resp) if resp.status() == StatusCode::UNAUTHORIZED => {
                        // Removed by the host, or the cluster was dissolved.
                        if self.role() == (Role::Member { host: host.clone(), token: token.clone() }) {
                            let _ = self.set_role(Role::Standalone);
                        }
                        *self.host_link.lock().unwrap() = HostLink::default();
                    }
                    Ok(resp) if resp.status().is_success() => {
                        {
                            // Delivered: drop the lines that went out.
                            let mut out = self.outbox.lock().unwrap();
                            for _ in 0..logs.len().min(out.len()) {
                                out.pop_front();
                            }
                        }
                        if let Ok(reply) = resp.json::<ReportReply>().await {
                            if let Some(spec) = reply.ensure_model.clone() {
                                self.clone().ensure_model(spec);
                            }
                            if let Some(fp) = reply.expect_host {
                                *self.expect_host.lock().unwrap() = Some((fp, now() + INVITE_TTL_SECS));
                            }
                            if let Some(s) = reply.strategy {
                                *self.host_strategy.lock().unwrap() = Some(s);
                            }
                            *self.host_serving.lock().unwrap() = reply.host_serving;
                            self.clone().follow_worker_order(reply.worker).await;
                        }
                        *self.host_link.lock().unwrap() = HostLink { connected: true, last_ok: Some(now()), error: None };
                    }
                    Ok(resp) => {
                        let mut link = self.host_link.lock().unwrap();
                        link.connected = false;
                        link.error = Some(format!("the host answered HTTP {}", resp.status()));
                    }
                    Err(e) => {
                        let mut link = self.host_link.lock().unwrap();
                        link.connected = false;
                        link.error = Some(if pinned_tls::is_pin_mismatch(e.root_cause()) {
                            "the host's certificate changed; nothing was sent".into()
                        } else {
                            format!("{} is unreachable", host.name)
                        });
                    }
                }
            }
            let auto: Vec<String> = {
                let pending = self.pending.lock().unwrap().clone();
                pending.into_iter().filter(|p| self.trusted(&p.host.fingerprint)).map(|p| p.host.id).collect()
            };
            for host_id in auto {
                if let Err(e) = self.approve_invite(&host_id).await {
                    *self.host_link.lock().unwrap() = HostLink { connected: false, last_ok: None, error: Some(format!("{e:#}")) };
                }
            }
            tokio::time::sleep(Duration::from_secs(if self.pending.lock().unwrap().is_empty() { HEARTBEAT_SECS } else { 1 })).await;
        }
    }
}

async fn send_invite(target: &Peer, msg: InviteMsg) -> Result<()> {
    let client = pinned_tls::pinned_client(&target.fingerprint)?;
    let resp = client
        .post(format!("{}/cluster/v1/invite", target.url))
        .json(&msg)
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .with_context(|| format!("cannot reach {}", target.name))?;
    if !resp.status().is_success() {
        bail!("{} declined: {}", target.name, error_text(resp).await);
    }
    Ok(())
}

/// `AB:CD:…` (first 8 bytes), as the apps show it.
pub fn short_fingerprint(fp: &str) -> String {
    pinned_tls::short(fp)
}

impl Cluster {
    /// As member: makes sure the host's model is on disk. Already present
    /// (the app's models folder or the Hugging Face cache) → ready; otherwise
    /// downloads it, resumable and verified, from the same pinned commit.
    fn ensure_model(self: Arc<Self>, spec: ModelSpec) {
        {
            let sync = self.sync.lock().unwrap();
            let same = sync.as_ref().is_some_and(|s| s.repo == spec.repo && s.revision == spec.revision);
            // Working on it, or done: nothing to do. A failed one is retried.
            if same && sync.as_ref().is_some_and(|s| s.state != "error") {
                return;
            }
        }
        if let Some(cancel) = self.sync_cancel.lock().unwrap().take() {
            cancel.store(true, std::sync::atomic::Ordering::Relaxed);
        }
        let base = ModelSync { label: spec.label.clone(), repo: spec.repo.clone(), revision: spec.revision.clone(), ..Default::default() };
        if crate::model::find_installed(&spec.repo, Some(&spec.revision)).is_some() {
            *self.sync.lock().unwrap() = Some(ModelSync { state: "ready".into(), ..base });
            self.log(format!("{} is already on this machine", spec.label));
            return;
        }
        *self.sync.lock().unwrap() = Some(ModelSync { state: "checking".into(), ..base.clone() });
        let cancel: crate::download::Cancel = Arc::new(std::sync::atomic::AtomicBool::new(false));
        *self.sync_cancel.lock().unwrap() = Some(cancel.clone());
        self.log(format!("downloading {} ({}@{}) for the cluster", spec.label, spec.repo, &spec.revision[..spec.revision.len().min(7)]));
        tokio::spawn(async move {
            let client = crate::download::client();
            let dest = crate::model::app_model_dir(&spec.repo);
            let me = self.clone();
            let result = async {
                let files = crate::download::list_files(&client, &spec.repo, &spec.revision).await?;
                crate::download::download(&client, &spec.repo, &spec.revision, &files, &dest, cancel, move |p| {
                    if let Some(s) = me.sync.lock().unwrap().as_mut() {
                        s.state = "downloading".into();
                        s.done_bytes = p.done_bytes;
                        s.total_bytes = p.total_bytes;
                        s.bytes_per_sec = p.bytes_per_sec;
                    }
                })
                .await
            }
            .await;
            let mut sync = self.sync.lock().unwrap();
            let Some(s) = sync.as_mut().filter(|s| s.repo == spec.repo && s.revision == spec.revision) else { return };
            match result {
                Ok(_) => {
                    s.state = "ready".into();
                    s.done_bytes = s.total_bytes;
                    drop(sync);
                    self.log(format!("{} downloaded", spec.label));
                }
                Err(e) => {
                    s.state = "error".into();
                    s.error = Some(format!("{e:#}"));
                    drop(sync);
                    self.log(format!("downloading {} failed: {e:#}", spec.label));
                }
            }
        });
    }

    /// As member: queues the backend's new log lines for the next report.
    async fn collect_backend_logs(&self) {
        let since = *self.backend_log_since.lock().unwrap();
        let until = now();
        *self.backend_log_since.lock().unwrap() = until;
        // Its own backend, and its share of the host's split model.
        for (container, prefix) in [(crate::docker::CONTAINER_NAME, ""), (crate::docker::WORKER_CONTAINER, "[split worker] ")] {
            let out = tokio::process::Command::new("docker")
                .args(["logs", "--since", &since.to_string(), "--until", &until.to_string(), container])
                .output()
                .await;
            let Ok(out) = out else { continue };
            if !out.status.success() {
                continue; // no such container on this machine
            }
            let text = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
            let mut outbox = self.outbox.lock().unwrap();
            for line in text.lines().filter(|l| !l.trim().is_empty()) {
                outbox.push_back(format!("{prefix}{line}"));
            }
            while outbox.len() > OUTBOX_LOG_LINES {
                outbox.pop_front();
            }
        }
    }

    /// As member: starts, replaces or stops the split-model worker so it
    /// matches the host's order.
    async fn follow_worker_order(self: Arc<Self>, order: Option<WorkerOrder>) {
        let current = self.worker.lock().unwrap().clone();
        let same = current.as_ref().map(|o| &o.run_id) == order.as_ref().map(|o| &o.run_id);
        if same {
            return;
        }
        if current.is_some() {
            let _ = crate::docker::remove_worker().await;
            self.log("Stopped this machine's share of the split model.".to_string());
        }
        *self.worker.lock().unwrap() = order.clone();
        let Some(order) = order else {
            *self.worker_status.lock().unwrap() = None;
            return;
        };
        let status = |state: &str, message: Option<String>| WorkerStatus { run_id: order.run_id.clone(), state: state.into(), message };
        *self.worker_status.lock().unwrap() = Some(status("starting", None));
        self.log(format!(
            "Starting this machine's share of {}: rank {} of {}, {} layers.",
            order.model.label, order.rank.rank, order.rank.nnodes, order.stage.layers
        ));
        let result = start_worker(&order).await;
        if let Err(e) = result {
            let message = format!("{e:#}");
            self.log(format!("Couldn't start the split-model worker: {message}"));
            *self.worker_status.lock().unwrap() = Some(status("failed", Some(message)));
        }
    }

    /// As member: the worker's state for the next report.
    async fn check_worker(&self) -> Option<WorkerStatus> {
        let mut status = self.worker_status.lock().unwrap().clone()?;
        if status.state == "failed" {
            return Some(status);
        }
        match crate::docker::worker_state().await {
            Ok(Some((run, st))) if run == status.run_id && st.running => status.state = "running".into(),
            Ok(Some((run, st))) if run == status.run_id => {
                let log = crate::docker::worker_logs_tail(60).await.unwrap_or_default();
                status.state = "failed".into();
                status.message = Some(crate::docker::diagnose_crash(&log).unwrap_or_else(|| {
                    format!("its SGLang worker stopped (exit code {})", st.exit_code)
                }));
            }
            Ok(_) => {}
            Err(e) => {
                status.state = "failed".into();
                status.message = Some(format!("{e:#}"));
            }
        }
        *self.worker_status.lock().unwrap() = Some(status.clone());
        Some(status)
    }

    /// As host: the shares members should run for this start (empty to stop).
    pub fn set_workers(&self, orders: HashMap<String, WorkerOrder>) {
        *self.workers.lock().unwrap() = orders;
    }

    /// As host: each member's worker state for the run in progress.
    pub fn worker_states(&self) -> Vec<(String, Option<WorkerStatus>)> {
        let orders = self.workers.lock().unwrap().clone();
        let live = self.live.lock().unwrap();
        orders
            .iter()
            .map(|(id, order)| {
                let name = self.members.lock().unwrap().iter().find(|m| &m.peer.id == id).map(|m| m.peer.name.clone()).unwrap_or_else(|| id.clone());
                let st = live
                    .get(id)
                    .and_then(|l| l.report.as_ref())
                    .and_then(|r| r.worker.clone())
                    .filter(|w| w.run_id == order.run_id);
                (name, st)
            })
            .collect()
    }
}

/// As member: launches its share of the host's split model.
async fn start_worker(order: &WorkerOrder) -> Result<()> {
    let dir = crate::model::find_installed(&order.model.repo, Some(&order.model.revision))
        .with_context(|| format!("{} isn't on this machine yet", order.model.label))?;
    if !crate::docker::image_exists(&order.image).await? {
        bail!("the backend image {} isn't on this machine", order.image);
    }
    let gpu = crate::gpu::query()
        .await?
        .into_iter()
        .max_by_key(|g| g.free_bytes)
        .context("no NVIDIA GPU found")?;
    let req = order.stage.requirement;
    let fraction = crate::vram::mem_fraction_static(&req, gpu.free_bytes, gpu.total_bytes).with_context(|| {
        format!(
            "its share needs {} of VRAM but {} is free on {}",
            crate::vram::fmt_gib(req.total_bytes),
            crate::vram::fmt_gib(gpu.free_bytes),
            gpu.name
        )
    })?;
    let mut rank = order.rank.clone();
    rank.interface = crate::net::lan_ip().and_then(crate::split::interface_for);
    crate::docker::run_worker(&crate::docker::WorkerSpec {
        image: order.image.clone(),
        gpu_index: gpu.index,
        model: crate::model::mount_for(&dir)?,
        model_key: order.model.key.clone(),
        mem_fraction_static: fraction,
        context_len: order.context_len,
        split: rank,
        dtype: order.dtype.clone(),
        run_id: order.run_id.clone(),
    })
    .await
}

fn url_for(ip: IpAddr, port: u16) -> String {
    match ip {
        IpAddr::V4(v4) => format!("https://{v4}:{port}"),
        IpAddr::V6(v6) => format!("https://[{v6}]:{port}"),
    }
}

fn bind_discovery() -> Result<tokio::net::UdpSocket> {
    let std_socket = std::net::UdpSocket::bind(("0.0.0.0", DISCOVERY_PORT))?;
    std_socket.set_broadcast(true)?;
    std_socket.set_nonblocking(true)?;
    Ok(tokio::net::UdpSocket::from_std(std_socket)?)
}

async fn error_text(resp: reqwest::Response) -> String {
    let status = resp.status();
    resp.json::<serde_json::Value>()
        .await
        .ok()
        .and_then(|v| v["error"].as_str().map(String::from))
        .unwrap_or_else(|| format!("HTTP {status}"))
}

fn write_private(path: &Path, bytes: &[u8]) -> Result<()> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, bytes)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
    }
    std::fs::rename(&tmp, path)?;
    Ok(())
}

/// Creates this server's self-signed certificate on first use; returns its DER.
fn ensure_certificate(dir: &Path) -> Result<Vec<u8>> {
    let (crt, key) = (dir.join("head.crt"), dir.join("head.key"));
    if !crt.exists() || !key.exists() {
        let cert = rcgen::generate_simple_self_signed(vec!["openphalanx-server".to_string()])?;
        write_private(&key, cert.signing_key.serialize_pem().as_bytes())?;
        write_private(&crt, cert.cert.pem().as_bytes())?;
    }
    let pem = std::fs::read(&crt)?;
    let der = rustls_pemfile::certs(&mut pem.as_slice()).next().context("certificate file is empty")??;
    Ok(der.to_vec())
}

fn tls_config(dir: &Path) -> Result<axum_server::tls_rustls::RustlsConfig> {
    let pem = std::fs::read(dir.join("head.crt"))?;
    let certs = rustls_pemfile::certs(&mut pem.as_slice()).collect::<Result<Vec<_>, _>>()?;
    let key_pem = std::fs::read(dir.join("head.key"))?;
    let key = rustls_pemfile::private_key(&mut key_pem.as_slice())?.context("no private key")?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()?
        .with_no_client_auth()
        .with_single_cert(certs, key)?;
    Ok(axum_server::tls_rustls::RustlsConfig::from_config(Arc::new(config)))
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(serde_json::json!({ "error": message }))).into_response()
}

// --------------------------------------------------------------------------
// HTTP handlers
// --------------------------------------------------------------------------

async fn health(State(c): State<Arc<Cluster>>) -> Json<serde_json::Value> {
    let b = c.beacon();
    Json(serde_json::json!({ "id": b.id, "name": b.name, "role": b.role, "host_id": b.host_id, "version": b.version }))
}

async fn invite_handler(State(c): State<Arc<Cluster>>, Json(msg): Json<InviteMsg>) -> Response {
    match c.receive_invite(msg) {
        Ok(()) => Json(serde_json::json!({ "ok": true, "pending": true })).into_response(),
        Err((status, m)) => error(status, &m),
    }
}

async fn host_request_handler(State(c): State<Arc<Cluster>>, Json(msg): Json<HostRequestMsg>) -> Response {
    match c.receive_host_request(msg) {
        Ok(()) => Json(serde_json::json!({ "ok": true, "pending": true })).into_response(),
        Err((status, m)) => error(status, &m),
    }
}

async fn join_handler(State(c): State<Arc<Cluster>>, Json(mut msg): Json<JoinMsg>) -> Response {
    if c.is_member() {
        return error(StatusCode::CONFLICT, "this server is no longer a host");
    }
    if !c.redeem_invite(&msg.invite_id, &msg.secret, &msg.peer.id) {
        tokio::time::sleep(Duration::from_secs(1)).await;
        return error(StatusCode::FORBIDDEN, "invalid or expired invitation");
    }
    msg.peer.name = clean_name(&msg.peer.name);
    match c.add_member(msg.peer) {
        Ok(token) => Json(JoinReply { token }).into_response(),
        Err(e) => error(StatusCode::INTERNAL_SERVER_ERROR, &format!("{e:#}")),
    }
}

async fn report_handler(
    State(c): State<Arc<Cluster>>,
    ConnectInfo(_peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(report): Json<Report>,
) -> Response {
    let Some(id) = c.authenticate(&headers) else {
        return error(StatusCode::UNAUTHORIZED, "not a member of this cluster");
    };
    c.record_report(&id, report);
    let expect_host = c.handover.lock().unwrap().clone().filter(|(_, until)| *until > now()).map(|(fp, _)| fp);
    let ensure_model = c.state.lock().unwrap().desired_model.clone();
    let host_serving = c.control.lock().unwrap().serving;
    let worker = c.workers.lock().unwrap().get(&id).cloned();
    Json(ReportReply { expect_host, strategy: Some(c.strategy()), ensure_model, host_serving, worker }).into_response()
}

async fn take_over_handler(State(c): State<Arc<Cluster>>, headers: HeaderMap, Json(peer): Json<Peer>) -> Response {
    let Some(id) = c.authenticate(&headers) else {
        return error(StatusCode::UNAUTHORIZED, "not a member of this cluster");
    };
    // The requester must be the member the token belongs to.
    let known = c.members.lock().unwrap().iter().find(|m| m.peer.id == id).map(|m| m.peer.clone());
    let Some(known) = known.filter(|k| k.id == peer.id && k.fingerprint == peer.fingerprint) else {
        return error(StatusCode::FORBIDDEN, "that isn't this member");
    };
    match c.grant_take_over(&id, &Peer { url: peer.url, ..known }) {
        Ok(peers) => Json(peers).into_response(),
        Err((status, m)) => error(status, &m),
    }
}

async fn bye_handler(State(c): State<Arc<Cluster>>, headers: HeaderMap) -> Response {
    let Some(id) = c.authenticate(&headers) else {
        return error(StatusCode::UNAUTHORIZED, "not a member of this cluster");
    };
    c.live.lock().unwrap().entry(id).or_default().gone = true;
    Json(serde_json::json!({ "ok": true })).into_response()
}

async fn leave_handler(State(c): State<Arc<Cluster>>, headers: HeaderMap) -> Response {
    let Some(id) = c.authenticate(&headers) else {
        return error(StatusCode::UNAUTHORIZED, "not a member of this cluster");
    };
    match c.remove_member(&id) {
        Ok(_) => Json(serde_json::json!({ "ok": true })).into_response(),
        Err(e) => error(StatusCode::INTERNAL_SERVER_ERROR, &format!("{e:#}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cluster() -> (tempfile::TempDir, Arc<Cluster>) {
        let dir = tempfile::tempdir().unwrap();
        let c = Cluster::open(dir.path()).unwrap();
        (dir, c)
    }

    fn peer(id: &str) -> Peer {
        Peer { id: id.into(), name: id.into(), url: format!("https://{id}:9092"), fingerprint: "AA".repeat(32) }
    }

    #[test]
    fn identity_and_role_persist() {
        let (dir, c) = cluster();
        let (id, fp) = (c.id(), c.fingerprint().to_string());
        c.set_role(Role::Host).unwrap();
        let again = Cluster::open(dir.path()).unwrap();
        assert_eq!((again.id(), again.fingerprint().to_string(), again.role()), (id, fp, Role::Host));
    }

    #[test]
    fn invitations_are_single_use_and_bound_to_the_invited_server() {
        let (_d, c) = cluster();
        let secret = "s3cret";
        c.outgoing.lock().unwrap().push(OutgoingInvite {
            invite_id: "inv".into(),
            secret_sha256: sha256_hex(secret),
            for_id: "b".into(),
            expires_at: now() + 60,
        });
        assert!(!c.redeem_invite("inv", secret, "someone-else"), "bound to the invited server");
        assert!(!c.redeem_invite("inv", "wrong", "b"));
        assert!(c.redeem_invite("inv", secret, "b"));
        assert!(!c.redeem_invite("inv", secret, "b"), "single use");
    }

    #[test]
    fn members_are_hashed_removable_and_hosts_revert_to_standalone() {
        let (dir, c) = cluster();
        let token = c.add_member(peer("b")).unwrap();
        assert_eq!(c.role(), Role::Host);
        let stored = std::fs::read_to_string(dir.path().join("members.json")).unwrap();
        assert!(!stored.contains(&token), "only the token hash is stored");
        let mut headers = HeaderMap::new();
        headers.insert("authorization", format!("Bearer {token}").parse().unwrap());
        assert_eq!(c.authenticate(&headers).as_deref(), Some("b"));
        c.record_report("b", Report { inventory: Inventory { cpus: 8, ..Default::default() }, ..Default::default() });
        assert!(c.members()[0].online);
        assert!(c.remove_member("b").unwrap());
        assert!(c.authenticate(&headers).is_none(), "removal revokes at once");
        assert_eq!(c.role(), Role::Standalone, "a host with no members is standalone");
    }

    #[test]
    fn members_refuse_strangers_and_pending_invitations_are_bounded() {
        let (_d, c) = cluster();
        for i in 0..MAX_PENDING {
            let msg = InviteMsg { host: peer(&format!("h{i}")), invite_id: "x".into(), secret: "y".into() };
            assert!(c.receive_invite(msg).is_ok());
        }
        let extra = InviteMsg { host: peer("one-too-many"), invite_id: "x".into(), secret: "y".into() };
        assert_eq!(c.receive_invite(extra).unwrap_err().0, StatusCode::TOO_MANY_REQUESTS);
        c.pending.lock().unwrap().clear();
        c.set_role(Role::Member { host: peer("h0"), token: "t".into() }).unwrap();
        let stranger = InviteMsg {
            host: Peer { fingerprint: "CC".repeat(32), ..peer("h1") },
            invite_id: "x".into(),
            secret: "y".into(),
        };
        assert_eq!(c.receive_invite(stranger).unwrap_err().0, StatusCode::CONFLICT);
        // A hand-over target announced by the current host is accepted.
        *c.expect_host.lock().unwrap() = Some(("CC".repeat(32), now() + 60));
        let handover = InviteMsg {
            host: Peer { fingerprint: "CC".repeat(32), ..peer("h1") },
            invite_id: "x".into(),
            secret: "y".into(),
        };
        assert!(c.receive_invite(handover).is_ok());
    }

    #[test]
    fn the_member_token_never_leaves_the_service() {
        let (_d, c) = cluster();
        c.set_role(Role::Member { host: peer("h"), token: "secret-token".into() }).unwrap();
        let json = serde_json::to_string(&c.view()).unwrap();
        assert!(!json.contains("secret-token"));
    }

    #[test]
    fn candidates_need_a_recent_beacon_and_a_passed_check() {
        let (_d, c) = cluster();
        let beacon = |id: &str| Beacon {
            magic: MAGIC.into(),
            id: id.into(),
            name: id.into(),
            port: CLUSTER_PORT,
            fingerprint: "BB".repeat(32),
            role: "standalone".into(),
            host_id: None,
            version: "0".into(),
            query: false,
        };
        let ip: IpAddr = "192.168.1.9".parse().unwrap();
        let mut cands = c.candidates.lock().unwrap();
        let mut add = |id: &str, seen: u64, reachable: Option<bool>| {
            cands.insert(id.into(), Candidate { beacon: beacon(id), address: ip, last_seen: seen, reachable, checked_at: now() });
        };
        add("ok", now(), Some(true));
        add("unchecked", now(), None);
        add("unreachable", now(), Some(false));
        add("stale", now() - 60, Some(true));
        drop(cands);
        let listed: Vec<String> = c.candidates().into_iter().map(|v| v.id).collect();
        assert_eq!(listed, vec!["ok".to_string()]);
    }

    #[test]
    fn a_member_accepts_a_host_request_only_from_its_own_host() {
        let (_d, c) = cluster();
        c.set_role(Role::Member { host: peer("h"), token: "t".into() }).unwrap();
        let from_stranger = HostRequestMsg { from: Peer { fingerprint: "DD".repeat(32), ..peer("x") }, members: vec![] };
        assert!(c.receive_host_request(from_stranger).is_err());
        let from_host = HostRequestMsg { from: peer("h"), members: vec![peer("h")] };
        assert!(c.receive_host_request(from_host).is_ok());
    }

    #[test]
    fn a_host_accepts_an_invitation_only_from_its_handover_target() {
        let (_d, c) = cluster();
        c.add_member(peer("m")).unwrap();
        let from = |fp: &str| InviteMsg { host: Peer { fingerprint: fp.repeat(32), ..peer("new") }, invite_id: "i".into(), secret: "s".into() };
        assert!(c.receive_invite(from("EE")).is_err(), "a host with members refuses strangers");
        *c.handover.lock().unwrap() = Some(("EE".repeat(32), now() + 60));
        assert!(c.receive_invite(from("EE")).is_ok());
    }

    #[test]
    fn strategy_defaults_to_split_persists_and_belongs_to_the_host() {
        let (dir, c) = cluster();
        assert_eq!(c.strategy(), Strategy::Split);
        c.set_strategy(Strategy::Replicas).unwrap();
        assert_eq!(Cluster::open(dir.path()).unwrap().strategy(), Strategy::Replicas);
        c.set_serving(true);
        assert!(c.set_strategy(Strategy::Split).is_err(), "not while the server runs");
        c.set_strategy(Strategy::Replicas).unwrap();
        c.set_serving(false);
        c.set_role(Role::Member { host: peer("h"), token: "t".into() }).unwrap();
        assert!(c.set_strategy(Strategy::Split).is_err(), "members follow the host");
        *c.host_strategy.lock().unwrap() = Some(Strategy::Split);
        assert_eq!(c.strategy(), Strategy::Split, "members show the host's choice");
    }

    #[test]
    fn hosts_keep_member_logs_and_members_queue_them() {
        let (_d, c) = cluster();
        c.add_member(peer("m")).unwrap();
        let lines: Vec<String> = (0..MEMBER_LOG_LINES + 5).map(|i| format!("line {i}")).collect();
        c.record_report("m", Report { logs: lines, ..Default::default() });
        let kept = c.member_logs("m");
        assert_eq!(kept.len(), MEMBER_LOG_LINES, "bounded");
        assert_eq!(kept.last().unwrap(), &format!("line {}", MEMBER_LOG_LINES + 4), "newest kept");
        assert!(c.members()[0].report.as_ref().unwrap().logs.is_empty(), "logs aren't repeated in the view");

        for i in 0..OUTBOX_LOG_LINES + 3 {
            c.log(format!("event {i}"));
        }
        assert_eq!(c.outbox.lock().unwrap().len(), OUTBOX_LOG_LINES);
    }

    #[test]
    fn the_desired_model_persists() {
        let (dir, c) = cluster();
        let spec = ModelSpec { key: "k".into(), label: "M".into(), repo: "org/m".into(), revision: "abc".into(), weight_bytes: 1 };
        c.set_desired_model(Some(spec.clone())).unwrap();
        assert_eq!(Cluster::open(dir.path()).unwrap().view().desired_model, Some(spec));
    }

    #[test]
    fn only_one_machine_controls_the_cluster() {
        let (_d, c) = cluster();
        c.add_member(peer("m")).unwrap();
        let rt = tokio::runtime::Runtime::new().unwrap();
        // The host starts: a member can no longer take control.
        rt.block_on(c.claim_start()).unwrap();
        assert_eq!(c.grant_take_over("m", &peer("m")).unwrap_err().0, StatusCode::CONFLICT);
        // Stopped: the member may take over; the host can't start meanwhile,
        // and a second member can't take it too.
        c.set_serving(false);
        assert!(c.grant_take_over("m", &peer("m")).is_ok());
        assert!(rt.block_on(c.claim_start()).is_err(), "control is being handed over");
        let other = Peer { fingerprint: "FF".repeat(32), ..peer("x") };
        assert!(c.grant_take_over("x", &other).is_err());
    }

    #[test]
    fn a_member_locked_by_a_serving_host_cant_start() {
        let (_d, c) = cluster();
        c.set_role(Role::Member { host: peer("h"), token: "t".into() }).unwrap();
        *c.host_serving.lock().unwrap() = true;
        assert!(c.view().locked_by_host);
        let rt = tokio::runtime::Runtime::new().unwrap();
        let err = rt.block_on(c.claim_start()).unwrap_err().to_string();
        assert!(err.contains("running the cluster's server"), "{err}");
    }

    #[test]
    fn goodbyes_and_silence_count_as_dropped() {
        let (_d, c) = cluster();
        c.add_member(peer("a")).unwrap();
        c.add_member(peer("b")).unwrap();
        c.record_report("a", Report::default());
        c.record_report("b", Report::default());
        assert!(c.dropped_members(12).is_empty(), "both reporting");
        c.live.lock().unwrap().get_mut("b").unwrap().gone = true;
        assert_eq!(c.dropped_members(12), vec!["b".to_string()]);
        assert!(!c.members().iter().find(|m| m.id == "b").unwrap().online);
        c.live.lock().unwrap().get_mut("a").unwrap().last_seen = Some(now() - 30);
        assert_eq!(c.dropped_members(12).len(), 2);
    }
}