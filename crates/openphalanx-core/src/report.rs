//! The incident journal and the problem report.
//!
//! When a start goes wrong the evidence is spread over this app, Docker, the
//! GPU driver and the kernel log, and a machine that froze has lost what was
//! on screen. So the app keeps a small journal of what happened (starts,
//! stops, a member dropping out, a port conflict, errors), notes whether its
//! last run ended cleanly, and can put all of it, with the machine's
//! specification, into one report: a Markdown text and a link that opens a
//! filled-in issue on GitHub.
//!
//! Nothing is sent by the app. The user reads the report and posts it.
//!
//! What a report never holds: tokens, pairing codes, certificate
//! fingerprints, prompts (the backend doesn't log them), and addresses or the
//! user's name (`redact`).

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tokio::process::Command;

use crate::{docker, gpu, paths, ports};

pub const ISSUES_URL: &str = "https://github.com/chophilip21/openPhalanx/issues/new";
/// Browsers and GitHub refuse very long links: the link carries a summary,
/// the saved file carries everything.
const URL_BODY_CHARS: usize = 5000;
const JOURNAL: &str = "events.jsonl";
const MARKER: &str = "running.json";
const JOURNAL_KEEP: usize = 400;
const JOURNAL_TRIM_AT: u64 = 256 * 1024;
const PROBE_TIMEOUT: Duration = Duration::from_secs(6);

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs())
}

// --------------------------------------------------------------------------
// The journal
// --------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Event {
    /// Unix seconds.
    pub t: u64,
    /// `app`, `server`, `cluster`, `ports`, `error`.
    pub kind: String,
    pub text: String,
}

/// Adds a line to the journal in the app's data folder. Never fails the
/// caller: a journal that can't be written is not a reason to stop a start.
pub fn record(kind: &str, text: &str) {
    record_in(&paths::data_dir(), kind, text);
}

pub fn record_in(dir: &Path, kind: &str, text: &str) {
    let _ = fs::create_dir_all(dir);
    let path = dir.join(JOURNAL);
    if fs::metadata(&path).is_ok_and(|m| m.len() > JOURNAL_TRIM_AT) {
        let kept = recent_in(dir, JOURNAL_KEEP);
        let body: String = kept.iter().filter_map(|e| serde_json::to_string(e).ok()).map(|l| l + "\n").collect();
        let _ = fs::write(&path, body);
    }
    let event = Event { t: now(), kind: kind.into(), text: text.chars().take(600).collect() };
    if let (Ok(mut f), Ok(line)) = (fs::OpenOptions::new().create(true).append(true).open(&path), serde_json::to_string(&event)) {
        let _ = writeln!(f, "{line}");
    }
}

/// The latest `n` events, oldest first.
pub fn recent_in(dir: &Path, n: usize) -> Vec<Event> {
    let text = fs::read_to_string(dir.join(JOURNAL)).unwrap_or_default();
    let all: Vec<Event> = text.lines().filter_map(|l| serde_json::from_str(l).ok()).collect();
    all[all.len().saturating_sub(n)..].to_vec()
}

// --------------------------------------------------------------------------
// Did the last run end cleanly?
// --------------------------------------------------------------------------

/// Written when the app starts, refreshed while it runs, removed when it
/// exits. One still there at the next start means the app was killed, it
/// crashed, or the machine froze or lost power.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunMarker {
    pub pid: u32,
    pub version: String,
    pub started: u64,
    /// The last time the app was seen alive.
    pub seen: u64,
    /// What the server was doing then (`starting`, `running`, `stopped` …).
    pub state: String,
}

fn alive(pid: u32) -> bool {
    pid != 0 && Path::new(&format!("/proc/{pid}")).exists()
}

/// Starts a run: returns the previous run's marker when that run did not end
/// cleanly, and writes this run's.
pub fn begin_run(dir: &Path, version: &str) -> Option<RunMarker> {
    let _ = fs::create_dir_all(dir);
    let path = dir.join(MARKER);
    let before: Option<RunMarker> = fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str(&t).ok());
    // Another copy still running (the app and `oppxs`, or an update's relaunch) is not a crash.
    let unclean = before.filter(|m| !alive(m.pid) || m.pid == std::process::id());
    let t = now();
    let mine = RunMarker { pid: std::process::id(), version: version.into(), started: t, seen: t, state: "stopped".into() };
    let _ = fs::write(&path, serde_json::to_string(&mine).unwrap_or_default());
    if let Some(m) = &unclean {
        record_in(dir, "app", &format!(
            "the previous run (version {}, started {}) did not end cleanly: last seen {} with the server {}",
            m.version, clock(m.started), clock(m.seen), m.state
        ));
    }
    record_in(dir, "app", &format!("started, version {version}"));
    unclean
}

