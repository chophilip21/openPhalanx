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
* Rust, Node.js 20+, and the WebKitGTK build packages:

```bash
sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev \
  libayatana-appindicator3-dev librsvg2-dev libxdo-dev build-essential
```

Build and start the app:

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

On the laptop, install the `oppx` client (needs [Rust](https://rustup.rs)) and Aider:

```bash
git clone -b dev https://github.com/chophilip21/openPhalanx.git
cargo install --path openPhalanx/client/oppx
uv tool install aider-chat        # or: pipx install aider-chat
```

Pair once, with the address and code shown in the app. `oppx` prints the server's certificate fingerprint; check that it matches the one in the app, then confirm:

```bash
oppx pair 192.168.1.77 ABCD-EFGH
oppx status                       # certificate, device and model all ✓
```

Then, in any git repo:

```bash
oppx aider                        # extra Aider options go after --, e.g. oppx aider -- src/main.rs
```

Need current information (new library versions, recent APIs, error messages)? `oppx aider --web` lets the server search the web automatically when a request needs it, through a private [SearXNG](https://docs.searxng.org) instance on the server. Without `--web`, ask for a search inside Aider with `/run oppx search "your query"`.

Aider edits your files locally and never commits; review with `git diff` and commit yourself. Remove a laptop with `oppx unpair` (or revoke it in the app's **Devices** page). Other OpenAI-compatible tools can use the server through `oppx proxy`.

## Learn more

* [`CLAUDE.md`](CLAUDE.md): architecture, security model, running without the GUI, development and troubleshooting
* [`milestone.md`](milestone.md): roadmap and progress
