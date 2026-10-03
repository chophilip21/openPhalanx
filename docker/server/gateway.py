"""Openphalanx gateway: the only network-facing process in the backend.

The coding agent runs on the client; this server holds no code and executes
nothing on a client's behalf. It authenticates paired devices in front of the
SGLang inference server, which listens on the container's loopback only.

Two listeners share one process (and therefore pairing/metrics state):

* Public API (``AGENT_PORT``, TLS): ``/health`` and ``/v1/pair`` are open;
  everything else (``/v1/whoami`` and the OpenAI-compatible inference proxy,
  ``/v1/models`` and ``/v1/chat/completions``) needs a device token. Request
  bodies (prompts, i.e. client code) are never logged or stored.
* Admin API (``ADMIN_PORT``, plain HTTP): used by the Openphalanx GUI only. The
  container publishes it on the host's loopback interface, and every call must
  carry the per-launch ``ADMIN_TOKEN``.

Pairing model: the GUI asks for a short-lived, single-use pairing code. A
client trades that code for a long-lived random device token, which is stored
only as a SHA-256 hash and can be revoked from the GUI.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

SGLANG_ROOT = f"http://127.0.0.1:{os.environ.get('SGLANG_PORT', '8080')}"
SGLANG_BASE = f"{SGLANG_ROOT}/v1"
MODEL_NAME = os.environ.get("SERVED_MODEL_NAME", "openphalanx-coder")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "9090"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "9091"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
# Largest accepted request body. A 32k-token prompt is ~130 KB of text.
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", str(4 * 1024 * 1024)))
# Generation can pause between tokens for a long prefill, so reads get a long timeout.
UPSTREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=60.0, pool=10.0)

PAIRING_TTL_S = 600
PAIRING_MAX_ATTEMPTS = 5
# Unambiguous alphabet (no 0/O, 1/I/L, U): 30^8 codes, about 39 bits.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 8

TLS_DIR = STATE_DIR / "tls"
CERT_PATH = TLS_DIR / "cert.pem"
KEY_PATH = TLS_DIR / "key.pem"
DEVICES_PATH = STATE_DIR / "devices.json"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def _write_private_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DeviceStore:
    """Paired devices, persisted with hashed tokens only."""

    def __init__(self, path: Path):
        self.path = path
        self.devices: dict[str, dict] = {}
        if path.exists():
            self.devices = {d["id"]: d for d in json.loads(path.read_text())}

    def save(self) -> None:
        _write_private_json(self.path, list(self.devices.values()))

    def add(self, name: str) -> tuple[dict, str]:
        token = secrets.token_urlsafe(32)
        device = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "token_sha256": _hash_token(token),
            "created_at": time.time(),
            "last_seen": None,
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        self.devices[device["id"]] = device
        self.save()
        return device, token

    def authenticate(self, token: str) -> dict | None:
        digest = _hash_token(token)
        for device in self.devices.values():
            if hmac.compare_digest(device["token_sha256"], digest):
                return device
        return None

    def touch(self, device: dict) -> None:
        device["last_seen"] = time.time()
        device["requests"] += 1
        self.save()

    def add_usage(self, device: dict, prompt_tokens: int, completion_tokens: int) -> None:
        device["prompt_tokens"] = device.get("prompt_tokens", 0) + prompt_tokens
        device["completion_tokens"] = device.get("completion_tokens", 0) + completion_tokens
        self.save()

    def revoke(self, device_id: str) -> bool:
        if self.devices.pop(device_id, None) is None:
            return False
        self.save()
        return True

    def public(self) -> list[dict]:
        return [
            {k: v for k, v in d.items() if k != "token_sha256"}
            for d in self.devices.values()
        ]


class Pairing:
    """At most one active pairing code; single-use, short-lived."""

    def __init__(self):
        self.code: str | None = None
        self.expires_at = 0.0
        self.attempts_left = 0

    def new(self) -> dict:
        self.code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        self.expires_at = time.time() + PAIRING_TTL_S
        self.attempts_left = PAIRING_MAX_ATTEMPTS
        return self.describe()

    def clear(self) -> None:
        self.code = None

    def active(self) -> bool:
        if self.code and time.time() >= self.expires_at:
            self.code = None
        return self.code is not None

    def describe(self) -> dict:
        if not self.active():
            return {"active": False}
        return {
            "active": True,
            "code": f"{self.code[:4]}-{self.code[4:]}",
            "expires_at": self.expires_at,
            "attempts_left": self.attempts_left,
        }

    def redeem(self, candidate: str) -> bool:
        if not self.active():
            return False
        normalized = re.sub(r"[\s-]", "", candidate).upper()
        if hmac.compare_digest(normalized.encode(), self.code.encode()):
            self.code = None
            return True
        self.attempts_left -= 1
        if self.attempts_left <= 0:
            self.code = None
        return False


class Metrics:
    def __init__(self):
        self.started_at = time.time()
        # Authenticated client requests (counted by the inference proxy).
        self.requests_total = 0
        self.requests_failed = 0
        self.requests_active = 0
        self.last_request_at: float | None = None

    def snapshot(self) -> dict:
        return dict(vars(self))


devices = DeviceStore(DEVICES_PATH)
pairing = Pairing()
metrics = Metrics()


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------


def ensure_tls() -> None:
    if CERT_PATH.exists() and KEY_PATH.exists():
        return
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", "3650", "-subj", "/CN=openphalanx",
            "-keyout", str(KEY_PATH), "-out", str(CERT_PATH),
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(KEY_PATH, 0o600)


def tls_fingerprint() -> str:
    der = ssl.PEM_cert_to_DER_cert(CERT_PATH.read_text())
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


# --------------------------------------------------------------------------
# SGLang probes
# --------------------------------------------------------------------------


async def sglang_ready() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{SGLANG_BASE}/models")
            r.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


_METRIC_RE = re.compile(r"^(sglang:[a-z_]+)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$")


async def sglang_metrics() -> dict:
    """Sums each SGLang Prometheus series across labels."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{SGLANG_ROOT}/metrics")
            r.raise_for_status()
    except httpx.HTTPError:
        return {}
    sums: dict[str, float] = {}
    for line in r.text.splitlines():
        m = _METRIC_RE.match(line)
        if m:
            sums[m.group(1)] = sums.get(m.group(1), 0.0) + float(m.group(2))
    prompt = sums.get("sglang:prompt_tokens_total", 0.0)
    cached = sums.get("sglang:cached_tokens_total", 0.0)
    return {
        "prompt_tokens_total": prompt,
        "generation_tokens_total": sums.get("sglang:generation_tokens_total", 0.0),
        "cached_tokens_total": cached,
        "cache_hit_ratio": cached / prompt if prompt else None,
        "gen_throughput": sums.get("sglang:gen_throughput"),
        "running_requests": sums.get("sglang:num_running_reqs"),
        "queued_requests": sums.get("sglang:num_queue_reqs"),
        "token_usage": sums.get("sglang:token_usage"),
    }


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

