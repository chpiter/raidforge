from __future__ import annotations

import threading
from pathlib import Path

from raidforge.agent import executor
from raidforge.agent.adapters import ErasePlan
from raidforge.agent.bootmedia import BootMediaMap
from raidforge.agent.clock import SystemClock
from raidforge.agent.config import AgentConfig
from raidforge.agent.models import AgentLimits, ConfirmDecision
from raidforge.agent.state import StateStore
from raidforge.agent.snapshot import HostSnapshot
from raidforge.config import default_config
from raidforge.core.models import CommandPlan, DiskInfo, Medium, SubmitResult, SubmitStatus, Vendor

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


def _erase_plan() -> ErasePlan:
    disk = DiskInfo(
        disk_id="c0e32s4",
        controller_index=0,
        vendor=Vendor.LSI,
        medium=Medium.HDD,
        serial="ZA1",
        wwn="0x5000c500a1b2c3d4",
        size_bytes=100,
    )
    plan = CommandPlan(
        disk_id=disk.disk_id,
        kind="hdd_overwrite",
        label="HDD overwrite",
        argv=["storcli64", "/c0/e32/s4", "start", "erase", "normal"],
        medium=Medium.HDD,
        vendor=Vendor.LSI,
    )
    return ErasePlan(controller=None, disks=[disk], plans=[plan], passes=1, eta_seconds=600)


def _task():
    return parse_task(
        make_envelope(
            task_type="erase_disks",
            params={
                "controller_id": "lsi:SN1",
                "mode": "overwrite",
                "verify": False,
                "pds": [
                    {
                        "serial": "ZA1",
                        "wwn": "0x5000c500a1b2c3d4",
                        "expected_size_bytes": 100,
                    }
                ],
            },
        )
    )


def test_confirm_rejection_means_zero_writes(tmp_path: Path, monkeypatch):
    runtime = _Runtime(tmp_path, _snapshot(), ConfirmDecision(False, "alice", "confirm_rejected"))
    monkeypatch.setattr(executor, "_prepare", lambda runtime, task, snapshot: _erase_plan())
    called = []
    monkeypatch.setattr(executor, "execute_plans", lambda *args, **kwargs: called.append(1))

    class _Ctx:
        def log(self, line):
            pass

        def progress(self, *args, **kwargs):
            pass

        def cancelled(self):
            return False

    result = executor.execute(_Ctx(), runtime, _task())
    assert result.status.value == "failed"
    assert result.error_code == "confirm_rejected"
    assert called == []
    assert not [call for call in runtime.client.calls if call[0] == "started"]
    assert result.confirm["confirmed"] is False


def test_approved_erase_reports_result_and_evidence(tmp_path: Path, monkeypatch):
    runtime = _Runtime(tmp_path, _snapshot(), ConfirmDecision(True, "alice", ""))
    monkeypatch.setattr(executor, "_prepare", lambda runtime, task, snapshot: _erase_plan())
    monkeypatch.setattr(executor, "_verify", lambda plan, snapshot, task: (True, {}))

    def fake_execute(plans, evidence, cfg, on_progress=None, should_stop=None):
        result = SubmitResult(
            disk_id=plans[0].disk_id,
            kind=plans[0].kind,
            status=SubmitStatus.ASYNC_SUBMITTED,
            return_code=0,
            duration_sec=0.1,
            argv=plans[0].argv,
            stdout="submitted",
        )
        if on_progress:
            on_progress(1, 1, result)
        return [result]

    monkeypatch.setattr(executor, "execute_plans", fake_execute)

    class _Ctx:
        def log(self, line):
            pass

        def progress(self, *args, **kwargs):
            pass

        def cancelled(self):
            return False

    result = executor.execute(_Ctx(), runtime, _task())
    assert result.status.value == "finished"
    assert len(result.commands) == 1
    assert result.commands[0].exit_code == 0
    assert any(call[0] == "started" for call in runtime.client.calls)
    assert any(call[0] == "progress" for call in runtime.client.calls)
    manifest = [record.filename for record in result.evidence_manifest]
    assert "cmd-001.json" in manifest
    assert "confirm.json" in manifest


def test_started_lease_superseded_drops_locally(tmp_path: Path, monkeypatch):
    from raidforge.agent.client import Response

    runtime = _Runtime(tmp_path, _snapshot(), ConfirmDecision(True, "alice", ""))
    runtime.client.push("started", Response(status=409, body={"ok": False, "error_code": "lease_superseded"}))
    monkeypatch.setattr(executor, "_prepare", lambda runtime, task, snapshot: _erase_plan())
    monkeypatch.setattr(executor, "execute_plans", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no writes")))

    class _Ctx:
        def log(self, line):
            pass

        def progress(self, *args, **kwargs):
            pass

        def cancelled(self):
            return False

    assert executor.execute(_Ctx(), runtime, _task()) is None
    assert runtime.store.load().current_task is None


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
