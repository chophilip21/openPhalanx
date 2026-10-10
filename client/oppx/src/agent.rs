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

/// The Aider release the chat frontend is built and tested against.
pub const AIDER_VERSION: &str = "0.86.2";

/// The OpenPhalanx chat frontend (a Python package plus its launcher), run
/// with Aider's own interpreter. Paths are relative to the frontend folder.
pub const FRONTEND: &[(&str, &str)] = &[
    ("run.py", include_str!("../frontend/run.py")),
    ("oppx_chat/__init__.py", include_str!("../frontend/oppx_chat/__init__.py")),
    ("oppx_chat/config.py", include_str!("../frontend/oppx_chat/config.py")),
    ("oppx_chat/term.py", include_str!("../frontend/oppx_chat/term.py")),
    ("oppx_chat/render.py", include_str!("../frontend/oppx_chat/render.py")),
    ("oppx_chat/context.py", include_str!("../frontend/oppx_chat/context.py")),
    ("oppx_chat/mcp.py", include_str!("../frontend/oppx_chat/mcp.py")),
    ("oppx_chat/agent.py", include_str!("../frontend/oppx_chat/agent.py")),
    ("oppx_chat/oppx_io.py", include_str!("../frontend/oppx_chat/oppx_io.py")),
    ("oppx_chat/routing.py", include_str!("../frontend/oppx_chat/routing.py")),
    ("oppx_chat/cache.py", include_str!("../frontend/oppx_chat/cache.py")),
    ("oppx_chat/commands.py", include_str!("../frontend/oppx_chat/commands.py")),
    ("oppx_chat/app.py", include_str!("../frontend/oppx_chat/app.py")),
];

/// The Python interpreter Aider was installed with, from its launcher's
/// shebang (`uv tool` and `pipx` write an absolute path there), so the
/// frontend can import `aider`, `prompt_toolkit` and `rich`.
pub fn aider_python(aider: &Path) -> Result<PathBuf> {
    // uv and pipx install into a venv: use the `python` next to the real
    // launcher. (With long paths uv writes a `#!/bin/sh` trampoline instead
    // of a Python shebang, so the shebang alone isn't enough.)
    if let Some(bin) = std::fs::canonicalize(aider).ok().and_then(|p| p.parent().map(Path::to_path_buf)) {
        let python = bin.join("python");
        if is_executable(&python) && bin.parent().is_some_and(|v| v.join("pyvenv.cfg").is_file()) {
            return Ok(python);
        }
    }
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
    use sha2::{Digest, Sha256};
    let mut hash = Sha256::new();
    for (path, text) in FRONTEND {
        hash.update(path.as_bytes());
        hash.update(text.as_bytes());
    }
    let id = format!("{:x}", hash.finalize());
    // One folder per frontend build, so a running session is never changed underneath.
    let root = dirs::cache_dir().context("cannot determine a cache directory")?.join("oppx");
    let dir = root.join(format!("frontend-{}-{}", env!("CARGO_PKG_VERSION"), &id[..12]));
    let launcher = dir.join("run.py");
    if !launcher.is_file() {
        let tmp = tempfile::tempdir_in({
            std::fs::create_dir_all(&root)?;
            &root
        })?;
        for (path, text) in FRONTEND {
            let file = tmp.path().join(path);
            std::fs::create_dir_all(file.parent().expect("relative path"))?;
            std::fs::write(file, text)?;
        }
        // Another oppx may have written it meanwhile; either copy is identical.
        if std::fs::rename(tmp.path(), &dir).is_err() && !launcher.is_file() {
            bail!("cannot write the chat frontend to {}", dir.display());
        }
    }
    Ok(launcher)
}

