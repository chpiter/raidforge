from __future__ import annotations

import json

from webplane.db import connect
from webplane.services import topology

PDS = [
    {
        "controller_id": "lsi:SNCTRL",
        "serial": "S1",
        "wwn": "0x5000c500a4a2b001",
        "expected_size_bytes": 1000,
        "media_type": "hdd",
        "state": "Unconfigured Good",
        "raid_member_of": "",
        "boot_protected": False,
    },
    {
        "controller_id": "lsi:SNCTRL",
        "serial": "S2",
        "wwn": "0x5000c500a4a2b002",
        "expected_size_bytes": 1000,
        "media_type": "ssd",
        "state": "Online",
        "raid_member_of": "0",
        "boot_protected": False,
    },
]

VDS = [
    {
        "controller_id": "lsi:SNCTRL",
        "controller_index": 0,
        "vd_id": "0",
        "vd_name": "vd0",
        "raid_level": "RAID1",
        "size_bytes": 2000,
        "state": "Optimal",
        "member_disk_ids": ["d1", "d2"],
        "member_serials": ["S1", "S2"],
        "spans": [["S1", "S2"]],
        "disk_group": "",
    }
]


def _run_result(client, headers, task, envelope, result, status="finished"):
    payload = {
        "task_id": task["task_id"],
        "lease_id": envelope["lease_id"],
        "status": status,
        "result": result,
        "evidence": {"status": "complete", "manifest": []},
    }
    response = client.post(
        f"/api/raidforge/agent/tasks/{task['task_id']}/result",
        json=payload,
        headers={
            **headers,
            "Idempotency-Key": f"{task['task_id']}:{envelope['lease_id']}",
        },
    )
    assert response.status_code == 200, response.get_json()
    return response


def _cache_pds(client, headers, create_task, claim_task, pds=None):
    task = create_task("agent-1", "list_pds")
    envelope = claim_task(headers)
    _run_result(
        client,
        headers,
        task,
        envelope,
        {"pds": PDS if pds is None else pds, "topology_hash": "hash-pds"},
    )
    return task


def _cache_vds(client, headers, create_task, claim_task, vds=None):
    task = create_task("agent-1", "list_vds")
    envelope = claim_task(headers)
    _run_result(
        client,
        headers,
        task,
        envelope,
        {"vds": VDS if vds is None else vds, "topology_hash": "hash-vds"},
    )
    return task


