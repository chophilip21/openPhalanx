//! `oppx`: the OpenPhalanx client. Pairs this machine with a GPU server
//! and runs a local coding agent (Aider) against it through a pinned proxy.

use std::ffi::OsString;
use std::io::{BufRead, IsTerminal, Write};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};

use oppx::config::{self, Config, Server};
use oppx::proxy::Proxy;
use oppx::{agent, api, tls};

#[derive(Parser)]
#[command(name = "oppx", version, about = "Use an OpenPhalanx GPU server from this machine")]
struct Cli {
    /// Config file (defaults to the user config dir, e.g. ~/.config/oppx/config.json).
    #[arg(long, global = true, env = "OPPX_CONFIG")]
    config: Option<PathBuf>,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Pair with a server using the code shown in the OpenPhalanx app.
    Pair {
        /// Server address, e.g. 192.168.1.77 or https://gpu.lan:9090.
        server: String,
        /// Pairing code from the app's Server page, e.g. K7QF-M2XD.
        code: String,
        /// Expected certificate fingerprint (as shown in the app); skips the prompt.
        #[arg(long)]
        fingerprint: Option<String>,
        /// Accept the server's certificate without comparing fingerprints.
        #[arg(long, short)]
        yes: bool,
        /// Name this device shows under in the app's Devices page.
        #[arg(long, value_name = "NAME")]
        device_name: Option<String>,
        /// Local name for the server (defaults to its address).
        #[arg(long = "as", value_name = "NAME")]
        alias: Option<String>,
        /// Replace an existing pairing with the same local name.
        #[arg(long)]
        force: bool,
    },
    /// Check the connection, the device token and the model.
    Status {
        /// Server name (defaults to the default server).
        name: Option<String>,
    },
    /// Forget a server and revoke this device's token on it.
    Unpair {
        /// Server name (defaults to the default server).
        name: Option<String>,
        /// Only forget it locally; don't contact the server.
        #[arg(long)]
        local_only: bool,
    },
    /// Run Aider in the current directory, using the server's model.
    ///
    /// Everything after `--` (or any unrecognized argument) goes to Aider,
    /// e.g. `oppx aider -- --message "add tests" src/lib.rs`.
    Aider {
        /// Server name (defaults to the default server).
        #[arg(long)]
        server: Option<String>,
        /// Arguments passed through to Aider.
        #[arg(trailing_var_arg = true, allow_hyphen_values = true, value_name = "AIDER_ARGS")]
        args: Vec<OsString>,
    },
    /// Run only the local proxy, for other OpenAI-compatible tools.
    Proxy {
        /// Server name (defaults to the default server).
        name: Option<String>,
        /// Local port (default: a free one).
        #[arg(long, default_value_t = 0)]
        port: u16,
    },
    /// List paired servers (tokens are never shown).
    Servers,
    /// Set the default server.
    Use {
        /// Server name, as listed by `oppx servers`.
        name: String,
    },
}

