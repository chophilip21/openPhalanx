//! Who holds the server's ports, and stopping our own leftovers.
//!
//! The app and `oppxs` are the same server and share its ports, so only one
//! can run on a machine. A copy left behind (an `oppxs` still running when the
//! app opens, a second app) keeps the ports: the new one can't serve the
//! cluster API or discovery and stays invisible to the other machines. Waiting
//! doesn't fix that; this finds the holder and, on request, stops it.
//!
//! Only OpenPhalanx's own processes are ever stopped. Anything else on these
//! ports is named, for the user to deal with.

use std::time::Duration;

use anyhow::{Context, Result};
use serde::Serialize;
use tokio::process::Command;

/// The `oppxs` control API's default port (`settings.control_port` moves it).
pub const CONTROL_PORT: u16 = 9094;

/// The ports a server process binds itself (the backend container's client
/// and admin ports belong to Docker and are handled by stopping the server).
fn server_ports(control_port: u16) -> [(u16, &'static str, &'static str); 3] {
    [
        (crate::cluster::CLUSTER_PORT, "tcp", "cluster API"),
        (crate::cluster::DISCOVERY_PORT, "udp", "discovery"),
        (control_port, "tcp", "oppxs control"),
    ]
}
/// Process names of this project's servers (`ss` cuts names at 15 characters).
const OURS: [&str; 3] = ["oppxs", "openphalanx", "openphalanx-serv"];

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Holder {
    pub port: u16,
    pub proto: String,
    /// What the port is for.
    pub purpose: String,
    /// None when the process belongs to another user (its pid isn't visible).
    pub pid: Option<u32>,
    pub name: String,
    /// One of this project's servers: safe for `free` to stop.
    pub ours: bool,
}

impl Holder {
    pub fn describe(&self) -> String {
        match self.pid {
            Some(pid) => format!("{} (pid {pid}) holds {} {} ({})", self.name, self.proto, self.port, self.purpose),
            None => format!("another user's process holds {} {} ({}); `sudo ss -ltnup` shows which", self.proto, self.port, self.purpose),
        }
    }
}

/// Parses `ss -H -ltnup`: one listening socket per line, with
/// `users:(("name",pid=1,fd=2),…)` for this user's processes.
pub fn parse_ss(out: &str, me: u32) -> Vec<Holder> {
    parse_ss_with(out, me, CONTROL_PORT)
}

fn parse_ss_with(out: &str, me: u32, control_port: u16) -> Vec<Holder> {
    let server_ports = server_ports(control_port);
    let mut found: Vec<Holder> = Vec::new();
    for line in out.lines() {
        let mut cols = line.split_whitespace();
        let (Some(proto), Some(local)) = (cols.next(), cols.nth(3)) else { continue };
        let Some(port) = local.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()) else { continue };
        let Some((_, _, purpose)) = server_ports.iter().find(|(p, pr, _)| *p == port && *pr == proto) else { continue };
        let users = line.split_once("users:(").map(|(_, u)| u).unwrap_or("");
        let mut any = false;
        for part in users.split("(\"").skip(1) {
            let Some((name, rest)) = part.split_once('"') else { continue };
            let pid = rest.split_once("pid=").and_then(|(_, r)| r.split(|c: char| !c.is_ascii_digit()).next()).and_then(|p| p.parse::<u32>().ok());
            any = true;
            if pid == Some(me) {
                continue;
            }
            let holder = Holder { port, proto: proto.into(), purpose: (*purpose).into(), pid, name: name.into(), ours: OURS.contains(&name) };
            if !found.contains(&holder) {
                found.push(holder);
            }
        }
        if !any {
            let holder = Holder { port, proto: proto.into(), purpose: (*purpose).into(), pid: None, name: "unknown".into(), ours: false };
            if !found.contains(&holder) {
                found.push(holder);
            }
        }
    }
    found
}

/// The three ports Settings can move (all on this machine; the cluster's
/// ports must match on every server, so they stay fixed).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Movable {
    Agent,
    Admin,
    Control,
}

impl Movable {
    pub fn label(self) -> &'static str {
        match self {
            Movable::Agent => "the client API",
            Movable::Admin => "the admin API",
            Movable::Control => "the oppxs control API",
        }
    }
}

