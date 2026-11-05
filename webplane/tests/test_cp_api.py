from __future__ import annotations

import hashlib
import io

import pytest

from webplane.db import connect

PD = {"serial": "S1", "wwn": "0x5000c500a4a2b001", "expected_size_bytes": 1000}
CONTROLLER = "lsi:SNCTRL"


def params_for(task_type: str):
    if task_type in {"refresh_hardware", "list_controllers", "list_pds", "list_vds"}:
        return {}
    if task_type == "create_raid":
        return {
            "controller_id": CONTROLLER,
            "name": "vd-test",
            "raid_level": "RAID0",
            "pds": [PD],
            "init": "fast",
            "dry_run": True,
        }
    if task_type == "delete_raid":
        return {
            "controller_id": CONTROLLER,
            "vd_id": "0",
            "vd_name": "vd-test",
            "expected_size_bytes": 1000,
            "dry_run": True,
        }
    if task_type == "clear_config":
        return {
            "controller_id": CONTROLLER,
            "expected_vd_count": 0,
            "include_foreign": False,
            "dry_run": True,
        }
    if task_type == "erase_disks":
        return {
            "controller_id": CONTROLLER,
            "mode": "thorough",
            "passes": 1,
            "verify": False,
            "allow_raid_member": False,
            "pds": [PD],
            "dry_run": True,
        }
    if task_type == "abort_current":
        return {"reason": "operator requested", "target_task_id": ""}
    raise AssertionError(task_type)


ALL_TYPES = [
    "refresh_hardware",
    "list_controllers",
    "list_pds",
    "list_vds",
    "create_raid",
    "delete_raid",
    "clear_config",
    "erase_disks",
    "abort_current",
]


def test_health(client):
    response = client.get("/api/cp/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["service"] == "webplane"


def test_task_types_endpoint(client):
    response = client.get("/api/cp/task-types")
    assert response.status_code == 200
    items = response.get_json()["items"]
    assert {item["type"] for item in items} == set(ALL_TYPES)
    destructive = {item["type"] for item in items if item["destructive"]}
    assert destructive == {"create_raid", "delete_raid", "clear_config", "erase_disks"}
    for item in items:
        assert item["template"]
        assert item["fields"]
    create = next(item for item in items if item["type"] == "create_raid")
    assert create["template"]["strip_size_kb"] is None
    assert create["template"]["init"] == "fast"
    init_field = next(field for field in create["fields"] if field["name"] == "init")
    assert [option["value"] for option in init_field["options"]] == ["fast", "none"]


@pytest.mark.parametrize("task_type", ALL_TYPES)
def test_create_all_task_types(client, register_agent, agent_headers, task_type):
    register_agent(agent_headers)
    response = client.post(
        "/api/cp/tasks",
        json={"agent_id": "agent-1", "type": task_type, "params": params_for(task_type)},
    )
    assert response.status_code == 201, response.get_json()
    task = response.get_json()["task"]
    assert task["type"] == task_type
    assert task["status"] == "pending"
    assert task["server_sn"] == "SN-1"


def test_create_raid_accepts_zero_stripe_as_controller_default(
    client, register_agent, agent_headers
):
    register_agent(agent_headers)
    params = params_for("create_raid")
    params["strip_size_kb"] = 0
    response = client.post(
        "/api/cp/tasks",
        json={"agent_id": "agent-1", "type": "create_raid", "params": params},
    )
    assert response.status_code == 201, response.get_json()
    task = response.get_json()["task"]
    assert task["params"]["strip_size_kb"] is None
    assert task["params"]["init"] == "fast"


@pytest.mark.parametrize(
    "task_type,params",
    [
        (
            "erase_disks",
            {
                "controller_id": CONTROLLER,
                "mode": "thorough",
                "verify": True,
                "pds": [PD],
            },
        ),
        (
            "create_raid",
            {
                "controller_id": CONTROLLER,
                "name": "x",
                "raid_level": "RAID0",
                "pds": [PD],
                "unknown_field": 1,
            },
        ),
        (
            "create_raid",
            {
                "controller_id": CONTROLLER,
                "name": "x",
                "raid_level": "RAID0",
                "pds": [PD],
                "strip_size_kb": 8,
            },
        ),
        (
            "create_raid",
            {
                "controller_id": CONTROLLER,
                "name": "x",
                "raid_level": "RAID0",
                "pds": [PD],
                "init": "quick",
            },
        ),
        (
            "create_raid",
            {
                "controller_id": CONTROLLER,
                "name": "x",
                "raid_level": "RAID0",
                "pds": [{"serial": "S1", "wwn": "", "expected_size_bytes": 1}],
            },
        ),
        (
            "delete_raid",
            {"controller_id": CONTROLLER, "vd_id": "0", "expected_size_bytes": 1},
        ),
        ("list_vds", {"controller_ids": [1]}),
    ],
)
def test_create_rejects_invalid_params(client, register_agent, agent_headers, task_type, params):
    register_agent(agent_headers)
    response = client.post(
        "/api/cp/tasks",
        json={"agent_id": "agent-1", "type": task_type, "params": params},
    )
    assert response.status_code == 400, response.get_json()
    body = response.get_json()
    assert body["error_code"]
    assert body.get("fields")


def test_create_rejects_bad_task_options(client, register_agent, agent_headers):
    register_agent(agent_headers)
    bad_timeout = client.post(
        "/api/cp/tasks",
        json={"agent_id": "agent-1", "type": "list_vds", "timeout_seconds": "abc"},
    )
    assert bad_timeout.status_code == 400
    bad_bool = client.post(
        "/api/cp/tasks",
        json={"agent_id": "agent-1", "type": "list_vds", "require_local_confirm": "yes"},
    )
    assert bad_bool.status_code == 400


def test_create_unknown_type_and_agent(client, register_agent, agent_headers):
    register_agent(agent_headers)
    unknown = client.post("/api/cp/tasks", json={"agent_id": "agent-1", "type": "nope"})
    assert unknown.status_code == 400
    missing = client.post("/api/cp/tasks", json={"agent_id": "ghost", "type": "list_vds"})
    assert missing.status_code == 404


def test_agents_list_and_detail(client, register_agent, agent_headers):
    register_agent(agent_headers)
    listing = client.get("/api/cp/agents")
    assert listing.status_code == 200
    assert listing.get_json()["total"] == 1

    detail = client.get("/api/cp/agents/agent-1")
    assert detail.status_code == 200
    agent = detail.get_json()["agent"]
    assert agent["hardware"]["model"] == "R740"
    assert "lsi:SNCTRL" in [item["controller_id"] for item in agent["capabilities"]["controllers"]]
    assert agent["task_counts"] == {}
    assert agent["recent_tasks"] == []

    assert client.get("/api/cp/agents/ghost").status_code == 404


def test_task_detail_events_and_evidence_download(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    task = create_task("agent-1", "list_vds")
    envelope = claim_task(agent_headers)
    lease = envelope["lease_id"]
    client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/started",
        json={"schema_version": 1, "agent_id": "agent-1", "lease_id": lease},
        headers=agent_headers,
    )
    blob = b'{"vds":[]}'
    sha = hashlib.sha256(blob).hexdigest()
    client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/evidence",
        data={
            "sha256": sha,
            "size": str(len(blob)),
            "kind": "json",
            "stage": "execute",
            "lease_id": lease,
            "file": (io.BytesIO(blob), "scan.json"),
        },
        headers=agent_headers,
        content_type="multipart/form-data",
    )
    result_body = {
        "task_id": task["task_id"],
        "lease_id": lease,
        "status": "finished",
        "result": {"vds": []},
        "evidence": {"status": "pending", "manifest": [{"filename": "scan.json"}]},
    }
    client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/result",
        json=result_body,
        headers={**agent_headers, "Idempotency-Key": f"{task['task_id']}:{lease}"},
    )

    detail = client.get(f"/api/cp/tasks/{task['task_id']}")
    assert detail.status_code == 200
    body = detail.get_json()["task"]
    assert body["status"] == "finished"
    kinds = [item["kind"] for item in body["events"]]
    assert "created" in kinds and "leased" in kinds and "started" in kinds
    assert "evidence" in kinds and "result" in kinds
    assert len(body["evidence"]) == 1
    assert body["evidence"][0]["download_url"].startswith("/api/cp/evidence/")

    download = client.get(body["evidence"][0]["download_url"])
    assert download.status_code == 200
    assert download.data == blob
    assert "attachment" in download.headers["Content-Disposition"]
    assert "scan.json" in download.headers["Content-Disposition"]


