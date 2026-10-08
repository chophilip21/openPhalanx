//! The server app's own updates: the latest GitHub release, the package that
//! matches how this copy was installed (AppImage or .deb), checked against the
//! release's `SHA256SUMS` before it replaces anything. The same trust model as
//! `oppx --update`: GitHub over TLS.

use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tokio::io::AsyncWriteExt;

/// GitHub repository that publishes releases.
pub const REPO: &str = "chophilip21/openPhalanx";

/// How this copy of the app was installed, which decides how it updates.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "snake_case", tag = "kind")]
pub enum Install {
    /// Run from an AppImage file (`$APPIMAGE`): replaced in place.
    AppImage { path: PathBuf },
    /// Installed from the .deb: reinstalled with apt (asks for the password).
    Deb,
    /// Built from source (or `tauri dev`): updated with git, not from here.
    Source,
}

impl Install {
    pub fn detect() -> Install {
        if let Some(path) = std::env::var_os("APPIMAGE").map(PathBuf::from).filter(|p| p.is_file()) {
            return Install::AppImage { path };
        }
        let packaged = std::env::current_exe().ok().is_some_and(|exe| {
            exe.starts_with("/usr")
                && std::process::Command::new("dpkg-query")
                    .arg("-S")
                    .arg(&exe)
                    .output()
                    .is_ok_and(|o| o.status.success())
        });
        if packaged {
            Install::Deb
        } else {
            Install::Source
        }
    }

    /// The release asset for this kind of install, picked by its suffix.
    fn asset<'a>(&self, assets: &'a [Asset]) -> Option<&'a Asset> {
        let suffix = match self {
            Install::AppImage { .. } => "_amd64.AppImage",
            Install::Deb => "_amd64.deb",
            Install::Source => return None,
        };
        assets.iter().find(|a| a.name.starts_with("Openphalanx_") && a.name.ends_with(suffix))
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Asset {
    pub name: String,
    pub browser_download_url: String,
    #[serde(default)]
    pub size: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Release {
    pub tag_name: String,
    pub html_url: String,
    #[serde(default)]
    pub assets: Vec<Asset>,
}

/// What the app shows: whether a newer version exists and whether it can
/// install it itself.
#[derive(Debug, Clone, Serialize)]
pub struct Check {
    pub current: String,
    pub latest: String,
    pub available: bool,
    pub install: Install,
    pub can_install: bool,
    /// Why it can't install it itself, when it can't.
    pub note: Option<String>,
    /// The release page (release notes).
    pub url: String,
}

fn client() -> Result<reqwest::Client> {
    Ok(reqwest::Client::builder()
        .user_agent(concat!("openphalanx/", env!("CARGO_PKG_VERSION")))
        .connect_timeout(std::time::Duration::from_secs(10))
        .build()?)
}

pub async fn latest_release() -> Result<Release> {
    let resp = client()?
        .get(format!("https://api.github.com/repos/{REPO}/releases/latest"))
        .header("accept", "application/vnd.github+json")
        .timeout(std::time::Duration::from_secs(20))
        .send()
        .await
        .context("couldn't reach GitHub")?;
    if !resp.status().is_success() {
        bail!("GitHub answered HTTP {} for the latest release", resp.status());
    }
    resp.json().await.context("unexpected answer from GitHub")
}

/// `latest` is a higher dotted version than `current` (a leading `v` is ignored).
pub fn newer(current: &str, latest: &str) -> bool {
    let parse = |v: &str| -> Vec<u64> {
        v.trim().trim_start_matches('v').split(['.', '-']).map_while(|p| p.parse().ok()).collect()
    };
    parse(latest) > parse(current)
}

pub async fn check(current: &str) -> Result<Check> {
    let release = latest_release().await?;
    let latest = release.tag_name.trim_start_matches('v').to_string();
    let install = Install::detect();
    let has_asset = install.asset(&release.assets).is_some();
    let note = match &install {
        Install::Source => Some("This copy was built from source: update it with git pull and a rebuild.".to_string()),
        _ if !has_asset => Some("This release has no package for this kind of install.".to_string()),
        _ => None,
    };
    Ok(Check {
        current: current.to_string(),
        available: newer(current, &latest),
        latest,
        can_install: note.is_none(),
        install,
        note,
        url: release.html_url,
    })
}

/// The expected SHA-256 of `name` in a `SHA256SUMS` file (`sha256sum` format).
pub fn expected_sum(sums: &str, name: &str) -> Option<String> {
    sums.lines().find_map(|line| {
        let (hash, file) = line.split_once(char::is_whitespace)?;
        (file.trim().trim_start_matches('*') == name).then(|| hash.to_lowercase())
    })
}

/// Downloads the latest release's package for `install`, checks it against
/// `SHA256SUMS`, and returns the file. `progress(done, total)` follows it.
pub async fn download(install: &Install, progress: impl Fn(u64, u64)) -> Result<(Release, PathBuf)> {
    let release = latest_release().await?;
    let asset = install.asset(&release.assets).context("this release has no package for this kind of install")?.clone();
    let sums_url = release
        .assets
        .iter()
        .find(|a| a.name == "SHA256SUMS")
        .context("the release has no SHA256SUMS to check the download against")?
        .browser_download_url
        .clone();
    let http = client()?;
    let sums = http.get(&sums_url).send().await?.error_for_status()?.text().await?;
    let want = expected_sum(&sums, &asset.name).with_context(|| format!("SHA256SUMS doesn't list {}", asset.name))?;

    // Next to the AppImage, so the final rename stays on one filesystem.
    let dir = match install {
        Install::AppImage { path } => path.parent().map(Path::to_path_buf).unwrap_or_else(std::env::temp_dir),
        _ => std::env::temp_dir(),
    };
    let file = dir.join(format!(".{}.download", asset.name));
    let resp = http.get(&asset.browser_download_url).send().await?.error_for_status()?;
    let total = resp.content_length().unwrap_or(asset.size);
    let mut out = tokio::fs::File::create(&file).await.with_context(|| format!("can't write {}", file.display()))?;
    let mut hasher = Sha256::new();
    let mut done = 0u64;
    let mut stream = resp.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.context("the download was interrupted")?;
        hasher.update(&chunk);
        out.write_all(&chunk).await?;
        done += chunk.len() as u64;
        progress(done, total);
    }
    out.flush().await?;
    drop(out);
    let got = format!("{:x}", hasher.finalize());
    if got != want {
        let _ = std::fs::remove_file(&file);
        bail!("the download of {} doesn't match its SHA-256 in SHA256SUMS; nothing was changed", asset.name);
    }
    Ok((release, file))
}

