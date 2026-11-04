from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from raidforge.agent.config import AgentConfig
from raidforge.agent.models import ConnectionState
from raidforge.agent.runtime import AgentRuntime


def _runtime() -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.agent_cfg = AgentConfig(
        server_url="http://old.example:8080", api_prefix="/api/raidforge"
    )
    runtime.connection = ConnectionState.CONNECTED
    runtime.note = ""
    runtime.last_error = {"code": "x", "at": "", "message": "x"}
    runtime.stopping = threading.Event()
    runtime._supervisor = object()
    runtime._pending_webplane = None
    runtime._heartbeat = SimpleNamespace(wake=lambda: None)
    runtime._poller = SimpleNamespace(wake=lambda: None)
    runtime.state = SimpleNamespace(evidence=SimpleNamespace(emit=lambda *a, **k: None))
    runtime.emit_status = lambda: None  # type: ignore[assignment]
    return runtime


def test_apply_webplane_mutates_the_shared_agent_config():
    runtime = _runtime()
    runtime.apply_webplane("http://new.example:9090/", "/api/raidforge/")
    assert runtime.agent_cfg.server_url == "http://new.example:9090"
    assert runtime.agent_cfg.base_url == "http://new.example:9090/api/raidforge"
    assert runtime.connection is ConnectionState.CONNECTING
    assert runtime.note == "控制面地址已更新"


def test_apply_webplane_rejects_invalid_url():
    runtime = _runtime()
    with pytest.raises(ValueError):
        runtime.apply_webplane("not a url")
    assert runtime.agent_cfg.server_url == "http://old.example:8080"


def test_request_webplane_defers_while_a_task_is_active():
    runtime = _runtime()
    runtime.active_task = lambda: object()  # type: ignore[assignment]
    assert runtime.request_webplane("http://new.example:9090") is False
    assert runtime.agent_cfg.server_url == "http://old.example:8080"
    assert runtime._pending_webplane == (
        "http://new.example:9090",
        "/api/raidforge",
    )
    assert runtime.note == "控制面变更等待任务结束"
