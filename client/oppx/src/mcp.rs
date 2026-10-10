//! Connectors: the MCP servers whose tools the model can call in the chat.
//!
//! This side owns the definitions, in the `mcpServers` format every MCP
//! client reads: the user's own in `mcp.json` next to oppx's config (0600, it
//! may hold tokens), and a repository's in `.mcp.json` at its root. The chat
//! frontend only reads them; it is the MCP client (`oppx_chat/mcp.py`), and
//! `probe` runs that module to check a connector and measure its tool list.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use serde_json::{json, Map, Value};

use crate::{agent, config, engine};

/// A repository's own connectors, at its root.
pub const PROJECT_FILE: &str = ".mcp.json";
/// A context window this small (or smaller) gets a warning whenever a connector is added.
pub const SMALL_CONTEXT: u64 = 32_768;
/// ... and any window, when one connector's tool list takes this share of every request.
pub const HEAVY_SHARE: f64 = 0.10;
const PROBE_TIMEOUT: Duration = Duration::from_secs(45);

/// The user's connectors: `OPPX_MCP_CONFIG`, or `mcp.json` next to the config file.
pub fn user_path(config: &Path) -> PathBuf {
    match std::env::var_os("OPPX_MCP_CONFIG").filter(|p| !p.is_empty()) {
        Some(p) => PathBuf::from(p),
        None => config.with_file_name("mcp.json"),
    }
}

/// Where the chat looks for a repository's connectors from `dir`: the git
/// root, or `dir` itself outside a repository.
pub fn project_root(dir: &Path) -> PathBuf {
    agent::repo_root(dir).unwrap_or_else(|| dir.to_path_buf())
}

/// Names are part of the tool names the model writes (`name.tool`).
pub fn validate_name(name: &str) -> Result<()> {
    let ok = !name.is_empty() && name.len() <= 32 && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if !ok {
        bail!("a connector's name may only have letters, digits, - and _ (at most 32), e.g. github");
    }
    Ok(())
}

/// One definitions file. Keys other than `mcpServers` are kept as they are.
pub struct File {
    pub path: PathBuf,
    doc: Map<String, Value>,
    private: bool,
}

impl File {
    /// A missing file means "no connectors yet". `private`: written 0600.
    pub fn load(path: &Path, private: bool) -> Result<File> {
        let doc = match std::fs::read(path) {
            Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
                Ok(Value::Object(doc)) => doc,
                _ => bail!("{} is not a valid connectors file (expected {{\"mcpServers\": {{…}}}})", path.display()),
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Map::new(),
            Err(e) => return Err(e).with_context(|| format!("cannot read {}", path.display())),
        };
        Ok(File { path: path.to_path_buf(), doc, private })
    }

    /// Name and definition of every connector.
    pub fn servers(&self) -> Vec<(String, Value)> {
        match self.doc.get("mcpServers") {
            Some(Value::Object(m)) => m.iter().filter(|(_, v)| v.is_object()).map(|(k, v)| (k.clone(), v.clone())).collect(),
            _ => Vec::new(),
        }
    }

    pub fn get(&self, name: &str) -> Option<Value> {
        self.servers().into_iter().find(|(n, _)| n == name).map(|(_, v)| v)
    }

    /// Returns whether it replaced one.
    pub fn insert(&mut self, name: &str, spec: Value) -> bool {
        let servers = self.doc.entry("mcpServers").or_insert_with(|| json!({}));
        if !servers.is_object() {
            *servers = json!({});
        }
        servers.as_object_mut().expect("just made an object").insert(name.to_string(), spec).is_some()
    }

    pub fn remove(&mut self, name: &str) -> bool {
        match self.doc.get_mut("mcpServers") {
            Some(Value::Object(m)) => m.remove(name).is_some(),
            _ => false,
        }
    }

    pub fn save(&self) -> Result<()> {
        let mut bytes = serde_json::to_vec_pretty(&self.doc)?;
        bytes.push(b'\n');
        if !self.private {
            return std::fs::write(&self.path, bytes).with_context(|| format!("cannot write {}", self.path.display()));
        }
        config::create_private_dir(self.path.parent().context("the connectors file has no parent directory")?)?;
        let tmp = self.path.with_extension("json.tmp");
        config::write_private(&tmp, &bytes)?;
        std::fs::rename(&tmp, &self.path).with_context(|| format!("cannot write {}", self.path.display()))
    }
}

