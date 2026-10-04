# OpenPhalanx

Run a capable coding model on your own GPU machine, and use it from any laptop on your network.

OpenPhalanx turns a Linux box with an NVIDIA GPU into a private coding-model server: a desktop app picks a model that safely fits your VRAM, starts it, and pairs your laptops with a one-time code. The coding agent ([Aider](https://aider.chat)) runs on the laptop next to your code, so your repo, tests and git stay local and only prompts travel to the server.

```
  Laptop (client)                              GPU server
 ┌────────────────────────────┐   HTTPS +   ┌────────────────────────────────┐
 │ your repo · tests · git    │ device token│ OpenPhalanx app (start/stop,   │
 │ Aider agent                │ ──────────▶ │   models, pairing, VRAM checks)│
 │ (edits files locally)      │ ◀────────── │ gateway → SGLang → model       │
 └────────────────────────────┘   answers   └────────────────────────────────┘
```

> **Status:** early development, Linux server only. The server app and the `oppx` client work end to end; see [`milestone.md`](milestone.md) for what's next.

## Run the server

You need:

* Linux x86-64 with an NVIDIA GPU (24 GB recommended) and a working `nvidia-smi`
* Docker, with your user in the `docker` group
* [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
Install the app (Ubuntu/Debian; the installer checks the items above first):

```bash
curl -fsSL https://github.com/chophilip21/openPhalanx/releases/latest/download/install-server.sh | sh
```

Or build it from source (also needs Rust, Node.js 20+ and the WebKitGTK packages listed in [`CLAUDE.md`](CLAUDE.md)):

```bash
git clone -b dev https://github.com/chophilip21/openPhalanx.git
cd openPhalanx/app
npm install
npx tauri dev        # or: npx tauri build --bundles deb, then install the .deb
```

Then, in the app:

1. **Models:** pick a model marked **Fits** (or **Best fit for this GPU**) and click **Download**. VRAM estimates include the KV cache and runtime, not just the download size.
2. **Server:** press the power button. The first start downloads the server image (~16 GB); loading the model takes 3–4 minutes.
3. When the button turns green, note the **pairing code** and the server address shown above it.

Open port `9090/tcp` to your laptops' network.

## Connect a laptop

On the laptop (Linux or macOS), install `oppx`. It sets up its own coding engine, so there's nothing else to install:

```bash
curl -fsSL https://github.com/chophilip21/openPhalanx/releases/latest/download/install.sh | sh
oppx --update                     # run any time to update oppx and its engine
```

Or from source, with [Rust](https://rustup.rs): `cargo install --path openPhalanx/client/oppx` in a clone of this repo. The installers need a published [release](https://github.com/chophilip21/openPhalanx/releases); until the first one, use the from-source steps.

Pair once, with the address and code shown in the app. `oppx` prints the server's certificate fingerprint; check that it matches the one in the app, then confirm:

```bash
oppx pair 192.168.1.77 ABCD-EFGH
oppx status                       # certificate, device and model all ✓
```

Then, in any git repo:

```bash
oppx                              # start a conversation
oppx "fix the failing test"       # start with a request
oppx -c                           # continue the last conversation (oppx -r to pick an older one)
oppx -p "explain src/main.rs"     # answer once and exit
```

The context window is managed for you: when a conversation grows, older messages are summarized and unused files are set aside automatically (`/context` shows what's in it). Keys and commands work like Claude Code: `/help`, `@file` to mention a file, `!cmd` to run a shell command, `# note` to save to project memory, `/init` to write a project summary, Shift+Tab for plan mode (no edits), Esc to interrupt, Ctrl-C twice to exit. Questions are answered without touching your files; requests are applied as edits.

When a request needs current information (new library versions, recent APIs, error messages), the server searches the web automatically through a private [SearXNG](https://docs.searxng.org) instance; `oppx --no-web` turns that off.

Edits are never committed: review with `git diff` and commit yourself. Remove a laptop with `oppx unpair` (or revoke it in the app's **Devices** page).

## Learn more

* [`CLAUDE.md`](CLAUDE.md): architecture, security model, running without the GUI, development and troubleshooting
* [`milestone.md`](milestone.md): roadmap and progress
