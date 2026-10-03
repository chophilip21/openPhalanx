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

/// Finds `aider` on `PATH`.
pub fn find_aider() -> Result<PathBuf> {
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .map(|dir| dir.join("aider"))
        .find(|p| is_executable(p))
        .context(
            "aider is not installed (or not on PATH). Install it with one of:\n  \
             uv tool install --python 3.12 aider-chat\n  pipx install --python python3.12 aider-chat\n\
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

/// Aider's arguments: our defaults, then the user's (which may override the
/// defaults), then the no-commit flags (which nothing may override).
pub fn aider_args(metadata: &Path, user_args: &[OsString]) -> Vec<OsString> {
    let mut args: Vec<OsString> = [
        "--model",
        AIDER_MODEL,
        "--edit-format",
        "diff",
        "--no-show-model-warnings",
        "--no-check-update",
        "--no-analytics",
        "--model-metadata-file",
    ]
    .iter()
    .map(OsString::from)
    .collect();
    args.push(metadata.as_os_str().to_owned());
    args.extend(user_args.iter().cloned());
    args.extend(NO_COMMIT_FLAGS.iter().map(OsString::from));
    args
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
        let args = aider_args(Path::new("/tmp/m.json"), &user);
        let tail: Vec<_> = args.iter().rev().take(2).map(|a| a.to_string_lossy().to_string()).collect();
        assert_eq!(tail, vec!["--no-dirty-commits", "--no-auto-commits"]);
        // User's --edit-format comes after the default, so it wins.
        let pos = |v: &str| args.iter().rposition(|a| a == v).unwrap();
        assert!(pos("whole") > pos("diff"));
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
