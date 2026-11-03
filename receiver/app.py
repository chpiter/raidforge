#!/usr/bin/env python3
"""Minimal evidence collector - fallback channel for erase sessions.

Deliverable D: a single ``POST /sessions`` endpoint that stores whatever the
erase box uploads.  Two body shapes are accepted:

1. ``multipart/form-data`` with a ``file`` field holding ``<session>.tar.gz``
   (this is what ``core/report.py`` sends);
2. raw JSON (the events stream) - useful for debugging with curl.

Everything is written under ``EVIDENCE_ROOT``::

    /srv/evidence/incoming/
        <session_id>.tar.gz              # whatever rsync/HTTP delivered
        <session_id>/
            manifest.json
            events.jsonl
            session.cast
            snapshots/*.svg
            controller_raw/*.txt

Security: this is intentionally dumb and must run behind a VPN/reverse proxy.
There is no authentication here - do NOT expose it to an untrusted network.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover - dependency is optional until runtime
    raise SystemExit(
        "FastAPI is not installed. Run:\n"
        "    pip install -r receiver/requirements.txt\n"
        "    uvicorn receiver.app:app --host 0.0.0.0 --port 8080"
    )

EVIDENCE_ROOT = Path(os.environ.get("EVIDENCE_ROOT", "/srv/evidence/incoming")).resolve()
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))

app = FastAPI(
    title="Erase Evidence Receiver",
    version="1.0.0",
    description="Accepts erase-session archives from the Debian live erase image.",
)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_session_id(value: str) -> str:
    """Reject path traversal and keep only boring characters."""

    candidate = os.path.basename((value or "").strip())
    cleaned = "".join(ch for ch in candidate if ch.isalnum() or ch in "._-")
    if not cleaned:
        cleaned = f"unnamed-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    return cleaned[:128]


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    """Liveness probe."""

    return {"status": "ok", "root": str(EVIDENCE_ROOT), "now": _utc()}


@app.get("/sessions")
async def list_sessions(limit: int = 50) -> Dict[str, Any]:
    """List stored sessions, newest first."""

    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    entries = sorted(
        (p for p in EVIDENCE_ROOT.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    return {
        "count": len(entries),
        "sessions": [
            {
                "session_id": p.name,
                "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "has_manifest": (p / "manifest.json").is_file(),
                "has_cast": (p / "session.cast").is_file(),
            }
            for p in entries
        ],
    }


@app.post("/sessions")
async def receive_session(
    file: Optional[UploadFile] = File(default=None),
    server_sn: str = Form(default=""),
    session_id: str = Form(default=""),
) -> JSONResponse:
    """Store one uploaded session archive and unpack it next to itself."""

    if file is None:
        raise HTTPException(status_code=400, detail="missing 'file' field")

    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    sid = _safe_session_id(session_id or file.filename or server_sn)
    target_dir = EVIDENCE_ROOT / sid
    tmp_file = EVIDENCE_ROOT / f".tmp-{sid}.tar.gz"

    total = 0
    try:
        with tmp_file.open("wb") as sink:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="archive too large")
                sink.write(chunk)
    except HTTPException:
        tmp_file.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tmp_file, "r:gz") as archive:
            # Defence in depth: never trust members that escape the directory.
            members = [
                member
                for member in archive.getmembers()
                if not Path(member.name).is_absolute() and ".." not in Path(member.name).parts
            ]
            archive.extractall(target_dir, members=members)
        shutil.move(str(tmp_file), str(target_dir.parent / f"{sid}.tar.gz"))
    except tarfile.TarError as exc:
        tmp_file.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"not a valid tar.gz: {exc}")

    events = (target_dir / "events.jsonl")
    event_count = 0
    last_event: Dict[str, Any] = {}
    if events.is_file():
        lines = [line for line in events.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        event_count = len(lines)
        try:
            last_event = json.loads(lines[-1])
        except json.JSONDecodeError:
            last_event = {}

    record = {
        "session_id": sid,
        "server_sn": server_sn,
        "received_at": _utc(),
        "bytes": total,
        "events": event_count,
        "last_event": last_event.get("event", ""),
        "path": str(target_dir),
    }
    (target_dir / "_receipt.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

    # Keep the operator facing log append-only.
    with (EVIDENCE_ROOT / "receipts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    return JSONResponse(status_code=201, content=record)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
