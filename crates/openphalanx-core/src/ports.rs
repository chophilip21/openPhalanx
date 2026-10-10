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

/// The ports a server process binds itself (the backend container's 9090 and
/// 9091 belong to Docker and are handled by stopping the server).
pub const SERVER_PORTS: [(u16, &str, &str); 3] = [
    (crate::cluster::CLUSTER_PORT, "tcp", "cluster API"),
    (crate::cluster::DISCOVERY_PORT, "udp", "discovery"),
    (9094, "tcp", "oppxs control"),
];
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
    let mut found: Vec<Holder> = Vec::new();
    for line in out.lines() {
        let mut cols = line.split_whitespace();
        let (Some(proto), Some(local)) = (cols.next(), cols.nth(3)) else { continue };
        let Some(port) = local.rsplit(':').next().and_then(|p| p.parse::<u16>().ok()) else { continue };
        let Some((_, _, purpose)) = SERVER_PORTS.iter().find(|(p, pr, _)| *p == port && *pr == proto) else { continue };
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

/// Every other process listening on a server port.
pub async fn holders() -> Result<Vec<Holder>> {
    let out = Command::new("ss").args(["-H", "-ltnup"]).output().await.context("couldn't run `ss` to see who holds the ports")?;
    Ok(parse_ss(&String::from_utf8_lossy(&out.stdout), std::process::id()))
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
