from __future__ import annotations

from raidforge.agent.client import AgentClient, Response
from raidforge.agent.config import AgentConfig
from raidforge.agent.models import EvidenceRecord

from agent_fakes import FakeClock, FakeTransport


def _config() -> AgentConfig:
    return AgentConfig(
        server_url="https://cp.example",
        api_prefix="/api/raidforge",
        token="rf_live_secret",
        agent_id="rf-SN123",
    )


def test_headers_are_exact_and_do_not_include_forbidden_fields():
    transport = FakeTransport([Response(status=200, body={"ok": True})])
    client = AgentClient(_config(), transport=transport, clock=FakeClock())
    client.heartbeat({"schema_version": 1})
    headers = transport.calls[0]["headers"]
    assert headers["X-Raidforge-Token"] == "rf_live_secret"
    assert headers["X-Raidforge-Agent-Id"] == "rf-SN123"
    assert headers["X-Raidforge-Protocol"] == "1"
    assert headers["Accept"] == "application/json"
    assert "Authorization" not in headers
    assert "raidforge-token" not in headers
    assert "X-Server-SN" not in headers
    assert "X-Agent-Signature" not in headers


def test_result_request_has_no_manual_content_type_and_has_idempotency_key():
    transport = FakeTransport([Response(status=200, body={"ok": True})])
    client = AgentClient(_config(), transport=transport, clock=FakeClock())
    client.result("tsk_01JB8Z4Q2M", {"lease_id": "lse_7f3c9a1b", "status": "finished"})
    call = transport.calls[0]
    assert call["headers"]["Idempotency-Key"] == "tsk_01JB8Z4Q2M:lse_7f3c9a1b"
    assert call["headers"]["Content-Type"] == "application/json"


def test_evidence_multipart_does_not_set_content_type_manually():
    transport = FakeTransport([Response(status=200, body={"ok": True})])
    client = AgentClient(_config(), transport=transport, clock=FakeClock())
    record = EvidenceRecord("cmd-001.json", 2, "a" * 64, "command_output", "application/json", "execute", "2026-11-04T10:00:00Z")
    client.evidence("tsk_01JB8Z4Q2M", "lse_7f3c9a1b", record, b"{}", manifest_index=0)
    call = transport.calls[0]
    assert "Content-Type" not in call["headers"]
    assert call["files"]["file"][0] == "cmd-001.json"
    assert call["data"]["lease_id"] == "lse_7f3c9a1b"


def test_retry_backoff_and_429_retry_after():
    clock = FakeClock()
    transport = FakeTransport(
        [
            Response(status=500, text="boom"),
            Response(status=429, headers={"retry-after": "7"}),
            Response(status=200, body={"ok": True}),
        ]
    )
    client = AgentClient(_config(), transport=transport, clock=clock)
    response = client.heartbeat({"schema_version": 1})
    assert response.ok
    assert len(transport.calls) == 3
    assert clock.sleeps[0] >= 1.0
    assert clock.sleeps[1] >= 7.0


def test_401_is_not_retried():
    transport = FakeTransport([Response(status=401, body={"ok": False, "error_code": "auth_failed"})])
    client = AgentClient(_config(), transport=transport, clock=FakeClock())
    response = client.heartbeat({"schema_version": 1})
    assert response.status == 401
    assert len(transport.calls) == 1


def test_token_scrubbed():
    client = AgentClient(_config(), transport=FakeTransport(), clock=FakeClock())
    assert client.scrub("token=rf_live_secret") == "token=***"