app = FastAPI(title="openphalanx-gateway")


def require_device(authorization: str = Header(default="")) -> dict:
    scheme, _, token = authorization.partition(" ")
    device = devices.authenticate(token) if scheme.lower() == "bearer" else None
    if device is None:
        raise HTTPException(status_code=401, detail="missing or invalid device token")
    return device


class PairRequest(BaseModel):
    code: str
    device_name: str = Field(min_length=1, max_length=64)


class PairResponse(BaseModel):
    device_id: str
    token: str


@app.get("/health")
async def health() -> dict:
    return {"gateway": "ok", "sglang": "ready" if await sglang_ready() else "unavailable"}


@app.post("/v1/pair", response_model=PairResponse)
async def pair(req: PairRequest) -> PairResponse:
    if not pairing.redeem(req.code):
        # Slow down guessing; codes also die after PAIRING_MAX_ATTEMPTS misses.
        await asyncio.sleep(1)
        raise HTTPException(status_code=403, detail="invalid or expired pairing code")
    device, token = devices.add(req.device_name)
    return PairResponse(device_id=device["id"], token=token)


@app.get("/v1/whoami")
async def whoami(device: dict = Depends(require_device)) -> dict:
    """Lets a client confirm its token is still valid (e.g. `openbase status`)."""
    devices.touch(device)
    return {"device_id": device["id"], "device_name": device["name"], "model": MODEL_NAME}


# --------------------------------------------------------------------------
# Inference proxy (OpenAI-compatible)
# --------------------------------------------------------------------------

_upstream: httpx.AsyncClient | None = None


def upstream() -> httpx.AsyncClient:
    global _upstream
    if _upstream is None:
        _upstream = httpx.AsyncClient(base_url=SGLANG_ROOT, timeout=UPSTREAM_TIMEOUT)
    return _upstream


