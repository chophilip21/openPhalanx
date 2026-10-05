# Audit findings (2026-10-04)

Audited the server (gateway, Docker launch, cluster, downloads, Tauri app) and the client (`oppx` proxy, pinned TLS, updater, engine, chat frontend). I checked every item against the code at the given line, and against the installed Aider 0.86.2 and SGLang (image 0.3.0) sources where noted. Only issues that could cause real trouble later are listed. Each one has the smallest fix I'd suggest. Nothing has been fixed yet.

Severity: **High**: fix before wider use. **Medium**: fix soon. **Low**: cheap, do it when you're nearby.

# bugs

### ~~B1. A disk error after a slot is taken leaks the slot for good (High)~~ ✅ Fixed
**Done:** `devices.touch` now runs inside the guarded block, and the slots are released on *any* exception. Non-HTTP errors are re-raised as before. This also covers a second path with the same leak: `devices.add_search` inside `_send_with_auto_search`. Verified with a scratch harness that makes `devices.json` writes fail: 10 failing requests give back all 8 server slots and all 4 device slots. The pre-fix code hung on the 5th request.

<details><summary>Original finding</summary>

`docker/server/gateway.py:834-841`: `_acquire()` takes a device slot and a server slot, then `devices.touch(device)` runs **outside** the `try` that releases them. `touch` rewrites `devices.json` on every call (`:167-170`), so a full disk or a read-only state dir raises `OSError`, FastAPI returns a 500, and both semaphores stay taken. After 4 such requests that device is locked out; after 8, every chat request waits 120 s and gets a 503 until the container restarts.
**Fix:** call `devices.touch` before `_acquire`, or inside the `try`.
</details>

### ~~B2. Gateway Python dependencies aren't pinned (Medium)~~ ✅ Fixed
**Done:** `docker/Dockerfile.server` pins the versions read from the tested 0.3.0 image: `fastapi==0.142.2 starlette==1.7.0 pydantic==2.13.5 uvicorn[standard]==0.54.0 httpx==0.28.1`. The pinned set installs cleanly, and the gateway's slot test passes on it. Bump these on purpose when upgrading.

<details><summary>Original finding</summary>

`docker/Dockerfile.server:16` installs `fastapi "uvicorn[standard]" httpx` with no versions. SGLang is pinned by digest, but rebuilding the same release tag later picks up whatever FastAPI, Starlette, httpx or uvicorn is newest that day. The gateway uses APIs that change between releases: `StreamingResponse(background=…)`, `request.stream()`, `upstream().send(..., stream=True)`. The same `0.3.0` tag could then behave differently.
**Fix:** pin exact versions (`fastapi==…`, `uvicorn[standard]==…`, `httpx==…`).
</details>

# security

### ~~S1. Members accept the host's model repo and revision without validation (path traversal) (Medium)~~ ✅ Fixed
**Done:** new `ModelSpec::check` (`cluster.rs`): the repo must pass `model::validate_repo` (now `pub(crate)`), and the revision must be a 40-hex commit. All 28 catalog entries already are. `ensure_model` refuses an invalid spec before any path or URL is built: it sets the sync state to `error`, logs once, and starts no download. `start_worker` checks it too. Covered by the tests `members_refuse_host_orders_that_escape_paths_or_add_flags` and `a_member_refuses_an_invalid_host_model_without_downloading`.

<details><summary>Original finding</summary>

`crates/openphalanx-core/src/cluster.rs:1488` builds `dest = models_dir().join(&spec.repo)`, and `download.rs:64/221` formats `repo` and `revision` straight into Hugging Face URLs. Both values come from the host's report reply (`ensure_model`, `WorkerOrder`) and are never checked. A local entry goes through `model::validate_repo` (`model.rs:75`); a cluster entry doesn't. A `..` in `repo` points the download outside the models folder. The `url` crate normalises `..` in the HTTP path, so a crafted repo plus revision can still resolve to a real repo. The result is `.json`/`.txt`/`.jinja` files written wherever the user can write (for example `~/.config/...`). Non-LFS files are only size-checked, not hashed.
**Fix:** in `ensure_model` and `start_worker`, call `validate_repo(&spec.repo)` and require `revision` to be 40 hex characters. Two lines; it also protects `is_app_managed` (`lib.rs:759`, a lexical `starts_with`) from `..` paths.
</details>

