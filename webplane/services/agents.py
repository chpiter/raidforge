"""Agent registry and heartbeat handling."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..auth import ApiError
from ..jsonutil import dumps, loads
from ..timeutil import iso, seconds_since, utcnow

_AGENT_COLUMNS = (
    "agent_id, server_sn, hostname, agent_version, protocol_version, reported_status, "
    "capabilities_json, hardware_json, boot_media_json, storage_json, current_task_json, "
    "queue_depth, queue_max, last_error, first_seen_at, last_heartbeat_at, last_seen_ip, updated_at"
)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def agent_status(last_heartbeat_at: str, now: datetime, offline_seconds: int) -> str:
    if not last_heartbeat_at:
        return "unknown"
    elapsed = seconds_since(last_heartbeat_at, now)
    if elapsed is None:
        return "unknown"
    return "online" if elapsed <= offline_seconds else "offline"


def get_agent(conn, agent_id: str):
    return conn.execute(
        f"SELECT {_AGENT_COLUMNS} FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()


def find_by_server_sn(conn, server_sn: str):
    if not server_sn:
        return None
    return conn.execute(
        f"SELECT {_AGENT_COLUMNS} FROM agents WHERE server_sn = ?", (server_sn,)
    ).fetchone()


def deleted_server_sns(conn) -> set[str]:
    return {
        row["server_sn"]
        for row in conn.execute("SELECT server_sn FROM deleted_servers").fetchall()
    }


def is_deleted(conn, server_sn: str) -> bool:
    if not server_sn:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM deleted_servers WHERE server_sn = ?", (server_sn,)
        ).fetchone()
        is not None
    )


def revive_server_sn(conn, server_sn: str) -> int:
    if not server_sn:
        return 0
    return conn.execute(
        "DELETE FROM deleted_servers WHERE server_sn = ?", (server_sn,)
    ).rowcount


def soft_delete_agent(
    conn, agent_id: str, now: datetime, offline_seconds: int
) -> Dict[str, Any]:
    row = get_agent(conn, agent_id)
    if row is None:
        raise ApiError("agent 不存在", code="agent_not_found", status=404)
    server_sn = str(row["server_sn"] or "").strip()
    if not server_sn:
        raise ApiError(
            "该服务器没有序列号，无法删除",
            code="server_sn_missing",
            status=409,
        )
    if agent_status(row["last_heartbeat_at"] or "", now, offline_seconds) != "offline":
        raise ApiError("只能删除离线服务器", code="agent_online", status=409)
    deleted_at = iso(now)
    conn.execute(
        "INSERT INTO deleted_servers (server_sn, agent_id, deleted_at) VALUES (?, ?, ?) "
        "ON CONFLICT(server_sn) DO UPDATE SET agent_id = excluded.agent_id, "
        "deleted_at = excluded.deleted_at",
        (server_sn, agent_id, deleted_at),
    )
    return {"agent_id": agent_id, "server_sn": server_sn, "deleted_at": deleted_at}


def ensure_placeholder_agent(conn, agent_id: str, server_sn: str, now: datetime) -> None:
    """Create an unknown agent row so a poller that beats its first heartbeat
    can still receive tasks."""

    if not agent_id:
        return
    existing = get_agent(conn, agent_id)
    if existing is not None:
        if server_sn and not existing["server_sn"]:
            conn.execute(
                "UPDATE agents SET server_sn = ?, updated_at = ? WHERE agent_id = ?",
                (server_sn, iso(now), agent_id),
            )
        return
    conn.execute(
        "INSERT INTO agents (agent_id, server_sn, first_seen_at, last_heartbeat_at, updated_at) "
        "VALUES (?, ?, ?, '', ?)",
        (agent_id, server_sn, iso(now), iso(now)),
    )


def upsert_heartbeat(
    conn,
    payload: Dict[str, Any],
    remote_addr: str,
    now: datetime,
    offline_seconds: int = 45,
) -> Dict[str, Any]:
    agent_id = str(payload.get("agent_id") or "").strip()
    if not agent_id:
        raise ApiError("heartbeat 缺少 agent_id", code="invalid_agent_id", status=400)
    server_sn = str(payload.get("server_sn") or "").strip()

    if server_sn:
        owner = find_by_server_sn(conn, server_sn)
        if owner is not None and owner["agent_id"] != agent_id:
            raise ApiError(
                f"server_sn {server_sn} 已绑定到 {owner['agent_id']}",
                code="server_sn_conflict",
                status=409,
            )
        revive_server_sn(conn, server_sn)

    current = get_agent(conn, agent_id)
    now_iso = iso(now)
    reported = payload.get("status")
    if not isinstance(reported, str):
        reported = dumps(reported)

    values = {
        "server_sn": server_sn,
        "hostname": str(payload.get("hostname") or "")[:255],
        "agent_version": str(payload.get("agent_version") or "")[:64],
        "protocol_version": _int(payload.get("protocol_version"), 1),
        "reported_status": reported[:64],
        "capabilities_json": dumps(payload.get("capabilities") or {}),
        "hardware_json": dumps(payload.get("hardware") or {}),
        "boot_media_json": dumps(payload.get("boot_media") or {}),
        "storage_json": dumps(payload.get("storage") or {}),
        "current_task_json": dumps(payload.get("current_task") or {}),
        "queue_depth": _int(payload.get("queue_depth"), 0),
        "queue_max": _int(payload.get("queue_max"), 0),
        "last_error": str(payload.get("last_error") or "")[:2000],
        "last_seen_ip": remote_addr or "",
        "last_heartbeat_at": now_iso,
        "updated_at": now_iso,
    }

    if current is None:
        conn.execute(
            "INSERT INTO agents (agent_id, server_sn, hostname, agent_version, protocol_version, "
            "reported_status, capabilities_json, hardware_json, boot_media_json, storage_json, "
            "current_task_json, queue_depth, queue_max, last_error, first_seen_at, "
            "last_heartbeat_at, last_seen_ip, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                values["server_sn"],
                values["hostname"],
                values["agent_version"],
                values["protocol_version"],
                values["reported_status"],
                values["capabilities_json"],
                values["hardware_json"],
                values["boot_media_json"],
                values["storage_json"],
                values["current_task_json"],
                values["queue_depth"],
                values["queue_max"],
                values["last_error"],
                now_iso,
                now_iso,
                values["last_seen_ip"],
                now_iso,
            ),
        )
    else:
        conn.execute(
            "UPDATE agents SET server_sn = ?, hostname = ?, agent_version = ?, "
            "protocol_version = ?, reported_status = ?, capabilities_json = ?, "
            "hardware_json = ?, boot_media_json = ?, storage_json = ?, current_task_json = ?, "
            "queue_depth = ?, queue_max = ?, last_error = ?, last_heartbeat_at = ?, "
            "last_seen_ip = ?, updated_at = ? WHERE agent_id = ?",
            (
                values["server_sn"],
                values["hostname"],
                values["agent_version"],
                values["protocol_version"],
                values["reported_status"],
                values["capabilities_json"],
                values["hardware_json"],
                values["boot_media_json"],
                values["storage_json"],
                values["current_task_json"],
                values["queue_depth"],
                values["queue_max"],
                values["last_error"],
                now_iso,
                values["last_seen_ip"],
                now_iso,
                agent_id,
            ),
        )

    return agent_payload(get_agent(conn, agent_id), now, offline_seconds=offline_seconds)


def agent_payload(row, now: datetime, offline_seconds: int) -> Dict[str, Any]:
    if row is None:
        return {}
    key = row.keys()
    capabilities = loads(row["capabilities_json"], {})
    if isinstance(capabilities, dict):
        task_types = capabilities.get("task_types")
        if isinstance(task_types, list):
            capabilities["task_types"] = [item for item in task_types if item != "erase_disks"]
    hardware = loads(row["hardware_json"], {})
    last_heartbeat = row["last_heartbeat_at"] or ""
    elapsed = seconds_since(last_heartbeat, now) if last_heartbeat else None
    payload = {
        "agent_id": row["agent_id"],
        "server_sn": row["server_sn"],
        "hostname": row["hostname"],
        "agent_version": row["agent_version"],
        "protocol_version": row["protocol_version"],
        "reported_status": row["reported_status"],
        "status": agent_status(last_heartbeat, now, offline_seconds),
        "hardware": hardware,
        "vendor": hardware.get("vendor", "") if isinstance(hardware, dict) else "",
        "model": hardware.get("model", "") if isinstance(hardware, dict) else "",
        "capabilities": capabilities,
        "boot_media": loads(row["boot_media_json"], {}),
        "storage": loads(row["storage_json"], {}),
        "current_task": loads(row["current_task_json"], {}),
        "queue_depth": row["queue_depth"],
        "queue_max": row["queue_max"],
        "last_error": row["last_error"],
        "first_seen_at": row["first_seen_at"],
        "last_heartbeat_at": last_heartbeat,
        "seconds_since_heartbeat": int(elapsed) if elapsed is not None else None,
        "last_seen_ip": row["last_seen_ip"],
        "updated_at": row["updated_at"],
    }
    if "task_counts" in key:
        payload["task_counts"] = loads(row["task_counts"], {})
    return payload


def list_agents(
    conn,
    *,
    status: str = "",
    query: str = "",
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 20,
    now: Optional[datetime] = None,
    offline_seconds: int = 45,
) -> Tuple[List[Dict[str, Any]], int]:
    now = now or utcnow()
    rows = conn.execute(
        f"SELECT {_AGENT_COLUMNS} FROM agents ORDER BY last_heartbeat_at DESC, agent_id ASC"
    ).fetchall()
    deleted_sns = deleted_server_sns(conn)
    items: List[Dict[str, Any]] = []
    needle = (query or "").strip().lower()
    for row in rows:
        payload = agent_payload(row, now, offline_seconds)
        payload_deleted = payload["server_sn"] in deleted_sns
        if payload_deleted and not include_deleted:
            continue
        if include_deleted:
            payload["deleted"] = payload_deleted
        if needle:
            haystack = " ".join(
                str(payload.get(key) or "")
                for key in ("agent_id", "server_sn", "hostname", "vendor", "model")
            ).lower()
            if needle not in haystack:
                continue
        if status and payload["status"] != status:
            continue
        items.append(payload)
    total = len(items)
    page = max(1, int(page))
    page_size = min(200, max(1, int(page_size)))
    start = (page - 1) * page_size
    return items[start : start + page_size], total


def agent_task_counts(conn, agent_id: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE agent_id = ? GROUP BY status",
        (agent_id,),
    ).fetchall()
    for row in rows:
        counts[row["status"]] = row["n"]
    return counts


__all__ = [
    "agent_payload",
    "agent_status",
    "agent_task_counts",
    "deleted_server_sns",
    "ensure_placeholder_agent",
    "find_by_server_sn",
    "get_agent",
    "is_deleted",
    "list_agents",
    "revive_server_sn",
    "soft_delete_agent",
    "upsert_heartbeat",
]
