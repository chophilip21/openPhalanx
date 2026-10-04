//! The coding engine (Aider), managed by oppx so users never install it.
//!
//! oppx keeps a private copy in its data directory (`~/.local/share/oppx/engine`
//! on Linux): uv, a uv-managed Python 3.12, and `aider-chat` pinned to
//! [`agent::AIDER_VERSION`]. Nothing touches the user's own Python or PATH, so a
//! system Python 3.13 (which breaks Aider) doesn't matter.
//!
//! An `aider` on PATH is still used when it is exactly the pinned version
//! (handy on development machines); otherwise the private copy is installed
//! on first use, which takes about a minute.

use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{bail, Context, Result};

use crate::{agent, ui};

const UV_INSTALLER: &str = "https://astral.sh/uv/install.sh";

/// `~/.local/share/oppx/engine` (or the platform's data directory).
pub fn dir() -> PathBuf {
    std::env::var_os("OPPX_ENGINE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| dirs::data_dir().unwrap_or_else(|| PathBuf::from(".")).join("oppx/engine"))
}

fn bin_dir() -> PathBuf {
    dir().join("bin")
}

/// The private Aider launcher (may not exist yet).
pub fn private_aider() -> PathBuf {
    bin_dir().join("aider")
}

/// Aider's version, read from the `aider_chat-X.Y.Z.dist-info` folder next to
/// its interpreter. Much faster than importing aider (about 2 s).
pub fn version_of(aider: &Path) -> Option<String> {
    let python = agent::aider_python(aider).ok()?;
    let venv = python.parent()?.parent()?;
    let lib = std::fs::read_dir(venv.join("lib")).ok()?;
    for py in lib.flatten() {
        let Ok(entries) = std::fs::read_dir(py.path().join("site-packages")) else { continue };
        for e in entries.flatten() {
            let name = e.file_name().to_string_lossy().to_string();
            if let Some(v) = name.strip_prefix("aider_chat-").and_then(|r| r.strip_suffix(".dist-info")) {
                return Some(v.to_string());
            }
        }
    }
    None
}

/// An installed engine at the pinned version, if there is one: the private
/// copy first, then an `aider` on PATH.
pub fn find() -> Option<PathBuf> {
    let private = private_aider();
    if private.is_file() && version_of(&private).as_deref() == Some(agent::AIDER_VERSION) {
        return Some(private);
    }
    agent::find_aider().ok().filter(|a| version_of(a).as_deref() == Some(agent::AIDER_VERSION))
}

/// Any Aider at all (for `--classic` and as a last resort): pinned first.
pub fn find_any() -> Option<PathBuf> {
    find().or_else(|| Some(private_aider()).filter(|p| p.is_file())).or_else(|| agent::find_aider().ok())
}

/// The engine to run, installing the private copy first if needed.
pub async fn ensure() -> Result<PathBuf> {
    if let Some(a) = find() {
        return Ok(a);
    }
    install().await?;
    Ok(private_aider())
}

fn uv_candidates() -> Vec<PathBuf> {
    let home = dirs::home_dir().unwrap_or_default();
    let mut c = vec![bin_dir().join("uv")];
    let path = std::env::var_os("PATH").unwrap_or_default();
    c.extend(std::env::split_paths(&path).map(|d| d.join("uv")));
    c.extend([home.join(".local/bin/uv"), home.join(".cargo/bin/uv")]);
    c
}

/// A uv binary: an existing one, or a private copy from Astral's installer.
async fn uv() -> Result<PathBuf> {
    if let Some(uv) = uv_candidates().into_iter().find(|p| p.is_file()) {
        return Ok(uv);
    }
    let script = reqwest::get(UV_INSTALLER)
        .await
        .and_then(|r| r.error_for_status())
        .context("cannot download uv (needed once to install the coding engine); check the internet connection")?
        .text()
        .await?;
    std::fs::create_dir_all(bin_dir())?;
    let file = tempfile::NamedTempFile::new()?;
    std::fs::write(file.path(), script)?;
    let out = Command::new("sh")
        .arg(file.path())
        .env("UV_UNMANAGED_INSTALL", bin_dir()) // into our folder, no shell profile changes
        .env("UV_PRINT_QUIET", "1")
        .output()
        .context("cannot run sh")?;
    let uv = bin_dir().join("uv");
    if !out.status.success() || !uv.is_file() {
        bail!("installing uv failed: {}", last_lines(&out.stderr, 3));
    }
    Ok(uv)
}

fn last_lines(bytes: &[u8], n: usize) -> String {
    let text = String::from_utf8_lossy(bytes);
    let lines: Vec<_> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    lines[lines.len().saturating_sub(n)..].join(" / ")
}

/// Installs (or replaces) the private engine at the pinned version.
pub async fn install() -> Result<()> {
    let sp = ui::spinner(format!(
        "Setting up the coding engine (Aider {}; one time, about a minute)",
        agent::AIDER_VERSION
    ));
    let result = install_inner().await;
    match &result {
        Ok(()) => sp.done(format!("Coding engine ready (Aider {})", agent::AIDER_VERSION)),
        Err(_) => sp.clear(),
    }
    result
}

async fn install_inner() -> Result<()> {
    let uv = uv().await?;
    let d = dir();
    let spec = format!("aider-chat=={}", agent::AIDER_VERSION);
    let out = Command::new(&uv)
        .args(["tool", "install", "--force", "--python", "3.12", &spec])
        .env("UV_TOOL_DIR", d.join("tools"))
        .env("UV_TOOL_BIN_DIR", bin_dir())
        .env("UV_PYTHON_INSTALL_DIR", d.join("python"))
        // A uv-managed 3.12, never the system Python (3.13 breaks Aider).
        .env("UV_PYTHON_PREFERENCE", "only-managed")
        .env_remove("VIRTUAL_ENV")
        .output()
        .with_context(|| format!("cannot run {}", uv.display()))?;
    if !out.status.success() {
        bail!("installing {spec} failed: {}", last_lines(&out.stderr, 3));
    }
    if version_of(&private_aider()).as_deref() != Some(agent::AIDER_VERSION) {
        bail!("installed {spec}, but {} doesn't report it", private_aider().display());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_version_from_dist_info() {
        let root = tempfile::tempdir().unwrap();
        let venv = root.path().join("venv");
        std::fs::create_dir_all(venv.join("bin")).unwrap();
        std::fs::create_dir_all(venv.join("lib/python3.12/site-packages/aider_chat-0.86.2.dist-info")).unwrap();
        let python = venv.join("bin/python");
        std::fs::write(&python, "").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&python, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let aider = venv.join("bin/aider");
        std::fs::write(&aider, format!("#!{}\nimport aider\n", python.display())).unwrap();
        assert_eq!(version_of(&aider).as_deref(), Some("0.86.2"));
    }
}
