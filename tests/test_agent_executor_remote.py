from __future__ import annotations

import threading
from pathlib import Path

from raidforge.agent import executor
from raidforge.agent.bootmedia import BootMediaMap
from raidforge.agent.clock import SystemClock
from raidforge.agent.config import AgentConfig
from raidforge.agent.models import AgentLimits, ConfirmDecision
from raidforge.agent.state import StateStore
from raidforge.agent.snapshot import HostSnapshot
from raidforge.config import default_config

from agent_fakes import FakeAdmission, FakeClient, FakeClock, make_envelope
from raidforge.agent.validation import parse_task


class _Runtime:
    def __init__(self, tmp_path: Path, snapshot: HostSnapshot, decision: ConfirmDecision) -> None:
        self.clock = FakeClock()
        self.cfg = default_config()
        self.agent_cfg = AgentConfig(
            server_url="https://cp.example",
            token="rf_live_x",
            agent_id="rf-SN12345678",
            evidence_dir=tmp_path / "evidence",
            state_dir=tmp_path / "state",
            log_dir=tmp_path / "log",
            confirm_timeout_seconds=60,
        )
        self.agent_id = "rf-SN12345678"
        self.server_sn = "SN12345678"
        self.store = StateStore(tmp_path / "state", clock=self.clock)
        self.client = FakeClient()
        self.limits = AgentLimits()
        self.stopping = threading.Event()
        self.admission = FakeAdmission()
        self.reporter = None
        self._snapshot = snapshot
        self._decision = decision
        self.confirm_request = None

    def capture_snapshot(self, fresh: bool = False):
        return self._snapshot

    def request_confirm(self, request):
        self.confirm_request = request
        return self._decision

    def default_operator(self):
        return "tester"


def _snapshot() -> HostSnapshot:
    boot = BootMediaMap(determined=True, disks=(), serials=frozenset({"x"}), wwns=frozenset({"0x" + "a" * 16}))
    return HostSnapshot(
        server_sn="SN12345678",
        hardware={"vendor": "Dell", "model": "R740", "serial_number": "SN12345678", "bios_version": "1"},
        controllers=(),
        pds=(),
        vds=(),
        capabilities={},
        boot_media=boot,
        storage={"evidence_dir": "", "free_bytes": 10**12},
        projection={},
        topology_hash="hash",
        collected_at="2026-11-04T10:00:00Z",
    )


def test_read_task_reports_finished(tmp_path: Path):
    runtime = _Runtime(tmp_path, _snapshot(), ConfirmDecision(True, "alice", ""))
    task = parse_task(make_envelope())

    class _Ctx:
        def log(self, line):
            pass

        def progress(self, *args, **kwargs):
            pass

        def cancelled(self):
            return False

    result = executor.execute(_Ctx(), runtime, task)
    assert result.status.value == "finished"
    assert result.result["topology_hash"] == "hash"