/// Fixed ports nothing may move onto: the cluster's, and the split model's
/// (on the host's network, so they would collide).
pub fn reserved(port: u16) -> Option<&'static str> {
    match port {
        crate::cluster::CLUSTER_PORT => Some("the cluster API"),
        crate::cluster::DISCOVERY_PORT => Some("discovery"),
        crate::split::SGLANG_HOST_PORT => Some("SGLang in a split model"),
        crate::docker::SEARXNG_LOOPBACK_PORT => Some("SearXNG in a split model"),
        p if (crate::split::DIST_PORT..=crate::split::DIST_PORT + 16).contains(&p) => Some("the split model's rendezvous"),
        _ => None,
    }
}

/// Why `which` can't move to `port`, given where the other movable ports are
/// now (`settings`), or None when it can.
pub fn port_problem(which: Movable, port: u16, settings: &crate::settings::Settings) -> Option<String> {
    if port < 1024 {
        return Some("Use a port from 1024 up: lower ones need root.".into());
    }
    if let Some(what) = reserved(port) {
        return Some(format!("Port {port} is used by {what}. Choose another."));
    }
    let others = [(Movable::Agent, settings.agent_port), (Movable::Admin, settings.admin_port), (Movable::Control, settings.control_port)];
    others
        .iter()
        .find(|(kind, p)| *kind != which && *p == port)
        .map(|(kind, _)| format!("Port {port} is already {}'s. Choose another.", kind.label()))
}

/// One port the server uses, and who is listening on it now.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PortInfo {
    pub port: u16,
    /// For a range, its last port (the split model's rendezvous uses 17).
    pub last: Option<u16>,
    pub proto: String,
    pub purpose: String,
    /// Who connects to it: "clients", "this machine", "cluster".
    pub reach: String,
    /// When it is open: "while serving", "while the app runs", "split model only".
    pub when: String,
    /// Which setting moves it (None: fixed).
    pub setting: Option<String>,
    /// Why it can't move, for a fixed port.
    pub fixed_because: Option<String>,
    /// The processes listening on it now, by name ("this app", "Docker", …);
    /// "not visible" for another user's process (the backend container's own,
    /// on a split model's host network).
    pub holders: Vec<String>,
}

const PROXY: &str = "docker-proxy";

/// A listening socket: protocol, port, and the processes (name, pid) when visible.
type Listener = (String, u16, Vec<(String, Option<u32>)>);
/// A port the server uses: port, last of a range, protocol, purpose, reach, when,
/// and the setting that moves it or why it can't move.
type PortRow = (u16, Option<u16>, &'static str, &'static str, &'static str, &'static str, Result<&'static str, &'static str>);

/// Every listening socket in `ss -H -ltnup` output.
fn listeners(out: &str) -> Vec<Listener> {
    let mut found = Vec::new();
    for line in out.lines() {
        let mut cols = line.split_whitespace();
        let (Some(proto), Some(local)) = (cols.next(), cols.nth(3)) else { continue };
        let Some(port) = local.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()) else { continue };
        let users = line.split_once("users:(").map(|(_, u)| u).unwrap_or("");
        let procs = users
            .split("(\"")
            .skip(1)
            .filter_map(|part| {
                let (name, rest) = part.split_once('"')?;
                let pid = rest.split_once("pid=").and_then(|(_, r)| r.split(|c: char| !c.is_ascii_digit()).next()).and_then(|p| p.parse().ok());
                Some((name.to_string(), pid))
            })
            .collect();
        found.push((proto.to_string(), port, procs));
    }
    found
}

