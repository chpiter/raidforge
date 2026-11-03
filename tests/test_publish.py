"""Core publish path: cumulative report, manifest ordering, upload policy."""

from __future__ import annotations

import json
from pathlib import Path

from raidforge.config import load_config
from raidforge.core import publish as publish_mod
from raidforge.core.evidence import EvidenceSession
from raidforge.core.models import (
    CommandPlan,
    DetectionResult,
    DiskInfo,
    EraseRun,
    Medium,
    Protocol,
    SubmitResult,
    SubmitStatus,
    Vendor,
)
from raidforge.core.publish import PublishOptions, publish_from_context
from raidforge.core.report import UploadResult
from raidforge.core.serverid import resolve_server_id
from raidforge.ui.state import AppContext


def _disk(disk_id: str = "c0e32s0") -> DiskInfo:
    return DiskInfo(
        disk_id=disk_id,
        controller_index=0,
        vendor=Vendor.LSI,
        medium=Medium.HDD,
        protocol=Protocol.SAS,
        enclosure=32,
        slot=0,
        model="TESTMODEL",
        serial="ZTN1",
        firmware="SN02",
        size_bytes=2000398934016,
        raid_state="Onln",
    )


def _run(index: int, epoch: int, disk: DiskInfo) -> EraseRun:
    plan = CommandPlan(
        disk_id=disk.disk_id,
        kind="hdd_thorough",
        label="HDD thorough",
        argv=["storcli", "/c0/e32/s0", "start", "erase", "thorough"],
        medium=disk.medium,
        vendor=disk.vendor,
    )
    result = SubmitResult(
        disk_id=disk.disk_id,
        kind=plan.kind,
        status=SubmitStatus.ASYNC_SUBMITTED,
        return_code=0,
        duration_sec=1.0,
        stdout="submitted",
        argv=plan.argv,
    )
    return EraseRun(
        index=index,
        run_epoch=epoch,
        started_at="2026-11-03T01:00:00Z",
        finished_at="2026-11-03T01:00:01Z",
        dry_run=False,
        cancelled=False,
        plans=[plan],
        results=[result],
        disks={disk.disk_id: disk},
    )