### ~~S2. `WorkerOrder` fields go unvalidated into SGLang's arguments (Medium)~~ ✅ Fixed
**Done:** new `WorkerOrder::check`, run first in `start_worker`. It checks the model (S1), allows `dtype` only from `auto/half/float16/bfloat16/float/float32`, requires `partition` to match `^\d+(,\d+)*$`, requires `dist_init_addr` to parse as a `SocketAddr` (the host always sends `ipv4:port`), and refuses an image name with a leading `-` or whitespace. A refused order shows up as a failed worker with the reason, and the host then stops the run. Unit-tested with smuggled-flag cases.

<details><summary>Original finding</summary>

`docker.rs:233-243` and `:388-391` put `dist_init_addr`, `partition` and `dtype` from the host into `SGLANG_EXTRA_ARGS`. `start-sglang.sh:26` then word-splits that variable unquoted. It isn't a shell injection, but it is **argument injection**: a `dtype` of `"bfloat16 --trust-remote-code"`, together with S1's arbitrary repo, runs remote Python on the member in a `--network host --ipc=host` GPU container. The member trusts the host by design, but today one compromised host means code execution on every member.
**Fix:** validate on the member before `run_worker`: `dtype` from a fixed list, `partition` matching `^\d+(,\d+)*$`, `dist_init_addr` matching `ip:port`.
</details>

### ~~S3. With auto-yes, a model edit can create or change files without asking (Medium)~~ ✅ Fixed
**Done:** `oppx_io.confirm_ask` now refuses Aider's two edit questions (`EDIT_QUESTIONS`) when the path resolves outside the repo root, or inside `.git/` (a hook there would run on the next git command). It shows "Refused an edit outside this project: …" and Aider skips the edit. Verified with a real Aider 0.86.2 `Coder` and our `OppxIO`, with and without a git repo: `new.py`, `a.py` and `sub/new.py` are allowed; `../escape.txt`, an absolute path outside the repo and `.git/hooks/pre-commit` are refused, and nothing is created. The old frontend created all three. Edits inside the repo are still auto-accepted, by design; they're shown as a diff and `/undo` reverts them.

<details><summary>Original finding</summary>

`client/oppx/src/agent.rs:308` passes `--yes-always`, and `oppx_io.py:248` answers every other confirmation with `True`. That includes Aider's *"Create new file?"* and *"Allow edits to file that has not been added to the chat?"*. In Aider 0.86.2, `Coder.allowed_to_edit` → `abs_root_path` does `Path(root) / path`, then `resolve()`, with **no check that the result stays inside the repo** (`base_coder.py:566-574, 2191-2236`).
* Inside a git repo, edits outside the repo only fail by accident, because the `git add` afterwards errors. A new file has already been created (empty) by then.
* Without a repo, Aider's "create a git repo?" prompt is auto-accepted too, in any folder except `~`.

The edit text can be steered by repo content, or by web results (automatic search is on by default, and the results are injected into the prompt).
**Fix:** in `confirm_ask`, when the question is about creating or editing a file, refuse (or ask the user) if `subject` resolves outside the repo root. One `Path.resolve().is_relative_to(root)` check.
</details>

### ~~S4. Split mode exposes unauthenticated, pickle-based traffic between ranks on the LAN (Medium, document it)~~ ✅ Documented
**Done:** CLAUDE.md now has a "Trusted LAN only" bullet under Split serving and a "Split model" line in the Security model, and the cluster panel's Split tooltip says the traffic between servers isn't authenticated, so use it only on a network you trust. The optional `ufw` rules were left out (not worth the complexity yet).

<details><summary>Original finding</summary>

In split mode, rank 0 and the workers run with `--network host`. The torch rendezvous (`9100`) and the NCCL/gloo ports listen on the LAN without authentication, and SGLang passes objects between ranks with `pickle.loads` (`sglang/srt/utils/common.py:2573, 2638`, `broadcast_pyobj`). Anyone on that network segment who can reach those ports while a split model is starting or running is a potential RCE. Pipeline parallelism across machines needs these ports, so this can't be fully fixed in our code.
**Fix:** say clearly in the docs and in the cluster panel that split mode requires a trusted LAN. Optionally, `ufw` rules limiting `9100-9116` and the ephemeral ports to member IPs while serving.
</details>

