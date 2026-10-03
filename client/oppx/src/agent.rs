//! Launches the user's local Aider against the loopback proxy.

use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde_json::json;

/// Model name Aider uses; the gateway serves its one model under any name.
pub const AIDER_MODEL: &str = "openai/openphalanx-coder";

/// Flags that always come last so no user argument can re-enable commits:
/// the agent edits the working tree, and the user commits.
pub const NO_COMMIT_FLAGS: [&str; 2] = ["--no-auto-commits", "--no-dirty-commits"];

/// The OpenPhalanx chat frontend (Python), run with Aider's own interpreter.
pub const FRONTEND: &str = include_str!("../frontend/oppx_chat.py");

/// The Python interpreter Aider was installed with, from its launcher's
/// shebang (`uv tool` and `pipx` write an absolute path there), so the
/// frontend can import `aider`, `prompt_toolkit` and `rich`.
pub fn aider_python(aider: &Path) -> Result<PathBuf> {
    let head = std::fs::read(aider).with_context(|| format!("cannot read {}", aider.display()))?;
    let first = String::from_utf8_lossy(&head[..head.len().min(512)]).lines().next().unwrap_or_default().to_string();
    let Some(shebang) = first.strip_prefix("#!") else {
        bail!("{} is not a Python launcher; use `oppx --classic`", aider.display());
    };
    let mut parts = shebang.split_whitespace();
    let program = parts.next().unwrap_or_default();
    // `#!/usr/bin/env python3` -> resolve python3 on PATH.
    let interpreter = if program.ends_with("/env") {
        let name = parts.find(|p| !p.starts_with('-')).unwrap_or("python3");
        let path = std::env::var_os("PATH").unwrap_or_default();
        std::env::split_paths(&path)
            .map(|d| d.join(name))
            .find(|p| is_executable(p))
            .with_context(|| format!("cannot find {name} for {}", aider.display()))?
    } else {
        PathBuf::from(program)
    };
    if !is_executable(&interpreter) {
        bail!("Aider's Python ({}) is missing; reinstall Aider or use `oppx --classic`", interpreter.display());
    }
    Ok(interpreter)
}

/// Writes the frontend to `<cache dir>/oppx/oppx_chat-<version>.py` (only
/// when missing or different) and returns its path.
pub fn write_frontend() -> Result<PathBuf> {
    let dir = dirs::cache_dir().context("cannot determine a cache directory")?.join("oppx");
    std::fs::create_dir_all(&dir)?;
    let path = dir.join(format!("oppx_chat-{}.py", env!("CARGO_PKG_VERSION")));
    if std::fs::read_to_string(&path).ok().as_deref() != Some(FRONTEND) {
        let tmp = path.with_extension("py.tmp");
        std::fs::write(&tmp, FRONTEND)?;
        std::fs::rename(&tmp, &path)?;
    }
    Ok(path)
}

/// Finds `aider` on `PATH`.
pub fn find_aider() -> Result<PathBuf> {
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .map(|dir| dir.join("aider"))
        .find(|p| is_executable(p))
        .context(
            "aider is not installed (or not on PATH). Install it with one of:\n  \
             uv tool install --python 3.12 aider-chat==0.86.2\n  pipx install --python python3.12 aider-chat==0.86.2\n\
             (Aider needs Python 3.12 or older; with 3.13 it fails on a missing `audioop` module.)",
        )
}

#[cfg(unix)]
fn is_executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

#[cfg(not(unix))]
fn is_executable(p: &Path) -> bool {
    p.is_file()
}

/// Aider model metadata, so it knows the real context window instead of
/// warning about an unknown model. Written to a private temp file.
pub fn model_metadata(context_len: u64) -> Result<tempfile::NamedTempFile> {
    let meta = json!({
        AIDER_MODEL: {
            "max_input_tokens": context_len,
            "max_tokens": context_len,
            "max_output_tokens": 8192.min(context_len / 2),
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
            "litellm_provider": "openai",
            "mode": "chat"
        }
    });
    let mut f = tempfile::Builder::new().prefix("oppx-model-").suffix(".json").tempfile()?;
    f.write_all(serde_json::to_string_pretty(&meta)?.as_bytes())?;
    f.flush()?;
    Ok(f)
}

