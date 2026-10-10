//! Client configuration: the servers this machine is paired with.
//!
//! Stored as JSON at `<config dir>/oppx/config.json` (`~/.config` on
//! Linux, `~/Library/Application Support` on macOS), or at `OPPX_CONFIG`.
//! The file holds device tokens, so on Unix it is written `0600` inside a
//! `0700` directory, atomically, and loading warns if it is readable by others.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

pub const DEFAULT_PORT: u16 = 9090;

/// One paired server.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Server {
    /// Normalized `https://host:port`.
    pub url: String,
    pub device_id: String,
    pub device_name: String,
    /// Bearer token for the gateway. Never printed.
    pub token: String,
    /// SHA-256 of the server's TLS certificate, `AB:CD:…` (32 bytes).
    pub fingerprint: String,
    /// Unix seconds.
    pub paired_at: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Config {
    /// Name of the server used when none is given.
    pub default: Option<String>,
    #[serde(default)]
    pub servers: BTreeMap<String, Server>,
}

/// `<config dir>/oppx/config.json`.
pub fn default_path() -> Result<PathBuf> {
    Ok(dirs::config_dir()
        .context("cannot determine the user config directory")?
        .join("oppx")
        .join("config.json"))
}

impl Config {
    /// Missing file means "no servers yet".
    pub fn load(path: &Path) -> Result<Config> {
        let bytes = match std::fs::read(path) {
            Ok(b) => b,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Config::default()),
            Err(e) => return Err(e).with_context(|| format!("cannot read {}", path.display())),
        };
        warn_if_exposed(path);
        serde_json::from_slice(&bytes).with_context(|| format!("{} is not valid oppx config", path.display()))
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        let dir = path.parent().context("config path has no parent directory")?;
        create_private_dir(dir)?;
        let tmp = path.with_extension("json.tmp");
        write_private(&tmp, &serde_json::to_vec_pretty(self)?)?;
        std::fs::rename(&tmp, path).with_context(|| format!("cannot write {}", path.display()))
    }

    /// The named server, or the default one.
    pub fn server(&self, name: Option<&str>) -> Result<(&str, &Server)> {
        let name = match name {
            Some(n) => n,
            None => self.default.as_deref().context("no server paired yet; run `oppx pair` first")?,
        };
        self.servers
            .get_key_value(name)
            .map(|(k, v)| (k.as_str(), v))
            .with_context(|| format!("no server named \"{name}\"; see `oppx servers`"))
    }

    /// Adds or replaces a server. The first server becomes the default.
    pub fn insert(&mut self, name: &str, server: Server) -> Result<()> {
        validate_name(name)?;
        self.servers.insert(name.to_string(), server);
        if self.default.is_none() {
            self.default = Some(name.to_string());
        }
        Ok(())
    }

    pub fn set_default(&mut self, name: &str) -> Result<()> {
        if !self.servers.contains_key(name) {
            bail!("no server named \"{name}\"; see `oppx servers`");
        }
        self.default = Some(name.to_string());
        Ok(())
    }

    /// Forgets a server; if it was the default, another one (if any) takes over.
    pub fn remove(&mut self, name: &str) -> Result<Server> {
        let server = self
            .servers
            .remove(name)
            .with_context(|| format!("no server named \"{name}\""))?;
        if self.default.as_deref() == Some(name) {
            self.default = self.servers.keys().next().cloned();
        }
        Ok(server)
    }
}

/// Server names: 1–32 of `[A-Za-z0-9_-]`, so they are safe in shells and paths.
pub fn validate_name(name: &str) -> Result<()> {
    let ok = (1..=32).contains(&name.len())
        && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if !ok {
        bail!("server name \"{name}\" must be 1-32 letters, digits, '-' or '_'");
    }
    Ok(())
}

/// Accepts `host`, `host:port`, `https://host[:port][/]`; returns
/// `https://host:port`. Plain `http` is refused: tokens only travel over TLS.
pub fn normalize_url(input: &str) -> Result<String> {
    let s = input.trim().trim_end_matches('/');
    let rest = if let Some(r) = s.strip_prefix("https://") {
        r
    } else if s.starts_with("http://") {
        bail!("the server must be reached over https://, not http://");
    } else if s.contains("://") {
        bail!("unsupported URL scheme in \"{input}\"");
    } else {
        s
    };
    if rest.is_empty() || rest.contains('/') || rest.contains('@') || rest.contains(char::is_whitespace) {
        bail!("\"{input}\" is not a server address (expected host or host:port)");
    }
    // IPv6 literals keep their brackets: [::1]:9090
    let (host, port) = match rest.rsplit_once(':') {
        Some((h, p)) if !h.is_empty() && !p.contains(']') && (!h.contains(':') || h.ends_with(']')) => {
            let port: u16 = p.parse().with_context(|| format!("invalid port in \"{input}\""))?;
            (h, port)
        }
        _ => (rest, DEFAULT_PORT),
    };
    if host.contains(':') && !host.starts_with('[') {
        bail!("IPv6 addresses must be in brackets, e.g. https://[fe80::1]:9090");
    }
    Ok(format!("https://{host}:{port}"))
}

