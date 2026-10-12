---
name: headless-backend
description: "Running the backend container by hand, and oppxs: its commands, the control API, Service, the systemd unit, limits and what was tested. Read before changing crates/openphalanx-server, service.rs or docker run arguments."
---

# Backend without the GUI, and the headless server (oppxs)

Part of the developer notes; the index is in `CLAUDE.md`.

## Run the backend without the GUI

Useful on a headless machine or while developing the backend. Build the image from the repo (the app does the same from the copy it carries), then run it; the container serves the model in `MODEL_PATH` (a Hugging Face repo id or a path inside the container):

```bash
docker build -f docker/Dockerfile.server -t openphalanx-backend:dev docker/
export ADMIN_TOKEN=$(openssl rand -hex 32)
mkdir -p ~/.local/share/openphalanx/backend-state

docker run -d --name openphalanx-backend --gpus all --ipc=host \
  -p 9090:9090 -p 127.0.0.1:9091:9091 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v ~/.local/share/openphalanx/backend-state:/state \
  -e ADMIN_TOKEN \
  openphalanx-backend:dev

# When the admin API answers, issue a pairing code:
curl -s -X POST -H "x-admin-token: $ADMIN_TOKEN" http://127.0.0.1:9091/admin/pairing

# Readiness (sglang turns "ready" after ~4 min):
curl -s -H "x-admin-token: $ADMIN_TOKEN" http://127.0.0.1:9091/admin/status | jq .sglang
```

Optional environment variables: `MODEL_PATH`, `CONTEXT_LENGTH`, `MEM_FRACTION_STATIC` (default `0.85`; the GUI computes it from free VRAM instead), `SGLANG_EXTRA_ARGS`. Without `APP_WATCHDOG_S` (which only the app sets) a backend started this way runs until you stop it. The GUI shows a container started this way as "running outside the app", and can stop it.

## Headless server (`oppxs`)

The server as a command, for machines without a desktop and for scripting. It comes inside the `.deb` (`/usr/bin/oppxs`, with a disabled systemd user unit) and as a static binary in each release; `install-server.sh --headless` installs only that, on any Linux, x86-64 or arm64.

```bash
oppxs run [--name NAME]                 # the server itself; a systemd user unit keeps it running
oppxs models [--all]                    # catalog: download size, VRAM need at the current context, fit, state
oppxs download <model>                  # verified, resumable; a catalog id or a unique part of one
oppxs use <model> | oppxs context <n>   # what `start` serves (the app's settings.json)
oppxs long-context on|off            # let models with a YaRN mode go past their native window (off by default)
oppxs port [admin|control] [<n>]        # show or move the client (9090), admin (9091) or control (9094) port
oppxs ports                             # every port the server uses and who holds it
oppxs web-search on|off                 # the private SearXNG for clients (on by default)
oppxs gpus [all | 0,2]                  # this machine's GPUs and which the server may use (all by default; several: experimental)
oppxs start | stop | status             # start follows progress until it serves or fails
oppxs pair | devices | revoke <device>  # pairing code with the fingerprint; paired clients
oppxs logs [-n LINES] [<member>]        # the backend's log, or a cluster member's
oppxs servers | invite | make-host | approve | decline | remove | leave | dissolve | strategy | model
```

* **How it's built:** `core::service::Service` is the orchestrator without a UI: `start` (cluster claim, pre-flight, which refuses a model with broken files, then `server::start`), `stop`, `status` (crash detection with `diagnose_crash`, adopting a backend that already runs, `set_serving` for the cluster), pairing and devices through the admin API. `oppxs run` runs it with the cluster service and a monitor that looks every 2 s, which also feeds the gateway's watchdog. The app still has the same logic in its own command layer (`app/src-tauri/src/lib.rs`) and is to move onto `Service`.
* **Control API** (`127.0.0.1:9094`, bearer token from `cluster/control.token`, 0600): `GET /control/server`, `POST /control/server/{start|stop|pair}`, `GET /control/devices`, `DELETE /control/devices/{id}`, plus the cluster routes. `models`, `download`, `use`, `context` and `logs` without a member need no running server: they read and write the same settings and model folders as the app.
* **Stopping it stops the model** (`Service::shutdown` on Ctrl-C or SIGTERM), like closing the app; a killed `oppxs` leaves it to the watchdog. So `systemctl --user restart oppxs` reloads the model.
* **Service:** `systemctl --user enable --now oppxs`; `loginctl enable-linger $USER` starts it at boot without a login. The user needs Docker access (the `docker` group).
* **Same rules as the app's slider and checkboxes** (in `oppxs` itself, `main.rs`): `context` and `long-context` are refused while a backend runs; `context` refuses more than the selected model's maximum (pointing to `long-context on` when that would allow it) or more than fits the VRAM free now; `long-context off` brings a longer context back to the native window; `start --force` first sets the context to the longest that fits, or the 2k minimum; `models` marks damaged copies "broken: download it again"; `status` shows the context a start would serve, never above the model's maximum. Tried against scratch settings (`XDG_CONFIG_HOME`), without starting a backend.
* **Limits for now:** it serves models that fit its own GPU and can be a cluster member; hosting a split model is started from the desktop app (`plan_split` lives there). The commands talk to `oppxs run` only, not to a running desktop app, and the two can't run on one machine at the same time (ports 9092–9094, shared state).
* **Tested on the 3090** (scratch settings, Qwen2.5-Coder-14B-AWQ at 16k): `run`, `start` (3 min 51 s to serving, image build included), `status`, `pair` and a real `oppx pair` and `oppx status` against it, `devices`, `revoke` (the client is then refused), `logs`, `stop` (2.8 s), a second `start`, and SIGTERM to the daemon (it exited in 3 s with both containers gone and the GPU's memory free). The backend carried `APP_WATCHDOG_S=60`. Not yet run: the static musl build, the `.deb` with `oppxs` inside and `install-server.sh --headless` (they need a release), and `oppxs` as a cluster host.
