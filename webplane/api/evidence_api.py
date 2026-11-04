"""HTTP evidence receiver (``POST /sessions``).

The erase appliance can ship a complete session archive over HTTP when SSH is
not available.  This endpoint is intentionally unauthenticated: deploy it only
on an internal network or behind a VPN/reverse proxy.

Archive layout written by the appliance::

    <session_id>.tar.gz
        <session_id>/
            manifest.json
            events.jsonl
            session.cast
            ...

The single top-level directory is flattened so the stored layout is::

    WEBPLANE_SESSION_DIR/<session_id>/
        manifest.json
        events.jsonl
        _receipt.json
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("evidence", __name__)


def _settings():
    return current_app.config["SETTINGS"]


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_session_id(value: str) -> str:
    candidate = os.path.basename(str(value or "").strip())
    cleaned = "".join(ch for ch in candidate if ch.isalnum() or ch in "._-")
    if not cleaned:
        cleaned = f"unnamed-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    return cleaned[:128]


def _safe_members(archive: tarfile.TarFile) -> Iterable[tarfile.TarInfo]:
    for member in archive.getmembers():
        name = member.name.replace("\\", "/")
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe archive member: {member.name!r}")
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"link/device member rejected: {member.name!r}")
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"unsupported archive member: {member.name!r}")
        yield member


def _flatten(staging: Path) -> Path:
    """Use the single top-level directory as the session root when present."""

    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging


def _receipt(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _record(
    session_id: str, server_sn: str, size: int, sha256: str, target: Path
) -> Dict[str, Any]:
    events = target / "events.jsonl"
    event_count = 0
    last_event = ""
    if events.is_file():
        try:
            lines = [
                line
                for line in events.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            event_count = len(lines)
            if lines:
                parsed = json.loads(lines[-1])
                if isinstance(parsed, dict):
                    last_event = str(parsed.get("event") or "")
        except (OSError, ValueError):
            pass
    return {
        "session_id": session_id,
        "server_sn": server_sn,
        "received_at": _utc(),
        "bytes": size,
        "sha256": sha256,
        "events": event_count,
        "last_event": last_event,
        "path": str(target),
    }


@bp.post("/sessions")
def receive_session():
    settings = _settings()
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error_code": "missing_file", "message": "missing 'file' field"}), 400

    server_sn = str(request.form.get("server_sn") or "")[:128]
    session_id = _safe_session_id(
        str(request.form.get("session_id") or upload.filename or server_sn)
    )
    root = Path(settings.session_dir)
    root.mkdir(parents=True, exist_ok=True)
    temp = root / f".upload-{session_id}-{uuid.uuid4().hex}.tar.gz"
    digest = hashlib.sha256()
    total = 0
    try:
        with temp.open("wb") as sink:
            while True:
                chunk = upload.stream.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > settings.max_session_bytes:
                    return (
                        jsonify(
                            {
                                "error_code": "session_too_large",
                                "message": "session archive exceeds the configured limit",
                            }
                        ),
                        413,
                    )
                digest.update(chunk)
                sink.write(chunk)
    finally:
        try:
            upload.close()
        except Exception:
            pass
    sha256 = digest.hexdigest()

    target = root / session_id
    existing = _receipt(target / "_receipt.json")
    if existing.get("sha256") == sha256:
        temp.unlink(missing_ok=True)
        payload = dict(existing)
        payload["duplicate"] = True
        return jsonify(payload), 200

    staging = Path(tempfile.mkdtemp(prefix=f".staging-{session_id}-", dir=str(root)))
    try:
        with tarfile.open(temp, "r:gz") as archive:
            members = list(_safe_members(archive))
            try:
                archive.extractall(staging, members=members, filter="data")
            except TypeError:  # Python < 3.11.4 has no extraction filters
                archive.extractall(staging, members=members)
        session_root = _flatten(staging)
        if target.exists():
            shutil.rmtree(target)
        os.replace(session_root, target)
    except (tarfile.TarError, ValueError, OSError) as exc:
        temp.unlink(missing_ok=True)
        return (
            jsonify({"error_code": "invalid_archive", "message": str(exc)[:500]}),
            400,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    try:
        os.replace(temp, root / f"{session_id}.tar.gz")
    except OSError:
        temp.unlink(missing_ok=True)

    record = _record(session_id, server_sn, total, sha256, target)
    (target / "_receipt.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    with (root / "receipts.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return jsonify(record), 201


@bp.get("/sessions")
def list_sessions():
    settings = _settings()
    root = Path(settings.session_dir)
    root.mkdir(parents=True, exist_ok=True)
    try:
        limit = max(1, min(500, int(request.args.get("limit", "50"))))
    except ValueError:
        limit = 50
    entries: List[Dict[str, Any]] = []
    directories = sorted(
        (item for item in root.iterdir() if item.is_dir() and not item.name.startswith(".")),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )[:limit]
    for directory in directories:
        record = _receipt(directory / "_receipt.json")
        record.setdefault("session_id", directory.name)
        record.setdefault("path", str(directory))
        entries.append(record)
    return jsonify({"count": len(entries), "sessions": entries})


__all__ = ["bp"]
