"""Erase job must publish the report before it reports success."""

from __future__ import annotations

from types import SimpleNamespace
from typing import List

import pytest

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
    utc_now_iso,
)
from raidforge.core.report import UploadResult
from raidforge.core.serverid import resolve_server_id
from raidforge.ui.models import ReportState, ReportStatus, TaskItem, TaskKind, TaskStatus
from raidforge.ui.state import AppState
from raidforge.ui.tasks import TaskContext, erase_job


class FakeRunner:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.logs: List[str] = []
        self.broadcasts: List[object] = []
        self.submitted: List[TaskItem] = []

    def emit_task(self, task: TaskItem, log_line: str | None = None) -> None:
        if log_line:
            self.logs.append(log_line)

    def request_snapshot(self, name: str, epoch: int = 0) -> None:
        self.logs.append(f"snapshot:{name}")

    def _safe_broadcast(self, message: object) -> None:
        self.broadcasts.append(message)

    def submit(self, task: TaskItem, job) -> TaskItem:  # pragma: no cover - must not be called
        self.submitted.append(task)
        self.state.task_queue.append(task)
        return task


class FakeServices:
    def __init__(
        self,
        state: AppState,
        report_state: ReportState = ReportState.UPLOADED,
        submit_status: SubmitStatus = SubmitStatus.ASYNC_SUBMITTED,
    ) -> None:
        self.state = state
        self.report_state = report_state
        self.submit_status = submit_status
        self.calls: List[str] = []
        self.publish_called = False

    def execute_erase(self, plans, disks, on_progress=None, should_stop=None, epoch=0):
        self.calls.append("erase")
        results = [
            SubmitResult(
                disk_id="sda",
                kind="ssd_sanitize",
                status=self.submit_status,
                return_code=0 if self.submit_status is SubmitStatus.ASYNC_SUBMITTED else 1,
                duration_sec=0.1,
            )
        ]
        if on_progress:
            on_progress(1, 1, results[0])
        return results

    def refresh_disks(self, controller_index: int) -> None:
        self.calls.append("refresh")

    def publish_report(self, on_status=None) -> ReportStatus:
        self.calls.append("publish")
        self.publish_called = True
        if on_status:
            on_status("fake upload")
        return ReportStatus(
            state=self.report_state,
            channel="rsync",
            message="ok" if self.report_state is ReportState.UPLOADED else "upload failed",
        )


def _state(tmp_path) -> AppState:
    cfg = load_config(
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {"enabled": True},
            "tools": {"command_timeout": 5},
        }
    )
    cfg.dry_run = False
    sid = resolve_server_id(force_values={"system_serial": "SN-TASK-TEST"})
    return AppState(cfg=cfg, evidence=EvidenceSession(cfg, sid.serial), server_id=sid)


def _context(state: AppState, services: FakeServices) -> tuple[TaskContext, FakeRunner]:
    task = TaskItem(
        id="task-erase",
        kind=TaskKind.ERASE,
        title="Erase 1 disk",
        status=TaskStatus.RUNNING,
        done=1,
        total=1,
    )
    runner = FakeRunner(state)
    return TaskContext(runner, task), runner


