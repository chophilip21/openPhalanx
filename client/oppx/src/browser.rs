//! The chat's web browser: Playwright's MCP server, run on this machine
//! (like everything that executes), headless, for one chat session at a time.
//!
//! It is installed next to the coding engine on first use (`oppx browser
//! install`, or a "yes" in the chat): `@playwright/mcp` pinned to
//! [`MCP_VERSION`] with npm (about 20 MB), using the system's Google Chrome
//! when there is one and otherwise Playwright's own Chrome for Testing (about
//! 170 MB). It needs Node.js 18 or newer. Nothing runs until a chat session
//! starts the browser, and it stops with the session.

use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{bail, Context, Result};

use crate::{engine, ui};

pub const MCP_VERSION: &str = "0.0.83";
const PACKAGE: &str = "@playwright/mcp";
/// What Playwright calls the browser it downloads itself.
const BUNDLED: &str = "chrome-for-testing";
const NODE_MIN: u32 = 18;

/// `<engine>/playwright`.
pub fn dir() -> PathBuf {
    engine::dir().join("playwright")
}

/// The MCP server's entry point (may not exist yet).
pub fn cli() -> PathBuf {
    dir().join("node_modules/@playwright/mcp/cli.js")
}

/// Where a downloaded browser goes (`PLAYWRIGHT_BROWSERS_PATH`).
pub fn browsers_dir() -> PathBuf {
    dir().join("browsers")
}

/// The installed `@playwright/mcp` version, from its package.json.
pub fn installed_version() -> Option<String> {
    let text = std::fs::read_to_string(dir().join("node_modules/@playwright/mcp/package.json")).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v.get("version")?.as_str().map(str::to_string)
}

pub fn installed() -> bool {
    cli().is_file() && installed_version().as_deref() == Some(MCP_VERSION)
}

/// Google Chrome where Playwright's `chrome` channel looks for it.
pub fn system_chrome() -> Option<PathBuf> {
    let candidates: &[&str] = if cfg!(target_os = "macos") {
        &["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    } else {
        &["/opt/google/chrome/chrome"]
    };
    candidates.iter().map(PathBuf::from).find(|p| p.is_file())
}

/// The `--browser` value: the system's Chrome, or the one Playwright downloads.
pub fn channel() -> &'static str {
    if system_chrome().is_some() {
        "chrome"
    } else {
        "chromium"
    }
}

fn bundled_present() -> bool {
    std::fs::read_dir(browsers_dir())
        .map(|d| d.flatten().any(|e| e.file_name().to_string_lossy().starts_with("chromium-")))
        .unwrap_or(false)
}

/// Ready to start: the package at the pinned version, Node.js, and a browser.
pub fn ready() -> bool {
    installed() && node().is_ok() && (system_chrome().is_some() || bundled_present())
}

/// A `node` on PATH at version 18 or newer.
pub fn node() -> Result<PathBuf> {
    let path = std::env::var_os("PATH").unwrap_or_default();
    let node = std::env::split_paths(&path)
        .map(|d| d.join("node"))
        .find(|p| p.is_file())
        .context("the browser needs Node.js 18 or newer (https://nodejs.org), and `node` isn't on PATH")?;
    let out = Command::new(&node).arg("--version").output().with_context(|| format!("cannot run {}", node.display()))?;
    let text = String::from_utf8_lossy(&out.stdout);
    match major(&text) {
        Some(m) if m >= NODE_MIN => Ok(node),
        _ => bail!("the browser needs Node.js {NODE_MIN} or newer; {} is {}", node.display(), text.trim()),
    }
}

fn major(version: &str) -> Option<u32> {
    version.trim().trim_start_matches('v').split('.').next()?.parse().ok()
}

fn npm(node: &Path) -> Result<PathBuf> {
    // The npm that comes with this node, else any on PATH.
    let beside = node.with_file_name("npm");
    if beside.is_file() {
        return Ok(beside);
    }
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .map(|d| d.join("npm"))
        .find(|p| p.is_file())
        .context("the browser is installed with npm, which isn't on PATH (it comes with Node.js)")
}

/// Installs (or updates) the browser, with a spinner.
pub fn install() -> Result<()> {
    let sp = ui::spinner(format!("Setting up the browser (Playwright MCP {MCP_VERSION}; one time)"));
    let result = install_inner(|msg| sp.set_message(msg));
    match &result {
        Ok(()) => sp.done(format!("Browser ready (Playwright MCP {MCP_VERSION}, {})", describe_browser())),
        Err(_) => sp.clear(),
    }
    result
}

fn install_inner(say: impl Fn(String)) -> Result<()> {
    let node = node()?;
    let d = dir();
    std::fs::create_dir_all(&d).with_context(|| format!("cannot create {}", d.display()))?;
    if !installed() {
        // A package.json of its own, so npm never walks up into another project.
        let manifest = d.join("package.json");
        if !manifest.is_file() {
            std::fs::write(&manifest, "{\"private\": true}\n")?;
        }
        let spec = format!("{PACKAGE}@{MCP_VERSION}");
        let out = Command::new(npm(&node)?)
            .args(["install", "--prefix"])
            .arg(&d)
            .args(["--save-exact", "--no-audit", "--no-fund", "--loglevel=error", &spec])
            .output()
            .context("cannot run npm")?;
        if !out.status.success() {
            bail!("installing {spec} failed: {}", last_lines(&out.stderr, 3));
        }
        if !installed() {
            bail!("installed {spec}, but {} isn't there", cli().display());
        }
    }
    if system_chrome().is_none() && !bundled_present() {
        say("Downloading Chrome for Testing for the browser (about 170 MB)".into());
        let out = Command::new(&node)
            .arg(cli())
            .args(["install-browser", BUNDLED])
            .env("PLAYWRIGHT_BROWSERS_PATH", browsers_dir())
            .output()
            .context("cannot run the browser installer")?;
        if !out.status.success() || !bundled_present() {
            bail!("downloading the browser failed: {}", last_lines(&[out.stdout, out.stderr].concat(), 3));
        }
    }
    Ok(())
}

pub fn describe_browser() -> String {
    match system_chrome() {
        Some(p) => format!("Google Chrome at {}", p.display()),
        None if bundled_present() => format!("Chrome for Testing in {}", browsers_dir().display()),
        None => "no browser yet".into(),
    }
}

fn last_lines(bytes: &[u8], n: usize) -> String {
    let text = String::from_utf8_lossy(bytes);
    let lines: Vec<_> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    lines[lines.len().saturating_sub(n)..].join(" / ")
}

/// What the chat needs to start it, as environment variables.
pub fn env(auto: bool) -> Vec<(&'static str, String)> {
    vec![
        ("OPPX_BROWSER_AUTO", if auto { "1" } else { "0" }.into()),
        ("OPPX_BROWSER_CLI", cli().display().to_string()),
        ("OPPX_BROWSER_NODE", node().map(|p| p.display().to_string()).unwrap_or_default()),
        ("OPPX_BROWSER_CHANNEL", channel().into()),
        ("OPPX_BROWSER_PATH", browsers_dir().display().to_string()),
        ("OPPX_BROWSER_READY", if ready() { "1" } else { "0" }.into()),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_node_major() {
        assert_eq!(major("v24.15.0\n"), Some(24));
        assert_eq!(major("v16.2.0"), Some(16));
        assert_eq!(major("garbage"), None);
    }
}
