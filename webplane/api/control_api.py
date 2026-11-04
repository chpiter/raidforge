"""Control-plane UI API (no authentication by design)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from flask import Blueprint, current_app, jsonify, request, send_file

from .. import __version__
from ..auth import ApiError
from ..db import get_db
from ..services import agents as agent_service
from ..services import evidence as evidence_service
from ..services import tasks as task_service
from ..task_types import get_task_type, get_task_types
from ..timeutil import iso, utcnow
from ..validation import validate_task_params

bp = Blueprint("cp_api", __name__, url_prefix="/api/cp")


def _settings():
    return current_app.config["SETTINGS"]


def _json_body() -> Dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ApiError("请求体必须是 JSON 对象", code="invalid_body", status=400)
    return payload


def _pagination() -> tuple[int, int]:
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    try:
        page_size = int(request.args.get("page_size", "20"))
    except ValueError:
        page_size = 20
    return max(1, page), min(200, max(1, page_size))


def _bool_value(value: Any, default: bool, name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ApiError(
            f"{name} 必须是 bool",
            code="invalid_params",
            status=400,
            fields=[{"field": name, "message": f"{name} 必须是 bool"}],
        )
    return value


def _int_value(value: Any, default: int, name: str, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(
            f"{name} 必须是整数",
            code="invalid_params",
            status=400,
            fields=[{"field": name, "message": f"{name} 必须是整数"}],
        ) from None
    if number < minimum or number > maximum:
        raise ApiError(
            f"{name} 超出范围 {minimum}..{maximum}",
            code="invalid_params",
            status=400,
            fields=[{"field": name, "message": f"范围 {minimum}..{maximum}"}],
        )
    return number


@bp.get("/health")
def health():
    conn = get_db()
    conn.execute("SELECT 1").fetchone()
    settings = _settings()
    stats = task_service.stats(conn, settings.agent_offline_seconds)
    return jsonify(
        {
            "status": "ok",
            "service": "webplane",
            "version": __version__,
            "db": "ok",
            "agents_online": stats["agents"]["online"],
            "tasks_pending": stats["tasks"]["pending"],
            "server_time": iso(),
        }
    )


@bp.get("/stats")
def stats():
    conn = get_db()
    settings = _settings()
    return jsonify(task_service.stats(conn, settings.agent_offline_seconds))


@bp.get("/agents")
def list_agents():
    settings = _settings()
    page, page_size = _pagination()
    items, total = agent_service.list_agents(
        get_db(),
        status=(request.args.get("status") or "").strip(),
        query=(request.args.get("q") or "").strip(),
        page=page,
        page_size=page_size,
        now=utcnow(),
        offline_seconds=settings.agent_offline_seconds,
    )
    return jsonify({"items": items, "total": total, "page": page, "page_size": page_size})


@bp.get("/agents/<agent_id>")
def get_agent(agent_id: str):
    settings = _settings()
    conn = get_db()
    row = agent_service.get_agent(conn, agent_id)
    if row is None:
        raise ApiError("agent 不存在", code="agent_not_found", status=404)
    payload = agent_service.agent_payload(row, utcnow(), settings.agent_offline_seconds)
    payload["task_counts"] = agent_service.agent_task_counts(conn, agent_id)
    recent, _ = task_service.list_tasks(conn, agent_id=agent_id, page=1, page_size=5)
    payload["recent_tasks"] = recent
    return jsonify({"agent": payload})


@bp.get("/agents/<agent_id>/tasks")
def agent_tasks(agent_id: str):
    page, page_size = _pagination()
    items, total = task_service.list_tasks(
        get_db(),
        agent_id=agent_id,
        status=(request.args.get("status") or "").strip(),
        page=page,
        page_size=page_size,
    )
    return jsonify({"items": items, "total": total, "page": page, "page_size": page_size})


@bp.get("/tasks")
def list_tasks():
    page, page_size = _pagination()
    items, total = task_service.list_tasks(
        get_db(),
        agent_id=(request.args.get("agent_id") or "").strip(),
        task_type=(request.args.get("type") or "").strip(),
        status=(request.args.get("status") or "").strip(),
        query=(request.args.get("q") or "").strip(),
        date_from=(request.args.get("from") or "").strip(),
        date_to=(request.args.get("to") or "").strip(),
        page=page,
        page_size=page_size,
    )
    return jsonify({"items": items, "total": total, "page": page, "page_size": page_size})


@bp.post("/tasks")
def create_task():
    payload = _json_body()
    task_type = str(payload.get("type") or "").strip()
    if get_task_type(task_type) is None:
        raise ApiError(f"未知任务类型 {task_type!r}", code="unsupported_task_type", status=400)

    params = validate_task_params(task_type, payload.get("params") or {})

    conn = get_db()
    agent_row = None
    agent_id = str(payload.get("agent_id") or "").strip()
    server_sn = str(payload.get("server_sn") or "").strip()
    if agent_id:
        agent_row = agent_service.get_agent(conn, agent_id)
    elif server_sn:
        agent_row = agent_service.find_by_server_sn(conn, server_sn)
    if agent_row is None:
        raise ApiError("目标 agent 不存在", code="agent_not_found", status=404)

    row = task_service.create_task(
        conn,
        _settings(),
        agent_row,
        task_type,
        params,
        require_local_confirm=_bool_value(payload.get("require_local_confirm"), True, "require_local_confirm"),
        timeout_seconds=_int_value(payload.get("timeout_seconds"), 600, "timeout_seconds", 30, 7200),
        priority=_int_value(payload.get("priority"), 100, "priority", 0, 1000),
        expires_in_seconds=_int_value(
            payload.get("expires_in_seconds"), 3600, "expires_in_seconds", 60, 86400
        ),
        created_by=str(payload.get("created_by") or "webplane-ui")[:64],
    )
    return jsonify({"task": task_service.task_payload(row)}), 201


@bp.get("/tasks/<task_id>")
def get_task(task_id: str):
    detail = task_service.task_detail(get_db(), task_id)
    if detail is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    for item in detail.get("evidence", []):
        item["download_url"] = f"/api/cp/evidence/{item['id']}/download"
    return jsonify({"task": detail})


@bp.post("/tasks/<task_id>/cancel")
def cancel_task(task_id: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    reason = str(payload.get("reason") or "operator cancelled")
    row = task_service.cancel_task(get_db(), task_id, reason=reason, now=utcnow())
    return jsonify(
        {
            "task_id": task_id,
            "status": row["status"],
            "cancel_requested": bool(row["cancel_requested"]),
        }
    )


@bp.get("/tasks/<task_id>/events")
def task_events(task_id: str):
    if task_service.get_task(get_db(), task_id) is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    return jsonify({"items": task_service.list_events(get_db(), task_id)})


@bp.get("/tasks/<task_id>/evidence")
def task_evidence(task_id: str):
    if task_service.get_task(get_db(), task_id) is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    items = evidence_service.list_evidence(get_db(), task_id)
    for item in items:
        item["download_url"] = f"/api/cp/evidence/{item['id']}/download"
    return jsonify({"items": items})


@bp.get("/evidence/<int:evidence_id>/download")
def download_evidence(evidence_id: int):
    settings = _settings()
    row = evidence_service.get_evidence(get_db(), evidence_id)
    if row is None:
        raise ApiError("证据不存在", code="evidence_not_found", status=404)
    root = Path(settings.evidence_dir).resolve()
    path = (root / row["rel_path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ApiError("证据文件不存在", code="evidence_not_found", status=404)
    return send_file(path, as_attachment=True, download_name=row["filename"])


@bp.get("/task-types")
def task_types():
    return jsonify({"items": get_task_types()})


__all__ = ["bp"]
