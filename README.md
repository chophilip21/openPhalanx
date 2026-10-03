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

> **Status:** early development. The server and app work; the `openbase` client CLI, which will make pairing and certificate pinning one command, is in progress (see [`milestone.md`](milestone.md)).

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

On the laptop, install Aider (`uv tool install aider-chat` or `pipx install aider-chat`), pair once, then work in any git repo:

```bash
SERVER=192.168.1.77            # the address shown in the app
TOKEN=$(curl -sk https://$SERVER:9090/v1/pair -H 'content-type: application/json' \
  -d '{"code":"ABCD-EFGH","device_name":"my-laptop"}' | jq -r .token)

OPENAI_API_BASE=https://$SERVER:9090/v1 OPENAI_API_KEY=$TOKEN \
  aider --model openai/openphalanx-coder --no-verify-ssl --no-auto-commits --no-dirty-commits
```

Keep the token: it stays valid until you revoke the device in the app (**Devices**). Pairing codes are single-use and expire after 10 minutes.

> `-k` and `--no-verify-ssl` skip certificate checks, so use this only on a network you trust. `openbase` will replace these steps with `openbase pair` and `openbase aider`, which pin the server's certificate.

## Learn more

* [`CLAUDE.md`](CLAUDE.md): architecture, security model, running without the GUI, development and troubleshooting
* [`milestone.md`](milestone.md): roadmap and progress
