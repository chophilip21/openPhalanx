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
use oppx::{agent, api, mcp, tls, ui};

#[derive(Parser)]
#[command(
    name = "oppx",
    version = concat!(env!("CARGO_PKG_VERSION"), " (", env!("OPPX_GIT_COMMIT"), ")"),
    about = "Use an OpenPhalanx GPU server from this machine",
    long_about = "Use an OpenPhalanx GPU server from this machine.\n\n\
                  Run `oppx` in a git repo to start the OpenPhalanx chat (keys and commands follow \
                  Claude Code), or pair first with `oppx pair <server> <code>`.\n\n\
                  Examples:\n  oppx\n  oppx \"fix the failing test\"\n  oppx -p \"explain src/main.rs\"\n  \
                  oppx -c   (continue the last conversation)\n  oppx -r   (pick a past conversation)\n  \
                  oppx --update",
    args_conflicts_with_subcommands = true
)]
struct Cli {
    /// Config file (defaults to the user config dir, e.g. ~/.config/oppx/config.json).
    #[arg(long, global = true, env = "OPPX_CONFIG")]
    config: Option<PathBuf>,

    /// With no command: start without automatic web search.
    #[arg(long)]
    no_web: bool,
    /// With no command: use Aider's own terminal interface instead of OpenPhalanx's.
    #[arg(long)]
    classic: bool,
    /// How the chat works: "agent" (the model reads files on demand with
    /// tools) or "aider" (Aider sends whole files and a repo map every time).
    #[arg(long, value_parser = ["agent", "aider"], default_value = "agent", env = "OPPX_ENGINE")]
    engine: String,
    /// Automatic web search is the default; kept so old habits don't break.
    #[arg(long, hide = true)]
    web: bool,

    /// Start the chat with this request.
    #[arg(value_name = "PROMPT")]
    prompt: Option<String>,
    /// Print mode: answer PROMPT once and exit (for scripts and quick questions).
    #[arg(short = 'p', long = "print")]
    print: bool,
    /// Continue the most recent conversation in this repo.
    #[arg(short = 'c', long = "continue")]
    continue_last: bool,
    /// Resume a past conversation in this repo (pick from a list, or give its id).
    #[arg(short = 'r', long, value_name = "ID", num_args = 0..=1, default_missing_value = "")]
    resume: Option<String>,
    /// Update oppx and its coding engine to the latest version.
    #[arg(long)]
    update: bool,

    #[command(subcommand)]
    command: Option<Command>,
}

/// How to start the coding session.
struct Launch {
    web: bool,
    classic: bool,
    engine: String,
    initial: Option<String>,
    print: bool,
    session: SessionChoice,
    /// The user's connectors file, for the chat's MCP client.
    connectors: PathBuf,
}

enum SessionChoice {
    New,
    Latest,
    Pick(String), // "" = ask
}

