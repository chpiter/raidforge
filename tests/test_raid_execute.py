"""Serial RAID execution: failure continuation, dry-run, timeout and cancel."""

from __future__ import annotations

from raidforge.config import default_config
from raidforge.core.models import RaidAction, RaidCommandPlan, RaidStatus, Vendor
from raidforge.core.raid import execute as execute_mod
from raidforge.core.raid.execute import execute_raid_plans
from raidforge.core.utils import CmdResult


class FakeEvidence:
    def __init__(self):
        self.events = []

    def emit(self, event, **payload):
        self.events.append((event, payload))
        return {"event": event, **payload}


def plan(index: int) -> RaidCommandPlan:
    return RaidCommandPlan(
        action=RaidAction.CREATE,
        controller_index=0,
        vendor=Vendor.LSI,
        label=f"create {index}",
        argv=["storcli", f"/c0/v{index}", "delete", "force"],
        level="RAID1",
        disk_ids=[f"c0e32s{index}"],
    )


def test_serial_order_and_failure_continues(monkeypatch):
    calls = []

    def fake_run(argv, timeout=60.0, **kwargs):
        calls.append((list(argv), timeout))
        return CmdResult(0 if len(calls) != 2 else 1, "", "boom" if len(calls) == 2 else "", 0.01, list(argv))

    monkeypatch.setattr(execute_mod, "run_cmd", fake_run)
    cfg = default_config()
    evidence = FakeEvidence()
    progress = []
    results = execute_raid_plans(
        [plan(0), plan(1), plan(2)],
        evidence,
        cfg,
        on_progress=lambda done, total, result: progress.append((done, total, result.status)),
    )
    assert [result.status for result in results] == [
        RaidStatus.ACCEPTED,
        RaidStatus.FAILED,
        RaidStatus.ACCEPTED,
    ]
    assert [call[0][1] for call in calls] == ["/c0/v0", "/c0/v1", "/c0/v2"]
    assert all(call[1] == cfg.raid.command_timeout for call in calls)
    assert progress == [(1, 3, RaidStatus.ACCEPTED), (2, 3, RaidStatus.FAILED), (3, 3, RaidStatus.ACCEPTED)]
    names = [event for event, _payload in evidence.events]
    assert names[0] == "raid_run_started"
    assert names[-1] == "raid_run_finished"


def test_timeout_is_reported(monkeypatch):
    def fake_run(argv, timeout=60.0, **kwargs):
        return CmdResult(124, "", "timeout", 120.0, list(argv), timed_out=True)

    monkeypatch.setattr(execute_mod, "run_cmd", fake_run)
    results = execute_raid_plans([plan(0)], FakeEvidence(), default_config())
    assert results[0].status is RaidStatus.TIMEOUT


def test_dry_run_submits_nothing(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise AssertionError("dry run must not execute")

    monkeypatch.setattr(execute_mod, "run_cmd", fake_run)
    cfg = default_config()
    cfg.dry_run = True
    results = execute_raid_plans([plan(0), plan(1)], FakeEvidence(), cfg)
    assert [result.status for result in results] == [RaidStatus.SKIPPED, RaidStatus.SKIPPED]


def test_cancel_stops_remaining_plans(monkeypatch):
    calls = []
    stop = False

    def fake_run(argv, timeout=60.0, **kwargs):
        nonlocal stop
        calls.append(list(argv))
        stop = True
        return CmdResult(0, "", "", 0.01, list(argv))

    monkeypatch.setattr(execute_mod, "run_cmd", fake_run)
    evidence = FakeEvidence()
    results = execute_raid_plans(
        [plan(0), plan(1), plan(2)],
        evidence,
        default_config(),
        should_stop=lambda: stop,
    )
    assert len(results) == 1
    assert len(calls) == 1
    names = [event for event, _payload in evidence.events]
    assert "raid_run_cancelled" in names


def test_status_counts_record_missing_plans():
    from raidforge.core.models import RaidActionRun

    run = RaidActionRun(
        index=1,
        epoch=1,
        action=RaidAction.CREATE,
        started_at="now",
        finished_at="now",
        dry_run=False,
        cancelled=True,
        controller_index=0,
        plans=[plan(0), plan(1)],
        results=[
            execute_mod.RaidActionResult(
                action=RaidAction.CREATE,
                controller_index=0,
                status=RaidStatus.ACCEPTED,
                return_code=0,
                duration_sec=0.1,
                label="create 0",
            )
        ],
    )
    counts = run.status_counts()
    assert counts["accepted"] == 1
    assert counts["not_attempted"] == 1
