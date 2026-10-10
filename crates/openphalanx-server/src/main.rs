//! `oppxs`: an OpenPhalanx server without the desktop app.
//!
//!   oppxs run                       run the server (keep it running; a systemd unit does this)
//!   oppxs models | download <model> | use <model> | context <tokens> | long-context on|off
//!   oppxs start | stop | status     the backend that serves the selected model
//!   oppxs pair | devices | revoke <device>
//!   oppxs logs [<member>]
//!   oppxs servers | invite | make-host | approve | decline | remove | leave | dissolve | strategy
//!
//! It shares its settings and state (`~/.config/openphalanx`,
//! `~/.local/share/openphalanx`) with the app, so run one or the other on a
//! machine, not both. Commands that act on the running server talk to it
//! over 127.0.0.1:9094 with a per-start token readable only by this user.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{bail, Context, Result};
use axum::extract::{Path as UrlPath, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clap::{Parser, Subcommand};
use openphalanx_core::cluster::{Cluster, ClusterView, Role};
use openphalanx_core::service::{ServerStatus, Service};
use openphalanx_core::settings::Settings;
use openphalanx_core::{catalog, download, gpu, model, paths, server, vram};
use rand::Rng;

const CONTROL_PORT: u16 = 9094;

#[derive(Parser)]
#[command(name = "oppxs", version, about = "Run an OpenPhalanx server without the desktop app")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run the server until stopped (Ctrl-C). Stopping it stops the model too.
    Run {
        /// Name shown to other servers and in the app (kept for next time).
        #[arg(long)]
        name: Option<String>,
    },
    /// The catalog: what each model needs, whether it fits this GPU, what is downloaded.
    Models {
        /// Also list models that don't fit this GPU.
        #[arg(long)]
        all: bool,
    },
    /// Download a model (verified, resumable). A catalog id or a unique part of one.
    Download { model: String },
    /// Select the model the server starts.
    Use { model: String },
    /// Set the context window in tokens (e.g. 32768).
    Context { tokens: u32 },
    /// Let models with a long-context mode (YaRN) go past their native window: on or off.
    LongContext { mode: String },
    /// Start serving the selected model.
    Start {
        /// Start although the model doesn't fit the free VRAM. It may fail
        /// to load, or crash when it runs out of GPU memory.
        #[arg(long)]
        force: bool,
    },
    /// Stop serving.
    Stop,
    /// A new pairing code for a client (`oppx pair <server> <code>`).
    Pair,
    /// Paired clients.
    Devices,
    /// Revoke a paired client (by name or id).
    Revoke { device: String },
    /// The backend, this server's role, its host or members, and pending requests.
    Status,
    /// Other Openphalanx servers on this network that this machine can reach.
    Servers,
    /// Invite a server (name or id) into this machine's cluster.
    Invite { server: String },
    /// Ask a server (name or id) to become host of this machine and its members.
    MakeHost { server: String },
    /// Accept a pending invitation or host request (by host name or id; the only one if omitted).
    Approve { from: Option<String> },
    /// Refuse a pending invitation or host request.
    Decline { from: String },
    /// Remove a member from this machine's cluster.
    Remove { member: String },
    /// Leave the cluster this machine is a member of.
    Leave,
    /// End the cluster this machine hosts (every member is removed).
    Dissolve,
    /// How the cluster uses its GPUs: `split` (one model across the servers,
    /// for models too big for one machine; the default) or `replicas` (a full
    /// copy per server, for more concurrent users).
    Strategy { strategy: String },
    /// The model this cluster serves: members download it if they don't have
    /// it. A catalog id (e.g. Qwen/Qwen2.5-Coder-14B-Instruct-AWQ), or `none`.
    /// (The app sets this when its server starts.)
    Model { model: String },
    /// The backend's log, or a cluster member's recent lines.
    Logs {
        member: Option<String>,
        /// Lines of the backend log to show.
        #[arg(short = 'n', long, default_value_t = 200)]
        lines: u32,
    },
}

fn token_path() -> PathBuf {
    paths::cluster_dir().join("control.token")
}

// --------------------------------------------------------------------------
// The running server and its loopback control API
// --------------------------------------------------------------------------

fn authorized(headers: &HeaderMap, token: &str) -> bool {
    headers.get("authorization").and_then(|v| v.to_str().ok()) == Some(&format!("Bearer {token}"))
}

fn fail(status: StatusCode, e: impl std::fmt::Display) -> Response {
    (status, Json(serde_json::json!({ "error": e.to_string() }))).into_response()
}

type Ctl = (Arc<Cluster>, Arc<String>);

/// The control API's state for the backend routes.
#[derive(Clone)]
struct Backend {
    service: Arc<Service>,
    token: Arc<String>,
}