/// Finds `aider` on `PATH`.
pub fn find_aider() -> Result<PathBuf> {
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .map(|dir| dir.join("aider"))
        .find(|p| is_executable(p))
        .context("aider is not on PATH")
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

/// One conversation's history files, kept outside the repo so the agent
/// leaves no files behind: `<state dir>/oppx/history/<repo>-<id>/<session>.md`,
/// plus one input history (up-arrow) shared by the repo's sessions.
pub struct History {
    pub chat: PathBuf,
    pub input: PathBuf,
}

/// A past conversation in this repo.
#[derive(Debug, Clone)]
pub struct Session {
    /// File stem, e.g. `20261003-154210`; what `oppx --resume <id>` takes.
    pub id: String,
    pub path: PathBuf,
    pub modified: std::time::SystemTime,
    pub first_message: String,
    pub messages: usize,
}

/// The per-repo folder holding its sessions.
pub fn sessions_dir(repo_root: &Path) -> Result<PathBuf> {
    use sha2::{Digest, Sha256};
    let base = dirs::state_dir()
        .or_else(dirs::data_local_dir)
        .context("cannot determine a state directory")?
        .join("oppx")
        .join("history");
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
    let dir = base.join(&stem);
    std::fs::create_dir_all(&dir)?;
    // Earlier versions kept one ever-growing file per repo; keep it as a session.
    let legacy = base.join(format!("{stem}.chat.md"));
    if legacy.is_file() {
        let _ = std::fs::rename(&legacy, dir.join("00000000-000000.md"));
    }
    let legacy_input = base.join(format!("{stem}.input"));
    if legacy_input.is_file() {
        let _ = std::fs::rename(&legacy_input, dir.join("input.history"));
    }
    Ok(dir)
}

/// A fresh conversation.
pub fn new_session(repo_root: &Path) -> Result<History> {
    let dir = sessions_dir(repo_root)?;
    let stamp = chrono_stamp();
    Ok(History { chat: dir.join(format!("{stamp}.md")), input: dir.join("input.history") })
}

/// An existing conversation, to be restored.
pub fn open_session(repo_root: &Path, session: &Session) -> Result<History> {
    Ok(History { chat: session.path.clone(), input: sessions_dir(repo_root)?.join("input.history") })
}

/// `YYYYmmdd-HHMMSS` in local time, without a date crate.
fn chrono_stamp() -> String {
    let out = std::process::Command::new("date").arg("+%Y%m%d-%H%M%S").output();
    match out {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).trim().to_string(),
        _ => format!(
            "{}",
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
        ),
    }
}

/// Past conversations in this repo, newest first (empty files skipped).
pub fn list_sessions(repo_root: &Path) -> Result<Vec<Session>> {
    let dir = sessions_dir(repo_root)?;
    let mut sessions = Vec::new();
    for entry in std::fs::read_dir(&dir)? {
        let path = entry?.path();
        if path.extension().is_none_or(|e| e != "md") {
            continue;
        }
        let text = std::fs::read_to_string(&path).unwrap_or_default();
        // Aider writes each user message as a "#### " line.
        let user: Vec<&str> = text
            .lines()
            .filter_map(|l| l.strip_prefix("#### "))
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .collect();
        if user.is_empty() {
            continue;
        }
        let first = user.iter().find(|l| !l.starts_with('/')).unwrap_or(&user[0]);
        sessions.push(Session {
            id: path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default(),
            modified: entry_modified(&path),
            first_message: first.chars().take(80).collect(),
            messages: user.len(),
            path,
        });
    }
    sessions.sort_by_key(|s| std::cmp::Reverse(s.modified));
    Ok(sessions)
}

fn entry_modified(path: &Path) -> std::time::SystemTime {
    std::fs::metadata(path).and_then(|m| m.modified()).unwrap_or(std::time::UNIX_EPOCH)
}

/// Aider model settings for the served model. Aider falls back to
/// `edit_format: whole` and no repo map for model names it doesn't know (ours
/// is `openphalanx-coder`), so state them explicitly. `reasoning_tag` strips
/// `<think>` blocks before edits are parsed, for any model that emits them.
/// `examples_as_sys_msg` keeps Aider's few-shot examples inside the system
/// prompt: sent as fake user turns, gpt-oss took them for the real conversation
/// ("your first question was: Change get_factorial()…").
pub fn model_settings(edit_format: &str) -> Result<tempfile::NamedTempFile> {
    let yaml = format!(
        "- name: {AIDER_MODEL}\n  edit_format: {edit_format}\n  use_repo_map: true\n  reasoning_tag: think\n  \
         examples_as_sys_msg: true\n"
    );
    let mut f = tempfile::Builder::new().prefix("oppx-settings-").suffix(".yml").tempfile()?;
    f.write_all(yaml.as_bytes())?;
    f.flush()?;
    Ok(f)
}