/// A definition from the command line: a URL (streamable HTTP) with
/// `Name: value` headers, or a command with `KEY=VALUE` environment.
pub fn spec(url: Option<&str>, command: &[String], env: &[String], headers: &[String]) -> Result<Value> {
    match (url, command.split_first()) {
        (Some(_), Some(_)) => bail!("give a command or --url, not both"),
        (None, None) => bail!(
            "what should it run? Give a command after `--`, or a URL:\n  \
             oppx mcp add <name> -- <command> [args]\n  oppx mcp add <name> --url https://…"
        ),
        (Some(url), None) => {
            if !(url.starts_with("https://") || url.starts_with("http://")) {
                bail!("--url must start with https:// or http://");
            }
            if !env.is_empty() {
                bail!("--env is for a command; a URL takes --header 'Name: value'");
            }
            let mut out = json!({"type": "http", "url": url});
            if !headers.is_empty() {
                out["headers"] = Value::Object(pairs(headers, ':', "--header 'Name: value'")?);
            }
            Ok(out)
        }
        (None, Some((program, args))) => {
            if !headers.is_empty() {
                bail!("--header is for a URL; a command takes --env KEY=VALUE");
            }
            let mut out = json!({"command": program, "args": args});
            if !env.is_empty() {
                out["env"] = Value::Object(pairs(env, '=', "--env KEY=VALUE")?);
            }
            Ok(out)
        }
    }
}

fn pairs(items: &[String], sep: char, usage: &str) -> Result<Map<String, Value>> {
    let mut out = Map::new();
    for item in items {
        let (k, v) = item.split_once(sep).with_context(|| format!("\"{item}\" isn't {usage}"))?;
        if k.trim().is_empty() {
            bail!("\"{item}\" isn't {usage}");
        }
        out.insert(k.trim().to_string(), Value::String(v.trim().to_string()));
    }
    Ok(out)
}

/// The command line or URL of a definition (never its env or headers, which may hold secrets).
pub fn describe(spec: &Value) -> String {
    if let Some(url) = spec.get("url").and_then(Value::as_str) {
        return url.to_string();
    }
    let mut words = vec![spec.get("command").and_then(Value::as_str).unwrap_or("?").to_string()];
    if let Some(args) = spec.get("args").and_then(Value::as_array) {
        words.extend(args.iter().map(|a| a.as_str().map_or_else(|| a.to_string(), str::to_string)));
    }
    words.join(" ")
}

/// "about 1,234 tokens of every request (3.8% of the server's 32k context)".
pub fn cost(tokens: u64, ctx: Option<u64>) -> String {
    let share = match ctx {
        Some(c) if c > 0 => format!(" ({:.1}% of the server's {}k context)", 100.0 * tokens as f64 / c as f64, c / 1024),
        _ => String::new(),
    };
    format!("about {} tokens of every request{share}", thousands(tokens))
}

fn thousands(n: u64) -> String {
    let digits = n.to_string();
    let groups: Vec<&str> = digits.as_bytes().rchunks(3).rev().map(|g| std::str::from_utf8(g).expect("ASCII digits")).collect();
    groups.join(",")
}

/// What to tell the user after adding a connector, given the server model's
/// context window and (when it could be measured) the size of the
/// connector's tool list. `None`: nothing to worry about, or the window
/// isn't known.
pub fn context_warning(ctx: Option<u64>, tokens: Option<u64>) -> Option<String> {
    let ctx = ctx.filter(|c| *c > 0)?;
    let small = ctx <= SMALL_CONTEXT;
    let heavy = tokens.is_some_and(|t| t as f64 / ctx as f64 >= HEAVY_SHARE);
    if !small && !heavy {
        return None;
    }
    let window = if small {
        format!("the server's model has a small context window ({}k tokens). ", ctx / 1024)
    } else {
        String::new()
    };
    let takes = match tokens {
        Some(t) if small => format!(
            "This connector's tool list takes about {} tokens of every request ({:.1}%)",
            thousands(t),
            100.0 * t as f64 / ctx as f64
        ),
        Some(t) => format!("This connector's tool list takes {}", cost(t, Some(ctx))),
        None => "Every connector's tool list is sent with every request".to_string(),
    };
    Some(format!(
        "{window}{takes}, and each tool result uses more. Keep only the connectors you need: \
         `oppx mcp list` shows them, `oppx mcp remove <name>` drops one."
    ))
}

/// What starting a connector showed.
#[derive(Debug, serde::Deserialize)]
pub struct Probe {
    pub ok: bool,
    #[serde(default)]
    pub error: String,
    #[serde(default)]
    pub tools: u64,
    /// About what its tool list adds to every request.
    #[serde(default)]
    pub tokens: u64,
}

