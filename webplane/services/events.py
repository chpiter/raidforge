"""Append-only task event stream."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..jsonutil import dumps, loads
from ..timeutil import iso, utcnow


def record_event(
    conn,
    task_id: str,
    kind: str,
    *,
    phase: str = "",
    message: str = "",
    data: Optional[Dict[str, Any]] = None,
    actor: str = "server",
    now=None,
) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, kind, phase, message, data_json, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, kind, phase, message[:2000], dumps(data or {}), actor, iso(now or utcnow())),
    )


def list_events(conn, task_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, kind, phase, message, data_json, actor, created_at "
        "FROM task_events WHERE task_id = ? ORDER BY id ASC LIMIT ?",
        (task_id, max(1, min(2000, int(limit)))),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "phase": row["phase"],
            "message": row["message"],
            "data": loads(row["data_json"], {}),
            "actor": row["actor"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


__all__ = ["record_event", "list_events"]
