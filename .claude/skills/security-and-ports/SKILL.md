---
name: security-and-ports
description: "Every port and state file the server and client use, and the security model: TLS pinning, pairing codes, device tokens, expiry, admin API, connectors, web search, the gateway's safety bars, split-model trust. Read before adding a port, an endpoint, a state file or anything that handles tokens or prompts."
---

# Ports, files and the security model

Part of the developer notes; the index is in `CLAUDE.md`.

## Ports and files

| | |
|---|---|
| `9090/tcp` (all interfaces) | Public gateway API, TLS only. `/health` and `/v1/pair` are open. `/v1/whoami`, `/v1/unpair` (self-revoke), `/v1/info` (model id, context, edit format), `/v1/tokenize`, `/v1/search`, `/v1/models` and `/v1/chat/completions` (OpenAI-compatible, streaming) need a device token |
| `9091/tcp` (`127.0.0.1` only) | Admin API for the GUI; needs the per-launch admin token |
| `9092/tcp` (all interfaces, while the app or `oppxs` runs) | Cluster API between servers (TLS, the server's own certificate). `health`, `invite`, `host-request` and `join` (needs an invitation secret) are open; `report` and `leave` need a member token |
| `9093/udp` (all interfaces) | Discovery beacons (LAN broadcast) |
| `9094/tcp` (`127.0.0.1` only) | `oppxs` control API (backend and cluster commands); needs `control.token` |
| `9096`, `9098` (`127.0.0.1` only), `9100`–`9116/tcp` and NCCL's ephemeral ports | Split model only: SGLang and SearXNG on the host's network, and the rendezvous and traffic between the ranks |
| `~/.local/share/openphalanx/cluster/` | This server's certificate (`head.crt`, `head.key`, 0600), identity, role and trusted hosts (`state.json`), members as host (`members.json`, hashed tokens), and the split run's worker orders (`workers.json`). Deleting it resets the server's cluster identity |
| `~/.config/openphalanx/settings.json` | Selected model, context length, selected GPUs, custom models |
| `~/.config/oppx/mcp.json` (client, 0600) and `<repo>/.mcp.json` | Connectors: the user's own MCP servers and a repository's (`oppx mcp add`). `~/.local/state/oppx/mcp-approved.json` records the repository connectors the user agreed to; `mcp-<name>.log` is a connector's stderr |
| `~/.local/share/openphalanx/models/` | Models downloaded by the GUI (verified, pinned to a commit) |
| `~/.local/share/openphalanx/backend-state/` | Backend TLS certificate and paired devices (hashed tokens). Deleting it unpairs every client and changes the fingerprint |
| `~/.local/share/openphalanx/searxng/settings.yml` | SearXNG settings (no secrets), mounted read-only |
| `~/.cache/huggingface/hub/` | Existing Hugging Face cache; reused read-only when a model is already there |

Open `9090/tcp` in your firewall for the clients' network, and `9092/tcp` plus `9093/udp` between cluster servers. Never expose `9091` or `9094`.

## Security model

* **TLS:** the public API uses a self-signed certificate created on first start. Clients pin its SHA-256 fingerprint when they pair (shown in the GUI), so later connections can't be intercepted.
* **Pairing codes:** 8 characters, about 39 bits. Each is single-use, valid for 10 minutes, and burned after 5 wrong attempts, with a 1 s delay after each miss.
* **Device tokens:** each client trades a pairing code for its own random 256-bit device token. The server stores only the SHA-256 hash, and you can revoke any device from the GUI.
* **Pairing expiry:** pairings last 1 day, 1 week (default), 1 month, 1 year or never (Client tab; `settings.pairing_ttl_days`). The app passes it as `DEVICE_TTL_DAYS` at start and changes it live with `PUT /admin/pairing-policy`; it applies to every device, counted from when it paired. Devices paired before expiry existed count from the upgrade (`since` in `backend-state/pairing_policy.json`). An expired token gets a 401 "pairing expired"; `oppx status`, the chat and `oppx search` say to pair again, and `oppx status` shows how long the pairing has left.
* **Admin API:** the admin API is reachable from the server machine only, and requires a random token generated at each launch.
* **Connectors (MCP):** run on the client with the user's own credentials; the server never sees their definitions. A repository's `.mcp.json` starts nothing until the user agreed to that exact definition, every tool call is confirmed (or allowed for the session), and what a connector returns goes to the model as untrusted reference text. Like any tool output, it becomes part of the prompts sent to the server.
* **Web search:** SearXNG is reachable only from the gateway (private network, no ports) and keeps no logs. Automatic search is on by default (the user's choice; `--no-web` opts out). Router-written queries leave the server for public search engines, so they can carry fragments of a request. Results are injected as untrusted reference text. The gateway keeps the latest queries and their results in memory for 15 minutes (the search guard's cache), never on disk, and paces and pauses searches so the engines don't block the server.
* **Safety bars** (gateway, any client):
  * forces `n=1` and drops `best_of`, and clamps `max_tokens` to half the context;
  * answers `413` for bodies over 8 characters per context token, before SGLang sees them;
  * allows at most 4 requests in flight per device and 8 server-wide, queueing for up to 120 s before a `503` with `retry-after`;
  * limits each device to 120 requests a minute (`429`).

  The settings live in `gateway.py`, and the `MAX_ACTIVE_REQUESTS`, `MAX_DEVICE_REQUESTS`, `QUEUE_WAIT_S` and `RATE_PER_MINUTE` variables override them. Slots are released through an idempotent cleanup that also runs as the response's background task, so a client that disconnects before streaming starts can't leak one. SGLang itself rejects over-length input with a clean 400 and queues overload: 12 concurrent 20k-token requests all completed.
* **Split model:** needs a trusted LAN; the traffic between ranks is unauthenticated and pickle-based (see the `cluster` topic, Split serving).
* **No code stored or executed:** the server keeps no code and runs no commands for clients. Request bodies (prompts) are never logged; only per-device request and token counts are kept, plus the in-memory request trace (timings, token counts, hashes). The trace holds prompt text only while the owner has switched **Capture content** on, never on disk (see the `trace` topic). Model weights are mounted read-only, and the backend never downloads weights on its own.