/// The ports a server with these settings uses, from `ss` output.
pub fn overview_from(out: &str, me: u32, settings: &crate::settings::Settings) -> Vec<PortInfo> {
    let dist = crate::split::DIST_PORT;
    const CLUSTER: &str = "Every server in a cluster finds and reaches the others on this port, so it is the same on all of them.";
    const SPLIT: &str = "Fixed for now: a split model's servers must agree on it.";
    let rows: [PortRow; 8] = [
        (settings.agent_port, None, "tcp", "Client API (the gateway: pairing, chat, search; TLS)", "clients", "while serving", Ok("agent_port")),
        (settings.admin_port, None, "tcp", "Admin API (this app talks to the backend)", "this machine", "while serving", Ok("admin_port")),
        (crate::cluster::CLUSTER_PORT, None, "tcp", "Cluster API (servers on the network; TLS)", "cluster", "while the app runs", Err(CLUSTER)),
        (crate::cluster::DISCOVERY_PORT, None, "udp", "Discovery (LAN broadcast beacons)", "cluster", "while the app runs", Err(CLUSTER)),
        (settings.control_port, None, "tcp", "oppxs control API (headless server commands)", "this machine", "while oppxs runs", Ok("control_port")),
        (crate::split::SGLANG_HOST_PORT, None, "tcp", "SGLang, behind the gateway", "this machine", "split model only", Err(SPLIT)),
        (crate::docker::SEARXNG_LOOPBACK_PORT, None, "tcp", "SearXNG, behind the gateway", "this machine", "split model only", Err(SPLIT)),
        (dist, Some(dist + 16), "tcp", "Rendezvous between the split model's servers", "cluster", "split model only", Err(SPLIT)),
    ];
    let all = listeners(out);
    rows.iter()
        .map(|&(port, last, proto, purpose, reach, when, movable)| {
            let mut holders: Vec<String> = Vec::new();
            for (p, at, procs) in &all {
                if p != proto || *at < port || *at > last.unwrap_or(port) {
                    continue;
                }
                let names: Vec<String> = if procs.is_empty() {
                    vec!["not visible (another user's process)".into()]
                } else {
                    procs
                        .iter()
                        .map(|(name, pid)| match (name.as_str(), pid) {
                            (_, Some(pid)) if *pid == me => "this app".to_string(),
                            (PROXY, _) => "Docker (a container's published port)".to_string(),
                            (n, Some(pid)) => format!("{n} (pid {pid})"),
                            (n, None) => n.to_string(),
                        })
                        .collect()
                };
                for n in names {
                    if !holders.contains(&n) {
                        holders.push(n);
                    }
                }
            }
            PortInfo {
                port,
                last,
                proto: proto.into(),
                purpose: purpose.into(),
                reach: reach.into(),
                when: when.into(),
                setting: movable.ok().map(str::to_string),
                fixed_because: movable.err().map(str::to_string),
                holders,
            }
        })
        .collect()
}

/// The server's ports and who holds them now.
pub async fn overview(settings: &crate::settings::Settings) -> Result<Vec<PortInfo>> {
    let out = Command::new("ss").args(["-H", "-ltnup"]).output().await.context("couldn't run `ss` to see who holds the ports")?;
    Ok(overview_from(&String::from_utf8_lossy(&out.stdout), std::process::id(), settings))
}

/// Every other process listening on a server port.
pub async fn holders() -> Result<Vec<Holder>> {
    let out = Command::new("ss").args(["-H", "-ltnup"]).output().await.context("couldn't run `ss` to see who holds the ports")?;
    let control = crate::settings::Settings::load().control_port;
    Ok(parse_ss_with(&String::from_utf8_lossy(&out.stdout), std::process::id(), control))
}