def test_cancel_pending_and_terminal(client, register_agent, agent_headers, create_task):
    register_agent(agent_headers)
    task = create_task("agent-1", "list_vds")
    response = client.post(f"/api/cp/tasks/{task['task_id']}/cancel", json={"reason": "test"})
    assert response.status_code == 200
    assert response.get_json()["status"] == "cancelled"
    detail = client.get(f"/api/cp/tasks/{task['task_id']}").get_json()["task"]
    assert detail["status"] == "cancelled"
    assert detail["cancel_requested"] is True
    again = client.post(f"/api/cp/tasks/{task['task_id']}/cancel", json={})
    assert again.status_code == 409


def test_cancel_running_sets_flag(client, register_agent, agent_headers, create_task, claim_task):
    register_agent(agent_headers)
    task = create_task("agent-1", "list_vds")
    envelope = claim_task(agent_headers)
    client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/started",
        json={"schema_version": 1, "agent_id": "agent-1", "lease_id": envelope["lease_id"]},
        headers=agent_headers,
    )
    response = client.post(f"/api/cp/tasks/{task['task_id']}/cancel", json={"reason": "stop"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "running"
    assert body["cancel_requested"] is True


def test_task_list_filters_and_pagination(client, register_agent, agent_headers, create_task):
    register_agent(agent_headers)
    first = create_task("agent-1", "list_vds")
    create_task("agent-1", "list_pds")

    by_type = client.get("/api/cp/tasks?type=list_vds").get_json()
    assert by_type["total"] == 1
    assert by_type["items"][0]["task_id"] == first["task_id"]

    paged = client.get("/api/cp/tasks?page=1&page_size=1").get_json()
    assert paged["total"] == 2
    assert len(paged["items"]) == 1

    by_query = client.get(f"/api/cp/tasks?q={first['task_id']}").get_json()
    assert by_query["total"] == 1

    by_agent = client.get("/api/cp/agents/agent-1/tasks").get_json()
    assert by_agent["total"] == 2


def test_stats_online_then_offline(settings, client, register_agent, agent_headers):
    register_agent(agent_headers)
    stats = client.get("/api/cp/stats").get_json()
    assert stats["agents"]["online"] == 1

    conn = connect(settings.db_path)
    try:
        conn.execute(
            "UPDATE agents SET last_heartbeat_at = ? WHERE agent_id = ?",
            ("2000-01-01T00:00:00Z", "agent-1"),
        )
        conn.commit()
    finally:
        conn.close()

    stats = client.get("/api/cp/stats").get_json()
    assert stats["agents"]["offline"] == 1
    assert stats["agents"]["online"] == 0