fn reply<T: serde::Serialize>(result: Result<T>) -> Response {
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => fail(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

async fn ctl_server(State(b): State<Backend>, headers: HeaderMap) -> Response {
    if !authorized(&headers, &b.token) {
        return fail(StatusCode::UNAUTHORIZED, "bad control token");
    }
    Json(b.service.status().await).into_response()
}

async fn ctl_server_action(State(b): State<Backend>, headers: HeaderMap, UrlPath(action): UrlPath<String>) -> Response {
    if !authorized(&headers, &b.token) {
        return fail(StatusCode::UNAUTHORIZED, "bad control token");
    }
    match action.as_str() {
        "start" => reply(b.service.start(false).await),
        "start-force" => reply(b.service.start(true).await),
        "stop" => reply(b.service.stop().await),
        "pair" => reply(b.service.new_pairing().await),
        _ => fail(StatusCode::NOT_FOUND, "unknown action"),
    }
}

async fn ctl_devices(State(b): State<Backend>, headers: HeaderMap) -> Response {
    if !authorized(&headers, &b.token) {
        return fail(StatusCode::UNAUTHORIZED, "bad control token");
    }
    reply(b.service.devices().await)
}

async fn ctl_revoke(State(b): State<Backend>, headers: HeaderMap, UrlPath(id): UrlPath<String>) -> Response {
    if !authorized(&headers, &b.token) {
        return fail(StatusCode::UNAUTHORIZED, "bad control token");
    }
    reply(b.service.revoke(&id).await)
}

#[derive(serde::Deserialize)]
struct ModelReq {
    model: Option<String>,
}

async fn ctl_model(State((c, token)): State<Ctl>, headers: HeaderMap, Json(req): Json<ModelReq>) -> Response {
    if !authorized(&headers, &token) {
        return fail(StatusCode::UNAUTHORIZED, "bad control token");
    }
    let spec = match req.model.as_deref() {
        None => None,
        Some(id) => match openphalanx_core::catalog::find(id) {
            Some(e) => Some(openphalanx_core::cluster::ModelSpec {
                key: format!("catalog:{}", e.id),
                label: format!("{} · {}", e.name, e.quant),
                repo: e.id.clone(),
                revision: e.revision.clone(),
                weight_bytes: e.weight_bytes,
            }),
            None => return fail(StatusCode::BAD_REQUEST, format!("{id} isn't in the catalog")),
        },
    };
    match c.set_desired_model(spec) {
        Ok(()) => Json(serde_json::json!({})).into_response(),
        Err(e) => fail(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

async fn ctl_logs(State((c, token)): State<Ctl>, headers: HeaderMap, UrlPath(id): UrlPath<String>) -> Response {
    if !authorized(&headers, &token) {
        return fail(StatusCode::UNAUTHORIZED, "bad control token");
    }
    Json(c.member_logs(&id)).into_response()
}

async fn ctl_status(State((c, token)): State<Ctl>, headers: HeaderMap) -> Response {
    if !authorized(&headers, &token) {
        return fail(StatusCode::UNAUTHORIZED, "bad control token");
    }
    Json(c.view()).into_response()
}

async fn ctl_action(State((c, token)): State<Ctl>, headers: HeaderMap, UrlPath((action, id)): UrlPath<(String, String)>) -> Response {
    if !authorized(&headers, &token) {
        return fail(StatusCode::UNAUTHORIZED, "bad control token");
    }
    let result: Result<serde_json::Value> = async {
        match action.as_str() {
            "invite" => c.invite(&id).await.map(|_| serde_json::json!({})),
            "make-host" => c.request_host(&id).await.map(|_| serde_json::json!({})),
            "approve-invite" => c.approve_invite(&id).await.map(|_| serde_json::json!({})),
            "approve-host" => c.approve_host_request(&id).await.map(|p| serde_json::json!({ "problems": p })),
            "decline" => {
                c.decline_invite(&id);
                c.decline_host_request(&id);
                Ok(serde_json::json!({}))
            }
            "remove" => match c.remove_member(&id)? {
                true => Ok(serde_json::json!({})),
                false => bail!("no such member"),
            },
            "leave" => c.leave().await.map(|_| serde_json::json!({})),
            "dissolve" => c.dissolve().map(|_| serde_json::json!({})),
            "strategy" => {
                let s: openphalanx_core::cluster::Strategy = serde_json::from_value(serde_json::json!(id))
                    .map_err(|_| anyhow::anyhow!("strategy must be `split` or `replicas`"))?;
                c.set_strategy(s).map(|_| serde_json::json!({}))
            }
            _ => bail!("unknown action"),
        }
    }
    .await;
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => fail(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

async fn run(name: Option<String>) -> Result<()> {
    let cluster = Cluster::open(&paths::cluster_dir())?;
    if let Some(n) = name {
        cluster.set_name(&n)?;
    }
    let token = Arc::new(hex::encode(rand::rng().random::<[u8; 32]>()));
    write_private(&token_path(), token.as_bytes())?;
    let service = Service::new(Some(cluster.clone()));
    let backend = Router::new()
        .route("/control/server", get(ctl_server))
        .route("/control/server/{action}", post(ctl_server_action))
        .route("/control/devices", get(ctl_devices))
        .route("/control/devices/{id}", axum::routing::delete(ctl_revoke))
        .with_state(Backend { service: service.clone(), token: token.clone() });
    let control = Router::new()
        .route("/control/status", get(ctl_status))
        .route("/control/{action}/{id}", post(ctl_action))
        .route("/control/model", post(ctl_model))
        .route("/control/logs/{id}", get(ctl_logs))
        .with_state((cluster.clone(), token))
        .merge(backend);
    tokio::spawn(service.clone().monitor());
    let listener = tokio::net::TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], CONTROL_PORT)))
        .await
        .with_context(|| format!("port {CONTROL_PORT} is in use; is oppxs or the OpenPhalanx app already running?"))?;
    tokio::spawn(async move { axum::serve(listener, control).await });

    println!("OpenPhalanx server \"{}\" ({})", cluster.name(), cluster.id());
    println!("  certificate  {}", openphalanx_core::cluster::short_fingerprint(cluster.fingerprint()));
    println!("  listening    {} (cluster) · UDP 9093 (discovery)", cluster.me().map(|p| p.url).unwrap_or_else(|| "no network address".into()));
    println!("  role         {}", describe_role(&cluster.role()));
    tokio::select! {
        r = cluster.clone().run() => r,
        _ = stop_signal() => {
            // Stops the backend too: nothing keeps the GPU without the server.
            service.shutdown().await;
            let _ = std::fs::remove_file(token_path());
            println!("Stopped.");
            Ok(())
        }
    }
}

fn write_private(path: &std::path::Path, bytes: &[u8]) -> Result<()> {
    std::fs::create_dir_all(path.parent().context("no parent")?)?;
    std::fs::write(path, bytes)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn describe_role(role: &Role) -> String {
    match role {
        Role::Standalone => "standalone (not in a cluster)".into(),
        Role::Host => "host of a cluster".into(),
        Role::Member { host, .. } => format!("member of {}'s cluster ({})", host.name, host.url),
    }
}

// --------------------------------------------------------------------------
// Control commands (talk to the running server)
// --------------------------------------------------------------------------

struct Control {
    client: reqwest::Client,
    token: String,
}

impl Control {
    fn connect() -> Result<Self> {
        let token = std::fs::read_to_string(token_path()).map_err(|_| {
            anyhow::anyhow!("the server isn't running here; start it with `oppxs run` (or `systemctl --user start oppxs`)")
        })?;
        Ok(Control { client: reqwest::Client::new(), token: token.trim().to_string() })
    }

    async fn view(&self) -> Result<ClusterView> {
        let resp = self
            .client
            .get(format!("http://127.0.0.1:{CONTROL_PORT}/control/status"))
            .bearer_auth(&self.token)
            .send()
            .await
            .context("the server isn't answering; start it with `oppxs run`")?;
        if !resp.status().is_success() {
            bail!("the server refused: HTTP {}", resp.status());
        }
        // ClusterView is Serialize-only in core; read it back as JSON.
        let v: serde_json::Value = resp.json().await?;
        serde_json::from_value(v).context("unexpected status reply")
    }

    /// Calls the control API and reads its JSON reply (or its error).
    async fn call<T: serde::de::DeserializeOwned>(&self, method: reqwest::Method, path: &str) -> Result<T> {
        let resp = self
            .client
            .request(method, format!("http://127.0.0.1:{CONTROL_PORT}/control/{path}"))
            .bearer_auth(&self.token)
            .send()
            .await
            .context("the server isn't answering; start it with `oppxs run`")?;
        let ok = resp.status().is_success();
        let v: serde_json::Value = resp.json().await.unwrap_or_default();
        if !ok {
            bail!("{}", v["error"].as_str().unwrap_or("failed"));
        }
        serde_json::from_value(v).context("unexpected reply from the server")
    }

    async fn server(&self) -> Result<ServerStatus> {
        self.call(reqwest::Method::GET, "server").await
    }

    async fn act(&self, action: &str, id: &str) -> Result<serde_json::Value> {
        let resp = self
            .client
            .post(format!("http://127.0.0.1:{CONTROL_PORT}/control/{action}/{id}"))
            .bearer_auth(&self.token)
            .send()
            .await?;
        let ok = resp.status().is_success();
        let v: serde_json::Value = resp.json().await.unwrap_or_default();
        if !ok {
            bail!("{}", v["error"].as_str().unwrap_or("failed"));
        }
        Ok(v)
    }
}

/// Finds an id by exact id or (unique) name.
fn pick<'a>(what: &str, wanted: &str, items: impl Iterator<Item = (&'a str, &'a str)>) -> Result<String> {
    let items: Vec<(&str, &str)> = items.collect();
    if let Some((id, _)) = items.iter().find(|(id, _)| *id == wanted) {
        return Ok(id.to_string());
    }
    let named: Vec<_> = items.iter().filter(|(_, name)| name.eq_ignore_ascii_case(wanted)).collect();
    match named.as_slice() {
        [(id, _)] => Ok(id.to_string()),
        [] => bail!("no {what} called \"{wanted}\""),
        _ => bail!("several {what}s are called \"{wanted}\"; use the id"),
    }
}

async fn control(cmd: Command) -> Result<()> {
    let ctl = Control::connect()?;
    let view = ctl.view().await?;
    let short = |fp: &str| openphalanx_core::cluster::short_fingerprint(fp);
    match cmd {
        Command::Start { force } => {
            let action = if force { "server/start-force" } else { "server/start" };
            if force && refuse_while_running("it").await.is_ok() {
                fit_context_for_force().await?;
            }
            ctl.call::<serde_json::Value>(reqwest::Method::POST, action).await?;
            // Follow it until it serves or fails (Ctrl-C only stops watching).
            let mut last = String::new();
            loop {
                let st = ctl.server().await?;
                let line = format!("{}{}", st.state, st.detail.as_deref().map(|d| format!(": {d}")).unwrap_or_default());
                if line != last {
                    println!("{line}");
                    last = line;
                }
                match st.state.as_str() {
                    "running" => {
                        println!("Serving {} at {}.", st.model.unwrap_or_default(), st.endpoint.unwrap_or_default());
                        println!("Pair a client: `oppxs pair`, then on the client `oppx pair <server> <code>`.");
                        break;
                    }
                    "error" | "stopped" => bail!("the server didn't start{}", st.detail.map(|d| format!(": {d}")).unwrap_or_default()),
                    _ => tokio::time::sleep(std::time::Duration::from_secs(2)).await,
                }
            }
        }
        Command::Stop => {
            ctl.call::<serde_json::Value>(reqwest::Method::POST, "server/stop").await?;
            println!("Stopped.");
        }
        Command::Pair => {
            let p: openphalanx_core::admin::Pairing = ctl.call(reqwest::Method::POST, "server/pair").await?;
            let st = ctl.server().await?;
            let server = st.endpoint.as_deref().and_then(|e| e.split(':').next()).unwrap_or("<server>").to_string();
            println!("Pairing code  {}  (single use, valid for 10 minutes)", p.code.unwrap_or_default());
            println!("Fingerprint   {}", st.fingerprint.unwrap_or_default());
            println!("On the client: oppx pair {server} <code>   and check that the fingerprint matches.");
        }
        Command::Devices => {
            let devices: Vec<openphalanx_core::admin::Device> = ctl.call(reqwest::Method::GET, "devices").await?;
            if devices.is_empty() {
                println!("No paired clients. Create a code with `oppxs pair`.");
            }
            let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64());
            for d in devices {
                let expires = match d.expires_at {
                    None => "never expires".to_string(),
                    Some(t) if t <= now => "expired".to_string(),
                    Some(t) => format!("expires in {:.0} d", ((t - now) / 86400.0).round()),
                };
                let requests = if d.requests == 1 { "request " } else { "requests" };
                println!("{:<24} {:<14} {:>6} {requests}  {expires}", d.name, d.id, d.requests);
            }
        }
        Command::Revoke { device } => {
            let devices: Vec<openphalanx_core::admin::Device> = ctl.call(reqwest::Method::GET, "devices").await?;
            let id = pick("device", &device, devices.iter().map(|d| (d.id.as_str(), d.name.as_str())))?;
            ctl.call::<serde_json::Value>(reqwest::Method::DELETE, &format!("devices/{id}")).await?;
            println!("Revoked {device}; it needs a new pairing code to connect again.");
        }
        Command::Status => {
            let st = ctl.server().await?;
            println!("server       {}{}", st.state, st.detail.as_deref().map(|d| format!(" ({d})")).unwrap_or_default());
            if let Some(m) = &st.model {
                println!("model        {m} · {} context", st.context_len.map(|c| format!("{}k", c / 1024)).unwrap_or_default());
            }
            if st.state == "running" {
                println!("clients      {} paired · pair at {}", st.devices, st.endpoint.as_deref().unwrap_or("?"));
            }
            println!("{} ({}) · {}", view.name, view.id, describe_role(&view.role));
            println!("certificate  {}", short(&view.fingerprint));
            println!("strategy     {}", serde_json::to_value(view.strategy)?.as_str().unwrap_or("?"));
            if let Some(m) = &view.desired_model {
                println!("model        {} ({}@{})", m.label, m.repo, &m.revision[..m.revision.len().min(7)]);
            }
            if let Some(s) = &view.model_sync {
                println!("model copy   {}: {}{}", s.label, s.state, s.error.as_deref().map(|e| format!(" ({e})")).unwrap_or_default());
            }
            if let Some(link) = &view.host_link {
                println!("host link    {}", if link.connected { "connected".to_string() } else { link.error.clone().unwrap_or_else(|| "connecting…".into()) });
            }
            for m in &view.members {
                let gpus = m.report.as_ref().map(|r| r.inventory.gpus.iter().map(|g| g.name.clone()).collect::<Vec<_>>().join(", ")).unwrap_or_default();
                println!("member       {} {} ({}) {}", if m.online { "●" } else { "○" }, m.name, m.id, gpus);
                if let Some(s) = m.report.as_ref().and_then(|r| r.model_sync.as_ref()) {
                    let progress = if s.state == "downloading" && s.total_bytes > 0 {
                        format!(" {}% · {:.0} MB/s", 100 * s.done_bytes / s.total_bytes, s.bytes_per_sec / 1e6)
                    } else {
                        String::new()
                    };
                    println!("               model {}: {}{progress}{}", s.label, s.state, s.error.as_deref().map(|e| format!(" ({e})")).unwrap_or_default());
                }
            }
            for i in &view.invites {
                println!("invitation   from {} ({}) · certificate {} · `oppxs approve {}`", i.host.name, i.host.url, short(&i.host.fingerprint), i.host.name);
            }
            for r in &view.host_requests {
                println!("host request from {} for {} server(s) · certificate {} · `oppxs approve {}`", r.from.name, r.members.len(), short(&r.from.fingerprint), r.from.name);
            }
            if let Some(e) = &view.discovery_error {
                println!("discovery    {e}");
            }
        }
        Command::Servers => {
            if view.candidates.is_empty() {
                println!("No other reachable Openphalanx servers on this network.");
            }
            for c in &view.candidates {
                println!("{:<24} {:<18} {:<28} {} {}", c.name, c.id, c.url, short(&c.fingerprint), if c.busy { "(in another cluster)" } else { "" });
            }
        }
        Command::Invite { server } => {
            let id = pick("server", &server, view.candidates.iter().map(|c| (c.id.as_str(), c.name.as_str())))?;
            ctl.act("invite", &id).await?;
            println!("Invited {server}. It joins once it approves (`oppxs approve` there, or in its app).");
        }
        Command::MakeHost { server } => {
            // Another server on the network, or one of this cluster's members.
            let all = view.candidates.iter().map(|c| (c.id.as_str(), c.name.as_str())).chain(view.members.iter().map(|m| (m.id.as_str(), m.name.as_str())));
            let id = pick("server", &server, all)?;
            ctl.act("make-host", &id).await?;
            println!("Asked {server} to host. Once it approves, this machine and its members move to it.");
        }
        Command::Approve { from } => {
            let invites = view.invites.iter().map(|i| (i.host.id.as_str(), i.host.name.as_str(), "invite"));
            let requests = view.host_requests.iter().map(|r| (r.from.id.as_str(), r.from.name.as_str(), "host"));
            let all: Vec<_> = invites.chain(requests).collect();
            let (id, kind) = match from {
                Some(f) => {
                    let id = pick("request from", &f, all.iter().map(|(id, name, _)| (*id, *name)))?;
                    let kind = all.iter().find(|(i, _, _)| *i == id).map(|(_, _, k)| *k).unwrap_or("invite");
                    (id, kind)
                }
                None => match all.as_slice() {
                    [(id, _, kind)] => (id.to_string(), *kind),
                    [] => bail!("nothing is waiting for approval"),
                    _ => bail!("several requests are waiting; name the one to approve"),
                },
            };
            if kind == "host" {
                let v = ctl.act("approve-host", &id).await?;
                println!("This machine is now the host; invitations were sent.");
                for p in v["problems"].as_array().into_iter().flatten() {
                    println!("  ! {}", p.as_str().unwrap_or_default());
                }
            } else {
                ctl.act("approve-invite", &id).await?;
                println!("Joined. This machine is now a member; clients pair with the host.");
            }
        }
        Command::Decline { from } => {
            let all = view.invites.iter().map(|i| (i.host.id.as_str(), i.host.name.as_str())).chain(view.host_requests.iter().map(|r| (r.from.id.as_str(), r.from.name.as_str())));
            let id = pick("request from", &from, all)?;
            ctl.act("decline", &id).await?;
            println!("Declined.");
        }
        Command::Remove { member } => {
            let id = pick("member", &member, view.members.iter().map(|m| (m.id.as_str(), m.name.as_str())))?;
            ctl.act("remove", &id).await?;
            println!("Removed {member}; it is standalone again.");
        }
        Command::Leave => {
            ctl.act("leave", "-").await?;
            println!("Left the cluster; this machine is standalone.");
        }
        Command::Dissolve => {
            ctl.act("dissolve", "-").await?;
            println!("Cluster dissolved; its members are standalone again.");
        }
        Command::Model { model } => {
            let body = if model.eq_ignore_ascii_case("none") { serde_json::json!({ "model": null }) } else { serde_json::json!({ "model": model }) };
            let resp = ctl.client.post(format!("http://127.0.0.1:{CONTROL_PORT}/control/model")).bearer_auth(&ctl.token).json(&body).send().await?;
            if !resp.status().is_success() {
                let v: serde_json::Value = resp.json().await.unwrap_or_default();
                bail!("{}", v["error"].as_str().unwrap_or("failed"));
            }
            println!("Cluster model set; members without it start downloading on their next report.");
        }
        Command::Logs { member, .. } => {
            let member = member.expect("the backend's log is handled before connecting");
            let id = pick("member", &member, view.members.iter().map(|m| (m.id.as_str(), m.name.as_str())))?;
            let lines: Vec<String> = ctl
                .client
                .get(format!("http://127.0.0.1:{CONTROL_PORT}/control/logs/{id}"))
                .bearer_auth(&ctl.token)
                .send()
                .await?
                .json()
                .await?;
            for l in lines {
                println!("{l}");
            }
        }
        Command::Strategy { strategy } => {
            ctl.act("strategy", &strategy.to_lowercase()).await?;
            println!("Strategy set to {}.", strategy.to_lowercase());
        }
        Command::Run { .. }
        | Command::Models { .. }
        | Command::Download { .. }
        | Command::Use { .. }
        | Command::Context { .. }
        | Command::LongContext { .. } => unreachable!("handled in main"),
    }
    Ok(())
}

// --------------------------------------------------------------------------
// Models and settings (no running server needed)
// --------------------------------------------------------------------------

/// A catalog entry by exact id, or by a part of the id that matches only one.
fn find_model(wanted: &str) -> Result<catalog::CatalogEntry> {
    let all = catalog::catalog();
    if let Some(e) = all.iter().find(|e| e.id.eq_ignore_ascii_case(wanted)) {
        return Ok(e.clone());
    }
    let w = wanted.to_lowercase();
    let hits: Vec<_> = all.iter().filter(|e| e.id.to_lowercase().contains(&w)).collect();
    match hits.as_slice() {
        [e] => Ok((*e).clone()),
        [] => bail!("no model matches \"{wanted}\"; see `oppxs models --all`"),
        many => {
            let ids = many.iter().take(8).map(|e| e.id.as_str()).collect::<Vec<_>>().join("\n  ");
            bail!("\"{wanted}\" matches {} models; be more specific:\n  {ids}", many.len())
        }
    }
}

async fn models(all: bool) -> Result<()> {
    let settings = Settings::load();
    let gpu = gpu::query().await.unwrap_or_default().into_iter().find(|g| g.index == settings.gpu_index);
    match &gpu {
        Some(g) => println!("{} · {} free · context {}k", g.name, vram::fmt_gib(g.free_bytes), settings.context_len / 1024),
        None => println!("No NVIDIA GPU found; fits can't be checked."),
    }
    println!("{:<52} {:>9} {:>9}  {:<10} state", "model", "download", "VRAM", "fit");
    let mut hidden = 0;
    for e in catalog::catalog() {
        let key = server::catalog_key(&e.id);
        let Some(m) = server::resolve(&settings, &key) else { continue };
        let req = m.requirement(settings.context_len, gpu.as_ref().and_then(|g| g.compute_capability));
        let fit = gpu.as_ref().map(|g| vram::check(&req, g.free_bytes).fit);
        if !all && fit == Some(vram::Fit::Insufficient) && m.installed_dir.is_none() {
            hidden += 1;
            continue;
        }
        let fit = match fit {
            Some(vram::Fit::Ok) => "fits",
            Some(vram::Fit::Tight) => "tight",
            Some(vram::Fit::Insufficient) => "won't fit",
            None => "?",
        };
        let state = match (settings.selected_model.as_deref() == Some(key.as_str()), m.installed_dir.is_some()) {
            // Damaged files: the server can't start with it (the app shows "Broken").
            (true, true) if m.broken.is_some() => "selected, broken: download it again",
            (false, true) if m.broken.is_some() => "broken: download it again",
            (true, true) => "selected",
            (true, false) => "selected, not downloaded",
            (false, true) => "downloaded",
            (false, false) => "",
        };
        println!("{:<52} {:>9} {:>9}  {:<10} {state}", e.id, vram::fmt_gib(e.weight_bytes), vram::fmt_gib(req.total_bytes), fit);
    }
    if hidden > 0 {
        println!("{hidden} more don't fit this GPU at this context (`oppxs models --all`).");
    }
    Ok(())
}

async fn download_model(wanted: &str) -> Result<()> {
    let e = find_model(wanted)?;
    let dest = model::app_model_dir(&e.id);
    println!("Downloading {} ({}) to {}", e.id, vram::fmt_gib(e.weight_bytes), dest.display());
    let client = download::client();
    let files = download::list_files(&client, &e.id, &e.revision).await?;
    let cancel: download::Cancel = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let mut last = std::time::Instant::now() - std::time::Duration::from_secs(5);
    download::download(&client, &e.id, &e.revision, &files, &dest, cancel, |p| {
        // One line a second is enough for a terminal or a log.
        if last.elapsed().as_secs() >= 1 && p.total_bytes > 0 {
            last = std::time::Instant::now();
            let what = if p.bytes_per_sec > 0.0 { format!("{:.0} MB/s", p.bytes_per_sec / 1e6) } else { p.current_file.clone() };
            println!("  {:>3}%  {} of {}  {what}", 100 * p.done_bytes / p.total_bytes, vram::fmt_gib(p.done_bytes), vram::fmt_gib(p.total_bytes));
        }
    })
    .await?;
    println!("Done. Select it with `oppxs use {}`.", e.id);
    Ok(())
}

fn use_model(wanted: &str) -> Result<()> {
    let e = find_model(wanted)?;
    let mut settings = Settings::load();
    settings.selected_model = Some(server::catalog_key(&e.id));
    settings.save()?;
    let downloaded = model::find_installed(&e.id, Some(&e.revision)).is_some();
    println!("Selected {}.{}", e.id, if downloaded { " Start it with `oppxs start`.".to_string() } else { format!(" It isn't downloaded yet: `oppxs download {}`.", e.id) });
    Ok(())
}

/// The running model keeps the settings it started with (as in the app).
async fn refuse_while_running(what: &str) -> Result<()> {
    if openphalanx_core::docker::inspect().await.ok().flatten().is_some_and(|c| c.state.running) {
        bail!("stop the server to change {what} (`oppxs stop`); it applies at the next start");
    }
    Ok(())
}

/// The selected model, and the longest context it fits in the VRAM free on
/// this GPU now (`None`: not even the minimum, or no GPU to measure).
async fn context_fit(settings: &Settings) -> Option<(server::ResolvedModel, Option<u32>)> {
    let m = server::resolve(settings, settings.selected_model.as_deref()?)?;
    let gpu = gpu::query().await.unwrap_or_default().into_iter().find(|g| g.index == settings.gpu_index);
    let fit = gpu.and_then(|g| vram::max_fitting_context(m.max_context, g.free_bytes, |c| m.requirement(c, g.compute_capability)));
    Some((m, fit))
}

/// The selected model's long-context mode from the catalog, on or not.
fn catalog_yarn(settings: &Settings) -> Option<catalog::Yarn> {
    catalog::find(settings.selected_model.as_deref()?.strip_prefix("catalog:")?)?.yarn
}

/// The same limits as the app's slider: the model's own maximum, and what
/// fits the VRAM free now.
async fn set_context(tokens: u32) -> Result<()> {
    if !(2048..=262_144).contains(&tokens) {
        bail!("the context must be between 2,048 and 262,144 tokens");
    }
    refuse_while_running("the context window").await?;
    let mut settings = Settings::load();
    if let Some((m, fit)) = context_fit(&settings).await {
        if tokens > m.max_context {
            let more = match catalog_yarn(&settings) {
                Some(y) if !settings.long_context => {
                    format!(" `oppxs long-context on` allows up to {} (recall is weaker past {}).", y.max_context(), y.original_max)
                }
                _ => String::new(),
            };
            bail!("{} supports up to {} tokens of context.{more}", m.label, m.max_context);
        }
        if let Some(max) = fit.filter(|max| tokens > *max) {
            bail!("{} fits up to {max} tokens of context in the VRAM free on this GPU; a longer context would run out of memory", m.label);
        }
    }
    settings.context_len = tokens;
    settings.save()?;
    println!("Context window set to {tokens} tokens; it applies the next time the server starts.");
    Ok(())
}

async fn set_long_context(mode: &str) -> Result<()> {
    let on = match mode {
        "on" => true,
        "off" => false,
        _ => bail!("use `oppxs long-context on` or `oppxs long-context off`"),
    };
    refuse_while_running("the long-context mode").await?;
    let mut settings = Settings::load();
    settings.long_context = on;
    let yarn = catalog_yarn(&settings);
    match (on, yarn) {
        (true, Some(y)) => println!(
            "Long-context mode on: the selected model can go up to {} tokens (`oppxs context <tokens>`). Past {} it finds details in the prompt about half as reliably in our tests; at {} and below nothing changes.",
            y.max_context(), y.original_max, y.original_max
        ),
        (true, None) => println!("Long-context mode on. The selected model has no such mode (YaRN), so nothing changes for it."),
        (false, Some(y)) if settings.context_len > y.original_max => {
            // As in the app: back into the native window.
            settings.context_len = y.original_max;
            println!("Long-context mode off; the context window is back to {} tokens, the selected model's native window.", y.original_max);
        }
        (false, _) => println!("Long-context mode off: models keep their native window."),
    }
    settings.save()?;
    Ok(())
}

/// `start --force`, as ticking "Force load" in the app: the context window
/// comes down to the longest the model fits now, or to the minimum when none
/// does, so the server isn't asked for a window the GPU can't hold.
async fn fit_context_for_force() -> Result<()> {
    let mut settings = Settings::load();
    let Some((m, fit)) = context_fit(&settings).await else { return Ok(()) };
    let now = m.context_len(settings.context_len);
    let target = now.min(fit.unwrap_or(vram::MIN_CONTEXT));
    if target < now {
        settings.context_len = target;
        settings.save()?;
        match fit {
            Some(_) => println!("Context window set to {target} tokens, the longest {} fits right now.", m.label),
            None => println!("Context window set to {target} tokens, the minimum: {} doesn't fit this GPU by our estimate.", m.label),
        }
    }
    Ok(())
}

/// The backend's own log (SGLang and the gateway), straight from Docker.
async fn backend_logs(lines: u32) -> Result<()> {
    let text = openphalanx_core::docker::logs_tail(lines).await?;
    if text.trim().is_empty() {
        println!("No backend log yet; start the server with `oppxs start`.");
    }
    print!("{text}");
    Ok(())
}

#[tokio::main]
async fn main() {
    // Output piped into `head` or `less` ends quietly instead of panicking.
    unsafe { libc::signal(libc::SIGPIPE, libc::SIG_DFL) };
    let cli = Cli::parse();
    let result = match cli.command {
        Command::Run { name } => run(name).await,
        Command::Models { all } => models(all).await,
        Command::Download { model } => download_model(&model).await,
        Command::Use { model } => use_model(&model),
        Command::Context { tokens } => set_context(tokens).await,
        Command::LongContext { mode } => set_long_context(&mode).await,
        Command::Logs { member: None, lines } => backend_logs(lines).await,
        other => control(other).await,
    };
    if let Err(e) = result {
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}

/// Ctrl-C, or SIGTERM (`systemctl stop`, `kill`): both leave cleanly, so a
/// member says goodbye and stops its split worker.
async fn stop_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    match signal(SignalKind::terminate()) {
        Ok(mut term) => {
            tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
        }
        Err(_) => {
            let _ = tokio::signal::ctrl_c().await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn models_are_found_by_id_or_a_unique_part() {
        let exact = find_model("Qwen/Qwen2.5-Coder-14B-Instruct-AWQ").unwrap();
        assert_eq!(exact.id, "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ");
        assert_eq!(find_model("qwen/qwen2.5-coder-14b-instruct-awq").unwrap().id, exact.id, "case doesn't matter");
        assert_eq!(find_model("gpt-oss-20b").unwrap().id, "openai/gpt-oss-20b");
        let several = find_model("Coder-14B").unwrap_err().to_string();
        assert!(several.contains("matches 2 models") && several.contains(&exact.id), "{several}");
        assert!(find_model("no-such-model").unwrap_err().to_string().contains("no model matches"));
    }

    #[test]
    fn names_resolve_to_ids() {
        let items = [("id1", "laptop"), ("id2", "Rig"), ("id3", "rig")];
        assert_eq!(pick("device", "id2", items.iter().copied()).unwrap(), "id2");
        assert_eq!(pick("device", "LAPTOP", items.iter().copied()).unwrap(), "id1");
        assert!(pick("device", "rig", items.iter().copied()).unwrap_err().to_string().contains("several"));
        assert!(pick("device", "none", items.iter().copied()).is_err());
    }
}