/// Stops this project's other server processes that hold a port: asked to
/// exit first (so they stop their model and say goodbye to the cluster), killed
/// after 8 s. Returns what was stopped, and what is left that isn't ours.
pub async fn free() -> Result<(Vec<String>, Vec<Holder>)> {
    let before = holders().await?;
    let mut pids: Vec<(u32, String)> = before.iter().filter(|h| h.ours).filter_map(|h| h.pid.map(|p| (p, h.name.clone()))).collect();
    pids.sort();
    pids.dedup();
    for (pid, _) in &pids {
        let _ = Command::new("kill").args(["-TERM", &pid.to_string()]).status().await;
    }
    let alive = |pid: u32| std::path::Path::new(&format!("/proc/{pid}")).exists();
    for _ in 0..16 {
        if !pids.iter().any(|(p, _)| alive(*p)) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    for (pid, _) in pids.iter().filter(|(p, _)| alive(*p)) {
        let _ = Command::new("kill").args(["-KILL", &pid.to_string()]).status().await;
    }
    tokio::time::sleep(Duration::from_millis(300)).await;
    let stopped = pids.iter().map(|(pid, name)| format!("{name} (pid {pid})")).collect();
    Ok((stopped, holders().await?.into_iter().filter(|h| !h.ours).collect()))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SS: &str = r#"udp   UNCONN 0      0            0.0.0.0:9093       0.0.0.0:*    users:(("openphalanx",pid=438605,fd=59))
udp   UNCONN 0      0            0.0.0.0:5353       0.0.0.0:*
tcp   LISTEN 0      1024         0.0.0.0:9092       0.0.0.0:*    users:(("openphalanx",pid=438605,fd=64))
tcp   LISTEN 0      1024       127.0.0.1:9094       0.0.0.0:*    users:(("oppxs",pid=777,fd=12),("oppxs",pid=778,fd=12))
tcp   LISTEN 0      4096         0.0.0.0:9090       0.0.0.0:*
tcp   LISTEN 0      128             [::]:9092          [::]:*    users:(("nginx",pid=42,fd=6))
tcp   LISTEN 0      128          0.0.0.0:9093       0.0.0.0:*    users:(("python3",pid=9,fd=3))
"#;

    #[test]
    fn overview_names_every_port_and_its_holders() {
        let ss = format!("{SS}tcp   LISTEN 0      4096         0.0.0.0:9191       0.0.0.0:*    users:((\"docker-proxy\",pid=5,fd=4))\n\
tcp   LISTEN 0      4096         0.0.0.0:9105       0.0.0.0:*\n");
        let settings = crate::settings::Settings { agent_port: 9191, ..Default::default() };
        let rows = overview_from(&ss, 438605, &settings);
        let get = |p: u16| rows.iter().find(|r| r.port == p).unwrap();
        assert_eq!(get(9191).holders, vec!["Docker (a container's published port)"]);
        assert_eq!(get(9191).setting.as_deref(), Some("agent_port"));
        assert_eq!(get(9091).setting.as_deref(), Some("admin_port"));
        assert_eq!(get(9094).setting.as_deref(), Some("control_port"));
        assert!(get(9092).setting.is_none() && get(9092).fixed_because.as_deref().unwrap().contains("same on all"));
        assert_eq!(get(9092).holders, vec!["this app", "nginx (pid 42)"]);
        assert_eq!(get(9093).holders, vec!["this app"], "udp only: tcp 9093 is something else");
        assert_eq!(get(9094).holders, vec!["oppxs (pid 777)", "oppxs (pid 778)"]);
        assert_eq!(get(9100).holders, vec!["not visible (another user's process)"], "9105 is in the range");
        assert!(get(9096).holders.is_empty());
        assert!(rows.iter().all(|r| r.port != 9090), "the client API moved");
    }

    #[test]
    fn a_port_cannot_move_onto_another() {
        let s = crate::settings::Settings::default();
        assert!(port_problem(Movable::Agent, 9090, &s).is_none(), "where it already is");
        assert!(port_problem(Movable::Agent, 8443, &s).is_none());
        assert!(port_problem(Movable::Agent, 443, &s).unwrap().contains("1024"));
        assert!(port_problem(Movable::Agent, 9092, &s).unwrap().contains("cluster API"));
        assert!(port_problem(Movable::Agent, 9110, &s).unwrap().contains("rendezvous"));
        assert!(port_problem(Movable::Agent, 9091, &s).unwrap().contains("the admin API's"));
        assert!(port_problem(Movable::Admin, 9090, &s).unwrap().contains("the client API's"));
        assert!(port_problem(Movable::Control, 9091, &s).unwrap().contains("the admin API's"));
        assert!(port_problem(Movable::Admin, 9117, &s).is_none());
    }

    #[test]
    fn a_moved_control_port_is_the_one_watched() {
        let ss = "tcp   LISTEN 0      1024       127.0.0.1:9200       0.0.0.0:*    users:((\"oppxs\",pid=7,fd=12))\n";
        assert!(parse_ss(ss, 1).is_empty(), "9200 isn't a server port by default");
        assert_eq!(parse_ss_with(ss, 1, 9200)[0].purpose, "oppxs control");
    }

    #[test]
    fn finds_who_holds_the_server_ports() {
        let h = parse_ss(SS, 1);
        let brief: Vec<(u16, &str, Option<u32>, &str, bool)> = h.iter().map(|x| (x.port, x.proto.as_str(), x.pid, x.name.as_str(), x.ours)).collect();
        assert_eq!(
            brief,
            vec![
                (9093, "udp", Some(438605), "openphalanx", true),
                (9092, "tcp", Some(438605), "openphalanx", true),
                (9094, "tcp", Some(777), "oppxs", true),
                (9094, "tcp", Some(778), "oppxs", true),
                (9092, "tcp", Some(42), "nginx", false), // not ours: named, never stopped
            ],
            "9090 is Docker's, 5353 isn't ours, and tcp 9093 isn't the discovery port"
        );
        // This process itself is not a holder.
        assert!(parse_ss(SS, 438605).iter().all(|x| x.pid != Some(438605)));
        assert!(h[0].describe().contains("openphalanx (pid 438605) holds udp 9093 (discovery)"));
    }

    #[test]
    fn a_port_held_by_another_user_is_reported_without_a_pid() {
        let h = parse_ss("tcp   LISTEN 0      1024         0.0.0.0:9092       0.0.0.0:*\n", 1);
        assert_eq!((h.len(), h[0].pid, h[0].ours), (1, None, false));
        assert!(h[0].describe().contains("another user's process"));
    }
}