/// Aider's arguments: our defaults, then the user's (which may override the
/// defaults), then the no-commit flags (which nothing may override).
pub fn aider_args(
    metadata: &Path,
    settings: &Path,
    edit_format: &str,
    history: &History,
    restore: bool,
    user_args: &[OsString],
) -> Vec<OsString> {
    let mut args: Vec<OsString> = [
        "--model",
        AIDER_MODEL,
        "--edit-format",
        edit_format,
        "--no-show-model-warnings",
        "--no-check-update",
        "--no-show-release-notes",
        "--no-analytics",
        // Answer Aider's confirmations (e.g. adding files the model asks for).
        // Shell commands still need an explicit yes; commits stay disabled below.
        "--yes-always",
        // Never edit the user's .gitignore; .aider* goes to .git/info/exclude instead.
        "--no-gitignore",
        // Aider offers to run any ```bash block in a reply, even one that is
        // just quoted file content (e.g. a README's install steps, sudo and all).
        // Commands run only when the user types them (!cmd / /run).
        "--no-suggest-shell-commands",
    ]
    .iter()
    .chain(THEME.iter())
    .map(OsString::from)
    .collect();
    args.push("--chat-history-file".into());
    args.push(history.chat.clone().into_os_string());
    args.push("--input-history-file".into());
    args.push(history.input.clone().into_os_string());
    if restore {
        args.push("--restore-chat-history".into());
    }
    args.push("--model-settings-file".into());
    args.push(settings.as_os_str().to_owned());
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
        let args = aider_args(Path::new("/tmp/m.json"), Path::new("/tmp/s.yml"), "diff", &history, false, &user);
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
    fn sessions_live_outside_the_repo_and_list_newest_first() {
        let repo = tempfile::tempdir().unwrap();
        let state = tempfile::tempdir().unwrap();
        // SAFETY: tests in this module don't read XDG_STATE_HOME concurrently.
        unsafe { std::env::set_var("XDG_STATE_HOME", state.path()) };
        let h = new_session(repo.path()).unwrap();
        assert!(!h.chat.starts_with(repo.path()) && !h.input.starts_with(repo.path()));
        let dir = sessions_dir(repo.path()).unwrap();
        std::fs::write(dir.join("20260101-090000.md"), "# aider chat\n#### /add x.rs\n#### fix the parser bug\nok\n").unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::write(dir.join("20260102-090000.md"), "#### add tests\n").unwrap();
        std::fs::write(dir.join("20260103-090000.md"), "no user messages\n").unwrap();
        let s = list_sessions(repo.path()).unwrap();
        assert_eq!(s.iter().map(|s| s.id.as_str()).collect::<Vec<_>>(), ["20260102-090000", "20260101-090000"]);
        assert_eq!(s[1].first_message, "fix the parser bug");
        assert_eq!(s[1].messages, 2);
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
        let app = FRONTEND.iter().find(|(p, _)| *p == "oppx_chat/app.py").expect("app module").1;
        assert!(app.contains("def run(argv)") && app.contains("return_coder=True"));
        // Every module in the package must be embedded, or imports fail at runtime.
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("frontend/oppx_chat");
        for entry in std::fs::read_dir(dir).unwrap().flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if name.ends_with(".py") {
                let path = format!("oppx_chat/{name}");
                assert!(FRONTEND.iter().any(|(p, _)| *p == path), "{path} is not in agent::FRONTEND");
            }
        }
    }

    #[test]
    fn writes_the_frontend_package() {
        let launcher = write_frontend().unwrap();
        assert!(launcher.ends_with("run.py"));
        assert!(launcher.parent().unwrap().join("oppx_chat/app.py").is_file());
    }

    #[test]
    fn model_settings_name_the_model() {
        let f = model_settings("whole").unwrap();
        let y = std::fs::read_to_string(f.path()).unwrap();
        assert!(y.contains("name: openai/openphalanx-coder") && y.contains("edit_format: whole"));
        assert!(y.contains("use_repo_map: true") && y.contains("reasoning_tag: think"));
        assert!(y.contains("examples_as_sys_msg: true"));
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