/// The app is alive, and the server is in this state.
pub fn heartbeat(dir: &Path, state: &str) {
    let path = dir.join(MARKER);
    let Some(mut m) = fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str::<RunMarker>(&t).ok()) else {
        return;
    };
    if m.pid != std::process::id() {
        return;
    }
    m.seen = now();
    m.state = state.into();
    let _ = fs::write(&path, serde_json::to_string(&m).unwrap_or_default());
}

/// A clean exit.
pub fn end_run(dir: &Path) {
    let path = dir.join(MARKER);
    let mine = fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str::<RunMarker>(&t).ok());
    if mine.is_some_and(|m| m.pid == std::process::id()) {
        let _ = fs::remove_file(&path);
        record_in(dir, "app", "closed");
    }
}

/// "14:06:48" in UTC (the journal's times are compared with each other and
/// with Docker's, which are UTC too).
pub fn clock(t: u64) -> String {
    format!("{:02}:{:02}:{:02} UTC", (t / 3600) % 24, (t / 60) % 60, t % 60)
}

/// "2026-10-12" from Unix seconds (civil-from-days; no date crate here).
pub fn date(t: u64) -> String {
    let z = (t / 86_400) as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let (d, m) = (doy - (153 * mp + 2) / 5 + 1, if mp < 10 { mp + 3 } else { mp - 9 });
    format!("{:04}-{:02}-{:02}", yoe + era * 400 + i64::from(m <= 2), m, d)
}

// --------------------------------------------------------------------------
// The report
// --------------------------------------------------------------------------

/// What the caller knows and the core can't find out by itself.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Context {
    pub version: String,
    /// How it was installed (`.deb`, AppImage, source).
    pub install: String,
    /// What the user says happened, in their words.
    pub description: String,
    /// The server's state and the message with it, as the app shows them.
    pub state: String,
    pub detail: String,
    /// The selected model, its context window, and other settings worth knowing, one per line.
    pub settings: Vec<String>,
    /// The cluster as lines: this machine's role, each member and whether it is online.
    pub cluster: Vec<String>,
    /// The previous run, when it did not end cleanly.
    pub unclean: Option<RunMarker>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Report {
    /// A one-line title for the issue.
    pub title: String,
    /// The whole report.
    pub markdown: String,
    /// Opens a new issue with the title and as much of the report as a link holds.
    pub url: String,
    /// Where the whole report was saved, when it could be.
    pub path: Option<String>,
}

async fn run(program: &str, args: &[&str]) -> String {
    let mut cmd = Command::new(program);
    cmd.args(args).kill_on_drop(true).stdin(std::process::Stdio::null());
    match tokio::time::timeout(PROBE_TIMEOUT, cmd.output()).await {
        Ok(Ok(out)) => String::from_utf8_lossy(&out.stdout).trim().to_string(),
        Ok(Err(_)) => String::new(),
        Err(_) => format!("({program} did not answer in {} s)", PROBE_TIMEOUT.as_secs()),
    }
}

fn os_release() -> String {
    let text = fs::read_to_string("/etc/os-release").unwrap_or_default();
    text.lines()
        .find_map(|l| l.strip_prefix("PRETTY_NAME="))
        .map_or_else(|| "unknown".into(), |v| v.trim_matches('"').to_string())
}

fn cpu_and_memory() -> (String, String) {
    let cpu = fs::read_to_string("/proc/cpuinfo").unwrap_or_default();
    let model = cpu.lines().find_map(|l| l.strip_prefix("model name")).map_or("unknown", |v| v.trim_start_matches([' ', '\t', ':']));
    let threads = cpu.lines().filter(|l| l.starts_with("processor")).count();
    let mem = fs::read_to_string("/proc/meminfo").unwrap_or_default();
    let kb = |key: &str| -> f64 {
        mem.lines()
            .find_map(|l| l.strip_prefix(key))
            .and_then(|v| v.split_whitespace().next()?.parse::<f64>().ok())
            .unwrap_or(0.0)
    };
    let gib = 1024.0 * 1024.0;
    (
        format!("{model} ({threads} threads)"),
        format!(
            "{:.1} GiB total, {:.1} GiB available, swap {:.1} of {:.1} GiB free",
            kb("MemTotal:") / gib,
            kb("MemAvailable:") / gib,
            kb("SwapFree:") / gib,
            kb("SwapTotal:") / gib
        ),
    )
}

