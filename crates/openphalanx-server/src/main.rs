//! `openphalanx-server`: an Openphalanx server without the desktop app.
//!
//!   openphalanx-server run                 run the server (discovery, cluster API, reports)
//!   openphalanx-server status              this server's role, its host or members, pending requests
//!   openphalanx-server servers             other servers on this network that are reachable
//!   openphalanx-server invite <server>     invite a server into this cluster (this machine hosts)
//!   openphalanx-server make-host <server>  ask a server to host this machine and its members
//!   openphalanx-server approve [<host>]    accept an invitation or a host request
//!   openphalanx-server decline <host>      refuse one
//!   openphalanx-server remove <member>     remove a member (as host)
//!   openphalanx-server leave | dissolve    leave this cluster (member) / end it (host)
//!
//! It shares its state (`~/.local/share/openphalanx/cluster`) with the app, so
//! run one or the other on a machine, not both. The control commands talk to
//! the running server over 127.0.0.1:9094 with a per-start token readable only
//! by this user (`control.token`).

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
use openphalanx_core::paths;
use rand::Rng;

const CONTROL_PORT: u16 = 9094;

#[derive(Parser)]
#[command(name = "openphalanx-server", version, about = "Run an Openphalanx server without the desktop app")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Run the server until stopped (Ctrl-C).
    Run {
        /// Name shown to other servers and in the app (kept for next time).
        #[arg(long)]
        name: Option<String>,
    },
    /// This server's role, host or members, and pending requests.
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
    /// A member's recent log lines (its backend and cluster events).
    Logs { member: String },
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
    let control = Router::new()
        .route("/control/status", get(ctl_status))
        .route("/control/{action}/{id}", post(ctl_action))
        .route("/control/model", post(ctl_model))
        .route("/control/logs/{id}", get(ctl_logs))
        .with_state((cluster.clone(), token));
    let listener = tokio::net::TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], CONTROL_PORT)))
        .await
        .with_context(|| format!("port {CONTROL_PORT} is in use; is another openphalanx-server running?"))?;
    tokio::spawn(async move { axum::serve(listener, control).await });

    println!("Openphalanx server \"{}\" ({})", cluster.name(), cluster.id());
    println!("  certificate  {}", openphalanx_core::cluster::short_fingerprint(cluster.fingerprint()));
    println!("  listening    {} (cluster) · UDP 9093 (discovery)", cluster.me().map(|p| p.url).unwrap_or_else(|| "no network address".into()));
    println!("  role         {}", describe_role(&cluster.role()));
    tokio::select! {
        r = cluster.clone().run() => r,
        _ = tokio::signal::ctrl_c() => {
            cluster.goodbye().await;
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
        let token = std::fs::read_to_string(token_path())
            .context("the server isn't running here; start it with `openphalanx-server run`")?;
        Ok(Control { client: reqwest::Client::new(), token: token.trim().to_string() })
    }

    async fn view(&self) -> Result<ClusterView> {
        let resp = self
            .client
            .get(format!("http://127.0.0.1:{CONTROL_PORT}/control/status"))
            .bearer_auth(&self.token)
            .send()
            .await
            .context("the server isn't answering; start it with `openphalanx-server run`")?;
        if !resp.status().is_success() {
            bail!("the server refused: HTTP {}", resp.status());
        }
        // ClusterView is Serialize-only in core; read it back as JSON.
        let v: serde_json::Value = resp.json().await?;
        serde_json::from_value(v).context("unexpected status reply")
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
        Command::Status => {
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
                println!("invitation   from {} ({}) · certificate {} · `openphalanx-server approve {}`", i.host.name, i.host.url, short(&i.host.fingerprint), i.host.name);
            }
            for r in &view.host_requests {
                println!("host request from {} for {} server(s) · certificate {} · `openphalanx-server approve {}`", r.from.name, r.members.len(), short(&r.from.fingerprint), r.from.name);
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
            println!("Invited {server}. It joins once it approves (`openphalanx-server approve` there, or in its app).");
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
        Command::Logs { member } => {
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
        Command::Run { .. } => unreachable!(),
    }
    Ok(())
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    let result = match cli.command {
        Command::Run { name } => run(name).await,
        other => control(other).await,
    };
    if let Err(e) = result {
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}
