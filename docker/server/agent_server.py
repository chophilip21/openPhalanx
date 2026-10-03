"""Headless agent server: exposes Aider over HTTP on 0.0.0.0:9090.

Aider has no built-in server mode, so each task runs the Aider CLI against a
throwaway git repo populated with the files sent by the client. The resulting
unified diff is returned for the client to apply to its local workspace.
"""

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

SGLANG_BASE = f"http://127.0.0.1:{os.environ.get('SGLANG_PORT', '8080')}/v1"
MODEL_NAME = os.environ.get("SERVED_MODEL_NAME", "openphalanx-coder")
EDIT_FORMAT = os.environ.get("AIDER_EDIT_FORMAT", "diff")
TASK_TIMEOUT_S = int(os.environ.get("AIDER_TIMEOUT_S", "900"))

app = FastAPI(title="openphalanx-agent")

# One GPU, one model: serialize tasks rather than oversubscribe SGLang.
_task_lock = asyncio.Lock()


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
            "aider",
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


@app.get("/health")
async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{SGLANG_BASE}/models")
            r.raise_for_status()
        sglang = "ready"
    except httpx.HTTPError:
        sglang = "unavailable"
    return {"agent": "ok", "sglang": sglang, "model": MODEL_NAME}


@app.post("/v1/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    async with _task_lock:
        try:
            return await asyncio.to_thread(_run_task, req)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="aider task timed out")