/// OpenPhalanx look for Aider's terminal UI (it can't be reshaped beyond colors).
const THEME: [&str; 16] = [
    "--user-input-color", "#5EEAD4",
    "--tool-output-color", "#8A95AB",
    "--tool-warning-color", "#FBBF24",
    "--tool-error-color", "#F87171",
    "--code-theme", "monokai",
    "--completion-menu-color", "#E8EDF6",
    "--completion-menu-bg-color", "#171F31",
    "--completion-menu-current-bg-color", "#1E2840",
];

/// Where one repo's chat and input history live: outside the repo, so the
/// agent leaves no files behind (`<state dir>/oppx/history/<repo id>.*`).
pub struct History {
    pub chat: PathBuf,
    pub input: PathBuf,
}

pub fn history_for(repo_root: &Path) -> Result<History> {
    use sha2::{Digest, Sha256};
    let base = dirs::state_dir()
        .or_else(dirs::data_local_dir)
        .context("cannot determine a state directory")?
        .join("oppx")
        .join("history");
    std::fs::create_dir_all(&base)?;
    let canonical = repo_root.canonicalize().unwrap_or_else(|_| repo_root.to_path_buf());
    let id: String = Sha256::digest(canonical.as_os_str().as_encoded_bytes())
        .iter()
        .take(8)
        .map(|b| format!("{b:02x}"))
        .collect();
    let stem = format!(
        "{}-{id}",
        canonical.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "repo".into())
    );
    Ok(History { chat: base.join(format!("{stem}.chat.md")), input: base.join(format!("{stem}.input")) })
}

/// Aider's arguments: our defaults, then the user's (which may override the
/// defaults), then the no-commit flags (which nothing may override).
pub fn aider_args(metadata: &Path, history: &History, user_args: &[OsString]) -> Vec<OsString> {
    let mut args: Vec<OsString> = [
        "--model",
        AIDER_MODEL,
        "--edit-format",
        "diff",
        "--no-show-model-warnings",
        "--no-check-update",
        "--no-show-release-notes",
        "--no-analytics",
        // Answer Aider's confirmations (e.g. adding files the model asks for).
        // Shell commands still need an explicit yes; commits stay disabled below.
        "--yes-always",
        // Never edit the user's .gitignore; .aider* goes to .git/info/exclude instead.
        "--no-gitignore",
    ]
    .iter()
    .chain(THEME.iter())
    .map(OsString::from)
    .collect();
    args.push("--chat-history-file".into());
    args.push(history.chat.clone().into_os_string());
    args.push("--input-history-file".into());
    args.push(history.input.clone().into_os_string());
    args.push("--model-metadata-file".into());
    args.push(metadata.as_os_str().to_owned());
    args.extend(user_args.iter().cloned());
    args.extend(NO_COMMIT_FLAGS.iter().map(OsString::from));
    args
}

/// The git repo containing `dir`, if any.
pub fn repo_root(dir: &Path) -> Option<PathBuf> {
    let out = std::process::Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .current_dir(dir)
        .output()
        .ok()?;
    out.status.success().then(|| PathBuf::from(String::from_utf8_lossy(&out.stdout).trim()))
}

/// Hides the agent's repo-map cache (`.aider.tags.cache.*`, which can't be
/// relocated) from `git status` via `.git/info/exclude`, which is local to this
/// clone and never committed; the user's `.gitignore` is left alone.
pub fn exclude_agent_files(repo: &Path) -> Result<()> {
    let out = std::process::Command::new("git")
        .args(["rev-parse", "--git-path", "info/exclude"])
        .current_dir(repo)
        .output()?;
    if !out.status.success() {
        return Ok(());
    }
    let path = repo.join(String::from_utf8_lossy(&out.stdout).trim());
    let current = std::fs::read_to_string(&path).unwrap_or_default();
    if current.lines().any(|l| l.trim() == ".aider*") {
        return Ok(());
    }
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let sep = if current.is_empty() || current.ends_with('\n') { "" } else { "\n" };
    std::fs::write(&path, format!("{current}{sep}# local files of the oppx coding agent\n.aider*\n"))?;
    Ok(())
}