/// Normalizes a SHA-256 fingerprint to `AB:CD:…` (with or without colons,
/// any case, optional `sha256:` prefix).
pub fn normalize_fingerprint(input: &str) -> Result<String> {
    let s = input.trim();
    let s = s.strip_prefix("sha256:").or_else(|| s.strip_prefix("SHA256:")).unwrap_or(s);
    let hex: String = s.chars().filter(|c| *c != ':').collect();
    if hex.len() != 64 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
        bail!("\"{input}\" is not a SHA-256 fingerprint (64 hex digits)");
    }
    let upper = hex.to_ascii_uppercase();
    Ok(upper.as_bytes().chunks(2).map(|c| std::str::from_utf8(c).unwrap()).collect::<Vec<_>>().join(":"))
}

#[cfg(unix)]
pub(crate) fn create_private_dir(dir: &Path) -> Result<()> {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    if !dir.exists() {
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(dir)
            .with_context(|| format!("cannot create {}", dir.display()))?;
    }
    // Tighten an existing directory too; ignore failures on dirs we don't own.
    let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700));
    Ok(())
}

#[cfg(not(unix))]
pub(crate) fn create_private_dir(dir: &Path) -> Result<()> {
    std::fs::create_dir_all(dir).with_context(|| format!("cannot create {}", dir.display()))
}

#[cfg(unix)]
pub(crate) fn write_private(path: &Path, bytes: &[u8]) -> Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("cannot write {}", path.display()))?;
    f.write_all(bytes)?;
    f.sync_all()?;
    Ok(())
}

#[cfg(not(unix))]
pub(crate) fn write_private(path: &Path, bytes: &[u8]) -> Result<()> {
    std::fs::write(path, bytes).with_context(|| format!("cannot write {}", path.display()))
}

#[cfg(unix)]
fn warn_if_exposed(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    if let Ok(meta) = std::fs::metadata(path) {
        let mode = meta.permissions().mode() & 0o777;
        if mode & 0o077 != 0 {
            eprintln!(
                "warning: {} is accessible by other users (mode {mode:o}) and contains device tokens; \
                 run: chmod 600 {}",
                path.display(),
                path.display()
            );
        }
    }
}

#[cfg(not(unix))]
fn warn_if_exposed(_path: &Path) {}

#[cfg(test)]
mod tests {
    use super::*;

    fn server(url: &str) -> Server {
        Server {
            url: url.into(),
            device_id: "abc123".into(),
            device_name: "laptop".into(),
            token: "secret-token".into(),
            fingerprint: normalize_fingerprint(&"ab".repeat(32)).unwrap(),
            paired_at: 1,
        }
    }

    #[test]
    fn round_trips_with_private_permissions() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested/oppx/config.json");
        let mut cfg = Config::default();
        cfg.insert("home", server("https://10.0.0.2:9090")).unwrap();
        cfg.save(&path).unwrap();
        assert_eq!(Config::load(&path).unwrap(), cfg);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = |p: &Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode(&path), 0o600);
            assert_eq!(mode(path.parent().unwrap()), 0o700);
        }
        assert!(!path.with_extension("json.tmp").exists());
    }

    #[test]
    fn missing_file_is_empty_config() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(Config::load(&dir.path().join("none.json")).unwrap(), Config::default());
    }

    #[test]
    fn default_follows_first_insert_and_removal() {
        let mut cfg = Config::default();
        assert!(cfg.server(None).is_err());
        cfg.insert("home", server("https://a:9090")).unwrap();
        cfg.insert("work", server("https://b:9090")).unwrap();
        assert_eq!(cfg.server(None).unwrap().0, "home");
        cfg.set_default("work").unwrap();
        assert_eq!(cfg.server(None).unwrap().0, "work");
        assert!(cfg.set_default("nope").is_err());
        cfg.remove("work").unwrap();
        assert_eq!(cfg.default.as_deref(), Some("home"));
        cfg.remove("home").unwrap();
        assert_eq!(cfg.default, None);
    }

    #[test]
    fn validates_names() {
        for ok in ["home", "gpu-box_2", "a"] {
            validate_name(ok).unwrap();
        }
        for bad in ["", "has space", "../x", "é", &"x".repeat(33)] {
            assert!(validate_name(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn normalizes_urls() {
        assert_eq!(normalize_url("192.168.1.77").unwrap(), "https://192.168.1.77:9090");
        assert_eq!(normalize_url("https://gpu.lan:8443/").unwrap(), "https://gpu.lan:8443");
        assert_eq!(normalize_url("gpu.lan:9091").unwrap(), "https://gpu.lan:9091");
        assert_eq!(normalize_url("https://[fe80::1]:9090").unwrap(), "https://[fe80::1]:9090");
        assert_eq!(normalize_url("[::1]").unwrap(), "https://[::1]:9090");
        for bad in ["http://gpu.lan", "ftp://x", "https://x/v1", "user@host", "fe80::1", "host:99999", ""] {
            assert!(normalize_url(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn normalizes_fingerprints() {
        let colons = "F2:27:FE:3E:7D:85:17:E9:".repeat(4).trim_end_matches(':').to_string();
        assert_eq!(normalize_fingerprint(&colons.to_lowercase()).unwrap(), colons);
        assert_eq!(normalize_fingerprint(&format!("sha256:{}", colons.replace(':', ""))).unwrap(), colons);
        assert!(normalize_fingerprint("F2:27").is_err());
        assert!(normalize_fingerprint(&"zz".repeat(32)).is_err());
    }
}