### S5. The backend image is pulled by a mutable tag (Low)
`docker.rs:15`: `DEFAULT_IMAGE` is `ghcr.io/…/openphalanx-backend:<version>`, a tag. Anyone who can push to that GHCR package (or a re-run of `publish-image.sh`) can swap what every GUI runs next, with GPU access and the backend state mount. SGLang and SearXNG are already pinned by digest; our own image isn't.
**Fix:** after publishing, record the digest (for example in `catalog.json` or a constant bumped by the release) and pull `…@sha256:`. If that's too much process for now, at least never re-push an existing tag.

### S6. LAN-facing endpoints without authentication have no bounds (Low)
These are cheap denial-of-service paths for anyone on the LAN:
* **Pending invitations never expire:** `cluster.rs:930-945` caps them at 8 but never drops old ones (`received_at` is never read). 8 fake invites to `/cluster/v1/invite` block real ones with 429 until each is declined by hand.
  * **Fix:** `pending.retain(|p| now() - p.received_at < INVITE_TTL_SECS)` before the cap; the same for `host_requests`.
* **Discovery accepts any number of servers:** beacons with random ids grow `candidates` freely, and `check_candidates` health-checks them **one by one** with a 3 s timeout (`cluster.rs:1316-1335`). Spoofed beacons stall the verification of real servers, and make this machine open TLS connections to any IP.
  * **Fix:** cap `candidates`, for example at 64, and ignore new ids beyond that.
* **Pairing codes can be burned:** 5 bad `/v1/pair` guesses from anyone kill the active code (`gateway.py:226-236`). This is a nuisance, not a breach; leave it unless it actually happens.

# performance

### P1. Every chat request rewrites `devices.json` 2–3 times, blocking the event loop (Low)
`gateway.py:167-179`: `touch`, `add_usage` (and `add_search`) each call `save()`, which writes indented JSON of all devices to disk synchronously inside the asyncio loop. That's small today, but it's the hot path. It also causes B1.
**Fix:** keep the counters in memory and save at most every few seconds (or on revoke and pair). Only pairing and revocation need an immediate write.

### P2. Each SGLang probe opens a new HTTP client, including from the unauthenticated `/health` (Low)
`gateway.py:293-311`: `sglang_ready()` and `sglang_metrics()` create a new `httpx.AsyncClient` per call. They're called by `/health` (open to the LAN), `/v1/info`, and the GUI's 2 s admin poll. A shared pooled client, `upstream()`, already exists.
**Fix:** use `upstream()` with `timeout=5` in both. A one-line change each.

# Checked and fine (don't re-audit)

* **Pinned TLS** (`crates/pinned-tls`): the certificate is pinned by fingerprint, but the handshake signature is still verified against the certificate's key. Accepting an 8-byte prefix when pairing leaves 64 bits of collision resistance.
* **Device tokens:** stored as SHA-256 hashes and compared with `hmac.compare_digest`. Pairing redemption has no `await`, so it's atomic.
* **Ports:** the admin API is on loopback, both with a bridge network (`-p 127.0.0.1:9091`) and with the host network (`ADMIN_HOST`). SGLang binds `127.0.0.1`. SearXNG publishes no ports, or loopback only. Nothing runs with `--trust-remote-code`.
* **Gateway streaming:** slot cleanup is idempotent and also runs as the response's background task. A cancelled speculative request is aborted (`_discard`).
* **Cluster:** member tokens are stored hashed. Join invitations are single-use, bound to one server, and expire after 10 minutes. Auto-accept only happens for trusted fingerprints, which can't be forged without the private key.
* **Downloads:** file names containing `/` are rejected (no path traversal from Hugging Face listings), and LFS files are SHA-256 verified.
* **Client:** the proxy listens on loopback with a per-run key, and the device token never reaches the agent. The config file is `0600` in a `0700` folder. `--update` and `install*.sh` check `SHA256SUMS`.
* **CI:** PR titles are passed to the workflows through `env:`, not `${{ }}` inside `run:`, so there's no script injection.
* **Tauri:** the CSP is strict, there's no `{@html}` or `innerHTML`, and only minimal capabilities are granted.
