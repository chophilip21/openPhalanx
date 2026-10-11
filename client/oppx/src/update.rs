//! `oppx --update`: one command to get the latest client and the coding
//! engine it was tested with.
//!
//! * **Release builds** (installed by `install.sh`; CI sets `OPPX_RELEASE_TARGET`
//!   at build time): download the latest GitHub release for this platform,
//!   check its SHA-256, and replace this binary.
//! * **Source builds** (`cargo install --path client/oppx`): fast-forward the
//!   checkout, then rebuild and reinstall if it changed.
//!
//! Both then make sure the pinned coding engine is installed (`engine`).

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{bail, Context, Result};
use sha2::{Digest, Sha256};

use crate::{agent, browser, engine, ui};

/// GitHub repository that publishes releases.
pub const REPO: &str = "chophilip21/openPhalanx";
/// The release asset target this binary was built for, if it is a release build.
pub const RELEASE_TARGET: Option<&str> = option_env!("OPPX_RELEASE_TARGET");

/// The checkout this binary was built from (two levels above client/oppx).
pub fn source_checkout() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn git(repo: &Path, args: &[&str]) -> Result<String> {
    let out = Command::new("git").args(args).current_dir(repo).output().context("git is not installed")?;
    if !out.status.success() {
        bail!("git {} failed: {}", args.join(" "), String::from_utf8_lossy(&out.stderr).trim());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn find_tool(name: &str, extra: &[PathBuf]) -> Option<PathBuf> {
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .map(|d| d.join(name))
        .chain(extra.iter().cloned())
        .find(|p| p.is_file())
}

/// Installed engine version (`None` if missing).
pub fn installed_engine() -> Option<String> {
    engine::find_any().and_then(|a| engine::version_of(&a))
}

#[derive(serde::Deserialize)]
struct Release {
    tag_name: String,
    assets: Vec<Asset>,
}

#[derive(serde::Deserialize)]
struct Asset {
    name: String,
    browser_download_url: String,
}

async fn get(client: &reqwest::Client, url: &str) -> Result<reqwest::Response> {
    client
        .get(url)
        .header("user-agent", concat!("oppx/", env!("CARGO_PKG_VERSION")))
        .send()
        .await
        .and_then(|r| r.error_for_status())
        .with_context(|| format!("cannot download {url}"))
}

/// Replaces this binary with the latest release, when it is newer.
async fn update_release(target: &str) -> Result<()> {
    let client = reqwest::Client::new();
    let sp = ui::spinner("Checking for a new release");
    let url = format!("https://api.github.com/repos/{REPO}/releases/latest");
    let response = client.get(&url).header("user-agent", concat!("oppx/", env!("CARGO_PKG_VERSION"))).send().await;
    let release: Release = match response {
        Ok(r) if r.status() == reqwest::StatusCode::NOT_FOUND => {
            sp.done("No release has been published yet");
            return Ok(());
        }
        Ok(r) => r.error_for_status().context("GitHub refused the release lookup")?.json().await?,
        Err(e) => {
            sp.clear();
            return Err(anyhow::Error::new(e).context("cannot reach GitHub (offline?)"));
        }
    };
    let latest = release.tag_name.trim_start_matches('v').to_string();
    if !is_newer(&latest, env!("CARGO_PKG_VERSION")) {
        sp.done(format!("oppx {} is the latest release", env!("CARGO_PKG_VERSION")));
        return Ok(());
    }
    sp.clear();

    let name = format!("oppx-{target}.tar.gz");
    let find = |n: &str| release.assets.iter().find(|a| a.name == n).map(|a| a.browser_download_url.clone());
    let (Some(url), Some(sums_url)) = (find(&name), find("SHA256SUMS")) else {
        bail!("release {} has no {name}; download it from https://github.com/{REPO}/releases", release.tag_name);
    };
    let sp = ui::spinner(format!("Downloading oppx {latest}"));
    let archive = get(&client, &url).await?.bytes().await?;
    let sums = get(&client, &sums_url).await?.text().await?;
    let want = sums
        .lines()
        .find_map(|l| l.split_once(char::is_whitespace).filter(|(_, f)| f.trim().trim_start_matches('*') == name))
        .map(|(h, _)| h.to_lowercase())
        .with_context(|| format!("SHA256SUMS has no entry for {name}"))?;
    let got = format!("{:x}", Sha256::digest(&archive));
    if got != want {
        sp.clear();
        bail!("checksum mismatch for {name} (expected {want}, got {got}); nothing was changed");
    }

    // Unpack next to the current binary, then rename over it (atomic, and
    // safe while this process is running).
    let exe = std::env::current_exe()?.canonicalize()?;
    let dir = exe.parent().context("binary has no parent directory")?;
    let work = tempfile::tempdir_in(dir).with_context(|| format!("cannot write to {}", dir.display()))?;
    let mut tgz = tempfile::NamedTempFile::new_in(work.path())?;
    tgz.write_all(&archive)?;
    let status = Command::new("tar").arg("-xzf").arg(tgz.path()).arg("-C").arg(work.path()).status()?;
    let new = work.path().join("oppx");
    if !status.success() || !new.is_file() {
        sp.clear();
        bail!("cannot unpack {name}");
    }
    std::fs::rename(&new, &exe).with_context(|| format!("cannot replace {}", exe.display()))?;
    sp.done(format!("oppx {} → {latest} installed", env!("CARGO_PKG_VERSION")));
    Ok(())
}

/// `a` > `b` for dotted numeric versions.
fn is_newer(a: &str, b: &str) -> bool {
    let parse = |v: &str| v.split('.').map(|x| x.parse::<u64>().unwrap_or(0)).collect::<Vec<_>>();
    parse(a) > parse(b)
}

pub async fn run() -> Result<()> {
    // Taken before the update: once the file is replaced, the system reports
    // this process's own path as "… (deleted)".
    let me = std::env::current_exe().ok();
    match RELEASE_TARGET {
        Some(target) => update_release(target).await?,
        None => update_source()?,
    }
    // The coding engine.
    match installed_engine() {
        Some(v) if v == agent::AIDER_VERSION && engine::find().is_some() => {
            ui::spinner("").done(format!("Coding engine is current (Aider {v})"))
        }
        _ => engine::install().await?,
    }
    // The browser, only when it was installed before (otherwise on first use).
    if browser::installed_version().is_some_and(|v| v != browser::MCP_VERSION) {
        browser::install()?;
    }
    // The version that runs next, read from the binary itself: the header
    // above shows the one that started this update.
    match me.as_deref().map(version_of).filter(|v| v != "?") {
        Some(now) => ui::line(ui::accent(format!("oppx {now} is installed. Run oppx to start."))),
        None => ui::line(ui::accent("Up to date. Run oppx to start.")),
    }
    Ok(())
}

/// What `--version` of a binary says ("0.6.1 (ff00f1a)"), or "?".
fn version_of(binary: &Path) -> String {
    Command::new(binary)
        .arg("--version")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().trim_start_matches("oppx ").to_string())
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "?".into())
}

