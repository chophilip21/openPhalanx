//! `oppx --update`: one command to get the latest client and the coding
//! engine it was tested with.
//!
//! `oppx` is built from a git checkout (`cargo install --path client/oppx`),
//! so updating means: fast-forward that checkout, rebuild and reinstall if it
//! changed, then make sure the pinned Aider version is installed.

use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{bail, Context, Result};

use crate::agent;
use crate::ui;

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

/// Installed Aider version, via its own interpreter (`None` if missing).
pub fn installed_engine() -> Option<String> {
    let aider = agent::find_aider().ok()?;
    let python = agent::aider_python(&aider).ok()?;
    let out = Command::new(python).args(["-c", "import aider; print(aider.__version__)"]).output().ok()?;
    out.status.success().then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Installs the pinned engine with uv (preferred) or pipx.
fn install_engine() -> Result<()> {
    let home = dirs::home_dir().unwrap_or_default();
    let spec = format!("aider-chat=={}", agent::AIDER_VERSION);
    let status = if let Some(uv) = find_tool("uv", &[home.join(".local/bin/uv"), home.join(".cargo/bin/uv")]) {
        Command::new(uv).args(["tool", "install", "--force", "--python", "3.12", &spec]).output()?
    } else if let Some(pipx) = find_tool("pipx", &[home.join(".local/bin/pipx")]) {
        Command::new(pipx).args(["install", "--force", "--python", "python3.12", &spec]).output()?
    } else {
        bail!("neither uv nor pipx is installed; install uv (https://docs.astral.sh/uv/) and run `oppx --update` again");
    };
    if !status.status.success() {
        let err = String::from_utf8_lossy(&status.stderr);
        bail!("installing {spec} failed: {}", err.lines().rev().take(3).collect::<Vec<_>>().join(" / "));
    }
    Ok(())
}

pub fn run() -> Result<()> {
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
        sp.done("oppx installed");
    }

    // 3. Coding engine.
    let sp = ui::spinner("Checking the coding engine");
    match installed_engine() {
        Some(v) if v == agent::AIDER_VERSION => sp.done(format!("Coding engine is current ({v})")),
        found => {
            sp.clear();
            let sp = ui::spinner(match &found {
                Some(v) => format!("Updating the coding engine ({v} → {})", agent::AIDER_VERSION),
                None => format!("Installing the coding engine ({})", agent::AIDER_VERSION),
            });
            match install_engine() {
                Ok(()) => sp.done(format!("Coding engine {} installed", agent::AIDER_VERSION)),
                Err(e) => {
                    sp.clear();
                    return Err(e);
                }
            }
        }
    }
    ui::line(ui::accent("Up to date. Run oppx to start."));
    Ok(())
}
