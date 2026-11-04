from __future__ import annotations

from types import SimpleNamespace

from raidforge.agent.bootmedia import BootMediaMap
from raidforge.agent.client import Response
from raidforge.agent.config import AgentConfig
from raidforge.agent.heartbeat import HeartbeatLoop
from raidforge.agent.models import ConnectionState
from raidforge.agent.snapshot import HostSnapshot

from agent_fakes import FakeClient, FakeClock, FakeStore


class _Snapshot:
    def __init__(self) -> None:
        self.boot = BootMediaMap(determined=True, disks=(), serials=frozenset({"x"}), wwns=frozenset({"0x" + "a" * 16}))
        self.snapshot = HostSnapshot(
            server_sn="SN12345678",
            hardware={"vendor": "Dell", "model": "R740", "serial_number": "SN12345678", "bios_version": "1"},
            controllers=(
                {
                    "controller_id": "lsi:SV1",
                    "vendor": "lsi",
                    "model": "9361",
                    "serial": "SV1",
                    "firmware": "1",
                    "tool": {"name": "storcli64", "path": "/usr/sbin/storcli64", "version": "7"},
                },
            ),
            pds=(),
            vds=(),
            capabilities={},
            boot_media=self.boot,
            storage={"evidence_dir": "/tmp", "free_bytes": 1},
            projection={},
            topology_hash="hash",
            collected_at="2026-11-04T10:00:00Z",
        )

    def capture(self, fresh: bool = False):
        return self.snapshot


class _Runtime:
    server_sn = "SN12345678"

    def __init__(self) -> None:
        self.events = []
        self.failures = []
        self.connection = ConnectionState.CONNECTING

    def heartbeat_interval(self):
        return 10

    def heartbeat_status(self):
        return "idle"

    def current_task_payload(self):
        return None

    def queue_depth(self):
        return 2

    def server_time_delta(self):
        return -2

    last_error = {"code": "", "at": "", "message": ""}

    def on_heartbeat_ok(self, body):
        self.events.append(("ok", body))
        self.connection = ConnectionState.CONNECTED

    def on_auth_failed(self, note):
        self.events.append(("auth", note))
        self.connection = ConnectionState.AUTH_FAILED

    def on_disabled(self, code):
        self.events.append(("disabled", code))
        self.connection = ConnectionState.DISABLED

    def on_protocol_too_old(self):
        self.events.append(("protocol",))
        self.connection = ConnectionState.PROTOCOL_TOO_OLD

    def on_maintenance(self):
        self.events.append(("maintenance",))

    def on_heartbeat_failure(self, count):
        self.failures.append(count)


def _loop(responses, runtime=None):
    cfg = AgentConfig(server_url="https://cp.example", token="rf_live_x", agent_id="rf-SN12345678")
    runtime = runtime or _Runtime()
    client = FakeClient()
    for response in responses:
        client.push("heartbeat", response)
    store = FakeStore()
    return HeartbeatLoop(client, _Snapshot(), store, runtime, cfg, FakeClock()), runtime, store


def test_payload_contains_required_sections():
    loop, _runtime, _store = _loop([])
    payload = loop.build_payload(_Snapshot().snapshot)
    for key in (
        "agent_id",
        "agent_type",
        "agent_version",
        "protocol_version",
        "server_sn",
        "status",
        "current_task",
        "queue_depth",
        "capabilities",
        "hardware",
        "boot_media",
        "storage",
        "last_error",
        "server_time_delta_s",
    ):
        assert key in payload
    assert payload["queue_max"] == 4
    assert payload["capabilities"]["controllers"][0]["controller_id"] == "lsi:SV1"


def test_success_marks_heartbeat():
    loop, runtime, store = _loop([Response(status=200, body={"ok": True, "enabled": True, "protocol": {"min": 1}})])
    assert loop.beat_once() is True
    assert store.heartbeats
    assert runtime.events[0][0] == "ok"


def test_auth_disabled_protocol_and_maintenance():
    loop, runtime, _ = _loop([Response(status=401)])
    assert loop.beat_once() is False
    assert runtime.events[-1][0] == "auth"

    loop, runtime, _ = _loop([Response(status=403, body={"error_code": "agent_disabled"})])
    loop.beat_once()
    assert runtime.events[-1] == ("disabled", "agent_disabled")

    loop, runtime, _ = _loop([Response(status=426)])
    loop.beat_once()
    assert runtime.events[-1][0] == "protocol"

    loop, runtime, _ = _loop([Response(status=503)])
    loop.beat_once()
    assert runtime.events[-1][0] == "maintenance"


def test_six_failures_reported():
    loop, runtime, _ = _loop([Response(status=500)] * 6)
    for _ in range(6):
        loop.beat_once()
    assert runtime.failures == [1, 2, 3, 4, 5, 6]