def test_list_pds_caches_per_controller(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    _cache_pds(client, agent_headers, create_task, claim_task)

    listing = client.get("/api/cp/agents/agent-1/controllers")
    assert listing.status_code == 200
    controllers = listing.get_json()["controllers"]
    assert len(controllers) == 1
    item = controllers[0]
    assert item["controller_id"] == "lsi:SNCTRL"
    assert item["pd_count"] == 2
    assert item["vd_count"] == 0
    assert item["has_pds"] is True
    assert item["has_vds"] is False
    assert item["known"] is True

    detail = client.get("/api/cp/agents/agent-1/controllers/lsi:SNCTRL")
    assert detail.status_code == 200
    body = detail.get_json()
    assert len(body["pds"]) == 2
    assert body["vds"] == []
    assert body["has_pds"] is True
    assert body["has_vds"] is False


def test_vds_ingest_keeps_pds(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    _cache_pds(client, agent_headers, create_task, claim_task)
    first = client.get("/api/cp/agents/agent-1/controllers/lsi:SNCTRL").get_json()
    pds_at = first["pds_collected_at"]

    _cache_vds(client, agent_headers, create_task, claim_task)
    detail = client.get("/api/cp/agents/agent-1/controllers/lsi:SNCTRL").get_json()
    assert len(detail["pds"]) == 2
    assert len(detail["vds"]) == 1
    assert detail["pds_collected_at"] == pds_at
    assert detail["vds_collected_at"]


def test_empty_vds_marks_collection_for_requested_controller(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    _cache_pds(client, agent_headers, create_task, claim_task)

    task = create_task(
        "agent-1", "list_vds", params={"controller_ids": ["lsi:SNCTRL"]}
    )
    envelope = claim_task(agent_headers)
    _run_result(
        client,
        agent_headers,
        task,
        envelope,
        {"vds": [], "topology_hash": "hash-empty"},
    )

    detail = client.get("/api/cp/agents/agent-1/controllers/lsi:SNCTRL").get_json()
    assert detail["vds"] == []
    assert detail["controller"]["vd_count"] == 0
    assert detail["vds_collected_at"]
    assert detail["has_vds"] is True
    assert len(detail["pds"]) == 2


def test_failed_result_does_not_overwrite_cache(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    _cache_pds(client, agent_headers, create_task, claim_task)

    task = create_task("agent-1", "list_pds")
    envelope = claim_task(agent_headers)
    _run_result(
        client,
        agent_headers,
        task,
        envelope,
        {"pds": [{**PDS[0], "serial": "REPLACED"}]},
        status="failed",
    )

    detail = client.get("/api/cp/agents/agent-1/controllers/lsi:SNCTRL").get_json()
    assert len(detail["pds"]) == 2
    assert {item["serial"] for item in detail["pds"]} == {"S1", "S2"}


def test_malformed_result_is_tolerated(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    for malformed in ("not-a-dict", {"pds": "nope"}, {"pds": [1, None, {"serial": "S1"}]}):
        task = create_task("agent-1", "list_pds")
        envelope = claim_task(agent_headers)
        response = _run_result(client, agent_headers, task, envelope, malformed)
        assert response.status_code == 200

    detail = client.get("/api/cp/agents/agent-1/controllers/lsi:SNCTRL").get_json()
    assert detail["pds"] == []
    assert detail["has_pds"] is False


def test_controller_list_merges_heartbeat(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(
        agent_headers,
        capabilities={
            "task_types": ["list_pds"],
            "controllers": [
                {
                    "controller_id": "lsi:SNCTRL",
                    "vendor": "lsi",
                    "model": "LSI 9300",
                    "serial": "SNCTRL",
                    "firmware": "1.0",
                    "index": 0,
                    "tool": "storcli",
                },
                {
                    "controller_id": "hpe:SNHPE",
                    "vendor": "hpe",
                    "model": "Smart Array",
                    "serial": "SNHPE",
                    "firmware": "2.0",
                    "index": 1,
                    "tool": {"name": "ssacli"},
                },
            ],
        },
    )
    _cache_pds(client, agent_headers, create_task, claim_task)

    controllers = client.get("/api/cp/agents/agent-1/controllers").get_json()["controllers"]
    assert [item["controller_id"] for item in controllers] == ["lsi:SNCTRL", "hpe:SNHPE"]
    assert controllers[0]["pd_count"] == 2
    assert controllers[0]["has_pds"] is True
    assert controllers[1]["pd_count"] == 0
    assert controllers[1]["has_pds"] is False
    assert controllers[1]["tool"] == "ssacli"
    assert all(item["known"] is True for item in controllers)


def test_unknown_and_snapshot_only_controller(
    settings, client, register_agent, agent_headers
):
    register_agent(agent_headers)
    assert client.get("/api/cp/agents/agent-1/controllers/ghost").status_code == 404

    conn = connect(settings.db_path)
    try:
        topology.ingest_result(
            conn,
            "agent-1",
            "list_pds",
            {
                "result": {
                    "pds": [
                        {
                            "controller_id": "lsi:OTHER",
                            "serial": "X1",
                            "wwn": "0x5000c500a4a2b009",
                            "expected_size_bytes": 1,
                        }
                    ]
                }
            },
        )
    finally:
        conn.close()

    controllers = client.get("/api/cp/agents/agent-1/controllers").get_json()["controllers"]
    other = next(item for item in controllers if item["controller_id"] == "lsi:OTHER")
    assert other["known"] is False
    detail = client.get("/api/cp/agents/agent-1/controllers/lsi:OTHER")
    assert detail.status_code == 200
    assert detail.get_json()["controller"]["known"] is False


def test_duplicate_result_is_idempotent(
    client, register_agent, agent_headers, create_task, claim_task
):
    register_agent(agent_headers)
    task = create_task("agent-1", "list_pds")
    envelope = claim_task(agent_headers)
    first = _run_result(client, agent_headers, task, envelope, {"pds": PDS})
    second = _run_result(client, agent_headers, task, envelope, {"pds": PDS})
    assert first.get_json()["duplicate"] is False
    assert second.get_json()["duplicate"] is True
    detail = client.get("/api/cp/agents/agent-1/controllers/lsi:SNCTRL").get_json()
    assert len(detail["pds"]) == 2


def test_ingest_sanitises_long_and_negative_values(settings, register_agent, agent_headers):
    register_agent(agent_headers)
    conn = connect(settings.db_path)
    try:
        topology.ingest_result(
            conn,
            "agent-1",
            "list_pds",
            {
                "result": {
                    "pds": [
                        {
                            "controller_id": "lsi:SNCTRL",
                            "serial": "x" * 2000,
                            "wwn": "w" * 2000,
                            "expected_size_bytes": -99,
                            "state": "s" * 2000,
                        }
                    ]
                }
            },
        )
        row = conn.execute(
            "SELECT pds_json FROM controller_snapshots WHERE agent_id=? AND controller_id=?",
            ("agent-1", "lsi:SNCTRL"),
        ).fetchone()
    finally:
        conn.close()
    items = json.loads(row["pds_json"])
    assert len(items) == 1
    assert len(items[0]["serial"]) <= 512
    assert len(items[0]["wwn"]) <= 512
    assert items[0]["expected_size_bytes"] == 0
