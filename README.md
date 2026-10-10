<div align="center">

# <img src="assets/openphalanx-shield-green.svg" alt="" height="32" align="absmiddle"> OpenPhalanx

**A private AI coding agent on your own GPU**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/v/release/chophilip21/openPhalanx?logo=github)](https://github.com/chophilip21/openPhalanx/releases)
![Status: early development](https://img.shields.io/badge/Status-Early%20development-informational.svg)

Run a coding model on your own Linux GPU machine and use it from any laptop on your network. The coding agent runs on the laptop next to your repo; the server only answers prompts and stores no code.

[🚀 Quick Start](#quick-start) • [📖 Developer docs](CLAUDE.md) • [⭐ GitHub](https://github.com/chophilip21/openPhalanx)

</div>

---

## Why OpenPhalanx?

* ✅ **Private**: your repo, tests and git stay on your laptop. Prompts go only to your own server, over TLS pinned to its certificate, and the server keeps no code.
* ✅ **Models that fit**: the app shows which models fit your GPU, counting the KV cache and runtime memory, not just the download size.
* ✅ **Whole-repo agent**: the agent reads your files on demand and runs your own tests and commands (after asking).
* ✅ **Managed context**: when a conversation grows, older content is trimmed or summarized automatically.
* ✅ **Web search**: the server searches the web through its own private SearXNG when a request needs current information.
* ✅ **Clusters**: servers on one network can form a cluster and split a model too big for one GPU across them.

## Quick Start

### Server (Linux x86-64 + NVIDIA GPU)

You need a working `nvidia-smi` (24 GB of VRAM recommended), Docker with your user in the `docker` group, and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). Install the app (Debian/Ubuntu; the installer checks these first):

```bash
curl -fsSL https://github.com/chophilip21/openPhalanx/releases/latest/download/install-server.sh | sh
```

Then, in the app:
1. **Models:** pick a model marked **Fits** and click **Download**.
2. **Server:** press the power button. The first start builds the server image (a 16 GB download, once); loading the model takes 3–4 minutes.
3. When the button turns green, note the **pairing code** and the server address, and open port `9090/tcp` to your laptops' network.

**No desktop?** Install only `oppxs`, the same server as a command (it also comes with the app). Keep it running with `systemctl --user enable --now oppxs`, then use `oppxs models`, `oppxs download <model>`, `oppxs use <model>`, `oppxs start` and `oppxs pair`:
```bash
curl -fsSL https://github.com/chophilip21/openPhalanx/releases/latest/download/install-server.sh | sh -s -- --headless
```

### Laptop (Linux or macOS)

Install `oppx`. It sets up its own coding engine, so there's nothing else to install:

```bash
curl -fsSL https://github.com/chophilip21/openPhalanx/releases/latest/download/install.sh | sh
oppx --update                     # any time: update oppx and its engine
```

Pair once with the address and code from the app. `oppx` prints the server's certificate fingerprint; check it matches the one in the app, then confirm:

```bash
oppx pair 192.168.1.77 ABCD-EFGH
oppx status                       # certificate, device and model all ✓
```

Then, in any git repo:

```bash
oppx                              # start a conversation
oppx "fix the failing test"       # start with a request
oppx -c                           # continue the last conversation (-r to pick an older one)
oppx -p "explain src/main.rs"     # answer once and exit
oppx --no-web                     # without automatic web search
```

Edits are never committed: review them with `git diff` and commit yourself. To remove a laptop, run `oppx unpair` (or revoke it on the app's **Devices** page).

## Keys and commands

| Key | Action |
|---|---|
| **Shift+Tab** | Plan mode (answers only, no edits) |
| **Esc** | Interrupt the model |
| **Ctrl-C** twice on an empty line | Exit |
| **@file** | Add a file to the conversation |
| **!cmd** | Run a shell command |
| **# note** | Save a note to project memory (`OPENPHALANX.md`) |
| **/help** | All commands (`/context`, `/init`, `/undo`, `/search`, …) |

## From source

Needs Rust, Node.js 20+ and the WebKitGTK packages listed in [CLAUDE.md](CLAUDE.md). Testing and the rules for changes are in [CONTRIBUTING.md](CONTRIBUTING.md).
```bash
git clone -b dev https://github.com/chophilip21/openPhalanx.git && cd openPhalanx
(cd app && npm install && npx tauri dev)    # server app
cargo install --path client/oppx && cargo install --path crates/openphalanx-server   # oppx (client), oppxs (server command)
```

## Learn more

* **[CLAUDE.md](CLAUDE.md)**: architecture, security model, clusters, the headless server, troubleshooting
* **[Releases](https://github.com/chophilip21/openPhalanx/releases)**: downloads and changelog