/// Lines of the kernel log that say the machine itself was in trouble: a
/// kernel fault, the out-of-memory killer, a GPU driver error, a stuck CPU.
/// From this boot and the one before (the one that may have frozen).
pub fn kernel_faults(log: &str, limit: usize) -> Vec<String> {
    const SIGNS: [&str; 10] = [
        "Oops", "BUG:", "general protection", "Out of memory", "oom-kill", "NVRM: Xid", "soft lockup", "hard LOCKUP",
        "Machine check", "blocked for more than",
    ];
    let hits: Vec<String> = log.lines().filter(|l| SIGNS.iter().any(|s| l.contains(s))).map(|l| l.chars().take(220).collect()).collect();
    hits[hits.len().saturating_sub(limit)..].to_vec()
}

/// Takes out what identifies the user or their network: the home folder's
/// name, IPv4 addresses, and long hexadecimal strings (tokens, fingerprints,
/// container ids keep their first 8 characters).
pub fn redact(text: &str) -> String {
    let mut out = text.to_string();
    if let Some(home) = dirs::home_dir().map(|h| h.to_string_lossy().to_string()).filter(|h| h.len() > 1) {
        out = out.replace(&home, "~");
    }
    let host = gethostname::gethostname().to_string_lossy().to_string();
    if host.len() >= 3 {
        out = out.replace(&host, "<host>");
    }
    mask_hex(&mask_ipv4(&out))
}