/// Installs a downloaded package over this copy. The caller relaunches.
pub async fn install(install: &Install, file: &Path) -> Result<()> {
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;
    match install {
        Install::AppImage { path } => {
            #[cfg(unix)]
            std::fs::set_permissions(file, std::fs::Permissions::from_mode(0o755))?;
            std::fs::rename(file, path).with_context(|| format!("can't replace {}", path.display()))?;
        }
        Install::Deb => {
            // apt reads it as its own user: make it readable.
            #[cfg(unix)]
            std::fs::set_permissions(file, std::fs::Permissions::from_mode(0o644))?;
            let out = tokio::process::Command::new("pkexec")
                .args(["apt-get", "install", "-y", "--allow-downgrades"])
                .arg(file)
                .output()
                .await
                .context("pkexec is not installed; install the .deb by hand")?;
            let _ = std::fs::remove_file(file);
            match out.status.code() {
                Some(0) => {}
                Some(126 | 127) => bail!("the password prompt was cancelled; nothing was changed"),
                _ => bail!(
                    "apt couldn't install the update: {}",
                    String::from_utf8_lossy(&out.stderr).lines().last().unwrap_or("unknown error")
                ),
            }
        }
        Install::Source => bail!("this copy was built from source: update it with git pull and a rebuild"),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compares_versions() {
        assert!(newer("0.5.1", "0.5.2"));
        assert!(newer("0.5.1", "v0.6.0"));
        assert!(newer("0.9.9", "0.10.0"));
        assert!(!newer("0.5.1", "0.5.1"));
        assert!(!newer("0.6.0", "0.5.9"));
    }

    #[test]
    fn reads_sha256sums() {
        let sums = "abc123  Openphalanx_0.5.1_amd64.deb\nDEF456 *Openphalanx_0.5.1_amd64.AppImage\n";
        assert_eq!(expected_sum(sums, "Openphalanx_0.5.1_amd64.deb").as_deref(), Some("abc123"));
        assert_eq!(expected_sum(sums, "Openphalanx_0.5.1_amd64.AppImage").as_deref(), Some("def456"));
        assert_eq!(expected_sum(sums, "oppx.tar.gz"), None);
    }

    #[test]
    fn picks_the_asset_for_the_install() {
        let a = |n: &str| Asset { name: n.into(), browser_download_url: String::new(), size: 0 };
        let assets = [a("oppx-x86_64-unknown-linux-musl.tar.gz"), a("Openphalanx_0.5.1_amd64.AppImage"), a("Openphalanx_0.5.1_amd64.deb")];
        let image = Install::AppImage { path: "/x".into() };
        assert_eq!(image.asset(&assets).unwrap().name, "Openphalanx_0.5.1_amd64.AppImage");
        assert_eq!(Install::Deb.asset(&assets).unwrap().name, "Openphalanx_0.5.1_amd64.deb");
        assert!(Install::Source.asset(&assets).is_none());
    }
}
