"""Agent-facing API.

Routes are registered under both ``/api/raidforge/agent`` (what the shipped
agent client calls) and ``/api/raidforge`` (the path used by the protocol
document), so either generation of the agent can talk to the same control plane.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, current_app, jsonify, request

from ..auth import (
    ApiError,
    agent_id_from_request,
    error_response,
    guard_agent_request,
    lease_from_request,
)
from ..db import get_db, transaction
from ..services import agents as agent_service
from ..services import evidence as evidence_service
from ..services import tasks as task_service
from ..timeutil import iso, utcnow

bp = Blueprint("agent_api", __name__)
_BASES = ("/api/raidforge/agent", "/api/raidforge")


def agent_route(rule: str, methods=("GET",)):
    def register(view):
        for base in _BASES:
            bp.add_url_rule(
                f"{base}{rule}",
                endpoint=view.__name__,
                view_func=view,
                methods=list(methods),
            )
        return view

    return register


@bp.before_request
def _guard():
    settings = current_app.config["SETTINGS"]
    try:
        guard_agent_request(settings)
    except ApiError as exc:
        return error_response(exc)
    return None


def _settings():
    return current_app.config["SETTINGS"]


def _json_body() -> Dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ApiError("请求体必须是 JSON 对象", code="invalid_body", status=400)
    return payload


@agent_route("/heartbeat", methods=["POST"])
def heartbeat():
    settings = _settings()
    payload = _json_body()
    agent_id = agent_id_from_request(payload)
    if not agent_id:
        raise ApiError("缺少 agent_id", code="invalid_agent_id", status=400)
    payload["agent_id"] = agent_id
    conn = get_db()
    now = utcnow()
    with transaction(conn):
        agent_service.upsert_heartbeat(
            conn,
            payload,
            request.remote_addr or "",
            now,
            offline_seconds=settings.agent_offline_seconds,
        )
    return jsonify(
        {
            "ok": True,
            "server_time": iso(now),
            "heartbeat_interval": settings.heartbeat_interval,
            "poll_interval": settings.poll_interval,
        }
    )


@agent_route("/tasks/next")
def next_task():
    settings = _settings()
    agent_id = agent_id_from_request()
    if not agent_id:
        raise ApiError("缺少 X-Raidforge-Agent-Id", code="invalid_agent_id", status=400)
    server_sn = (request.args.get("server_sn") or "").strip()
    conn = get_db()
    now = utcnow()
    agent_service.ensure_placeholder_agent(conn, agent_id, server_sn, now)
    task = task_service.claim_next_task(conn, settings, agent_id, server_sn, now=now)
    if task is None:
        return "", 204
    return jsonify(task_service.task_envelope(task))


@agent_route("/tasks/<task_id>")
def get_task(task_id: str):
    conn = get_db()
    row = task_service.get_task(conn, task_id)
    if row is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    return jsonify(task_service.task_envelope(row))


@agent_route("/tasks/<task_id>/started", methods=["POST"])
def started(task_id: str):
    payload = _json_body()
    lease_id = lease_from_request(payload)
    agent_id = agent_id_from_request(payload)
    conn = get_db()
    row = task_service.start_task(conn, task_id, lease_id, agent_id, now=utcnow())
    return jsonify({"ok": True, "status": row["status"]})


@agent_route("/tasks/<task_id>/progress", methods=["POST"])
def progress(task_id: str):
    payload = _json_body()
    lease_id = lease_from_request(payload)
    conn = get_db()
    cancel_requested = task_service.apply_progress(
        conn, task_id, lease_id, payload, now=utcnow()
    )
    return jsonify({"ok": True, "cancel_requested": cancel_requested})


@agent_route("/tasks/<task_id>/cancel_requested")
def cancel_requested(task_id: str):
    conn = get_db()
    row = task_service.get_task(conn, task_id)
    if row is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    return jsonify(
        {
            "task_id": task_id,
            "cancel_requested": bool(row["cancel_requested"]),
            "status": row["status"],
        }
    )


@agent_route("/tasks/<task_id>/evidence", methods=["POST"])
def evidence(task_id: str):
    settings = _settings()
    conn = get_db()
    row = task_service.get_task(conn, task_id)
    if row is None:
        raise ApiError("任务不存在", code="task_not_found", status=404)
    upload = request.files.get("file")
    if upload is None:
        raise ApiError("缺少 file 字段", code="missing_file", status=400)
    agent_id = agent_id_from_request()
    record = evidence_service.save_evidence(
        conn,
        settings,
        row,
        upload,
        request.form,
        agent_id,
        now=utcnow(),
    )
    status = 200 if record.get("duplicate") else 201
    return jsonify(record), status


@agent_route("/tasks/<task_id>/result", methods=["POST"])
def result(task_id: str):
    payload = _json_body()
    conn = get_db()
    row, duplicate = task_service.finish_task(
        conn,
        task_id,
        payload,
        idempotency_key=(request.headers.get("Idempotency-Key") or "").strip(),
        now=utcnow(),
    )
    return jsonify({"ok": True, "duplicate": duplicate, "status": row["status"]})


@agent_route("/tasks/<task_id>/release", methods=["POST"])
def release(task_id: str):
    payload = _json_body()
    lease_id = lease_from_request(payload)
    conn = get_db()
    row = task_service.release_task(
        conn,
        task_id,
        lease_id,
        reason=str(payload.get("reason") or ""),
        now=utcnow(),
    )
    return jsonify({"ok": True, "status": row["status"]})


__all__ = ["bp", "agent_route"]
