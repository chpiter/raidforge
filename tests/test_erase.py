"""Tests for the serial execution layer (constraint 12).

No process is really spawned: ``core.erase.run_cmd`` is replaced, which is also
how the "accepted vs failed vs timeout" classification is exercised.
"""

from __future__ import annotations

from typing import List

import pytest

from raidforge.config import default_config
from raidforge.core import commands as cmd_mod
from raidforge.core import erase as erase_mod
from raidforge.core.evidence import EvidenceSession
from raidforge.core.models import (
    ControllerInfo,
    DiskInfo,
    Medium,
    Protocol,
    SubmitStatus,
    Vendor,
)
from raidforge.core.utils import CmdResult


def cfg_for(tmp_path):
    cfg = default_config()
    cfg.evidence.root = str(tmp_path)
    cfg.tools.command_timeout = 60
    return cfg


def make_disks(count: int = 3) -> List[DiskInfo]:
    return [
        DiskInfo(
            disk_id=f"c0e32s{i}",
            controller_index=0,
            vendor=Vendor.LSI,
            medium=Medium.HDD,
            protocol=Protocol.SAS,
            enclosure=32,
            slot=i,
        )
        for i in range(count)
    ]


def test_execute_plans_runs_serially_and_marks_submitted(tmp_path, monkeypatch):
    """Commands run one after another; rc 0 becomes ASYNC_SUBMITTED."""

    cfg = cfg_for(tmp_path)
    evidence = EvidenceSession(cfg, "SN-0", session_id="SN-0_TEST")
    plans = cmd_mod.plan_for_selection(make_disks(3), cfg)

    seen: List[list] = []
    timeouts: List[float] = []

    def fake_run(cmd, timeout=60.0, **kwargs):
        seen.append(list(cmd))
        timeouts.append(timeout)
        return CmdResult(0, "Status = Success", "", 0.5, list(cmd))

    monkeypatch.setattr(erase_mod, "run_cmd", fake_run)

    calls = []

    results = erase_mod.execute_plans(
        plans, evidence, cfg, on_progress=lambda d, t, r: calls.append((d, t, r.status))
    )
    assert len(results) == 3
    assert all(r.status is SubmitStatus.ASYNC_SUBMITTED for r in results)
    # Strictly serial: one invocation per plan, emitted in order.
    assert [len(c) for c in seen] == [5, 5, 5]
    assert [c[1] for c in seen] == ["/c0/e32/s0", "/c0/e32/s1", "/c0/e32/s2"]
    assert seen[0][-1] == "thorough"
    # Progress callback fires once per plan, with a growing counter.
    assert calls[-1][:2] == (3, 3)
    # Constraint 12: every call carries the configured 60 second budget.
    assert timeouts == [60.0, 60.0, 60.0]


def test_nonzero_return_code_is_failure(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    evidence = EvidenceSession(cfg, "SN-1", session_id="SN-1_TEST")
    plans = cmd_mod.plan_for_selection(make_disks(2), cfg)

    def fake_run(cmd, timeout=60.0, **kwargs):
        return CmdResult(1, "", "controller rejected the command", 0.2, list(cmd))

    monkeypatch.setattr(erase_mod, "run_cmd", fake_run)

    results = erase_mod.execute_plans(plans, evidence, cfg)
    assert all(r.status is SubmitStatus.FAILED for r in results)
    assert all(r.ok is False for r in results)


def test_timeout_is_recorded_not_raised(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    evidence = EvidenceSession(cfg, "SN-2", session_id="SN-2_TEST")
    plans = cmd_mod.plan_for_selection(make_disks(1), cfg)

    def fake_run(cmd, timeout=60.0, **kwargs):
        return CmdResult(124, "", "timeout after 60s", 60.0, list(cmd), timed_out=True)

    monkeypatch.setattr(erase_mod, "run_cmd", fake_run)

    results = erase_mod.execute_plans(plans, evidence, cfg)
    assert results[0].status is SubmitStatus.TIMEOUT
    assert results[0].return_code == 124


def test_dry_run_submits_nothing(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    cfg.dry_run = True
    evidence = EvidenceSession(cfg, "SN-3", session_id="SN-3_TEST")
    plans = cmd_mod.plan_for_selection(make_disks(2), cfg)

    def fake_run(cmd, timeout=60.0, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("dry-run must not spawn anything")

    monkeypatch.setattr(erase_mod, "run_cmd", fake_run)

    results = erase_mod.execute_plans(plans, evidence, cfg)
    assert all(r.status is SubmitStatus.SKIPPED for r in results)


def test_selection_gate(tmp_path):
    """Unknown media can never survive selection filtering."""

    disks = make_disks(2) + [
        DiskInfo(disk_id="c0e32s7", controller_index=0, vendor=Vendor.LSI, medium=Medium.UNKNOWN)
    ]
    assert erase_mod.selection_is_erasable(disks) is True
    assert [d.disk_id for d in erase_mod.rejected_disks(disks)] == ["c0e32s7"]

    only_unknown = disks[2:]
    assert erase_mod.selection_is_erasable(only_unknown) is False
    assert cmd_mod.plan_for_selection(only_unknown, default_config()) == []