#[tokio::main]
async fn main() {
    if let Err(e) = run(Cli::parse()).await {
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}

async fn run(cli: Cli) -> Result<()> {
    let path = match cli.config {
        Some(p) => p,
        None => config::default_path()?,
    };
    let mut cfg = Config::load(&path)?;
    match cli.command {
        Command::Pair { server, code, fingerprint, yes, device_name, alias, force } => {
            pair(&mut cfg, &server, &code, fingerprint, yes, device_name, alias, force).await?;
            cfg.save(&path)?;
        }
        Command::Status { name } => status(&cfg, name.as_deref()).await?,
        Command::Unpair { name, local_only } => {
            unpair(&mut cfg, name.as_deref(), local_only).await?;
            cfg.save(&path)?;
        }
        Command::Aider { server, args } => {
            let code = run_aider(&cfg, server.as_deref(), &args).await?;
            std::process::exit(code);
        }
        Command::Proxy { name, port } => run_proxy(&cfg, name.as_deref(), port).await?,
        Command::Servers => {
            if cfg.servers.is_empty() {
                println!("No servers paired yet. Pair with: oppx pair <server> <code>");
            }
            for (name, s) in &cfg.servers {
                let mark = if cfg.default.as_deref() == Some(name) { "*" } else { " " };
                println!("{mark} {name:<16} {:<28} device {:<16} cert {}", s.url, s.device_name, tls::short(&s.fingerprint));
            }
            println!("config: {}", path.display());
        }
        Command::Use { name } => {
            cfg.set_default(&name)?;
            cfg.save(&path)?;
            println!("Default server: {name}");
        }
    }
    Ok(())
}

/// A local server name derived from its address: `https://192.168.1.77:9090` -> `192-168-1-77`.
fn name_for(url: &str) -> String {
    let host = url.trim_start_matches("https://");
    let host = host.rsplit_once(':').map_or(host, |(h, _)| h);
    let name: String = host
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '-' })
        .collect::<String>()
        .trim_matches('-')
        .chars()
        .take(32)
        .collect();
    if name.is_empty() { "server".into() } else { name }
}

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[allow(clippy::too_many_arguments)]
async fn pair(
    cfg: &mut Config,
    server: &str,
    code: &str,
    expected: Option<String>,
    yes: bool,
    device_name: Option<String>,
    alias: Option<String>,
    force: bool,
) -> Result<()> {
    let url = config::normalize_url(server)?;
    let alias = alias.unwrap_or_else(|| name_for(&url));
    config::validate_name(&alias)?;
    if cfg.servers.contains_key(&alias) && !force {
        bail!("already paired as \"{alias}\"; use --force to replace it, or --as <name> to keep both");
    }

    println!("Connecting to {url} …");
    let fp = tls::probe_fingerprint(&url).await?;
    println!("Server certificate fingerprint: {}", tls::short(&fp));
    println!("  (full SHA-256: {fp})");

    match expected {
        Some(e) if tls::matches(&e, &fp)? => println!("Fingerprint matches --fingerprint."),
        Some(_) => bail!(
            "the server's certificate does not match --fingerprint. Do not pair: you may be talking to a \
             different machine than the one running OpenPhalanx."
        ),
        None if yes => println!("Skipping the fingerprint comparison (--yes)."),
        None => {
            if !std::io::stdin().is_terminal() {
                bail!("can't ask for confirmation without a terminal; pass --fingerprint <FP> (or --yes)");
            }
            print!("Does it match the fingerprint on the app's Server page? [y/N] ");
            std::io::stdout().flush()?;
            let mut answer = String::new();
            std::io::stdin().lock().read_line(&mut answer)?;
            if !matches!(answer.trim().to_ascii_lowercase().as_str(), "y" | "yes") {
                bail!("pairing cancelled; the fingerprint was not confirmed");
            }
        }
    }

    // From here on, only the certificate just approved is trusted, so the
    // pairing code and the returned token can't go anywhere else.
    let client = tls::pinned_client(&fp)?;
    let device_name = device_name.unwrap_or_else(|| gethostname::gethostname().to_string_lossy().into_owned());
    let paired = api::pair(&client, &url, code, &device_name).await?;

    let replaced = cfg.servers.contains_key(&alias);
    cfg.insert(
        &alias,
        Server {
            url: url.clone(),
            device_id: paired.device_id,
            device_name: device_name.clone(),
            token: paired.token,
            fingerprint: fp,
            paired_at: now(),
        },
    )?;
    let default = if cfg.default.as_deref() == Some(alias.as_str()) { ", default" } else { "" };
    println!("Paired \"{device_name}\" with {url}. Saved as \"{alias}\"{default}.");
    if replaced {
        println!("The previous pairing was replaced; revoke the old device in the app's Devices page.");
    }
    println!("Next: oppx status");
    Ok(())
}

async fn status(cfg: &Config, name: Option<&str>) -> Result<()> {
    let (name, s) = cfg.server(name)?;
    println!("{name}: {}", s.url);
    let client = tls::pinned_client(&s.fingerprint)?;
    let health = match api::health(&client, &s.url).await {
        Ok(h) => h,
        Err(e) if tls::is_pin_mismatch(e.as_ref()) => bail!(
            "WARNING: the server's TLS certificate has changed (pinned {}).\n\
             Either its state was reset (the certificate is regenerated if backend-state is deleted), or \
             something is intercepting the connection. Nothing was sent. If you expected this, run \
             `oppx unpair {name} --local-only` and pair again.",
            tls::short(&s.fingerprint)
        ),
        Err(e) => return Err(e.context(format!("cannot reach {}", s.url))),
    };
    println!("  certificate  pinned {} ✓", tls::short(&s.fingerprint));
    match api::whoami(&client, &s.url, &s.token).await? {
        Some(me) => println!("  device       {} ({}) ✓", me.device_name, me.device_id),
        None => bail!(
            "this device's token was rejected: it was revoked in the app. Pair again with \
             `oppx pair {} <code> --as {name} --force`",
            s.url
        ),
    }
    if health.sglang == "ready" {
        let ctx = api::context_len(&client, &s.url, &s.token).await?;
        let ctx = ctx.map(|c| format!(", {}k context", c / 1024)).unwrap_or_default();
        println!("  model        ready{ctx}");
    } else {
        println!("  model        loading (the server is starting; try again in a minute)");
    }
    Ok(())
}