def openai_error(status: int, message: str, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": kind}})


class _TooLarge(Exception):
    pass


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        raise _TooLarge
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_REQUEST_BYTES:
            raise _TooLarge
    return bytes(body)


def _record_usage(device: dict, payload: bytes) -> None:
    """Adds token counts from a response or SSE chunk that carries `usage`."""
    try:
        usage = json.loads(payload).get("usage")
    except (ValueError, AttributeError):
        return
    if isinstance(usage, dict):
        devices.add_usage(
            device, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        )


@app.get("/v1/models")
async def list_models(device: dict = Depends(require_device)) -> Response:
    try:
        r = await upstream().get("/v1/models")
    except httpx.HTTPError:
        return openai_error(503, "The model is still loading; try again shortly.", "service_unavailable")
    return Response(r.content, status_code=r.status_code, media_type="application/json")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, device: dict = Depends(require_device)) -> Response:
    try:
        raw = await _read_body(request)
    except _TooLarge:
        return openai_error(413, f"Request body exceeds {MAX_REQUEST_BYTES} bytes.")
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return openai_error(400, "Request body must be a JSON object.")
    # One served model: whatever name the client sends, it gets this one.
    body["model"] = MODEL_NAME
    stream = bool(body.get("stream"))
    client_wants_usage = False
    if stream:
        # Always ask SGLang for the final usage chunk so tokens can be counted
        # per device; it is stripped again below if the client didn't ask.
        options = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
        client_wants_usage = bool(options.get("include_usage"))
        body["stream_options"] = {**options, "include_usage": True}

    devices.touch(device)
    metrics.requests_total += 1
    metrics.requests_active += 1
    metrics.last_request_at = time.time()
    try:
        resp = await upstream().send(
            upstream().build_request("POST", "/v1/chat/completions", json=body), stream=True
        )
    except httpx.HTTPError:
        metrics.requests_active -= 1
        metrics.requests_failed += 1
        return openai_error(503, "The model is still loading; try again shortly.", "service_unavailable")
    if resp.status_code >= 400:
        metrics.requests_failed += 1
    media_type = resp.headers.get("content-type", "application/json")

    if not stream:
        try:
            content = await resp.aread()
        finally:
            await resp.aclose()
            metrics.requests_active -= 1
        _record_usage(device, content)
        return Response(content, status_code=resp.status_code, media_type=media_type)

    async def relay():
        # Re-frame on SSE event boundaries so the usage event can be inspected
        # and, when the gateway injected it, removed before reaching the client.
        buf = b""
        try:
            async for chunk in resp.aiter_raw():
                buf += chunk
                *events, buf = buf.split(b"\n\n")
                out = []
                for event in events:
                    if event.startswith(b"data: {") and b'"usage"' in event:
                        try:
                            obj = json.loads(event[6:])
                        except ValueError:
                            obj = None
                        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                            usage = obj["usage"]
                            devices.add_usage(
                                device,
                                int(usage.get("prompt_tokens") or 0),
                                int(usage.get("completion_tokens") or 0),
                            )
                            if not client_wants_usage and obj.get("choices") == []:
                                continue
                    out.append(event + b"\n\n")
                if out:
                    yield b"".join(out)
            if buf:
                yield buf
        except httpx.HTTPError:
            metrics.requests_failed += 1
        finally:
            # Also runs when the client disconnects; closing the upstream
            # stream makes SGLang abort the generation.
            await resp.aclose()
            metrics.requests_active -= 1

    return StreamingResponse(
        relay(), status_code=resp.status_code, media_type=media_type, headers={"cache-control": "no-cache"}
    )


# --------------------------------------------------------------------------
# Admin API (GUI only)
# --------------------------------------------------------------------------

admin = FastAPI(title="openphalanx-admin")


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if not ADMIN_TOKEN or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")


@admin.get("/admin/status", dependencies=[Depends(require_admin)])
async def admin_status() -> dict:
    ready = await sglang_ready()
    return {
        "sglang": "ready" if ready else "unavailable",
        "model": MODEL_NAME,
        "gateway": metrics.snapshot(),
        "inference": await sglang_metrics() if ready else {},
        "pairing": pairing.describe(),
        "devices": len(devices.devices),
        "tls_fingerprint": tls_fingerprint(),
    }


@admin.post("/admin/pairing", dependencies=[Depends(require_admin)])
async def admin_new_pairing() -> dict:
    return pairing.new()


@admin.delete("/admin/pairing", dependencies=[Depends(require_admin)])
async def admin_clear_pairing() -> dict:
    pairing.clear()
    return pairing.describe()


@admin.get("/admin/devices", dependencies=[Depends(require_admin)])
async def admin_devices() -> list[dict]:
    return devices.public()


@admin.delete("/admin/devices/{device_id}", dependencies=[Depends(require_admin)])
async def admin_revoke(device_id: str) -> dict:
    if not devices.revoke(device_id):
        raise HTTPException(status_code=404, detail="unknown device")
    return {"revoked": device_id}


async def main() -> None:
    ensure_tls()
    public_server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=AGENT_PORT,
            ssl_certfile=str(CERT_PATH),
            ssl_keyfile=str(KEY_PATH),
        )
    )
    admin_server = uvicorn.Server(
        uvicorn.Config(admin, host="0.0.0.0", port=ADMIN_PORT, log_level="warning")
    )
    await asyncio.gather(public_server.serve(), admin_server.serve())


if __name__ == "__main__":
    asyncio.run(main())
