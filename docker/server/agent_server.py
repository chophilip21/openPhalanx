"""Headless agent server: exposes Aider over HTTPS on 0.0.0.0:9090.

Aider has no built-in server mode, so each task runs the Aider CLI against a
throwaway git repo populated with the files sent by the client. The resulting
unified diff is returned for the client to apply to its local workspace.

Two listeners share one process (and therefore pairing/metrics state):

* Public API (``AGENT_PORT``, TLS): ``/health``, ``/v1/pair``, ``/v1/run``.
  Everything except ``/health`` and ``/v1/pair`` needs a device token.
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
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

SGLANG_ROOT = f"http://127.0.0.1:{os.environ.get('SGLANG_PORT', '8080')}"
SGLANG_BASE = f"{SGLANG_ROOT}/v1"
MODEL_NAME = os.environ.get("SERVED_MODEL_NAME", "openphalanx-coder")
EDIT_FORMAT = os.environ.get("AIDER_EDIT_FORMAT", "diff")
TASK_TIMEOUT_S = int(os.environ.get("AIDER_TIMEOUT_S", "900"))
AIDER_BIN = os.environ.get("AIDER_BIN", "/opt/aider/bin/aider")
AGENT_PORT = int(os.environ.get("AGENT_PORT", "9090"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "9091"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))

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
        self.tasks_total = 0
        self.tasks_failed = 0
        self.tasks_active = 0
        self.tasks_queued = 0
        self.last_task_at: float | None = None

    def snapshot(self) -> dict:
        return dict(vars(self))


devices = DeviceStore(DEVICES_PATH)
pairing = Pairing()
metrics = Metrics()
# One GPU, one model: serialize tasks rather than oversubscribe SGLang.
_task_lock = asyncio.Lock()


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
# Aider task execution
# --------------------------------------------------------------------------


class RunRequest(BaseModel):
    prompt: str
    # Relative path -> file contents. Files Aider may read and edit.
    files: dict[str, str] = {}


class RunResponse(BaseModel):
    exit_code: int
    output: str
    diff: str


def _safe_relpath(path: str) -> PurePosixPath:
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise HTTPException(status_code=400, detail=f"invalid file path: {path!r}")
    return p


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _run_task(req: RunRequest) -> RunResponse:
    with tempfile.TemporaryDirectory(prefix="openphalanx-") as tmp:
        work = Path(tmp)
        _git(work, "init", "-q")
        # Keep Aider's own history/cache files out of the returned diff.
        (work / ".git" / "info" / "exclude").write_text(".aider*\n")

        rel_paths = [_safe_relpath(p) for p in req.files]
        for rel, content in zip(rel_paths, req.files.values()):
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "--allow-empty", "-m", "client snapshot")

        cmd = [
            AIDER_BIN,
            "--model", f"openai/{MODEL_NAME}",
            "--edit-format", EDIT_FORMAT,
            "--yes-always",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--no-pretty",
            "--no-stream",
            "--no-check-update",
            "--no-show-release-notes",
            "--no-show-model-warnings",
            "--no-analytics",
            "--no-gitignore",
            "--message", req.prompt,
            *[str(p) for p in rel_paths],
        ]
        env = {
            **os.environ,
            "OPENAI_API_BASE": SGLANG_BASE,
            "OPENAI_API_KEY": "sk-local",
        }
        proc = subprocess.run(
            cmd,
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TASK_TIMEOUT_S,
        )

        # Stage everything so new/deleted files diff as proper creations/deletions.
        _git(work, "add", "-A")
        diff = _git(work, "diff", "--cached", "--binary")
        return RunResponse(
            exit_code=proc.returncode,
            output=proc.stdout + proc.stderr,
            diff=diff,
        )


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

app = FastAPI(title="openphalanx-agent")


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
    return {"agent": "ok", "sglang": "ready" if await sglang_ready() else "unavailable"}


@app.post("/v1/pair", response_model=PairResponse)
async def pair(req: PairRequest) -> PairResponse:
    if not pairing.redeem(req.code):
        # Slow down guessing; codes also die after PAIRING_MAX_ATTEMPTS misses.
        await asyncio.sleep(1)
        raise HTTPException(status_code=403, detail="invalid or expired pairing code")
    device, token = devices.add(req.device_name)
    return PairResponse(device_id=device["id"], token=token)


@app.post("/v1/run", response_model=RunResponse)
async def run(req: RunRequest, device: dict = Depends(require_device)) -> RunResponse:
    devices.touch(device)
    metrics.tasks_queued += 1
    async with _task_lock:
        metrics.tasks_queued -= 1
        metrics.tasks_active += 1
        metrics.tasks_total += 1
        metrics.last_task_at = time.time()
        try:
            result = await asyncio.to_thread(_run_task, req)
            if result.exit_code != 0:
                metrics.tasks_failed += 1
            return result
        except subprocess.TimeoutExpired:
            metrics.tasks_failed += 1
            raise HTTPException(status_code=504, detail="aider task timed out")
        except Exception:
            metrics.tasks_failed += 1
            raise
        finally:
            metrics.tasks_active -= 1


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
        "agent": metrics.snapshot(),
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