async fn unpair(cfg: &mut Config, name: Option<&str>, local_only: bool) -> Result<()> {
    let (name, s) = cfg.server(name).map(|(n, s)| (n.to_string(), s.clone()))?;
    if !local_only {
        let result = match tls::pinned_client(&s.fingerprint) {
            Ok(client) => api::unpair(&client, &s.url, &s.token).await,
            Err(e) => Err(e),
        };
        match result {
            Ok(true) => println!("The server revoked this device's token."),
            Ok(false) => println!("The server had already revoked this device."),
            Err(e) => eprintln!(
                "warning: could not reach the server to revoke this device ({e:#}).\n\
                 Revoke \"{}\" in the app's Devices page.",
                s.device_name
            ),
        }
    }
    cfg.remove(&name)?;
    println!("Forgot \"{name}\".");
    match &cfg.default {
        Some(d) => println!("Default server is now \"{d}\"."),
        None if !cfg.servers.is_empty() => {}
        None => println!("No servers left."),
    }
    Ok(())
}

/// Confirms the server is usable before starting anything, and returns its
/// model's context window.
async fn preflight(name: &str, s: &Server) -> Result<u64> {
    let client = tls::pinned_client(&s.fingerprint)?;
    let health = match api::health(&client, &s.url).await {
        Ok(h) => h,
        Err(e) if tls::is_pin_mismatch(e.as_ref()) => bail!(
            "the server's TLS certificate has changed; nothing was sent. Run `oppx status {name}` for details."
        ),
        Err(e) => return Err(e.context(format!("cannot reach {} (\"{name}\")", s.url))),
    };
    if api::whoami(&client, &s.url, &s.token).await?.is_none() {
        bail!("this device was revoked on \"{name}\". Pair again: oppx pair {} <code> --as {name} --force", s.url);
    }
    if health.sglang != "ready" {
        bail!("the model on \"{name}\" is still loading; try again in a minute (`oppx status` shows progress)");
    }
    Ok(api::context_len(&client, &s.url, &s.token).await?.unwrap_or(32_768))
}

async fn run_proxy(cfg: &Config, name: Option<&str>, port: u16) -> Result<()> {
    let (name, s) = cfg.server(name)?;
    let ctx = preflight(name, s).await?;
    let proxy = Proxy::bind(s, port).await?;
    println!("Proxy for \"{name}\" ({}) is running.", s.url);
    println!("  OPENAI_API_BASE={}", proxy.base_url());
    println!("  OPENAI_API_KEY={}", proxy.local_key);
    println!("  model: {} ({}k context)", agent::AIDER_MODEL.trim_start_matches("openai/"), ctx / 1024);
    println!("The key is valid only while this proxy runs. Press Ctrl-C to stop.");
    tokio::select! {
        r = proxy.serve() => r,
        _ = tokio::signal::ctrl_c() => Ok(()),
    }
}

async fn run_aider(cfg: &Config, name: Option<&str>, user_args: &[OsString]) -> Result<i32> {
    agent::reject_commit_flags(user_args)?;
    let aider = agent::find_aider()?;
    let (name, s) = cfg.server(name)?;
    let ctx = preflight(name, s).await?;
    let metadata = agent::model_metadata(ctx)?;
    let proxy = Proxy::bind(s, 0).await?;
    let (base, key) = (proxy.base_url(), proxy.local_key.clone());
    let serving = tokio::spawn(proxy.serve());
    eprintln!("oppx: using \"{name}\" ({}), {}k context. Edits stay uncommitted.", s.url, ctx / 1024);

    let mut child = tokio::process::Command::new(&aider)
        .args(agent::aider_args(metadata.path(), user_args))
        .env("OPENAI_API_BASE", &base)
        .env("OPENAI_API_KEY", &key)
        // Don't let an unrelated OpenAI/LiteLLM setup in the user's env redirect requests.
        .env_remove("OPENAI_BASE_URL")
        .env_remove("OPENAI_API_TYPE")
        .spawn()
        .with_context(|| format!("cannot start {}", aider.display()))?;

    // Ctrl-C belongs to Aider (it shares the terminal); keep oppx and its
    // proxy alive until Aider itself exits.
    let swallow = tokio::spawn(async {
        loop {
            if tokio::signal::ctrl_c().await.is_err() {
                break;
            }
        }
    });
    let status = child.wait().await?;
    swallow.abort();
    serving.abort();
    Ok(status.code().unwrap_or(1))
}

#[cfg(test)]
mod tests {
    use super::name_for;

    #[test]
    fn derives_server_names() {
        assert_eq!(name_for("https://192.168.1.77:9090"), "192-168-1-77");
        assert_eq!(name_for("https://gpu.lan:9090"), "gpu-lan");
        assert_eq!(name_for("https://[fe80::1]:9090"), "fe80--1");
    }
}