fn mask_ipv4(text: &str) -> String {
    let b = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < b.len() {
        let starts = b[i].is_ascii_digit() && (i == 0 || !(b[i - 1].is_ascii_alphanumeric() || b[i - 1] == b'.'));
        if starts {
            // Four groups of 1-3 digits joined by dots, and nothing number-like after them.
            let (mut j, mut groups, mut ok) = (i, 0, true);
            while groups < 4 {
                let from = j;
                while j < b.len() && b[j].is_ascii_digit() && j - from < 3 {
                    j += 1;
                }
                if j == from || text[from..j].parse::<u32>().map_or(true, |v| v > 255) {
                    ok = false;
                    break;
                }
                groups += 1;
                if groups < 4 {
                    if j < b.len() && b[j] == b'.' {
                        j += 1;
                    } else {
                        ok = false;
                        break;
                    }
                }
            }
            let ends = j >= b.len() || !(b[j].is_ascii_alphanumeric() || (b[j] == b'.' && j + 1 < b.len() && b[j + 1].is_ascii_digit()));
            if ok && ends {
                out.push_str("<ip>");
                i = j;
                continue;
            }
        }
        // Copy one whole character (the text may hold non-ASCII).
        let ch = text[i..].chars().next().unwrap_or(' ');
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

fn mask_hex(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut run = String::new();
    let flush = |run: &mut String, out: &mut String| {
        if run.len() >= 24 && run.chars().any(|c| c.is_ascii_digit()) && run.chars().any(|c| c.is_ascii_alphabetic()) {
            out.push_str(&run[..8]);
            out.push('…');
        } else {
            out.push_str(run);
        }
        run.clear();
    };
    for c in text.chars() {
        if c.is_ascii_hexdigit() {
            run.push(c);
        } else {
            flush(&mut run, &mut out);
            out.push(c);
        }
    }
    flush(&mut run, &mut out);
    out
}

fn percent_encode(text: &str) -> String {
    let mut out = String::with_capacity(text.len() * 3);
    for b in text.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// The link that opens a new issue with this title and body.
pub fn issue_url(title: &str, body: &str) -> String {
    let mut short: String = body.chars().take(URL_BODY_CHARS).collect();
    if body.chars().count() > URL_BODY_CHARS {
        short.push_str("\n\n… (cut to fit a link: paste the full report here; the app copied it and saved it to a file)");
    }
    format!("{ISSUES_URL}?labels=bug&title={}&body={}", percent_encode(title), percent_encode(&short))
}

fn section(out: &mut String, title: &str, lines: &[String]) {
    if lines.is_empty() {
        return;
    }
    out.push_str(&format!("\n### {title}\n\n"));
    for l in lines {
        out.push_str(&format!("- {l}\n"));
    }
}

fn fenced(out: &mut String, title: &str, body: &str) {
    if body.trim().is_empty() {
        return;
    }
    out.push_str(&format!("\n<details><summary>{title}</summary>\n\n```\n{}\n```\n\n</details>\n", body.trim_end()));
}

fn tail(text: &str, lines: usize) -> String {
    let all: Vec<&str> = text.lines().collect();
    all[all.len().saturating_sub(lines)..].iter().map(|l| l.chars().take(300).collect::<String>()).collect::<Vec<_>>().join("\n")
}

/// The parts of a report, already gathered: what `markdown` lays out.
/// Separate from the gathering so the layout can be tested without Docker, a
/// GPU or a kernel log.
#[derive(Debug, Clone, Default)]
pub struct Facts {
    pub os: String,
    pub kernel: String,
    pub cpu: String,
    pub memory: String,
    pub gpus: Vec<String>,
    pub driver: String,
    pub docker: String,
    pub container: Vec<String>,
    pub ports: Vec<String>,
    pub events: Vec<Event>,
    pub kernel_faults: Vec<String>,
    pub backend_log: String,
    pub worker_log: String,
}

pub fn markdown(ctx: &Context, f: &Facts) -> (String, String) {
    let what = if !ctx.description.trim().is_empty() {
        ctx.description.trim().lines().next().unwrap_or("").chars().take(90).collect::<String>()
    } else if let Some(m) = &ctx.unclean {
        format!("The app did not shut down cleanly (server was {})", m.state)
    } else if ctx.state == "error" || ctx.state == "paused" {
        ctx.detail.chars().take(90).collect()
    } else {
        "Problem report".to_string()
    };
    let title = format!("[report] {what}");

    let mut out = String::new();
    out.push_str("## What happened\n\n");
    out.push_str(if ctx.description.trim().is_empty() { "_(describe what you did and what you saw)_" } else { ctx.description.trim() });
    out.push('\n');
    if let Some(m) = &ctx.unclean {
        out.push_str(&format!(
            "\n**The previous run did not end cleanly.** Version {}, started {} {}, last seen alive {} with the server \
             `{}`. The app was killed, it crashed, or the machine froze or lost power.\n",
            m.version, date(m.started), clock(m.started), clock(m.seen), m.state
        ));
    }
    section(&mut out, "Server", &[
        format!("OpenPhalanx {} ({})", ctx.version, if ctx.install.is_empty() { "unknown install" } else { &ctx.install }),
        format!("State: `{}`{}", ctx.state, if ctx.detail.is_empty() { String::new() } else { format!(": {}", ctx.detail) }),
    ]);
    section(&mut out, "Settings", &ctx.settings);
    section(&mut out, "Cluster", &ctx.cluster);
    let mut machine = vec![
        format!("OS: {}", f.os),
        format!("Kernel: {}", f.kernel),
        format!("CPU: {}", f.cpu),
        format!("Memory: {}", f.memory),
        format!("NVIDIA driver: {}", if f.driver.is_empty() { "not found" } else { &f.driver }),
        format!("Docker: {}", if f.docker.is_empty() { "not reachable" } else { &f.docker }),
    ];
    machine.extend(f.gpus.iter().map(|g| format!("GPU {g}")));
    section(&mut out, "Machine", &machine);
    section(&mut out, "Backend container", &f.container);
    section(&mut out, "Ports held by a server process", &f.ports);
    section(&mut out, "Kernel log: signs of trouble (this boot and the one before)", &f.kernel_faults);
    let events: Vec<String> = f.events.iter().map(|e| format!("`{} {}` {}: {}", date(e.t), clock(e.t), e.kind, e.text)).collect();
    section(&mut out, "What the app recorded (oldest first)", &events);
    fenced(&mut out, "Backend log (last lines)", &f.backend_log);
    fenced(&mut out, "Split worker log (last lines)", &f.worker_log);
    out.push_str("\n_Generated by OpenPhalanx. Addresses, the user's name and long hexadecimal strings were removed; prompts are never logged._\n");
    (title, redact(&out))
}

/// Gathers everything and writes the report. Every probe has a time limit:
/// a report is most wanted when something on the machine no longer answers.
pub async fn gather(ctx: Context) -> Report {
    let dir = paths::data_dir();
    let (cpu, memory) = cpu_and_memory();
    let gpus = match tokio::time::timeout(PROBE_TIMEOUT, gpu::query()).await {
        Ok(Ok(list)) => list
            .iter()
            .map(|g| {
                let gib = 1024.0 * 1024.0 * 1024.0;
                format!(
                    "{}: {} ({:.1} GiB, {:.1} free{}{})",
                    g.index,
                    g.name,
                    g.total_bytes as f64 / gib,
                    g.free_bytes as f64 / gib,
                    g.utilization_pct.map_or(String::new(), |u| format!(", {u}% load")),
                    g.temperature_c.map_or(String::new(), |t| format!(", {t} °C"))
                )
            })
            .collect(),
        Ok(Err(e)) => vec![format!("not readable: {e}")],
        Err(_) => vec![format!("nvidia-smi did not answer in {} s", PROBE_TIMEOUT.as_secs())],
    };
    let container = match tokio::time::timeout(PROBE_TIMEOUT, docker::inspect()).await {
        Ok(Ok(Some(c))) => {
            let mut lines = vec![
                format!("Image: {}", c.image),
                format!(
                    "Status: {}{}{}",
                    c.state.status,
                    if c.state.running { String::new() } else { format!(", exit code {}", c.state.exit_code) },
                    if c.state.oom_killed { ", killed for using too much memory".to_string() } else { String::new() }
                ),
                format!("Started by this app: {}", if c.managed { "yes" } else { "no" }),
            ];
            if !c.state.error.is_empty() {
                lines.push(format!("Docker's error: {}", c.state.error));
            }
            if let Some(key) = &c.model_key {
                lines.push(format!("Model: {key}"));
            }
            if let Some(n) = c.context_len {
                lines.push(format!("Context: {n} tokens"));
            }
            lines
        }
        Ok(Ok(None)) => vec!["No backend container exists.".into()],
        Ok(Err(e)) => vec![format!("Docker could not be asked: {e}")],
        Err(_) => vec![format!("Docker did not answer in {} s", PROBE_TIMEOUT.as_secs())],
    };
    let held = tokio::time::timeout(PROBE_TIMEOUT, ports::holders()).await.ok().and_then(Result::ok).unwrap_or_default();
    let ports = held
        .iter()
        .map(|h| format!("{} ({}): {}{}", h.port, h.purpose, h.name, h.pid.map_or(String::new(), |p| format!(", pid {p}"))))
        .collect();
    let mut kernel_log = run("journalctl", &["-k", "-b", "-1", "--no-pager", "-o", "short"]).await;
    kernel_log.push('\n');
    kernel_log.push_str(&run("journalctl", &["-k", "-b", "0", "--no-pager", "-o", "short"]).await);
    let backend_log = tokio::time::timeout(PROBE_TIMEOUT, docker::logs_tail(80)).await.ok().and_then(Result::ok).unwrap_or_default();
    let worker_log = tokio::time::timeout(PROBE_TIMEOUT, docker::worker_logs_tail(40)).await.ok().and_then(Result::ok).unwrap_or_default();
    let facts = Facts {
        os: os_release(),
        kernel: run("uname", &["-r"]).await,
        cpu,
        memory,
        gpus,
        driver: run("nvidia-smi", &["--query-gpu=driver_version", "--format=csv,noheader"]).await.lines().next().unwrap_or("").to_string(),
        docker: tokio::time::timeout(PROBE_TIMEOUT, docker::daemon_version()).await.ok().and_then(Result::ok).unwrap_or_default(),
        container,
        ports,
        events: recent_in(&dir, 60),
        kernel_faults: kernel_faults(&kernel_log, 12),
        backend_log: tail(&backend_log, 80),
        worker_log: tail(&worker_log, 40),
    };
    let (title, markdown) = markdown(&ctx, &facts);
    let path = save(&dir, &markdown);
    Report { url: issue_url(&title, &markdown), title, markdown, path: path.map(|p| p.to_string_lossy().to_string()) }
}

fn save(dir: &Path, markdown: &str) -> Option<PathBuf> {
    let folder = dir.join("reports");
    fs::create_dir_all(&folder).ok()?;
    let t = now();
    let path = folder.join(format!("report-{}-{:02}{:02}{:02}.md", date(t), (t / 3600) % 24, (t / 60) % 60, t % 60));
    fs::write(&path, markdown).ok()?;
    // Keep the latest 20.
    let mut all: Vec<PathBuf> = fs::read_dir(&folder).ok()?.flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|e| e == "md")).collect();
    all.sort();
    for old in all.iter().rev().skip(20) {
        let _ = fs::remove_file(old);
    }
    Some(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_journal_keeps_the_latest_events_in_order() {
        let dir = tempfile::tempdir().unwrap();
        assert!(recent_in(dir.path(), 10).is_empty());
        for i in 0..5 {
            record_in(dir.path(), "server", &format!("event {i}"));
        }
        let got = recent_in(dir.path(), 3);
        assert_eq!(got.iter().map(|e| e.text.as_str()).collect::<Vec<_>>(), ["event 2", "event 3", "event 4"]);
        assert!(got.iter().all(|e| e.kind == "server" && e.t > 0));
        // A damaged line is skipped, not fatal.
        fs::OpenOptions::new().append(true).open(dir.path().join(JOURNAL)).unwrap().write_all(b"{ not json\n").unwrap();
        record_in(dir.path(), "error", "after the damage");
        assert_eq!(recent_in(dir.path(), 1)[0].text, "after the damage");
    }

    #[test]
    fn a_journal_that_grew_too_big_is_cut_to_its_latest_events() {
        let dir = tempfile::tempdir().unwrap();
        let line = "x".repeat(500);
        for _ in 0..700 {
            record_in(dir.path(), "server", &line);
        }
        let size = fs::metadata(dir.path().join(JOURNAL)).unwrap().len();
        assert!(size < JOURNAL_TRIM_AT + 400 * 600, "{size}");
        assert!(recent_in(dir.path(), 1000).len() <= JOURNAL_KEEP + 300);
    }

    #[test]
    fn a_run_that_did_not_end_is_noticed_at_the_next_start() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(begin_run(dir.path(), "0.7.3"), None, "the first run has nothing before it");
        heartbeat(dir.path(), "starting");
        // The app is killed here: no end_run. The next start (the same test
        // process stands in for the new one) finds the marker.
        let before = begin_run(dir.path(), "0.7.4").expect("the run before did not end");
        assert_eq!((before.version.as_str(), before.state.as_str()), ("0.7.3", "starting"));
        assert!(recent_in(dir.path(), 10).iter().any(|e| e.text.contains("did not end cleanly")));
        // A clean exit leaves nothing to notice.
        end_run(dir.path());
        assert!(!dir.path().join(MARKER).exists());
        assert_eq!(begin_run(dir.path(), "0.7.4"), None);
        // Another copy that is still running (pid 1 always is) is not a crash.
        let other = RunMarker { pid: 1, version: "0.7.4".into(), started: 1, seen: 2, state: "running".into() };
        fs::write(dir.path().join(MARKER), serde_json::to_string(&other).unwrap()).unwrap();
        assert_eq!(begin_run(dir.path(), "0.7.4"), None);
    }

    #[test]
    fn dates_and_times_read_as_expected() {
        assert_eq!(date(0), "1970-01-01");
        assert_eq!(date(1_791_839_407), "2026-10-12");
        assert_eq!(date(951_782_400), "2000-02-29");
        assert_eq!(clock(1_791_839_407), "21:10:07 UTC");
    }

    #[test]
    fn a_report_names_nobody() {
        let home = dirs::home_dir().unwrap().to_string_lossy().to_string();
        let text = format!(
            "model at {home}/.local/share/openphalanx/models/x, host 192.168.1.77:9100 and 100.125.91.43, \
             token 9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a, version 0.7.3, image 0.7.3-de0fb8294a88, at 14:06:48, \
             10.0.0.300 is not an address, 1.2.3.4.5 neither"
        );
        let got = redact(&text);
        assert!(!got.contains(&home) && got.contains("~/.local/share/openphalanx/models/x"), "{got}");
        assert!(got.contains("host <ip>:9100 and <ip>,"), "{got}");
        assert!(got.contains("token 9f8a7b6c…") && !got.contains("5d4e3f2a"), "{got}");
        assert!(got.contains("version 0.7.3,") && got.contains("0.7.3-de0fb8294a88") && got.contains("14:06:48"), "{got}");
        assert!(got.contains("10.0.0.300 is not") && got.contains("1.2.3.4.5 neither"), "{got}");
        assert_eq!(redact("温度 21 °C at 192.168.1.5"), "温度 21 °C at <ip>");
    }

    #[test]
    fn the_kernel_log_is_searched_for_signs_of_trouble() {
        let log = "Oct 12 09:46:20 host kernel: thermal_sys: Registered thermal governor\n\
                   Oct 12 13:47:08 host kernel: Oops: general protection fault, kernel NULL pointer dereference\n\
                   Oct 12 13:47:09 host kernel: usb 1-1: new device\n\
                   Oct 12 14:00:00 host kernel: Out of memory: Killed process 123 (python)\n\
                   Oct 12 14:00:01 host kernel: NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus";
        let got = kernel_faults(log, 12);
        assert_eq!(got.len(), 3);
        assert!(got[0].contains("Oops") && got[2].contains("Xid"));
        assert_eq!(kernel_faults(log, 1), vec![got[2].clone()], "the latest ones are kept");
        assert!(kernel_faults("nothing wrong here", 5).is_empty());
    }

    #[test]
    fn the_report_reads_top_down_and_the_link_fits() {
        let ctx = Context {
            version: "0.7.3".into(),
            install: ".deb".into(),
            description: "Start froze the machine.\nI had removed the 4090 first.".into(),
            state: "paused".into(),
            detail: "Paused: laptop-4090 dropped out of the cluster.".into(),
            settings: vec!["Model: Qwen3.6-27B-AWQ, 65536 tokens".into()],
            cluster: vec!["This machine: host, split strategy".into(), "Member 1: offline".into()],
            unclean: Some(RunMarker { pid: 9, version: "0.7.3".into(), started: 1_791_839_093, seen: 1_791_839_407, state: "starting".into() }),
        };
        let facts = Facts {
            os: "Pop!_OS 24.04 LTS".into(),
            kernel: "7.1.5".into(),
            cpu: "i9 (32 threads)".into(),
            memory: "31.1 GiB total".into(),
            gpus: vec!["0: RTX 3090 (24.0 GiB, 22.7 free)".into()],
            driver: "580.173.02".into(),
            docker: "29.0".into(),
            container: vec!["Status: exited, exit code 255".into()],
            ports: vec!["9092 (cluster API): oppxs, pid 4242".into()],
            events: vec![Event { t: 1_791_839_208, kind: "server".into(), text: "start pressed".into() }],
            kernel_faults: vec!["kernel: Oops: general protection fault".into()],
            backend_log: "Capture target decode CUDA graph begin".into(),
            worker_log: String::new(),
        };
        let (title, md) = markdown(&ctx, &facts);
        assert_eq!(title, "[report] Start froze the machine.");
        let order = ["## What happened", "did not end cleanly", "### Server", "### Settings", "### Cluster", "### Machine",
                     "### Backend container", "### Ports held", "### Kernel log", "### What the app recorded", "Backend log"];
        let at: Vec<usize> = order.iter().map(|s| md.find(s).unwrap_or_else(|| panic!("missing {s}"))).collect();
        assert!(at.windows(2).all(|w| w[0] < w[1]), "{at:?}");
        assert!(md.contains("last seen alive 21:10:07 UTC with the server `starting`"));
        assert!(md.contains("`2026-10-12 21:06:48 UTC` server: start pressed"));
        assert!(!md.contains("Split worker log"), "an empty part is left out");
        // Without a description the title comes from what is known.
        let (title, md2) = markdown(&Context { description: String::new(), ..ctx.clone() }, &facts);
        assert!(title.contains("did not shut down cleanly"), "{title}");
        assert!(md2.contains("_(describe what you did"));
        // The link: the right repository, a bug label, and short enough to open.
        let url = issue_url(&title, &"line of the report\n".repeat(2000));
        assert!(url.starts_with("https://github.com/chophilip21/openPhalanx/issues/new?labels=bug&title=%5Breport%5D"));
        assert!(url.len() < 8000, "{}", url.len());
        assert!(url.contains("cut%20to%20fit%20a%20link"));
        assert!(!url.contains(' ') && !url.contains('\n'));
    }
}
