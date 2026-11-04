"""Per-file evidence storage with hash verification and limits."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..auth import ApiError
from ..jsonutil import loads
from ..timeutil import iso, utcnow
from .events import record_event

_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_filename(raw: str) -> str:
    name = str(raw or "").strip()
    if "/" in name or "\\" in name:
        raise ApiError("证据文件名不能包含路径", code="invalid_filename", status=400)
    if name in {"", ".", ".."} or not _FILENAME_RE.match(name):
        raise ApiError("证据文件名非法", code="invalid_filename", status=400)
    return name


def _form_int(form: Any, name: str) -> Optional[int]:
    raw = form.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def task_evidence_bytes(conn, task_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS total FROM evidence WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row["total"] if row else 0)


def list_evidence(conn, task_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, task_id, filename, rel_path, sha256, size, kind, stage, content_type, "
        "collected_at, manifest_index, received_at, uploader_agent_id "
        "FROM evidence WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_evidence(conn, evidence_id: int):
    return conn.execute(
        "SELECT id, task_id, filename, rel_path, sha256, size, kind, stage, content_type, "
        "collected_at, manifest_index, received_at, uploader_agent_id "
        "FROM evidence WHERE id = ?",
        (int(evidence_id),),
    ).fetchone()


def save_evidence(
    conn,
    settings,
    task_row,
    file_storage,
    form: Any,
    agent_id: str,
    now=None,
) -> Dict[str, Any]:
    task_id = task_row["task_id"]
    if task_row["lease_id"]:
        supplied_lease = str(form.get("lease_id") or "").strip()
        if supplied_lease and supplied_lease != task_row["lease_id"]:
            raise ApiError("租约不匹配", code="lease_mismatch", status=409)

    filename = safe_filename(file_storage.filename or "")
    task_dir = Path(settings.evidence_dir) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    target = task_dir / filename

    existing = conn.execute(
        "SELECT id, sha256, size FROM evidence WHERE task_id = ? AND filename = ?",
        (task_id, filename),
    ).fetchone()

    temp = task_dir / f".part-{uuid.uuid4().hex}"
    digest = hashlib.sha256()
    size = 0
    try:
        with temp.open("wb") as sink:
            while True:
                chunk = file_storage.stream.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_evidence_bytes:
                    raise ApiError("证据文件超过单文件上限", code="evidence_too_large", status=413)
                digest.update(chunk)
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())

        sha256 = digest.hexdigest()
        declared_sha = str(form.get("sha256") or "").strip().lower()
        if declared_sha and declared_sha != sha256:
            raise ApiError("sha256 不匹配", code="sha256_mismatch", status=400)
        declared_size = _form_int(form, "size")
        if declared_size is not None and declared_size != size:
            raise ApiError("size 不匹配", code="size_mismatch", status=400)

        if existing is not None:
            if existing["sha256"] == sha256 and int(existing["size"]) == size:
                return {
                    "ok": True,
                    "duplicate": True,
                    "evidence_id": existing["id"],
                    "filename": filename,
                    "size": size,
                    "sha256": sha256,
                }
            raise ApiError(
                "同名证据文件已存在且内容不同", code="evidence_conflict", status=400
            )

        total = task_evidence_bytes(conn, task_id)
        if total + size > settings.max_task_evidence_bytes:
            raise ApiError(
                "任务证据总量超过上限", code="evidence_too_large", status=413
            )

        os.replace(temp, target)
        _fsync_dir(task_dir)

        now_iso = iso(now or utcnow())
        cursor = conn.execute(
            "INSERT INTO evidence (task_id, filename, rel_path, sha256, size, kind, stage, "
            "content_type, collected_at, manifest_index, received_at, uploader_agent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                filename,
                str(Path(task_id) / filename),
                sha256,
                size,
                str(form.get("kind") or "other")[:32],
                str(form.get("stage") or "execute")[:32],
                str(form.get("content_type") or file_storage.content_type or "application/octet-stream")[:128],
                str(form.get("collected_at") or "")[:64],
                _form_int(form, "manifest_index") or 0,
                now_iso,
                agent_id,
            ),
        )
        record_event(
            conn,
            task_id,
            "evidence",
            message=f"received {filename}",
            data={"filename": filename, "size": size, "sha256": sha256},
            actor="agent" if agent_id else "server",
            now=now,
        )
        _refresh_evidence_status(conn, task_row)
        return {
            "ok": True,
            "duplicate": False,
            "evidence_id": cursor.lastrowid,
            "filename": filename,
            "size": size,
            "sha256": sha256,
        }
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _refresh_evidence_status(conn, task_row) -> None:
    result = loads(task_row["result_json"], {})
    evidence = result.get("evidence") if isinstance(result, dict) else None
    manifest = evidence.get("manifest") if isinstance(evidence, dict) else None
    if not isinstance(manifest, list) or not manifest:
        return
    uploaded = conn.execute(
        "SELECT COUNT(*) AS n FROM evidence WHERE task_id = ?", (task_row["task_id"],)
    ).fetchone()["n"]
    status = "complete" if int(uploaded) >= len(manifest) else "partial"
    conn.execute(
        "UPDATE tasks SET evidence_status = ? WHERE task_id = ?",
        (status, task_row["task_id"]),
    )


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


__all__ = [
    "get_evidence",
    "list_evidence",
    "safe_filename",
    "save_evidence",
    "task_evidence_bytes",
]
