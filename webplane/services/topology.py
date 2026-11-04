"""Per-controller PDS/VD snapshot cache built from read-only task results."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..db import transaction
from ..jsonutil import dumps, loads
from ..timeutil import iso, seconds_since, utcnow

log = logging.getLogger(__name__)

STALE_SECONDS = 900
MAX_ITEMS = 1024
TEXT_LIMIT = 512

def _text(value: Any, limit: int = TEXT_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = dumps(value)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    return text[:limit]


def _int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _text_list(value: Any, limit: int = 256) -> List[str]:
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value[:limit] if item is not None]


def _spans(value: Any) -> List[List[str]]:
    if not isinstance(value, list):
        return []
    result: List[List[str]] = []
    for span in value[:64]:
        if isinstance(span, list):
            result.append([_text(item) for item in span[:128] if item is not None])
    return result


def _sanitize_pd(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    controller_id = _text(item.get("controller_id")).strip()
    if not controller_id:
        return None
    return {
        "controller_id": controller_id,
        "serial": _text(item.get("serial")),
        "wwn": _text(item.get("wwn")),
        "expected_size_bytes": _int(
            item.get("expected_size_bytes", item.get("size_bytes", 0))
        ),
        "media_type": _text(item.get("media_type")).lower(),
        "state": _text(item.get("state", item.get("raid_state", ""))),
        "raid_member_of": _text(item.get("raid_member_of")),
        "boot_protected": bool(item.get("boot_protected")),
    }


def _sanitize_vd(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    controller_id = _text(item.get("controller_id")).strip()
    if not controller_id:
        return None
    return {
        "controller_id": controller_id,
        "controller_index": _int(item.get("controller_index")),
        "vd_id": _text(item.get("vd_id")),
        "vd_name": _text(item.get("vd_name")),
        "raid_level": _text(item.get("raid_level")),
        "size_bytes": _int(item.get("size_bytes")),
        "state": _text(item.get("state")),
        "member_disk_ids": _text_list(item.get("member_disk_ids")),
        "member_serials": _text_list(item.get("member_serials")),
        "spans": _spans(item.get("spans")),
        "disk_group": _text(item.get("disk_group")),
    }


def _upsert_pds(conn, agent_id: str, controller_id: str, items: List[Dict[str, Any]], collected: str, topology_hash: str, updated: str) -> None:
    conn.execute(
        "INSERT INTO controller_snapshots "
        "(agent_id, controller_id, pds_json, pds_collected_at, topology_hash, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(agent_id, controller_id) DO UPDATE SET "
        "pds_json = excluded.pds_json, "
        "pds_collected_at = excluded.pds_collected_at, "
        "topology_hash = CASE WHEN excluded.topology_hash <> '' "
        "THEN excluded.topology_hash ELSE controller_snapshots.topology_hash END, "
        "updated_at = excluded.updated_at",
        (agent_id, controller_id, dumps(items), collected, topology_hash, updated),
    )


def _upsert_vds(conn, agent_id: str, controller_id: str, items: List[Dict[str, Any]], collected: str, topology_hash: str, updated: str) -> None:
    conn.execute(
        "INSERT INTO controller_snapshots "
        "(agent_id, controller_id, vds_json, vds_collected_at, topology_hash, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(agent_id, controller_id) DO UPDATE SET "
        "vds_json = excluded.vds_json, "
        "vds_collected_at = excluded.vds_collected_at, "
        "topology_hash = CASE WHEN excluded.topology_hash <> '' "
        "THEN excluded.topology_hash ELSE controller_snapshots.topology_hash END, "
        "updated_at = excluded.updated_at",
        (agent_id, controller_id, dumps(items), collected, topology_hash, updated),
    )


def ingest_result(
    conn,
    agent_id: str,
    task_type: str,
    payload: Dict[str, Any],
    now: Optional[datetime] = None,
    controller_ids: Optional[List[str]] = None,
) -> Dict[str, int]:
    """Cache list_pds/list_vds results per controller. Never raises."""

    try:
        if task_type not in {"list_pds", "list_vds"} or not isinstance(payload, dict):
            return {}
        result = payload.get("result")
        if not isinstance(result, dict):
            result = payload
        key = "pds" if task_type == "list_pds" else "vds"
        raw_items = result.get(key)
        if not isinstance(raw_items, list):
            return {}
        sanitizer = _sanitize_pd if task_type == "list_pds" else _sanitize_vd
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in raw_items[:MAX_ITEMS]:
            if not isinstance(item, dict):
                continue
            sanitized = sanitizer(item)
            if sanitized is None:
                continue
            groups.setdefault(sanitized["controller_id"], []).append(sanitized)
        # An explicitly targeted controller with zero rows is still a completed
        # collection; without this the UI keeps re-issuing the read task.
        for controller_id in controller_ids or []:
            controller_id = str(controller_id or "").strip()[:128]
            if controller_id:
                groups.setdefault(controller_id, [])
        if not groups:
            return {}
        collected = iso(now or utcnow())
        topology_hash = _text(result.get("topology_hash"), 128)
        counts: Dict[str, int] = {}
        with transaction(conn):
            for controller_id, items in groups.items():
                if task_type == "list_pds":
                    _upsert_pds(
                        conn, agent_id, controller_id, items, collected, topology_hash, collected
                    )
                else:
                    _upsert_vds(
                        conn, agent_id, controller_id, items, collected, topology_hash, collected
                    )
                counts[controller_id] = len(items)
        return counts
    except Exception:  # pragma: no cover - defensive
        log.warning("topology ingest failed for %s", agent_id, exc_info=True)
        return {}


def snapshot_map(conn, agent_id: str) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM controller_snapshots WHERE agent_id = ?", (agent_id,)
    ).fetchall()
    return {row["controller_id"]: row for row in rows}


def _heartbeat_controllers(agent_row) -> List[Dict[str, Any]]:
    if agent_row is None:
        return []
    capabilities = loads(agent_row["capabilities_json"], {})
    controllers = capabilities.get("controllers") if isinstance(capabilities, dict) else None
    if not isinstance(controllers, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in controllers:
        if not isinstance(item, dict):
            continue
        controller_id = _text(item.get("controller_id")).strip()
        if not controller_id:
            continue
        tool = item.get("tool")
        tool_name = tool
        if isinstance(tool, dict):
            tool_name = tool.get("name") or tool.get("path") or ""
        result.append(
            {
                "controller_id": controller_id,
                "vendor": _text(item.get("vendor")),
                "model": _text(item.get("model")),
                "serial": _text(item.get("serial")),
                "firmware": _text(item.get("firmware")),
                "index": _int(item.get("index")),
                "destructive_allowed": bool(item.get("destructive_allowed", True)),
                "tool": _text(tool_name),
                "known": True,
            }
        )
    return result


def _snapshot_payload(row) -> Dict[str, Any]:
    if row is None:
        return {
            "pds": [],
            "vds": [],
            "pds_collected_at": "",
            "vds_collected_at": "",
            "topology_hash": "",
            "updated_at": "",
        }
    pds = loads(row["pds_json"], [])
    vds = loads(row["vds_json"], [])
    return {
        "pds": pds if isinstance(pds, list) else [],
        "vds": vds if isinstance(vds, list) else [],
        "pds_collected_at": row["pds_collected_at"] or "",
        "vds_collected_at": row["vds_collected_at"] or "",
        "topology_hash": row["topology_hash"] or "",
        "updated_at": row["updated_at"] or "",
    }


def controller_summaries(
    conn,
    agent_row,
    now: Optional[datetime] = None,
    offline_seconds: int = 45,
) -> List[Dict[str, Any]]:
    now = now or utcnow()
    snapshots = snapshot_map(conn, agent_row["agent_id"])
    merged: Dict[str, Dict[str, Any]] = {}
    for item in _heartbeat_controllers(agent_row):
        merged[item["controller_id"]] = item
    for controller_id, row in snapshots.items():
        if controller_id not in merged:
            merged[controller_id] = {
                "controller_id": controller_id,
                "vendor": "",
                "model": "",
                "serial": "",
                "firmware": "",
                "index": 0,
                "destructive_allowed": False,
                "tool": "",
                "known": False,
            }
    summaries: List[Dict[str, Any]] = []
    for controller_id, item in merged.items():
        snapshot = _snapshot_payload(snapshots.get(controller_id))
        payload = dict(item)
        payload.update(
            {
                "pd_count": len(snapshot["pds"]),
                "vd_count": len(snapshot["vds"]),
                "pds_collected_at": snapshot["pds_collected_at"],
                "vds_collected_at": snapshot["vds_collected_at"],
                "topology_hash": snapshot["topology_hash"],
                "has_pds": bool(snapshot["pds_collected_at"]),
                "has_vds": bool(snapshot["vds_collected_at"]),
                "stale": _is_stale(snapshot, now),
            }
        )
        summaries.append(payload)
    summaries.sort(key=lambda item: (item.get("index", 0), item.get("controller_id", "")))
    return summaries


def controller_detail(
    conn,
    agent_row,
    controller_id: str,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    now = now or utcnow()
    summaries = controller_summaries(conn, agent_row, now)
    controller = next(
        (item for item in summaries if item["controller_id"] == controller_id), None
    )
    if controller is None:
        return None
    snapshot = _snapshot_payload(snapshot_map(conn, agent_row["agent_id"]).get(controller_id))
    return {
        "agent_id": agent_row["agent_id"],
        "controller": controller,
        "pds": snapshot["pds"],
        "vds": snapshot["vds"],
        "pds_collected_at": snapshot["pds_collected_at"],
        "vds_collected_at": snapshot["vds_collected_at"],
        "topology_hash": snapshot["topology_hash"],
        "updated_at": snapshot["updated_at"],
        "has_pds": bool(snapshot["pds_collected_at"]),
        "has_vds": bool(snapshot["vds_collected_at"]),
        "stale": _is_stale(snapshot, now),
    }


def _is_stale(snapshot: Dict[str, Any], now: datetime) -> bool:
    for key in ("pds_collected_at", "vds_collected_at"):
        value = snapshot.get(key) or ""
        if not value:
            continue
        elapsed = seconds_since(value, now)
        if elapsed is not None and elapsed > STALE_SECONDS:
            return True
    return False


__all__ = [
    "STALE_SECONDS",
    "controller_detail",
    "controller_summaries",
    "ingest_result",
    "snapshot_map",
]
