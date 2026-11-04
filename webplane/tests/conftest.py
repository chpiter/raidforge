"""Shared fixtures for the webplane test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webplane.app import create_app  # noqa: E402
from webplane.config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, maintenance=False, agent_token="")


@pytest.fixture
def app(settings):
    application = create_app(settings)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def make_headers():
    def factory(agent_id: str = "agent-1", token: str = "") -> dict:
        headers = {
            "X-Raidforge-Agent-Id": agent_id,
            "X-Raidforge-Protocol": "1",
            "X-Raidforge-Agent-Version": "1.2.0",
        }
        if token:
            headers["X-Raidforge-Token"] = token
        return headers

    return factory


@pytest.fixture
def agent_headers(make_headers):
    return make_headers("agent-1")


@pytest.fixture
def register_agent(client):
    def factory(headers=None, server_sn: str = "SN-1", **overrides):
        headers = headers or {"X-Raidforge-Agent-Id": "agent-1", "X-Raidforge-Protocol": "1"}
        payload = {
            "schema_version": 1,
            "agent_id": headers.get("X-Raidforge-Agent-Id", "agent-1"),
            "agent_type": "raidforge",
            "agent_version": "1.2.0",
            "protocol_version": 1,
            "server_sn": server_sn,
            "hostname": "host-1",
            "status": "connected",
            "current_task": {},
            "queue_depth": 0,
            "queue_max": 4,
            "capabilities": {
                "task_types": [
                    "refresh_hardware",
                    "list_controllers",
                    "list_pds",
                    "list_vds",
                    "create_raid",
                    "delete_raid",
                    "clear_config",
                    "erase_disks",
                    "abort_current",
                ],
                "controllers": [
                    {
                        "controller_id": "lsi:SNCTRL",
                        "vendor": "lsi",
                        "model": "LSI 9300",
                        "serial": "SNCTRL",
                        "firmware": "1.0",
                        "tool": "storcli",
                    }
                ],
            },
            "hardware": {"vendor": "Dell", "model": "R740", "serial_number": server_sn},
            "boot_media": {"determined": True},
            "storage": {"free_bytes": 1024},
        }
        payload.update(overrides)
        response = client.post("/api/raidforge/agent/heartbeat", json=payload, headers=headers)
        assert response.status_code == 200, response.get_json()
        return payload

    return factory


@pytest.fixture
def create_task(client):
    def factory(agent_id: str = "agent-1", task_type: str = "list_vds", params=None, **extra):
        body = {"agent_id": agent_id, "type": task_type, "params": params or {}, **extra}
        response = client.post("/api/cp/tasks", json=body)
        assert response.status_code == 201, response.get_json()
        return response.get_json()["task"]

    return factory


@pytest.fixture
def claim_task(client):
    def factory(headers=None, server_sn: str = "SN-1"):
        headers = headers or {"X-Raidforge-Agent-Id": "agent-1", "X-Raidforge-Protocol": "1"}
        response = client.get(
            f"/api/raidforge/agent/tasks/next?server_sn={server_sn}", headers=headers
        )
        assert response.status_code == 200, response.get_json()
        return response.get_json()

    return factory