/// Starts the connector once with the chat's own MCP client and asks for its
/// tools. `None` when the coding engine isn't installed yet (the chat then
/// checks it at its first start) or the check itself couldn't run. `approve`
/// records the user's consent for a repository connector they just added.
pub async fn probe(name: &str, root: &Path, user_file: &Path, approve: bool) -> Option<Probe> {
    let python = agent::aider_python(&engine::find()?).ok()?;
    let frontend = agent::write_frontend().ok()?;
    let mut cmd = tokio::process::Command::new(python);
    cmd.args(["-m", "oppx_chat.mcp", "probe", name, "--root"])
        .arg(root)
        .args(approve.then_some("--approve"))
        .env("PYTHONPATH", frontend.parent()?)
        .env("OPPX_MCP_CONFIG", user_file)
        .env("OPPX_VERSION", env!("CARGO_PKG_VERSION"))
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);
    let out = tokio::time::timeout(PROBE_TIMEOUT, cmd.output()).await.ok()?.ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    serde_json::from_str(text.lines().last()?).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn builds_definitions_in_the_common_format() {
        let s = spec(None, &strings(&["npx", "-y", "@acme/server"]), &strings(&["TOKEN=a=b"]), &[]).unwrap();
        assert_eq!(s, json!({"command": "npx", "args": ["-y", "@acme/server"], "env": {"TOKEN": "a=b"}}));
        assert_eq!(describe(&s), "npx -y @acme/server", "secrets in env are never shown");
        let s = spec(Some("https://mcp.example.com/mcp"), &[], &[], &strings(&["Authorization: Bearer x:y"])).unwrap();
        assert_eq!(s, json!({"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer x:y"}}));
        assert_eq!(describe(&s), "https://mcp.example.com/mcp");
        assert!(spec(None, &[], &[], &[]).is_err(), "neither a command nor a URL");
        assert!(spec(Some("https://x"), &strings(&["npx"]), &[], &[]).is_err(), "both");
        assert!(spec(Some("ftp://x"), &[], &[], &[]).is_err());
        assert!(spec(None, &strings(&["npx"]), &strings(&["NOEQUALS"]), &[]).is_err());
        assert!(spec(None, &strings(&["npx"]), &[], &strings(&["A: b"])).is_err(), "headers are for URLs");
    }

    #[test]
    fn adds_lists_and_removes_keeping_other_keys() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("oppx/mcp.json");
        assert!(File::load(&path, true).unwrap().servers().is_empty(), "a missing file is an empty list");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, r#"{"other": 1, "mcpServers": {"old": {"command": "x"}, "junk": 3}}"#).unwrap();
        let mut f = File::load(&path, true).unwrap();
        assert!(!f.insert("github", json!({"command": "npx", "args": []})));
        assert!(f.insert("github", json!({"command": "uvx", "args": []})), "replaced");
        f.save().unwrap();
        let f = File::load(&path, true).unwrap();
        assert_eq!(f.servers().iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>(), ["github", "old"]);
        assert_eq!(f.get("github").unwrap()["command"], "uvx");
        let raw: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(raw["other"], 1, "keys we don't know stay");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        }
        let mut f = f;
        assert!(f.remove("github") && !f.remove("github"));
        f.save().unwrap();
        assert!(File::load(&path, true).unwrap().get("github").is_none());
        std::fs::write(&path, "[]").unwrap();
        assert!(File::load(&path, true).is_err());
    }

    #[test]
    fn names_are_safe_in_tool_names() {
        assert!(validate_name("github").is_ok() && validate_name("my_db-2").is_ok());
        for bad in ["", "a.b", "a b", "a/b", &"x".repeat(33)] {
            assert!(validate_name(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn warns_on_small_windows_and_heavy_tool_lists() {
        // A small window: always, with the measured size when there is one.
        let w = context_warning(Some(32_768), Some(1_200)).unwrap();
        assert!(w.contains("small context window (32k tokens)") && w.contains("1,200 tokens") && w.contains("3.7%"), "{w}");
        assert!(context_warning(Some(16_384), None).unwrap().contains("small context window (16k tokens)"));
        // A large one: only when the list is heavy.
        assert!(context_warning(Some(131_072), Some(2_000)).is_none());
        let w = context_warning(Some(131_072), Some(20_000)).unwrap();
        assert!(!w.contains("small") && w.contains("20,000 tokens") && w.contains("128k"), "{w}");
        assert!(context_warning(Some(131_072), None).is_none());
        assert!(context_warning(None, Some(50_000)).is_none(), "an unknown window says nothing");
        assert_eq!(cost(950, None), "about 950 tokens of every request");
    }

    #[test]
    fn the_connectors_file_sits_next_to_the_config() {
        // (OPPX_MCP_CONFIG overrides it; not set in tests.)
        if std::env::var_os("OPPX_MCP_CONFIG").is_none() {
            assert_eq!(user_path(Path::new("/x/oppx/config.json")), Path::new("/x/oppx/mcp.json"));
        }
    }
}
