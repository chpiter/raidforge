"""Task queue, lease handling and terminal results."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..auth import ApiError
from ..db import transaction
from ..ids import new_lease_id, new_task_id
from ..jsonutil import dumps, loads
from ..timeutil import iso, parse_iso, utcnow
from . import topology as topology_service
from .events import record_event, list_events

log = logging.getLogger(__name__)

TERMINAL = {"finished", "failed", "cancelled", "expired"}
PHASES = {
    "received",
    "validated",
    "queued",
    "confirm_pending",
    "precheck",
    "running",
    "verifying",
    "reporting",
    "done",
    "failed",
    "cancelling",
    "released",
}
REPORTED_TERMINAL = {"finished", "failed"}


def get_task(conn, task_id: str):
    return conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()


def task_envelope(row) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": row["task_id"],
        "type": row["type"],
        "server_sn": row["server_sn"],
        "params": loads(row["params_json"], {}),
        "require_local_confirm": bool(row["require_local_confirm"]),
        "timeout_seconds": row["timeout_seconds"],
        "lease_id": row["lease_id"] or "",
        "lease_expires_at": row["lease_expires_at"] or "",
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "priority": row["priority"],
        "status": row["status"],
        "cancel_requested": bool(row["cancel_requested"]),
    }


def task_payload(row) -> Dict[str, Any]:
    if row is None:
        return {}
    keys = row.keys()
    payload = {
        "task_id": row["task_id"],
        "type": row["type"],
        "agent_id": row["agent_id"],
        "server_sn": row["server_sn"],
        "params": loads(row["params_json"], {}),
        "status": row["status"],
        "phase": row["phase"],
        "lease_id": row["lease_id"],
        "lease_expires_at": row["lease_expires_at"],
        "cancel_requested": bool(row["cancel_requested"]),
        "require_local_confirm": bool(row["require_local_confirm"]),
        "timeout_seconds": row["timeout_seconds"],
        "priority": row["priority"],
        "attempts": row["attempts"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "expires_at": row["expires_at"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "result": loads(row["result_json"], {}),
        "progress": loads(row["progress_json"], {}),
        "evidence_status": row["evidence_status"],
        "updated_at": row["updated_at"],
    }
    if "evidence_count" in keys:
        payload["evidence_count"] = row["evidence_count"]
    return payload


def _lease_expired(row, now: datetime) -> bool:
    expires = parse_iso(row["lease_expires_at"] or "")
    return expires is not None and now >= expires


def _require_lease(row, lease_id: str) -> None:
    if not lease_id:
        raise ApiError("缺少 lease_id", code="lease_mismatch", status=409)
    if (row["lease_id"] or "") != lease_id:
        raise ApiError("租约不匹配", code="lease_mismatch", status=409)


def create_task(
    conn,
    settings,
    agent_row,
    task_type: str,
    params: Dict[str, Any],
    *,
    require_local_confirm: bool = True,
    timeout_seconds: int = 600,
    priority: int = 100,
    expires_in_seconds: int = 3600,
    created_by: str = "webplane-ui",
    now: Optional[datetime] = None,
) -> Any:
    if task_type == "erase_disks":
        raise ApiError(
            "远程擦除已禁用，请在本地 TUI 执行",
            code="remote_erase_disabled",
            status=403,
        )
    now = now or utcnow()
    task_id = new_task_id()
    created_at = iso(now)
    expires_at = iso(now + timedelta(seconds=int(expires_in_seconds)))
    timeout_seconds = min(7200, max(30, int(timeout_seconds)))
    priority = min(1000, max(0, int(priority)))
    with transaction(conn):
        conn.execute(
            "INSERT INTO tasks (task_id, type, agent_id, server_sn, params_json, status, phase, "
            "require_local_confirm, timeout_seconds, priority, created_by, created_at, "
            "expires_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 'queued', ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                task_type,
                agent_row["agent_id"],
                agent_row["server_sn"],
                dumps(params),
                1 if require_local_confirm else 0,
                timeout_seconds,
                priority,
                created_by,
                created_at,
                expires_at,
                created_at,
            ),
        )
        record_event(
            conn,
            task_id,
            "created",
            phase="queued",
            message=f"{task_type} queued",
            data={"created_by": created_by, "params": params},
            actor="ui",
            now=now,
        )
    return get_task(conn, task_id)


def _recycle_expired_leases(conn, settings, agent_id: str, now: datetime) -> None:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE agent_id = ? AND status = 'leased' "
        "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
        (agent_id, iso(now)),
    ).fetchall()
    for row in rows:
        if int(row["attempts"] or 0) >= settings.max_task_attempts:
            conn.execute(
                "UPDATE tasks SET status='failed', phase='failed', error_code='lease_abandoned', "
                "error_message='租约多次过期，任务放弃', finished_at=?, updated_at=? "
                "WHERE task_id = ?",
                (iso(now), iso(now), row["task_id"]),
            )
            record_event(
                conn,
                row["task_id"],
                "expired",
                phase="failed",
                message="lease abandoned after max attempts",
                now=now,
            )
        else:
            conn.execute(
                "UPDATE tasks SET status='pending', phase='queued', lease_id=NULL, "
                "lease_expires_at=NULL, updated_at=? WHERE task_id = ?",
                (iso(now), row["task_id"]),
            )
            record_event(
                conn,
                row["task_id"],
                "released",
                phase="queued",
                message="lease expired, task requeued",
                now=now,
            )


def _expire_pending(conn, agent_id: str, now: datetime) -> None:
    rows = conn.execute(
        "SELECT task_id FROM tasks WHERE agent_id = ? AND status = 'pending' AND expires_at <= ?",
        (agent_id, iso(now)),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE tasks SET status='expired', phase='failed', error_code='task_expired', "
            "error_message='任务已过期', finished_at=?, updated_at=? WHERE task_id = ?",
            (iso(now), iso(now), row["task_id"]),
        )
        record_event(conn, row["task_id"], "expired", phase="failed", message="task expired", now=now)


def claim_next_task(conn, settings, agent_id: str, server_sn: str, now: Optional[datetime] = None):
    now = now or utcnow()
    with transaction(conn):
        _recycle_expired_leases(conn, settings, agent_id, now)
        _expire_pending(conn, agent_id, now)
        for _attempt in range(2):
            row = conn.execute(
                "SELECT * FROM tasks WHERE agent_id = ? AND status = 'pending' "
                "AND type != 'erase_disks' AND expires_at > ? "
                "ORDER BY priority ASC, created_at ASC LIMIT 1",
                (agent_id, iso(now)),
            ).fetchone()
            if row is None:
                return None
            lease_id = new_lease_id()
            lease_expires = iso(now + timedelta(seconds=settings.lease_seconds))
            cursor = conn.execute(
                "UPDATE tasks SET status='leased', phase='queued', lease_id=?, "
                "lease_expires_at=?, attempts=attempts+1, updated_at=? "
                "WHERE task_id = ? AND status = 'pending'",
                (lease_id, lease_expires, iso(now), row["task_id"]),
            )
            if cursor.rowcount == 1:
                record_event(
                    conn,
                    row["task_id"],
                    "leased",
                    phase="queued",
                    message=f"leased to {agent_id}",
                    data={"lease_id": lease_id, "server_sn": server_sn},
                    actor="agent",
                    now=now,
                )
                return get_task(conn, row["task_id"])
    return None


def start_task(conn, task_id: str, lease_id: str, agent_id: str, now: Optional[datetime] = None):
    now = now or utcnow()
    row = get_task(conn, task_id)
    if row is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    if row["type"] == "erase_disks":
        raise ApiError(
            "远程擦除已禁用，请在本地 TUI 执行",
            code="remote_erase_disabled",
            status=403,
        )
    if row["status"] in TERMINAL:
        raise ApiError("任务已终态", code="task_terminal", status=409)
    _require_lease(row, lease_id)
    if _lease_expired(row, now):
        raise ApiError("租约已过期", code="lease_expired", status=409)
    if row["status"] == "running":
        return row
    if row["status"] != "leased":
        raise ApiError("任务未处于已派发状态", code="task_terminal", status=409)
    with transaction(conn):
        conn.execute(
            "UPDATE tasks SET status='running', phase='running', started_at=?, updated_at=? "
            "WHERE task_id = ? AND lease_id = ?",
            (iso(now), iso(now), task_id, lease_id),
        )
        record_event(
            conn,
            task_id,
            "started",
            phase="running",
            message=f"started by {agent_id}",
            actor="agent",
            now=now,
        )
    return get_task(conn, task_id)


def apply_progress(
    conn,
    task_id: str,
    lease_id: str,
    payload: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    now = now or utcnow()
    row = get_task(conn, task_id)
    if row is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    if row["status"] in TERMINAL:
        raise ApiError("任务已终态", code="task_terminal", status=409)
    _require_lease(row, lease_id)
    phase = str(payload.get("phase") or "")
    if phase not in PHASES:
        phase = row["phase"]
    progress = {
        "done": payload.get("done"),
        "total": payload.get("total"),
        "message": payload.get("message") or "",
        "phase": phase,
        "at": iso(now),
    }
    if payload.get("data") is not None:
        progress["data"] = payload.get("data")
    with transaction(conn):
        conn.execute(
            "UPDATE tasks SET phase=?, progress_json=?, updated_at=? WHERE task_id = ?",
            (phase, dumps(progress), iso(now), task_id),
        )
        record_event(
            conn,
            task_id,
            "progress",
            phase=phase,
            message=str(payload.get("message") or "")[:1000],
            data={"done": payload.get("done"), "total": payload.get("total")},
            actor="agent",
            now=now,
        )
    refreshed = get_task(conn, task_id)
    return bool(refreshed and refreshed["cancel_requested"])


def finish_task(
    conn,
    task_id: str,
    payload: Dict[str, Any],
    idempotency_key: str = "",
    now: Optional[datetime] = None,
) -> Tuple[Any, bool]:
    now = now or utcnow()
    row = get_task(conn, task_id)
    if row is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    supplied = str(payload.get("lease_id") or "").strip()
    if not supplied and idempotency_key:
        supplied = idempotency_key.split(":", 1)[-1]
    if row["status"] in TERMINAL:
        if supplied and supplied == (row["lease_id"] or ""):
            _ingest_topology(conn, row, payload, now)
            return row, True
        raise ApiError("任务已终态", code="task_terminal", status=409)
    _require_lease(row, supplied)

    reported = str(payload.get("status") or "").strip()
    if reported not in REPORTED_TERMINAL:
        raise ApiError("result.status 必须是 finished 或 failed", code="invalid_result", status=400)
    status = reported
    phase = "done" if status == "finished" else "failed"
    error_code = payload.get("error_code")
    error_message = payload.get("error_message")
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    evidence_status = str(evidence.get("status") or row["evidence_status"] or "none")
    manifest = evidence.get("manifest")
    if isinstance(manifest, list):
        uploaded = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE task_id = ?", (task_id,)
        ).fetchone()["n"]
        if manifest and int(uploaded) >= len(manifest):
            evidence_status = "complete"
        elif int(uploaded) > 0:
            evidence_status = "partial"
    finished_at = str(payload.get("finished_at") or "") or iso(now)
    with transaction(conn):
        conn.execute(
            "UPDATE tasks SET status=?, phase=?, finished_at=?, result_json=?, error_code=?, "
            "error_message=?, evidence_status=?, last_idempotency_key=?, updated_at=? "
            "WHERE task_id = ?",
            (
                status,
                phase,
                finished_at,
                dumps(payload),
                error_code,
                error_message,
                evidence_status,
                idempotency_key or f"{task_id}:{supplied}",
                iso(now),
                task_id,
            ),
        )
        record_event(
            conn,
            task_id,
            "result",
            phase=phase,
            message=f"{status}" + (f": {error_code}" if error_code else ""),
            data={
                "status": status,
                "error_code": error_code,
                "evidence_status": evidence_status,
                "terminal_state_unknown": bool(payload.get("terminal_state_unknown")),
            },
            actor="agent",
            now=now,
        )
    if status == "finished":
        _ingest_topology(conn, row, payload, now)
    return get_task(conn, task_id), False


def _ingest_topology(conn, row, payload: Dict[str, Any], now: datetime) -> None:
    if str(payload.get("status") or "") != "finished":
        return
    try:
        params = loads(row["params_json"], {})
        controller_ids = params.get("controller_ids") if isinstance(params, dict) else None
        topology_service.ingest_result(
            conn,
            row["agent_id"],
            row["type"],
            payload,
            now=now,
            controller_ids=controller_ids if isinstance(controller_ids, list) else None,
        )
    except Exception:  # pragma: no cover - cache must never break the task
        log.warning("topology ingest failed for %s", row["task_id"], exc_info=True)


def release_task(
    conn,
    task_id: str,
    lease_id: str,
    reason: str = "",
    now: Optional[datetime] = None,
):
    now = now or utcnow()
    row = get_task(conn, task_id)
    if row is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    if row["status"] in TERMINAL:
        if lease_id and lease_id == (row["lease_id"] or ""):
            return row
        raise ApiError("任务已终态", code="task_terminal", status=409)
    if row["status"] == "pending":
        return row
    if row["status"] != "leased":
        raise ApiError("任务已开始执行", code="already_started", status=409)
    _require_lease(row, lease_id)
    with transaction(conn):
        conn.execute(
            "UPDATE tasks SET status='pending', phase='queued', lease_id=NULL, "
            "lease_expires_at=NULL, updated_at=? WHERE task_id = ?",
            (iso(now), task_id),
        )
        record_event(
            conn,
            task_id,
            "released",
            phase="queued",
            message=reason or "released by agent",
            actor="agent",
            now=now,
        )
    return get_task(conn, task_id)


def cancel_task(conn, task_id: str, reason: str = "", now: Optional[datetime] = None):
    now = now or utcnow()
    row = get_task(conn, task_id)
    if row is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    if row["status"] in TERMINAL:
        raise ApiError("任务已终态", code="task_terminal", status=409)
    if row["status"] in ("pending", "leased"):
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET status='cancelled', phase='failed', cancel_requested=1, "
                "error_code='cancelled', error_message=?, finished_at=?, updated_at=? "
                "WHERE task_id = ?",
                (reason or "operator cancelled", iso(now), iso(now), task_id),
            )
            record_event(
                conn,
                task_id,
                "cancel_requested",
                phase="failed",
                message=reason or "operator cancelled",
                actor="ui",
                now=now,
            )
    else:
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET cancel_requested=1, phase='cancelling', updated_at=? "
                "WHERE task_id = ?",
                (iso(now), task_id),
            )
            record_event(
                conn,
                task_id,
                "cancel_requested",
                phase="cancelling",
                message=reason or "operator requested cancel",
                actor="ui",
                now=now,
            )
    return get_task(conn, task_id)


def cancel_remote_erase_tasks(
    conn,
    reason: str = "远程擦除已禁用，请在本地 TUI 执行",
    now: Optional[datetime] = None,
) -> int:
    """Cancel erase tasks left over from before remote erase was removed."""

    now = now or utcnow()
    rows = conn.execute(
        "SELECT task_id FROM tasks WHERE type = 'erase_disks' "
        "AND status IN ('pending', 'leased', 'running')"
    ).fetchall()
    cancelled = 0
    for row in rows:
        try:
            cancel_task(conn, row["task_id"], reason=reason, now=now)
        except ApiError as exc:
            log.warning("cannot cancel legacy erase task %s: %s", row["task_id"], exc)
            continue
        cancelled += 1
    return cancelled


def list_tasks(
    conn,
    *,
    agent_id: str = "",
    task_type: str = "",
    status: str = "",
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    page_size: int = 20,
) -> Tuple[List[Dict[str, Any]], int]:
    where = []
    params: List[Any] = []
    if agent_id:
        where.append("t.agent_id = ?")
        params.append(agent_id)
    if task_type:
        where.append("t.type = ?")
        params.append(task_type)
    if status:
        where.append("t.status = ?")
        params.append(status)
    if query:
        where.append("(t.task_id LIKE ? OR t.server_sn LIKE ? OR t.type LIKE ? OR t.agent_id LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like, like])
    if date_from:
        where.append("t.created_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("t.created_at <= ?")
        params.append(date_to)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM tasks t {clause}", tuple(params)
    ).fetchone()
    total = int(total_row["n"] if total_row else 0)
    page = max(1, int(page))
    page_size = min(200, max(1, int(page_size)))
    rows = conn.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM evidence e WHERE e.task_id = t.task_id) "
        f"AS evidence_count FROM tasks t {clause} ORDER BY t.created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_size, (page - 1) * page_size),
    ).fetchall()
    return [task_payload(row) for row in rows], total


def task_detail(conn, task_id: str) -> Optional[Dict[str, Any]]:
    row = get_task(conn, task_id)
    if row is None:
        return None
    payload = task_payload(row)
    payload["events"] = list_events(conn, task_id)
    evidence_rows = conn.execute(
        "SELECT id, filename, size, sha256, kind, stage, content_type, collected_at, "
        "manifest_index, received_at FROM evidence WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    payload["evidence"] = [dict(item) for item in evidence_rows]
    return payload


def stats(conn, offline_seconds: int = 45, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or utcnow()
    from .agents import (  # local import avoids a cycle at module load
        agent_status,
        deleted_server_sns,
    )

    deleted_sns = deleted_server_sns(conn)
    agents_total = 0
    online = 0
    offline = 0
    deleted = 0
    for row in conn.execute(
        "SELECT server_sn, last_heartbeat_at FROM agents"
    ).fetchall():
        if row["server_sn"] in deleted_sns:
            deleted += 1
            continue
        agents_total += 1
        state = agent_status(row["last_heartbeat_at"] or "", now, offline_seconds)
        if state == "online":
            online += 1
        elif state == "offline":
            offline += 1

    task_counts = {key: 0 for key in TERMINAL | {"pending", "leased", "running"}}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall():
        task_counts[row["status"]] = row["n"]
    cutoff = iso(now - timedelta(hours=24))
    failed_24h = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE status='failed' AND finished_at >= ?",
        (cutoff,),
    ).fetchone()["n"]
    evidence_row = conn.execute(
        "SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes FROM evidence"
    ).fetchone()
    return {
        "agents": {
            "total": agents_total,
            "online": online,
            "offline": offline,
            "deleted": deleted,
        },
        "tasks": {
            "pending": task_counts.get("pending", 0),
            "leased": task_counts.get("leased", 0),
            "running": task_counts.get("running", 0),
            "finished": task_counts.get("finished", 0),
            "failed": task_counts.get("failed", 0),
            "cancelled": task_counts.get("cancelled", 0),
            "expired": task_counts.get("expired", 0),
            "failed_24h": int(failed_24h or 0),
        },
        "evidence": {
            "files": int(evidence_row["files"] if evidence_row else 0),
            "bytes": int(evidence_row["bytes"] if evidence_row else 0),
        },
        "server_time": iso(now),
    }


def expire_stale(conn, settings, now: Optional[datetime] = None) -> int:
    now = now or utcnow()
    changed = 0
    with transaction(conn):
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE status='pending' AND expires_at <= ?", (iso(now),)
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET status='expired', phase='failed', error_code='task_expired', "
                "finished_at=?, updated_at=? WHERE task_id = ?",
                (iso(now), iso(now), row["task_id"]),
            )
            record_event(conn, row["task_id"], "expired", phase="failed", message="task expired", now=now)
            changed += 1
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status='leased' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at <= ?",
            (iso(now),),
        ).fetchall()
        for row in rows:
            if int(row["attempts"] or 0) >= settings.max_task_attempts:
                conn.execute(
                    "UPDATE tasks SET status='failed', phase='failed', error_code='lease_abandoned', "
                    "finished_at=?, updated_at=? WHERE task_id = ?",
                    (iso(now), iso(now), row["task_id"]),
                )
                record_event(conn, row["task_id"], "expired", phase="failed", message="lease abandoned", now=now)
            else:
                conn.execute(
                    "UPDATE tasks SET status='pending', phase='queued', lease_id=NULL, "
                    "lease_expires_at=NULL, updated_at=? WHERE task_id = ?",
                    (iso(now), row["task_id"]),
                )
                record_event(conn, row["task_id"], "released", phase="queued", message="lease expired", now=now)
            changed += 1
    return changed


__all__ = [
    "TERMINAL",
    "apply_progress",
    "cancel_remote_erase_tasks",
    "cancel_task",
    "claim_next_task",
    "create_task",
    "expire_stale",
    "finish_task",
    "get_task",
    "list_events",
    "list_tasks",
    "release_task",
    "start_task",
    "stats",
    "task_detail",
    "task_envelope",
    "task_payload",
]
