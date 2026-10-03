//! `openbase`: the Openphalanx client. Pairs this machine with a GPU server
//! and (from Phase 4.4) runs a local coding agent against it.

use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

use openbase::config::{self, Config};

#[derive(Parser)]
#[command(name = "openbase", version, about = "Use an Openphalanx GPU server from this machine")]
struct Cli {
    /// Config file (defaults to the user config dir, e.g. ~/.config/openbase/config.json).
    #[arg(long, global = true, env = "OPENBASE_CONFIG")]
    config: Option<PathBuf>,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// List paired servers (tokens are never shown).
    Servers,
    /// Set the default server.
    Use {
        /// Server name, as listed by `openbase servers`.
        name: String,
    },
}

fn main() {
    if let Err(e) = run(Cli::parse()) {
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}

fn run(cli: Cli) -> Result<()> {
    let path = match cli.config {
        Some(p) => p,
        None => config::default_path()?,
    };
    let mut cfg = Config::load(&path)?;
    match cli.command {
        Command::Servers => {
            if cfg.servers.is_empty() {
                println!("No servers paired yet. Pair with: openbase pair <server> <code>");
            }
            for (name, s) in &cfg.servers {
                let mark = if cfg.default.as_deref() == Some(name) { "*" } else { " " };
                let fp: String = s.fingerprint.chars().take(23).collect();
                println!("{mark} {name:<16} {:<28} device {:<16} cert {fp}…", s.url, s.device_name);
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