/// Git identity for the agent's process only, when the repo has none: Aider
/// otherwise writes "Your Name" into the repo's .git/config. Passed as
/// command-scope config (GIT_CONFIG_COUNT), which nothing persists, and never
/// overrides an identity the user has. The agent never commits anyway.
pub fn placeholder_git_identity(repo: &Path) -> Vec<(String, String)> {
    if std::env::var_os("GIT_CONFIG_COUNT").is_some() {
        return Vec::new(); // don't clobber the user's own command-scope config
    }
    let has = |key: &str| {
        std::process::Command::new("git")
            .args(["config", "--get", key])
            .current_dir(repo)
            .output()
            .is_ok_and(|o| o.status.success() && !o.stdout.trim_ascii().is_empty())
    };
    let missing: Vec<(&str, &str)> = [("user.name", "OpenPhalanx agent"), ("user.email", "agent@openphalanx.invalid")]
        .into_iter()
        .filter(|(k, _)| !has(k))
        .collect();
    if missing.is_empty() {
        return Vec::new();
    }
    let mut env = vec![("GIT_CONFIG_COUNT".to_string(), missing.len().to_string())];
    for (i, (k, v)) in missing.iter().enumerate() {
        env.push((format!("GIT_CONFIG_KEY_{i}"), k.to_string()));
        env.push((format!("GIT_CONFIG_VALUE_{i}"), v.to_string()));
    }
    env
}

/// Rejects arguments that would turn commits back on, with a clear reason.
pub fn reject_commit_flags(user_args: &[OsString]) -> Result<()> {
    for a in user_args {
        let s = a.to_string_lossy();
        if matches!(s.as_ref(), "--auto-commits" | "--dirty-commits") {
            bail!("{s} is not allowed: oppx never lets the agent commit. Review with `git diff` and commit yourself.");
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_commit_flags_come_last() {
        let user = vec![OsString::from("--edit-format"), OsString::from("whole"), OsString::from("src/main.rs")];
        let history = History { chat: "/h/c.md".into(), input: "/h/i".into() };
        let args = aider_args(Path::new("/tmp/m.json"), &history, &user);
        for flag in ["--no-show-release-notes", "--yes-always", "--no-gitignore"] {
            assert!(args.iter().any(|a| a == flag), "{flag}");
        }
        let tail: Vec<_> = args.iter().rev().take(2).map(|a| a.to_string_lossy().to_string()).collect();
        assert_eq!(tail, vec!["--no-dirty-commits", "--no-auto-commits"]);
        // User's --edit-format comes after the default, so it wins.
        let pos = |v: &str| args.iter().rposition(|a| a == v).unwrap();
        assert!(pos("whole") > pos("diff"));
    }

    #[test]
    fn excludes_agent_files_locally_and_idempotently() {
        let dir = tempfile::tempdir().unwrap();
        let ok = std::process::Command::new("git").args(["init", "-q"]).current_dir(dir.path()).status().unwrap();
        assert!(ok.success());
        exclude_agent_files(dir.path()).unwrap();
        exclude_agent_files(dir.path()).unwrap();
        let ex = std::fs::read_to_string(dir.path().join(".git/info/exclude")).unwrap();
        assert_eq!(ex.matches(".aider*").count(), 1);
        assert!(!dir.path().join(".gitignore").exists());
    }

    #[test]
    fn history_lives_outside_the_repo() {
        let dir = tempfile::tempdir().unwrap();
        let h = history_for(dir.path()).unwrap();
        assert!(!h.chat.starts_with(dir.path()) && !h.input.starts_with(dir.path()));
        assert_eq!(history_for(dir.path()).unwrap().chat, h.chat, "stable per repo");
    }

    #[test]
    fn finds_python_from_shebang() {
        let dir = tempfile::tempdir().unwrap();
        let py = dir.path().join("python3");
        std::fs::write(&py, "").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&py, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let launcher = dir.path().join("aider");
        std::fs::write(&launcher, format!("#!{}\nimport aider\n", py.display())).unwrap();
        assert_eq!(aider_python(&launcher).unwrap(), py);
        std::fs::write(&launcher, "ELF binary").unwrap();
        assert!(aider_python(&launcher).is_err());
    }

    #[test]
    fn frontend_is_embedded() {
        assert!(FRONTEND.contains("def run(argv)") && FRONTEND.contains("return_coder=True"));
    }

    #[test]
    fn refuses_commit_flags() {
        assert!(reject_commit_flags(&[OsString::from("--auto-commits")]).is_err());
        assert!(reject_commit_flags(&[OsString::from("--dirty-commits")]).is_err());
        assert!(reject_commit_flags(&[OsString::from("--message"), OsString::from("hi")]).is_ok());
    }

    #[test]
    fn metadata_names_the_model() {
        let f = model_metadata(32768).unwrap();
        let v: serde_json::Value = serde_json::from_slice(&std::fs::read(f.path()).unwrap()).unwrap();
        assert_eq!(v[AIDER_MODEL]["max_input_tokens"], 32768);
        assert_eq!(v[AIDER_MODEL]["max_output_tokens"], 8192);
    }
}
