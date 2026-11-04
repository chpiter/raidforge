from __future__ import annotations

import hashlib
import io
import re

import pytest

from webplane.app import create_app
from webplane.config import Settings
from webplane.db import connect

TASK_ID_RE = re.compile(r"^tsk_[A-Za-z0-9]{8,64}$")
LEASE_ID_RE = re.compile(r"^lse_[A-Za-z0-9]{8,64}$")


def _db_update(settings, sql, params):
    conn = connect(settings.db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _result(task_id, lease_id, status="finished", **extra):
    payload = {
        "task_id": task_id,
        "lease_id": lease_id,
        "schema_version": 1,
        "agent_id": "agent-1",
        "status": status,
        "result": {},
        "evidence": {"status": "pending", "manifest": []},
    }
    payload.update(extra)
    return payload


def test_heartbeat_registers_agent(client, register_agent, agent_headers):
    register_agent(agent_headers)
    response = client.get("/api/cp/agents")
    assert response.status_code == 200
    items = response.get_json()["items"]
    assert len(items) == 1
    agent = items[0]
    assert agent["agent_id"] == "agent-1"
    assert agent["server_sn"] == "SN-1"
    assert agent["status"] == "online"
    assert agent["capabilities"]["controllers"][0]["controller_id"] == "lsi:SNCTRL"


def test_token_is_optional_but_enforced_when_configured(tmp_path, make_headers):
    settings = Settings(data_dir=tmp_path, maintenance=False, agent_token="secret")
    app = create_app(settings)
    client = app.test_client()
    headers = make_headers("agent-1")
    body = {"schema_version": 1, "agent_id": "agent-1", "server_sn": "SN-1"}

    assert client.post("/api/raidforge/agent/heartbeat", json=body, headers=headers).status_code == 401
    wrong = make_headers("agent-1", token="nope")
    assert client.post("/api/raidforge/agent/heartbeat", json=body, headers=wrong).status_code == 401
    good = make_headers("agent-1", token="secret")
    assert client.post("/api/raidforge/agent/heartbeat", json=body, headers=good).status_code == 200


def test_protocol_mismatch_returns_426(client):
    headers = {"X-Raidforge-Agent-Id": "agent-1", "X-Raidforge-Protocol": "2"}
    response = client.post("/api/raidforge/agent/heartbeat", json={"agent_id": "agent-1"}, headers=headers)
    assert response.status_code == 426
    assert response.get_json()["error_code"] == "protocol_too_old"


def test_server_sn_conflict(client, register_agent, agent_headers, make_headers):
    register_agent(agent_headers, server_sn="SN-1")
    other = make_headers("agent-2")
    body = {
        "schema_version": 1,
        "agent_id": "agent-2",
        "server_sn": "SN-1",
        "agent_version": "1.2.0",
        "protocol_version": 1,
    }
    response = client.post("/api/raidforge/agent/heartbeat", json=body, headers=other)
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "server_sn_conflict"


def test_next_returns_204_when_empty(client, register_agent, agent_headers):
    register_agent(agent_headers)
    response = client.get("/api/raidforge/agent/tasks/next?server_sn=SN-1", headers=agent_headers)
    assert response.status_code == 204
    assert response.data == b""


def test_claim_returns_valid_envelope(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    task = create_task(agent_headers["X-Raidforge-Agent-Id"], "list_vds", {"controller_ids": None})
    envelope = claim_task(agent_headers)
    assert TASK_ID_RE.match(envelope["task_id"])
    assert LEASE_ID_RE.match(envelope["lease_id"])
    assert envelope["task_id"] == task["task_id"]
    assert envelope["type"] == "list_vds"
    assert envelope["server_sn"] == "SN-1"
    assert envelope["params"] == {"controller_ids": None, "include_raw_output": False}
    assert envelope["status"] == "leased"
    assert envelope["expires_at"]


def test_lease_expiry_requeues_and_reassigns(
    settings, client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    task = create_task(agent_headers["X-Raidforge-Agent-Id"], "list_vds")
    first = claim_task(agent_headers)
    _db_update(
        settings,
        "UPDATE tasks SET lease_expires_at = ? WHERE task_id = ?",
        ("2000-01-01T00:00:00Z", task["task_id"]),
    )
    second = claim_task(agent_headers)
    assert second["task_id"] == first["task_id"]
    assert second["lease_id"] != first["lease_id"]

    conn = connect(settings.db_path)
    try:
        row = conn.execute("SELECT attempts, status FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone()
    finally:
        conn.close()
    assert row["attempts"] == 2
    assert row["status"] == "leased"


def test_started_rejects_lease_mismatch(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    task = create_task(agent_headers["X-Raidforge-Agent-Id"], "list_vds")
    claim_task(agent_headers)
    response = client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/started",
        json={"schema_version": 1, "agent_id": "agent-1", "lease_id": "lse_deadbeef"},
        headers=agent_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error_code"] == "lease_mismatch"


def test_progress_reflects_cancel_request(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    task = create_task(agent_headers["X-Raidforge-Agent-Id"], "list_vds")
    envelope = claim_task(agent_headers)
    lease = envelope["lease_id"]
    client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/started",
        json={"schema_version": 1, "agent_id": "agent-1", "lease_id": lease},
        headers=agent_headers,
    )
    client.post(f"/api/cp/tasks/{task['task_id']}/cancel", json={"reason": "stop"})
    response = client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/progress",
        json={
            "schema_version": 1,
            "agent_id": "agent-1",
            "lease_id": lease,
            "phase": "running",
            "done": 1,
            "total": 3,
            "message": "working",
        },
        headers=agent_headers,
    )
    assert response.status_code == 200
    assert response.get_json()["cancel_requested"] is True


def test_result_is_idempotent(client, register_agent, agent_headers, create_task, claim_task):
    register_agent(agent_headers)
    task = create_task(agent_headers["X-Raidforge-Agent-Id"], "list_vds")
    envelope = claim_task(agent_headers)
    lease = envelope["lease_id"]
    client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/started",
        json={"schema_version": 1, "agent_id": "agent-1", "lease_id": lease},
        headers=agent_headers,
    )
    headers = {**agent_headers, "Idempotency-Key": f"{task['task_id']}:{lease}"}
    first = client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/result",
        json=_result(task["task_id"], lease),
        headers=headers,
    )
    assert first.status_code == 200
    assert first.get_json()["duplicate"] is False
    second = client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/result",
        json=_result(task["task_id"], lease),
        headers=headers,
    )
    assert second.status_code == 200
    assert second.get_json()["duplicate"] is True


def test_release_unstarted_requeues(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    task = create_task(agent_headers["X-Raidforge-Agent-Id"], "list_vds")
    envelope = claim_task(agent_headers)
    response = client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/release",
        json={"schema_version": 1, "agent_id": "agent-1", "lease_id": envelope["lease_id"]},
        headers=agent_headers,
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "pending"

    again = claim_task(agent_headers)
    assert again["lease_id"] != envelope["lease_id"]
    started = client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/started",
        json={"schema_version": 1, "agent_id": "agent-1", "lease_id": envelope["lease_id"]},
        headers=agent_headers,
    )
    assert started.status_code == 409


def test_evidence_upload_duplicate_and_conflict(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    task = create_task(agent_headers["X-Raidforge-Agent-Id"], "list_vds")
    envelope = claim_task(agent_headers)
    lease = envelope["lease_id"]
    blob = b'{"ok":true}'
    sha = hashlib.sha256(blob).hexdigest()
    url = f"/api/raidforge/agent/tasks/{task['task_id']}/evidence"

    def upload(name, data, declared_sha):
        return client.post(
            url,
            data={
                "sha256": declared_sha,
                "size": str(len(data)),
                "kind": "json",
                "stage": "execute",
                "lease_id": lease,
                "file": (io.BytesIO(data), name),
            },
            headers=agent_headers,
            content_type="multipart/form-data",
        )

    first = upload("scan.json", blob, sha)
    assert first.status_code == 201, first.get_json()
    duplicate = upload("scan.json", blob, sha)
    assert duplicate.status_code == 200
    assert duplicate.get_json()["duplicate"] is True

    conflict = upload("scan.json", b"different", hashlib.sha256(b"different").hexdigest())
    assert conflict.status_code == 400
    assert conflict.get_json()["error_code"] == "evidence_conflict"

    traversal = upload("../evil.json", blob, sha)
    assert traversal.status_code == 400
    assert traversal.get_json()["error_code"] == "invalid_filename"


def test_evidence_size_limit(tmp_path, make_headers):
    settings = Settings(data_dir=tmp_path, maintenance=False, max_evidence_bytes=4)
    app = create_app(settings)
    client = app.test_client()
    headers = make_headers("agent-1")
    client.post(
        "/api/raidforge/agent/heartbeat",
        json={"agent_id": "agent-1", "server_sn": "SN-1"},
        headers=headers,
    )
    task = client.post(
        "/api/cp/tasks", json={"agent_id": "agent-1", "type": "list_vds", "params": {}}
    ).get_json()["task"]
    envelope = client.get(
        "/api/raidforge/agent/tasks/next?server_sn=SN-1", headers=headers
    ).get_json()
    response = client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/evidence",
        data={
            "lease_id": envelope["lease_id"],
            "file": (io.BytesIO(b"too-large"), "big.bin"),
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 413


@pytest.mark.parametrize("path", ["/api/raidforge/heartbeat", "/api/raidforge/agent/heartbeat"])
def test_documented_alias_paths(client, make_headers, path):
    headers = make_headers("agent-alias")
    response = client.post(
        path, json={"agent_id": "agent-alias", "server_sn": "SN-ALIAS"}, headers=headers
    )
    assert response.status_code == 200