def test_erase_job_publishes_real_report_before_returning(tmp_path, monkeypatch):
    pytest.importorskip("docx", reason="needs python-docx to build the report")
    state = _state(tmp_path)
    state.network_ready = True
    state.cfg.report.wait_for_route_seconds = 0
    state.cfg.report.run_upload_seconds = 2
    state.detection = DetectionResult(tool="sg3_utils", vendor=Vendor.GENERIC, controllers=[])
    disk = DiskInfo(
        disk_id="sda",
        controller_index=0,
        vendor=Vendor.GENERIC,
        medium=Medium.SSD,
        protocol=Protocol.UNKNOWN,
        device_node="/dev/sda",
        size_bytes=20_000_000_000,
    )
    state.inventory = [disk]
    uploads = []

    def fake_upload(cfg, session_dir, deadline=None, on_status=None, queue_on_failure=True, skip_route_wait=False):
        uploads.append((session_dir, queue_on_failure, skip_route_wait))
        if on_status:
            on_status("fake upload")
        return UploadResult("rsync", True, "fake upload")

    monkeypatch.setattr(publish_mod, "upload_session", fake_upload)

    class RealPublishServices(FakeServices):
        def execute_erase(self, plans, disks, on_progress=None, should_stop=None, epoch=0):
            self.calls.append("erase")
            plan = CommandPlan(
                disk_id="sda",
                kind="ssd_blockerase",
                label="SSD blockerase",
                argv=["blkdiscard", "--force", "/dev/sda"],
                medium=Medium.SSD,
                vendor=Vendor.GENERIC,
            )
            result = SubmitResult(
                disk_id="sda",
                kind=plan.kind,
                status=SubmitStatus.ASYNC_SUBMITTED,
                return_code=0,
                duration_sec=0.1,
            )
            self.state.erase_history.append(
                EraseRun(
                    index=1,
                    run_epoch=epoch,
                    started_at=utc_now_iso(),
                    finished_at=utc_now_iso(),
                    dry_run=False,
                    cancelled=False,
                    plans=[plan],
                    results=[result],
                    disks={"sda": disk},
                )
            )
            self.state.results = [result]
            if on_progress:
                on_progress(1, 1, result)
            return [result]

        def publish_report(self, on_status=None):
            self.calls.append("publish")
            self.publish_called = True
            from raidforge.ui.services import Services

            return Services(self.state).publish_report(on_status=on_status)

    services = RealPublishServices(state)
    context, runner = _context(state, services)
    status = erase_job(
        services,
        [SimpleNamespace(disk_id="sda")],
        {"sda": disk},
        epoch=1,
        controller_index=0,
    )(context)

    assert status is TaskStatus.SUCCEEDED
    assert services.calls == ["erase", "publish", "refresh"]
    assert uploads and uploads[0][1] is False
    assert state.report_status.state is ReportState.UPLOADED
    reports = list(state.evidence.root.glob("*-raidforge-report-*.docx"))
    assert reports, "real DOCX report was not written before erase_job returned"
    assert runner.submitted == []


def test_erase_job_publishes_report_before_returning(tmp_path):
    state = _state(tmp_path)
    services = FakeServices(state)
    context, runner = _context(state, services)

    status = erase_job(
        services,
        [SimpleNamespace(disk_id="sda")],
        {"sda": object()},
        epoch=1,
        controller_index=0,
    )(context)

    assert status is TaskStatus.SUCCEEDED
    assert services.calls == ["erase", "publish", "refresh"]
    assert services.publish_called is True
    assert state.report_status.state is ReportState.UPLOADED
    assert runner.submitted == []
    assert any("generating report" in line for line in runner.logs)


def test_erase_job_publishes_report_after_failed_submit(tmp_path):
    state = _state(tmp_path)
    services = FakeServices(state, submit_status=SubmitStatus.FAILED)
    context, runner = _context(state, services)

    status = erase_job(
        services,
        [SimpleNamespace(disk_id="sda")],
        {"sda": object()},
        epoch=1,
        controller_index=0,
    )(context)

    assert status is TaskStatus.FAILED
    assert services.publish_called is True
    assert services.calls == ["erase", "publish"]
    assert state.report_status.state is ReportState.UPLOADED
    assert runner.submitted == []


def test_erase_job_report_failure_does_not_fail_erase(tmp_path):
    state = _state(tmp_path)
    services = FakeServices(state, report_state=ReportState.FAILED)
    context, runner = _context(state, services)

    status = erase_job(
        services,
        [SimpleNamespace(disk_id="sda")],
        {"sda": object()},
        epoch=1,
        controller_index=0,
    )(context)

    assert status is TaskStatus.SUCCEEDED
    assert services.publish_called is True
    assert state.report_status.state is ReportState.FAILED
    assert any("warnings" in line for line in runner.logs)