def _context(tmp_path) -> AppContext:
    cfg = load_config(
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {"enabled": True, "wait_for_route_seconds": 0, "run_upload_seconds": 2},
            "tools": {"command_timeout": 5},
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SNPUB01"})
    ev = EvidenceSession(cfg, sid.serial, session_id="SNPUB01_20261103T010000Z")
    ctx = AppContext(cfg=cfg, evidence=ev, server_id=sid)
    ctx.detection = DetectionResult(tool="storcli", vendor=Vendor.LSI, controllers=[])
    return ctx


def _fake_builder(monkeypatch):
    calls = []

    def build(**kwargs):
        calls.append(kwargs)
        path = Path(kwargs["out_dir"]) / "SNPUB01-raidforge-report-20261103.docx"
        path.write_bytes(b"fake docx " + str(len(kwargs["runs"])).encode())
        return path

    monkeypatch.setattr(publish_mod, "build_erase_report", build)
    return calls


class _Uploads:
    def __init__(self, ok=True, queued=False):
        self.ok = ok
        self.queued = queued
        self.calls = []

    def __call__(self, cfg, root, deadline=None, on_status=None, **kwargs):
        self.calls.append({"root": root, "deadline": deadline, **kwargs})
        if on_status:
            on_status("fake upload")
        return UploadResult(
            "rsync" if self.ok else "pending",
            self.ok,
            "fake upload",
            queued=self.queued,
        )


def _events(ctx) -> list:
    return [
        json.loads(line)
        for line in ctx.evidence.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_run_publish_builds_cumulative_manifest_without_queueing(tmp_path, monkeypatch):
    builder = _fake_builder(monkeypatch)
    uploads = _Uploads(ok=True)
    monkeypatch.setattr(publish_mod, "upload_session", uploads)

    ctx = _context(tmp_path)
    disk = _disk()
    ctx.inventory = [disk]
    ctx.erase_history = [_run(1, 1, disk), _run(2, 2, disk)]

    outcome = publish_from_context(
        ctx,
        PublishOptions(
            phase="run",
            upload=True,
            queue_on_failure=False,
            budget_seconds=2,
            skip_route_wait=True,
        ),
    )

    assert outcome.error == ""
    assert builder and len(builder[0]["runs"]) == 2
    assert outcome.manifest["report_file"] == "SNPUB01-raidforge-report-20261103.docx"
    assert len(outcome.manifest["erase_runs"]) == 2
    assert outcome.manifest["tool"] == "storcli"
    assert outcome.manifest["disk_count"] == 1
    assert outcome.manifest["last_upload"]["ok"] is True
    assert uploads.calls[0]["queue_on_failure"] is False
    assert uploads.calls[0]["skip_route_wait"] is True
    assert ctx.publish_state.generation == 1
    assert _events(ctx)[-1]["event"] == "upload"
    assert _events(ctx)[-1]["phase"] == "run"


def test_run_failure_never_queues_and_exit_can_queue(tmp_path, monkeypatch):
    _fake_builder(monkeypatch)
    uploads = _Uploads(ok=False, queued=False)
    monkeypatch.setattr(publish_mod, "upload_session", uploads)

    ctx = _context(tmp_path)
    ctx.inventory = [_disk()]
    ctx.erase_history = [_run(1, 1, ctx.inventory[0])]

    run_outcome = publish_from_context(
        ctx,
        PublishOptions(phase="run", upload=True, queue_on_failure=False),
    )
    assert run_outcome.upload is not None
    assert run_outcome.upload.queued is False
    assert uploads.calls[0]["queue_on_failure"] is False
    assert not (Path(ctx.cfg.report.pending_root)).exists()

    exit_outcome = publish_from_context(
        ctx,
        PublishOptions(phase="exit", upload=True, queue_on_failure=True),
    )
    assert uploads.calls[-1]["queue_on_failure"] is True
    assert exit_outcome.upload is not None


def test_publish_cleans_stale_temp_report(tmp_path, monkeypatch):
    _fake_builder(monkeypatch)
    monkeypatch.setattr(publish_mod, "upload_session", _Uploads(ok=True))

    ctx = _context(tmp_path)
    stale = ctx.evidence.root / ".raidforge-report-killed.docx"
    stale.write_bytes(b"partial")
    ctx.inventory = [_disk()]
    ctx.erase_history = [_run(1, 1, ctx.inventory[0])]

    outcome = publish_from_context(ctx, PublishOptions(phase="run", upload=False))
    assert not stale.exists()
    paths = {entry["path"] for entry in outcome.manifest["files"]}
    assert ".raidforge-report-killed.docx" not in paths


def test_parent_publish_inherits_child_facts(tmp_path, monkeypatch):
    _fake_builder(monkeypatch)
    uploads = _Uploads(ok=True)
    monkeypatch.setattr(publish_mod, "upload_session", uploads)

    child = _context(tmp_path)
    child.inventory = [_disk(), _disk("c0e32s1")]
    child.erase_history = [_run(1, 1, child.inventory[0])]
    child_outcome = publish_from_context(
        child, PublishOptions(phase="run", upload=True, queue_on_failure=False)
    )
    assert child_outcome.manifest["report_file"]

    cfg = child.cfg
    sid = child.server_id
    parent_ev = EvidenceSession(cfg, sid.serial, session_id=child.evidence.session_id)
    parent = AppContext(cfg=cfg, evidence=parent_ev, server_id=sid)
    parent_outcome = publish_from_context(
        parent, PublishOptions(phase="exit", upload=False, queue_on_failure=True)
    )

    for key in ("report_file", "erase_runs", "inventory", "submissions", "tool", "disk_count"):
        assert parent_outcome.manifest[key] == child_outcome.manifest[key], key