/// Puts a copy of `built` in place of the running executable when they are
/// different files. Written beside it first and renamed over it, which works
/// while it runs. Returns the path it replaced, or None when the running one
/// is `built` itself.
fn replace_running(built: &Path) -> Result<Option<PathBuf>> {
    let running = std::env::current_exe()?.canonicalize()?;
    let built = built.canonicalize().with_context(|| format!("{} is missing after the build", built.display()))?;
    if running == built {
        return Ok(None);
    }
    let tmp = running.with_extension("new");
    std::fs::copy(&built, &tmp).with_context(|| format!("can't write {}", tmp.display()))?;
    std::fs::rename(&tmp, &running).with_context(|| format!("can't replace {}", running.display()))?;
    Ok(Some(running))
}

fn update_source() -> Result<()> {
    let repo = source_checkout();
    let repo = repo.canonicalize().with_context(|| {
        format!(
            "the source checkout oppx was built from ({}) is gone; clone the repo again and run \
             `cargo install --path client/oppx`",
            repo.display()
        )
    })?;
    if git(&repo, &["rev-parse", "--is-inside-work-tree"]).is_err() {
        bail!("{} is not a git checkout; reinstall with `cargo install --path client/oppx` from a clone", repo.display());
    }

    // 1. Pull.
    let sp = ui::spinner(format!("Checking for updates in {}", repo.display()));
    let dirty = git(&repo, &["status", "--porcelain", "--untracked-files=no"])?;
    if !dirty.is_empty() {
        sp.clear();
        bail!("{} has local changes; commit or stash them, then run `oppx --update` again", repo.display());
    }
    let before = git(&repo, &["rev-parse", "--short", "HEAD"])?;
    let fetched = git(&repo, &["fetch", "--quiet"]);
    let behind: usize = git(&repo, &["rev-list", "--count", "HEAD..@{u}"]).ok().and_then(|n| n.parse().ok()).unwrap_or(0);
    if let Err(e) = fetched {
        sp.clear();
        return Err(e.context("cannot reach the remote (offline, or no access to the repo?)"));
    }
    let pulled = if behind > 0 {
        git(&repo, &["pull", "--ff-only", "--quiet"]).context("cannot fast-forward; resolve it with git in the checkout")?;
        let after = git(&repo, &["rev-parse", "--short", "HEAD"])?;
        sp.done(format!("Pulled {behind} new commit{} ({before} → {after})", if behind == 1 { "" } else { "s" }));
        true
    } else {
        sp.done(format!("Source is up to date ({before})"));
        false
    };

    // 2. Rebuild when the source changed, or this binary was built from an
    //    older commit than the checkout (e.g. someone pulled by hand).
    let head = git(&repo, &["rev-parse", "--short", "HEAD"])?;
    let built_from = env!("OPPX_GIT_COMMIT");
    if pulled || built_from != head {
        let home = dirs::home_dir().unwrap_or_default();
        let cargo = find_tool("cargo", &[home.join(".cargo/bin/cargo")])
            .context("cargo is not installed (https://rustup.rs)")?;
        let sp = ui::spinner("Building and installing oppx (about a minute)");
        let out = Command::new(cargo)
            .args(["install", "--quiet", "--locked", "--path"])
            .arg(repo.join("client/oppx"))
            .output()?;
        if !out.status.success() {
            sp.clear();
            let err = String::from_utf8_lossy(&out.stderr);
            bail!("build failed:\n{}", err.lines().rev().take(12).collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>().join("\n"));
        }
        // `cargo install` writes to cargo's bin folder. When the `oppx` that is
        // running lives somewhere else (a copy in ~/.local/bin, say), that one
        // comes first on PATH and would stay as it was: the update said
        // "installed" and `oppx --version` still showed the old version.
        let root = std::env::var_os("CARGO_INSTALL_ROOT").map(PathBuf::from).unwrap_or_else(|| home.join(".cargo"));
        let built = root.join("bin").join("oppx");
        match replace_running(&built) {
            Ok(Some(path)) => sp.done(format!("oppx {} installed ({} and {})", version_of(&built), path.display(), built.display())),
            Ok(None) => sp.done(format!("oppx {} installed", version_of(&built))),
            Err(e) => {
                sp.clear();
                bail!(
                    "the new oppx is in {}, but the `oppx` you are running is {} and couldn't be replaced ({e:#}). \
                     Copy it over by hand, or remove the old one.",
                    built.display(),
                    std::env::current_exe().map(|p| p.display().to_string()).unwrap_or_default()
                );
            }
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::is_newer;

    #[test]
    fn compares_versions_numerically() {
        assert!(is_newer("0.10.0", "0.9.9"));
        assert!(is_newer("1.0.0", "0.99.0"));
        assert!(is_newer("0.2.1", "0.2.0"));
        assert!(!is_newer("0.2.0", "0.2.0"));
        assert!(!is_newer("0.1.9", "0.2.0"));
    }
}