#[derive(Subcommand)]
enum Command {
    /// Pair with a server using the code shown in the OpenPhalanx app, or
    /// with the server on this machine: `oppx pair self` (no code needed).
    Pair {
        /// Server address, e.g. 192.168.1.77 or https://gpu.lan:9090; or `self`.
        server: String,
        /// Pairing code from the app's Server page, e.g. K7QF-M2XD (not for `self`).
        code: Option<String>,
        /// Expected certificate fingerprint (as shown in the app); skips the prompt.
        #[arg(long)]
        fingerprint: Option<String>,
        /// Accept the server's certificate without comparing fingerprints.
        #[arg(long, short)]
        yes: bool,
        /// Name this device shows under on the app's Client page.
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
    /// Run Aider with its own terminal interface (plain `oppx` uses OpenPhalanx's).
    ///
    /// Everything after `--` (or any unrecognized argument) goes to Aider,
    /// e.g. `oppx aider -- --message "add tests" src/lib.rs`.
    Aider {
        /// Server name (defaults to the default server).
        #[arg(long)]
        server: Option<String>,
        /// Don't let the server search the web automatically.
        #[arg(long)]
        no_web: bool,
        /// Automatic web search is the default; kept so old habits don't break.
        #[arg(long, hide = true)]
        web: bool,
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
        /// Don't let the server search the web automatically.
        #[arg(long)]
        no_web: bool,
        /// Automatic web search is the default; kept so old habits don't break.
        #[arg(long, hide = true)]
        web: bool,
    },
    /// Search the web through the server's private SearXNG.
    ///
    /// Inside Aider: `/run oppx search "your query"` adds the results to the chat.
    Search {
        /// What to search for.
        #[arg(required = true, num_args = 1.., value_name = "QUERY")]
        query: Vec<String>,
        /// Number of results.
        #[arg(short = 'n', long, default_value_t = 6, value_parser = clap::value_parser!(u8).range(1..=20))]
        max: u8,
        /// Server name (defaults to the default server).
        #[arg(long)]
        server: Option<String>,
    },
    /// Connectors: MCP servers whose tools the model can call in the chat.
    ///
    /// A connector is a program on this machine or a URL that offers tools
    /// (GitHub, a database, a browser…). The chat lists them for the model,
    /// and asks you before each call. In the chat, `/mcp` does the same.
    ///
    /// Examples:
    ///   oppx mcp add github -e GITHUB_TOKEN=… -- npx -y @modelcontextprotocol/server-github
    ///   oppx mcp add docs --url https://mcp.example.com/mcp -H "Authorization: Bearer …"
    ///   oppx mcp list
    ///   oppx mcp remove github
    ///
    /// Each connector's tool list is sent with every request, so on a model
    /// with a small context window add only the ones you need.
    #[command(verbatim_doc_comment)]
    Mcp {
        #[command(subcommand)]
        action: Option<McpCommand>,
    },
    /// List paired servers (tokens are never shown).
    Servers,
    /// Set the default server.
    Use {
        /// Server name, as listed by `oppx servers`.
        name: String,
    },
}

#[derive(Subcommand)]
enum McpCommand {
    /// Add a connector: a command to run (after `--`), or a URL.
    ///
    /// oppx mcp add github -e GITHUB_TOKEN=… -- npx -y @modelcontextprotocol/server-github
    /// oppx mcp add docs --url https://mcp.example.com/mcp
    #[command(verbatim_doc_comment)]
    Add {
        /// A short name; the model sees its tools as <name>.<tool>.
        name: String,
        /// A connector reached over HTTP (MCP's streamable HTTP), instead of a command.
        #[arg(long, value_name = "URL")]
        url: Option<String>,
        /// Environment for the command; repeat for more. `${VAR}` in a value is read from your shell at start.
        #[arg(long, short = 'e', value_name = "KEY=VALUE")]
        env: Vec<String>,
        /// A header for --url, e.g. "Authorization: Bearer …"; repeat for more.
        #[arg(long, short = 'H', value_name = "NAME: VALUE")]
        header: Vec<String>,
        /// Save it in this repository's .mcp.json (shared with the repo) instead of your own list.
        #[arg(long)]
        project: bool,
        /// Replace a connector with the same name.
        #[arg(long)]
        force: bool,
        /// The command that starts the connector, and its arguments.
        #[arg(last = true, value_name = "COMMAND")]
        command: Vec<String>,
    },
    /// Remove a connector (yours, or this repository's with --project).
    #[command(alias = "rm")]
    Remove {
        name: String,
        /// Remove it from this repository's .mcp.json.
        #[arg(long)]
        project: bool,
    },
    /// Show the connectors you added and this repository's (the default).
    #[command(alias = "ls")]
    List,
}

#[tokio::main]
async fn main() {
    ui::init();
    if let Err(e) = run(Cli::parse()).await {
        ui::error(format!("{e:#}"));
        std::process::exit(1);
    }
}

/// `oppx` with no command and no paired server: the banner and how to start.
fn welcome(cfg: &Config) {
    ui::banner();
    let steps: &[(&str, &str)] = if cfg.servers.is_empty() {
        &[
            ("oppx pair <server> <code>", "pair with the code shown in the OpenPhalanx app"),
            ("oppx status", "check the connection and the model"),
            ("oppx", "start coding in the current repo"),
        ]
    } else {
        &[
            ("oppx", "start coding in the current repo (--no-web: no web search)"),
            ("oppx search \"query\"", "search the web through the server"),
            ("oppx mcp add <name> -- <cmd>", "add a connector (MCP server) for the chat"),
            ("oppx status", "check the connection and the model"),
            ("oppx servers", "list paired servers"),
        ]
    };
    for (cmd, what) in steps {
        println!("  {}  {}", ui::accent(format!("{cmd:<26}")), ui::dim(what));
    }
    println!("\n  {}", ui::dim("oppx --help for all commands"));
}

async fn run(cli: Cli) -> Result<()> {
    let path = match cli.config {
        Some(p) => p,
        None => config::default_path()?,
    };
    if cli.update {
        ui::banner();
        return oppx::update::run().await;
    }
    let mut cfg = Config::load(&path)?;
    // Plain `oppx` starts coding right away once a server is paired.
    let command = match cli.command {
        Some(c) => c,
        None if cfg.servers.is_empty() => {
            welcome(&cfg);
            return Ok(());
        }
        None => {
            if cli.print && cli.prompt.is_none() {
                bail!("-p needs a prompt, e.g. oppx -p \"explain src/main.rs\"");
            }
            let launch = Launch {
                connectors: mcp::user_path(&path),
                web: !cli.no_web,
                classic: cli.classic,
                engine: cli.engine.clone(),
                initial: cli.prompt,
                print: cli.print,
                session: match (cli.continue_last, cli.resume) {
                    (_, Some(id)) => SessionChoice::Pick(id),
                    (true, None) => SessionChoice::Latest,
                    (false, None) => SessionChoice::New,
                },
            };
            let code = run_aider(&cfg, None, &launch, &[]).await?;
            std::process::exit(code);
        }
    };
    match command {
        Command::Pair { server, code, fingerprint, yes, device_name, alias, force } => {
            if server == "self" {
                // The server is on this machine: ask it for the code and the
                // certificate fingerprint directly, so there is nothing to copy.
                let alias = alias.unwrap_or_else(|| "local".into());
                // Before asking: a new code replaces the one the app shows.
                if cfg.servers.contains_key(&alias) && !force {
                    bail!("already paired as \"{alias}\"; use --force to replace it, or --as <name> to keep both");
                }
                let local = local_pairing().await?;
                let alias = Some(alias);
                pair(&mut cfg, &local.url, &local.code, Some(local.fingerprint), yes, device_name, alias, force).await?;
            } else {
                let code = code.context("the pairing code is missing: oppx pair <server> <code> (or: oppx pair self)")?;
                pair(&mut cfg, &server, &code, fingerprint, yes, device_name, alias, force).await?;
            }
            cfg.save(&path)?;
        }
        Command::Status { name } => status(&cfg, name.as_deref()).await?,
        Command::Unpair { name, local_only } => {
            unpair(&mut cfg, name.as_deref(), local_only).await?;
            cfg.save(&path)?;
        }
        Command::Aider { server, no_web, web: _, args } => {
            let launch = Launch {
                connectors: mcp::user_path(&path),
                web: !no_web,
                classic: true,
                engine: "aider".into(),
                initial: None,
                print: false,
                session: SessionChoice::New,
            };
            let code = run_aider(&cfg, server.as_deref(), &launch, &args).await?;
            std::process::exit(code);
        }
        Command::Proxy { name, port, no_web, web: _ } => run_proxy(&cfg, name.as_deref(), port, !no_web).await?,
        Command::Search { query, max, server } => run_search(&cfg, server.as_deref(), &query.join(" "), max).await?,
        Command::Mcp { action } => match action.unwrap_or(McpCommand::List) {
            McpCommand::Add { name, url, env, header, project, force, command } => {
                let spec = mcp::spec(url.as_deref(), &command, &env, &header)?;
                mcp_add(&cfg, &path, &name, spec, project, force, !env.is_empty() || !header.is_empty()).await?
            }
            McpCommand::Remove { name, project } => mcp_remove(&path, &name, project)?,
            McpCommand::List => mcp_list(&path)?,
        },
        Command::Servers => {
            if cfg.servers.is_empty() {
                println!("No servers paired yet. Pair with: {}", ui::accent("oppx pair <server> <code>"));
            }
            let rows: Vec<String> = cfg
                .servers
                .iter()
                .map(|(name, s)| {
                    let mark = if cfg.default.as_deref() == Some(name) { ui::accent("★") } else { " ".into() };
                    format!(
                        "{mark} {:<16} {:<28} {} {}",
                        name,
                        s.url,
                        ui::dim(format!("device {:<14}", s.device_name)),
                        ui::dim(format!("cert {}", tls::short(&s.fingerprint)))
                    )
                })
                .collect();
            if !rows.is_empty() {
                ui::panel("Paired servers", &rows);
            }
            println!("  {}", ui::dim(format!("config: {}", path.display())));
        }
        Command::Use { name } => {
            cfg.set_default(&name)?;
            cfg.save(&path)?;
            ui::line(ui::ok("default", &name));
        }
    }
    Ok(())
}

/// The context window of the default server's model, to judge what a
/// connector costs: from the chat when it runs us (`/mcp add`), otherwise
/// asked from the server, briefly. `None`: not paired, or it isn't answering.
async fn server_context(cfg: &Config) -> Option<u64> {
    if let Some(ctx) = std::env::var("OPPX_CONTEXT").ok().and_then(|c| c.parse().ok()) {
        return Some(ctx);
    }
    let (_, s) = cfg.server(None).ok()?;
    let client = tls::pinned_client(&s.fingerprint).ok()?;
    let ask = async {
        match api::info(&client, &s.url, &s.token).await {
            Ok(Some(i)) => Some(i.context_length),
            _ => api::context_len(&client, &s.url, &s.token).await.ok().flatten(),
        }
    };
    tokio::time::timeout(std::time::Duration::from_secs(4), ask).await.ok().flatten()
}

async fn mcp_add(cfg: &Config, config: &std::path::Path, name: &str, spec: serde_json::Value, project: bool, force: bool, secrets: bool) -> Result<()> {
    mcp::validate_name(name)?;
    let root = mcp::project_root(&std::env::current_dir()?);
    let user_file = mcp::user_path(config);
    let path = if project { root.join(mcp::PROJECT_FILE) } else { user_file.clone() };
    let mut file = mcp::File::load(&path, !project)?;
    if file.get(name).is_some() && !force {
        bail!("a connector named \"{name}\" is already in {}; use --force to replace it", path.display());
    }
    file.insert(name, spec.clone());
    file.save()?;
    ui::line(ui::ok("added", format!("{name}  {}", ui::dim(mcp::describe(&spec)))));
    println!("  {}", ui::dim(format!("saved in {}", path.display())));
    if project {
        if secrets {
            ui::warning(format!(
                "{} is usually committed with the repository: keep secrets out of it. Write ${{VAR}} as the value \
                 and set VAR in your shell.",
                mcp::PROJECT_FILE
            ));
        }
        if mcp::File::load(&user_file, true)?.get(name).is_some() {
            ui::warning(format!("you also have your own connector named \"{name}\"; yours is the one the chat uses."));
        }
    }
    // Inside the chat (/mcp add), the chat connects to it right away and
    // reports on it with the numbers of the whole session.
    if std::env::var_os("OPPX_IN_CHAT").is_some() {
        return Ok(());
    }
    let sp = ui::spinner(format!("Starting {name}"));
    let (probe, ctx) = tokio::join!(mcp::probe(name, &root, &user_file, project), server_context(cfg));
    sp.clear();
    let tokens = match &probe {
        Some(p) if p.ok => {
            let tools = format!("{} tool{}", p.tools, if p.tools == 1 { "" } else { "s" });
            ui::line(ui::ok("connected", format!("{tools} · {}", mcp::cost(p.tokens, ctx))));
            Some(p.tokens)
        }
        Some(p) => {
            ui::line(ui::bad("not running", &p.error));
            println!(
                "  {}",
                ui::dim(format!("It is saved as written. Fix it with: oppx mcp add {name} --force …   or drop it: oppx mcp remove {name}"))
            );
            None
        }
        None => None, // no engine yet: the chat checks it at its first start
    };
    if let Some(w) = mcp::context_warning(ctx, tokens) {
        ui::warning(w);
    }
    println!("  Next: {} uses it in the chat  ·  {}", ui::accent("oppx"), ui::accent("oppx mcp list"));
    Ok(())
}

fn mcp_remove(config: &std::path::Path, name: &str, project: bool) -> Result<()> {
    let root = mcp::project_root(&std::env::current_dir()?);
    let mut files = Vec::new();
    if !project {
        files.push(mcp::File::load(&mcp::user_path(config), true)?);
    }
    // Without --project, a name that is only in the repository's file is removed there.
    files.push(mcp::File::load(&root.join(mcp::PROJECT_FILE), false)?);
    for mut file in files {
        if file.remove(name) {
            file.save()?;
            ui::line(ui::ok("removed", format!("{name}  {}", ui::dim(format!("from {}", file.path.display())))));
            return Ok(());
        }
    }
    bail!("no connector named \"{name}\"; `oppx mcp list` shows them");
}

fn mcp_list(config: &std::path::Path) -> Result<()> {
    let root = mcp::project_root(&std::env::current_dir()?);
    let user_file = mcp::user_path(config);
    let mine = mcp::File::load(&user_file, true)?.servers();
    let repo = mcp::File::load(&root.join(mcp::PROJECT_FILE), false)?.servers();
    if mine.is_empty() && repo.is_empty() {
        println!("No connectors yet. A connector is an MCP server whose tools the model can call in the chat.");
        println!("  Add one: {}", ui::accent("oppx mcp add <name> -- <command> [args]"));
        println!("       or: {}", ui::accent("oppx mcp add <name> --url https://…"));
        return Ok(());
    }
    let mut rows = Vec::new();
    for (name, spec) in &mine {
        rows.push(format!("{name:<16} {} {}", ui::dim(format!("{:<10}", "yours")), mcp::describe(spec)));
    }
    for (name, spec) in &repo {
        // On a shared name the user's own connector is the one that runs.
        let hidden = mine.iter().any(|(n, _)| n == name);
        let what = if hidden { ui::dim(format!("{} (yours is used instead)", mcp::describe(spec))) } else { mcp::describe(spec) };
        rows.push(format!("{name:<16} {} {what}", ui::dim(format!("{:<10}", "this repo"))));
    }
    ui::panel("Connectors", &rows);
    println!("  {}", ui::dim(format!("yours: {}", user_file.display())));
    if !repo.is_empty() {
        println!("  {}", ui::dim(format!("this repo: {}", root.join(mcp::PROJECT_FILE).display())));
    }
    println!("  {}", ui::dim("In the chat, /mcp shows each connector's tools and what they cost per request."));
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

/// What `oppx pair self` needs from the server on this machine.
struct LocalPairing {
    url: String,
    code: String,
    fingerprint: String,
}

/// Gets a pairing code and the certificate fingerprint from the OpenPhalanx
/// server running on this machine, through its loopback admin API. The admin
/// token is read from the backend container, so this needs Docker access:
/// the same access that already lets a user read the token by hand.
async fn local_pairing() -> Result<LocalPairing> {
    let out = std::process::Command::new("docker")
        .args(["inspect", "--format", "{{.State.Running}} {{json .Config.Env}}", "openphalanx-backend"])
        .output()
        .context("`oppx pair self` needs the docker command, to reach the server on this machine")?;
    let text = String::from_utf8_lossy(&out.stdout);
    if !out.status.success() || !text.starts_with("true ") {
        let err = String::from_utf8_lossy(&out.stderr);
        if err.contains("permission denied") {
            bail!("your user can't use Docker, so the local server can't be asked for a code. Pair with a code instead: oppx pair 127.0.0.1 <code>");
        }
        bail!("no OpenPhalanx server is running on this machine. Start it in the app (or `oppxs start`), then run `oppx pair self` again.");
    }
    let (token, admin, url) = local_endpoints(text.trim_start_matches("true ").trim())?;
    let http = reqwest::Client::builder().timeout(std::time::Duration::from_secs(10)).build()?;
    let get = |method: reqwest::Method, path: &str| http.request(method, format!("{admin}{path}")).header("x-admin-token", &token);
    let pairing: serde_json::Value = get(reqwest::Method::POST, "/admin/pairing")
        .send()
        .await
        .context("the local server isn't answering yet; try again in a moment")?
        .error_for_status()?
        .json()
        .await?;
    let status: serde_json::Value = get(reqwest::Method::GET, "/admin/status").send().await?.error_for_status()?.json().await?;
    Ok(LocalPairing {
        url,
        code: pairing["code"].as_str().context("the local server gave no pairing code")?.to_string(),
        fingerprint: status["tls_fingerprint"].as_str().context("the local server gave no fingerprint")?.to_string(),
    })
}

/// From the backend container's environment (JSON list of `NAME=value`):
/// the admin token, the admin API's URL and the address clients pair with.
fn local_endpoints(env_json: &str) -> Result<(String, String, String)> {
    let env: Vec<String> = serde_json::from_str(env_json).context("unexpected docker output")?;
    let var = |name: &str| env.iter().find_map(|e| e.strip_prefix(&format!("{name}=")).map(str::to_string));
    let token = var("ADMIN_TOKEN").filter(|t| !t.is_empty()).context("the local server has no admin token")?;
    let admin = format!("http://127.0.0.1:{}", var("ADMIN_PORT").unwrap_or_else(|| "9091".into()));
    let agent = format!("127.0.0.1:{}", var("AGENT_PORT").unwrap_or_else(|| "9090".into()));
    Ok((token, admin, agent))
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

    let sp = ui::spinner(format!("Connecting to {url}"));
    let fp = match tls::probe_fingerprint(&url).await {
        Ok(fp) => fp,
        Err(e) => {
            sp.clear();
            return Err(e);
        }
    };
    sp.done(format!("Connected to {url}"));
    ui::panel(
        "Server certificate",
        &[
            format!("{}  {}", ui::dim("as shown in the app"), ui::accent(tls::short(&fp))),
            format!("{}  {}", ui::dim("full SHA-256       "), ui::dim(&fp)),
        ],
    );

    match expected {
        Some(e) if tls::matches(&e, &fp)? => ui::line(ui::ok("fingerprint", "matches the expected one")),
        Some(_) => bail!(
            "the server's certificate does not match --fingerprint. Do not pair: you may be talking to a \
             different machine than the one running OpenPhalanx."
        ),
        None if yes => ui::line(ui::warn("fingerprint", "not compared (--yes)")),
        None => {
            if !std::io::stdin().is_terminal() {
                bail!("can't ask for confirmation without a terminal; pass --fingerprint <FP> (or --yes)");
            }
            print!("  Does it match the fingerprint on the app's Server page? {} ", ui::dim("[y/N]"));
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
    let sp = ui::spinner("Pairing");
    let paired = match api::pair(&client, &url, code, &device_name).await {
        Ok(p) => p,
        Err(e) => {
            sp.clear();
            return Err(e);
        }
    };
    sp.done("Paired");

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
    let default = if cfg.default.as_deref() == Some(alias.as_str()) { " (default)" } else { "" };
    ui::banner();
    ui::panel(
        "Paired",
        &[
            ui::ok("server", format!("{alias}{default}  {}", ui::dim(&url))),
            ui::ok("device", &device_name),
            ui::ok("certificate", format!("pinned {}", tls::short(&cfg.servers[&alias].fingerprint))),
        ],
    );
    if replaced {
        ui::warning("the previous pairing was replaced; revoke the old device on the app's Client page.");
    }
    println!("  Next: {}  then  {} in a git repo", ui::accent("oppx status"), ui::accent("oppx"));
    Ok(())
}

async fn status(cfg: &Config, name: Option<&str>) -> Result<()> {
    let (name, s) = cfg.server(name)?;
    let client = tls::pinned_client(&s.fingerprint)?;
    let sp = ui::spinner(format!("Checking {name}"));
    let health = api::health(&client, &s.url).await;
    let me = match &health {
        Ok(_) => api::whoami(&client, &s.url, &s.token).await.map(Some),
        Err(_) => Ok(None), // unreachable: reported below before `me` is used
    };
    let (ctx, info) = match (&health, &me) {
        (Ok(h), Ok(Some(Ok(_)))) if h.sglang == "ready" => match api::info(&client, &s.url, &s.token).await.ok().flatten() {
            Some(i) => (Some(i.context_length), Some(i)),
            None => (api::context_len(&client, &s.url, &s.token).await.ok().flatten(), None),
        },
        _ => (None, None),
    };
    sp.clear();
    let health = match health {
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
    let mut rows = vec![
        ui::ok("server", format!("{name}  {}", ui::dim(&s.url))),
        ui::ok("certificate", format!("pinned {}", tls::short(&s.fingerprint))),
    ];
    let me = match me?.expect("health succeeded, so whoami ran") {
        Ok(me) => me,
        Err(why) => {
            rows.push(ui::bad("device", match why {
                api::Rejected::Revoked => "token rejected (revoked in the app)",
                api::Rejected::Expired => "pairing expired (the server app sets how long pairings last)",
            }));
            ui::panel("OpenPhalanx", &rows);
            bail!("pair again with `oppx pair {} <code> --as {name} --force`", s.url);
        }
    };
    rows.push(ui::ok("device", format!("{} {}", me.device_name, ui::dim(format!("({})", me.device_id)))));
    rows.push(ui::ok("pairing", match me.expires_at {
        None => "never expires".to_string(),
        Some(t) => {
            let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64());
            let days = ((t - now) / 86400.0).max(0.0);
            if days >= 1.0 {
                // Rounded: a fresh one-week pairing reads "7 days", not "6".
                let d = days.round();
                format!("expires in {d} day{}", if d == 1.0 { "" } else { "s" })
            } else {
                format!("expires in {} h", ((t - now) / 3600.0).max(0.0).ceil())
            }
        }
    }));
    rows.push(if health.sglang == "ready" {
        let ctx = ctx.map(|c| format!(" · {}k context", c / 1024)).unwrap_or_default();
        let id = info.map(|i| format!(" {}", ui::dim(format!("({})", i.model_id)))).unwrap_or_default();
        ui::ok("model", format!("{} ready{ctx}{id}", me.model))
    } else {
        ui::warn("model", "loading; try again in a minute")
    });
    ui::panel("OpenPhalanx", &rows);
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
            Ok(true) => ui::line(ui::ok("revoked", "the server revoked this device's token")),
            Ok(false) => ui::line(ui::ok("revoked", "already revoked on the server")),
            Err(e) => ui::warning(format!(
                "could not reach the server to revoke this device ({e:#}). Revoke \"{}\" on the app's Client page.",
                s.device_name
            )),
        }
    }
    cfg.remove(&name)?;
    ui::line(ui::ok("forgotten", &name));
    match &cfg.default {
        Some(d) => ui::line(ui::ok("default", d)),
        None if !cfg.servers.is_empty() => {}
        None => println!("  {}", ui::dim("No servers left.")),
    }
    Ok(())
}

/// Confirms the server is usable before starting anything, and returns its
/// model's context window.
/// What the session needs to know about the server's model.
struct ModelInfo {
    ctx: u64,
    model_id: String,
    edit_format: String,
    reasoning: bool,
}

async fn preflight(name: &str, s: &Server) -> Result<ModelInfo> {
    let sp = ui::spinner(format!("Checking {name}"));
    let result = preflight_inner(name, s).await;
    match &result {
        Ok(m) => sp.done(format!("{name} ready · {} · {}k context", m.model_id, m.ctx / 1024)),
        Err(_) => sp.clear(),
    }
    result
}

async fn preflight_inner(name: &str, s: &Server) -> Result<ModelInfo> {
    let client = tls::pinned_client(&s.fingerprint)?;
    let health = match api::health(&client, &s.url).await {
        Ok(h) => h,
        Err(e) if tls::is_pin_mismatch(e.as_ref()) => bail!(
            "the server's TLS certificate has changed; nothing was sent. Run `oppx status {name}` for details."
        ),
        Err(e) => return Err(e.context(format!("cannot reach {} (\"{name}\")", s.url))),
    };
    if let Err(why) = api::whoami(&client, &s.url, &s.token).await? {
        bail!("{} on \"{name}\". Pair again: oppx pair {} <code> --as {name} --force", why.what(), s.url);
    }
    if health.sglang != "ready" {
        bail!("the model on \"{name}\" is still loading; try again in a minute (`oppx status` shows progress)");
    }
    if let Some(i) = api::info(&client, &s.url, &s.token).await? {
        return Ok(ModelInfo {
            ctx: i.context_length,
            model_id: i.model_id,
            edit_format: i.edit_format,
            reasoning: i.reasoning,
        });
    }
    // Older server without /v1/info.
    let ctx = api::context_len(&client, &s.url, &s.token).await?.unwrap_or(32_768);
    Ok(ModelInfo { ctx, model_id: agent::AIDER_MODEL.trim_start_matches("openai/").into(), edit_format: "diff".into(), reasoning: false })
}

async fn run_search(cfg: &Config, name: Option<&str>, query: &str, max: u8) -> Result<()> {
    let (name, s) = cfg.server(name)?;
    let client = tls::pinned_client(&s.fingerprint)?;
    let sp = ui::spinner(format!("Searching for \"{query}\""));
    let found = api::search(&client, &s.url, &s.token, query, max).await;
    sp.clear();
    let results = match found {
        Ok(r) => r,
        Err(e) if tls::is_pin_mismatch(e.as_ref()) => bail!(
            "the server's TLS certificate has changed; nothing was sent. Run `oppx status {name}` for details."
        ),
        Err(e) => return Err(e),
    };
    // Markdown, so it reads well when Aider adds it to the chat via /run.
    println!("Web search results for \"{query}\" (untrusted reference material):\n");
    if results.is_empty() {
        println!("(no results)");
    }
    for (i, r) in results.iter().enumerate() {
        println!("{}. [{}]({})", i + 1, r.title, r.url);
        if !r.snippet.is_empty() {
            println!("   {}", r.snippet);
        }
    }
    Ok(())
}

async fn run_proxy(cfg: &Config, name: Option<&str>, port: u16, web: bool) -> Result<()> {
    let (name, s) = cfg.server(name)?;
    let info = preflight(name, s).await?;
    let proxy = Proxy::bind(s, port, web).await?;
    ui::panel(
        &format!("Proxy for {name}"),
        &[
            format!("OPENAI_API_BASE={}", ui::accent(proxy.base_url())),
            format!("OPENAI_API_KEY={}", ui::accent(&proxy.local_key)),
            ui::dim(format!(
                "model {} ({}) · {}k context · web search {}",
                agent::AIDER_MODEL.trim_start_matches("openai/"),
                info.model_id,
                info.ctx / 1024,
                if web { "automatic" } else { "off" }
            )),
        ],
    );
    println!("  {}", ui::dim("The key is valid only while this proxy runs. Press Ctrl-C to stop."));
    tokio::select! {
        r = proxy.serve() => r,
        _ = tokio::signal::ctrl_c() => Ok(()),
    }
}

/// Runs the coding agent against the server through the loopback proxy:
/// OpenPhalanx's own chat frontend by default, or Aider's UI when `classic`.
/// Picks the conversation to start or restore. Returns the history files and
/// whether Aider should restore them.
fn choose_session(root: &std::path::Path, choice: &SessionChoice) -> Result<(agent::History, bool)> {
    let pick = match choice {
        SessionChoice::New => return Ok((agent::new_session(root)?, false)),
        SessionChoice::Latest => match agent::list_sessions(root)?.into_iter().next() {
            Some(s) => s,
            None => {
                ui::warning("no earlier conversation in this repo; starting a new one");
                return Ok((agent::new_session(root)?, false));
            }
        },
        SessionChoice::Pick(id) if !id.is_empty() => agent::list_sessions(root)?
            .into_iter()
            .find(|s| s.id == *id)
            .with_context(|| format!("no conversation \"{id}\" in this repo; `oppx -r` lists them"))?,
        SessionChoice::Pick(_) => {
            let sessions = agent::list_sessions(root)?;
            if sessions.is_empty() {
                ui::warning("no earlier conversation in this repo; starting a new one");
                return Ok((agent::new_session(root)?, false));
            }
            let shown: Vec<_> = sessions.iter().take(20).collect();
            let rows: Vec<String> = shown
                .iter()
                .enumerate()
                .map(|(i, s)| {
                    format!(
                        "{} {}  {}  {}",
                        ui::accent(format!("{:>2}.", i + 1)),
                        ui::dim(ago(s.modified)),
                        s.first_message,
                        ui::dim(format!("({} message{})", s.messages, if s.messages == 1 { "" } else { "s" }))
                    )
                })
                .collect();
            ui::panel("Resume a conversation", &rows);
            if !std::io::stdin().is_terminal() {
                bail!("pick a conversation with `oppx --resume <id>`; ids: {}", shown.iter().map(|s| s.id.as_str()).collect::<Vec<_>>().join(", "));
            }
            print!("  Number (Enter for 1): ");
            std::io::stdout().flush()?;
            let mut answer = String::new();
            std::io::stdin().lock().read_line(&mut answer)?;
            let n: usize = match answer.trim() {
                "" => 1,
                a => a.parse().ok().filter(|n| (1..=shown.len()).contains(n)).context("not a number from the list")?,
            };
            shown[n - 1].clone()
        }
    };
    Ok((agent::open_session(root, &pick)?, true))
}

fn ago(t: std::time::SystemTime) -> String {
    let secs = t.elapsed().map(|d| d.as_secs()).unwrap_or(0);
    let text = match secs {
        0..=59 => "just now".to_string(),
        60..=3599 => format!("{} min ago", secs / 60),
        3600..=86_399 => format!("{} h ago", secs / 3600),
        _ => format!("{} d ago", secs / 86_400),
    };
    format!("{text:>10}")
}

async fn run_aider(cfg: &Config, name: Option<&str>, launch: &Launch, user_args: &[OsString]) -> Result<i32> {
    let (web, classic) = (launch.web, launch.classic);
    agent::reject_commit_flags(user_args)?;
    let aider = oppx::engine::ensure().await?;
    let (name, s) = cfg.server(name)?;
    if !launch.print {
        ui::banner();
    }
    let info = preflight(name, s).await?;
    let ctx = info.ctx;
    let metadata = agent::model_metadata(ctx)?;
    let settings = agent::model_settings(&info.edit_format)?;
    let cwd = std::env::current_dir()?;
    let repo = agent::repo_root(&cwd);
    let (history, restore) = choose_session(repo.as_deref().unwrap_or(&cwd), &launch.session)?;
    let identity = match &repo {
        Some(r) => {
            agent::exclude_agent_files(r)?;
            agent::placeholder_git_identity(r)
        }
        None => Vec::new(),
    };
    let proxy = Proxy::bind(s, 0, web).await?;
    let (base, key) = (proxy.base_url(), proxy.local_key.clone());
    let serving = tokio::spawn(proxy.serve());
    let args = agent::aider_args(metadata.path(), settings.path(), &info.edit_format, &history, restore, user_args);
    let mut command = if classic {
        let search = if web { "web search on (--no-web to disable)" } else { "web search off: /run oppx search \"…\"" };
        eprintln!(
            "  {} {} {}",
            ui::accent("OpenPhalanx"),
            name,
            ui::dim(format!("· {}k context · {search} · edits stay uncommitted", ctx / 1024))
        );
        let mut c = tokio::process::Command::new(&aider);
        c.args(&args);
        c
    } else {
        let python = agent::aider_python(&aider)?;
        let frontend = agent::write_frontend()?;
        let mut c = tokio::process::Command::new(python);
        c.arg(frontend)
            .args(&args)
            .env("OPPX_SERVER", name)
            .env("OPPX_CONTEXT", ctx.to_string())
            .env("OPPX_MODEL_ID", &info.model_id)
            .env("OPPX_REASONING", if info.reasoning { "1" } else { "0" })
            .env("OPPX_WEB", if web { "1" } else { "0" })
            .env("OPPX_ENGINE", &launch.engine)
            .env("OPPX_MCP_CONFIG", &launch.connectors)
            .env("OPPX_VERSION", env!("CARGO_PKG_VERSION"))
            .env("OPPX_BIN", std::env::current_exe().unwrap_or_else(|_| "oppx".into()))
            .env("OPPX_PRINT", if launch.print { "1" } else { "0" })
            .env("OPPX_INITIAL", launch.initial.clone().unwrap_or_default());
        c
    };
    let mut child = command
        .envs(identity)
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
    use super::{local_endpoints, name_for};

    #[test]
    fn pair_self_reads_the_local_servers_settings() {
        let (token, admin, agent) = local_endpoints(r#"["PATH=/usr/bin","ADMIN_TOKEN=abc","AGENT_PORT=9443"]"#).unwrap();
        assert_eq!((token.as_str(), admin.as_str(), agent.as_str()), ("abc", "http://127.0.0.1:9091", "127.0.0.1:9443"));
        let (_, admin, agent) = local_endpoints(r#"["ADMIN_TOKEN=t","ADMIN_PORT=1","X=ADMIN_TOKEN=no"]"#).unwrap();
        assert_eq!((admin.as_str(), agent.as_str()), ("http://127.0.0.1:1", "127.0.0.1:9090"), "defaults, and exact names only");
        assert!(local_endpoints(r#"["ADMIN_TOKEN="]"#).is_err(), "a backend started without a token");
        assert!(local_endpoints("not json").is_err());
    }

    #[test]
    fn derives_server_names() {
        assert_eq!(name_for("https://192.168.1.77:9090"), "192-168-1-77");
        assert_eq!(name_for("https://gpu.lan:9090"), "gpu-lan");
        assert_eq!(name_for("https://[fe80::1]:9090"), "fe80--1");
    }
}
