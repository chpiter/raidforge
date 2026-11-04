"""Serial task queue for erase/RAID/report/refresh work.

The queue is deliberately UI-level.  Hardware execution still goes through the
existing ``execute_plans`` / ``execute_raid_plans`` functions on daemon threads;
there is no polling loop and no background timer.
"""

from __future__ import annotations

import queue
import threading
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional

from ..core.models import (
    CommandPlan,
    DiskInfo,
    RaidAction,
    RaidCommandPlan,
    RaidStatus,
    SubmitStatus,
    utc_now_iso,
)
from .messages import DataRefreshed, ReportStatusChanged, TaskStateChanged
from .models import (
    RefreshScope,
    ReportState,
    ReportStatus,
    TaskItem,
    TaskKind,
    TaskStatus,
)
from .services import Services
from .state import AppState


@dataclass
class TaskContext:
    """Small facade handed to a job; all UI updates are marshalled to the loop."""

    runner: "TaskRunner"
    task: TaskItem

    def log(self, line: str) -> None:
        if not line:
            return
        self.task.log(line)
        self.runner.emit_task(self.task, log_line=line)

    def progress(self, done: int, total: int, message: str = "") -> None:
        self.task.done = done
        self.task.total = total
        if message:
            self.task.log(message)
        self.runner.emit_task(self.task, log_line=message or None)

    def snapshot(self, name: str) -> None:
        self.runner.request_snapshot(name, self.task.epoch)

    def cancelled(self) -> bool:
        return self.task.status is TaskStatus.CANCELLED or self.runner.state.cancel_event.is_set()

    def enqueue(self, task: TaskItem, job: "Job") -> None:
        self.runner.submit(task, job)


Job = Callable[[TaskContext], Optional[TaskStatus]]


class TaskRunner:
    """One dispatcher thread; jobs run one at a time via ``ctx.spawn``."""

    def __init__(self, state: AppState, app, broadcast: Callable[[object], None], services: Services) -> None:
        self.state = state
        self.app = app
        self.broadcast = broadcast
        self.services = services
        self._queue: "queue.Queue[Optional[tuple[TaskItem, Job]]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._dispatch, name="ui-task-dispatcher", daemon=True)
        self._thread.start()

    # -- public -------------------------------------------------------------
    def submit(self, task: TaskItem, job: Job) -> TaskItem:
        self.state.task_queue.append(task)
        self.emit_task(task)
        self._queue.put((task, job))
        return task

    def cancel_active(self) -> bool:
        if self.state.active_task_id is None:
            return False
        self.state.cancel_event.set()
        for task in self.state.task_queue:
            if task.id == self.state.active_task_id:
                task.status = TaskStatus.CANCELLED
                task.log("cancel requested; waiting for the current command")
                self.emit_task(task)
                break
        return True

    def shutdown(self) -> None:
        self._stop.set()
        self._queue.put(None)

    # -- internals ----------------------------------------------------------
    def _dispatch(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                break
            task, job = item
            if task.status is TaskStatus.CANCELLED:
                continue
            self._run(task, job)

    def _run(self, task: TaskItem, job: Job) -> None:
        state = self.state
        state.cancel_event.clear()
        state.active_task_id = task.id
        task.status = TaskStatus.RUNNING
        task.started_at = utc_now_iso()
        task.log("started")
        self.emit_task(task)

        ctx = TaskContext(self, task)
        result: Optional[TaskStatus] = None
        try:
            result = job(ctx)
        except Exception as exc:  # pragma: no cover - jobs must not kill the queue
            task.error = f"{type(exc).__name__}: {exc}"
            task.log(f"FAILED: {task.error}")
            result = TaskStatus.FAILED
        finally:
            if task.status is TaskStatus.CANCELLED:
                final = TaskStatus.CANCELLED
            else:
                final = result or TaskStatus.SUCCEEDED
            task.status = final
            task.finished_at = utc_now_iso()
            if final is TaskStatus.SUCCEEDED:
                task.log("finished")
            elif final is TaskStatus.PARTIAL:
                task.log("finished with warnings")
            elif final is TaskStatus.FAILED:
                task.log("finished with errors")
            elif final is TaskStatus.CANCELLED:
                task.log("cancelled")
            state.active_task_id = None
            state.cancel_event.clear()
            self.emit_task(task)
            self._safe_broadcast(DataRefreshed(RefreshScope.TASKS, generation=state.refresh.generation))

    def emit_task(self, task: TaskItem, log_line: Optional[str] = None) -> None:
        message = TaskStateChanged(
            task.id,
            task.status,
            task.done,
            task.total,
            task.error,
            log_line,
        )
        self._threadsafe(lambda: self.broadcast(message))

    def request_snapshot(self, name: str, epoch: int = 0) -> None:
        app = self.app
        if app is None:
            return
        try:
            app.call_ui(app.request_snapshot, name, epoch)
        except Exception:  # pragma: no cover - app shutting down
            pass

    def _safe_broadcast(self, message: object) -> None:
        self._threadsafe(lambda: self.broadcast(message))

    def _threadsafe(self, fn: Callable[[], None]) -> None:
        app = self.app
        if app is None:
            fn()
            return
        try:
            app.call_ui(fn)
        except Exception:  # pragma: no cover - app shutting down
            pass


# ---------------------------------------------------------------------------
# Job builders
# ---------------------------------------------------------------------------


def refresh_job(services: Services, scope: RefreshScope = RefreshScope.ALL, controller_index: Optional[int] = None) -> Job:
    def job(ctx: TaskContext) -> TaskStatus:
        state = services.state
        state.mark_refresh_started(scope)
        ctx.log("scanning controllers, disks and RAID topology ...")
        try:
            if scope is RefreshScope.CONTROLLER and controller_index is not None:
                services.refresh_controller(controller_index)
            elif scope is RefreshScope.DISKS and controller_index is not None:
                services.refresh_disks(controller_index)
            else:
                services.refresh_all()
                controller_index = None
        except Exception as exc:
            state.mark_refresh_finished(str(exc))
            ctx.log(f"refresh failed: {exc}")
            services.state.evidence.emit("ui_refresh_failed", scope=scope.value, error=str(exc)[:300])
            return TaskStatus.FAILED
        state.mark_refresh_finished()
        state.evidence.emit("ui_refresh", scope=scope.value, controller_index=controller_index)
        ctx.runner._safe_broadcast(
            DataRefreshed(scope, controller_index=controller_index, generation=state.refresh.generation)
        )
        ctx.log("refresh complete")
        return TaskStatus.SUCCEEDED

    return job


def erase_job(
    services: Services,
    plans: List[CommandPlan],
    disks: dict,
    epoch: int,
    controller_index: Optional[int] = None,
) -> Job:
    def job(ctx: TaskContext) -> TaskStatus:
        state = services.state

        def on_progress(done: int, total: int, result) -> None:
            marker = {
                SubmitStatus.ASYNC_SUBMITTED: "OK",
                SubmitStatus.SUBMITTED: "OK",
                SubmitStatus.SKIPPED: "SKIP",
                SubmitStatus.CANCELLED: "STOP",
            }.get(result.status, "FAIL")
            ctx.progress(
                done,
                total,
                f"{marker} {result.disk_id} {result.status.value} rc={result.return_code} {result.duration_sec:.2f}s",
            )

        ctx.log(f"submitting {len(plans)} erase command(s)")
        results = services.execute_erase(
            plans,
            disks,
            on_progress=on_progress,
            should_stop=state.cancel_event.is_set,
            epoch=epoch,
        )
        accepted = sum(1 for result in results if result.ok)
        failed = len(results) - accepted
        ctx.snapshot("20-erase-progress")
        if state.cancel_event.is_set() and len(results) < len(plans):
            return TaskStatus.CANCELLED
        if state.cfg.dry_run:
            ctx.log("dry-run: commands were not submitted")
            return TaskStatus.SUCCEEDED
        if failed and accepted:
            return TaskStatus.PARTIAL
        if failed and not accepted:
            return TaskStatus.FAILED
        if controller_index is not None:
            try:
                services.refresh_disks(controller_index)
                ctx.runner._safe_broadcast(
                    DataRefreshed(
                        RefreshScope.DISKS,
                        controller_index=controller_index,
                        disk_ids=frozenset(plan.disk_id for plan in plans),
                        generation=state.refresh.generation,
                    )
                )
            except Exception as exc:  # pragma: no cover - refresh is best effort
                ctx.log(f"post-run disk refresh failed: {exc}")
        if not state.cfg.dry_run:
            report_task = make_task(TaskKind.REPORT, "Publish report", epoch=epoch)
            ctx.enqueue(report_task, report_job(services))
        return TaskStatus.SUCCEEDED

    return job


def raid_job(
    services: Services,
    plans: List[RaidCommandPlan],
    action: RaidAction,
    controller_index: int,
    epoch: int,
) -> Job:
    def job(ctx: TaskContext) -> TaskStatus:
        state = services.state

        def on_progress(done: int, total: int, result) -> None:
            marker = {
                RaidStatus.ACCEPTED: "OK",
                RaidStatus.SKIPPED: "SKIP",
                RaidStatus.CANCELLED: "STOP",
            }.get(result.status, "FAIL")
            ctx.progress(
                done,
                total,
                f"{marker} {result.action.value} {result.status.value} rc={result.return_code} {result.duration_sec:.2f}s",
            )

        ctx.log(f"submitting {len(plans)} RAID command(s) for c{controller_index}")
        results, observed, detail = services.execute_raid(
            plans,
            action,
            controller_index,
            on_progress=on_progress,
            should_stop=state.cancel_event.is_set,
            epoch=epoch,
        )
        ctx.snapshot("21-raid-progress")
        ctx.log(f"rescan: {observed} - {detail}")
        ctx.runner._safe_broadcast(
            DataRefreshed(
                RefreshScope.CONTROLLER,
                controller_index=controller_index,
                generation=state.refresh.generation,
            )
        )
        accepted = sum(1 for result in results if result.status is RaidStatus.ACCEPTED)
        failed = len(results) - accepted
        if state.cancel_event.is_set() and len(results) < len(plans):
            return TaskStatus.CANCELLED
        if state.cfg.dry_run:
            ctx.log("dry-run: RAID commands were not submitted")
            return TaskStatus.SUCCEEDED
        if observed == "mismatch":
            return TaskStatus.PARTIAL
        if failed and not accepted:
            return TaskStatus.FAILED
        if failed:
            return TaskStatus.PARTIAL
        if not state.cfg.dry_run:
            report_task = make_task(TaskKind.REPORT, "Publish report", epoch=epoch)
            ctx.enqueue(report_task, report_job(services))
        return TaskStatus.SUCCEEDED

    return job


def report_job(services: Services, on_status: Optional[Callable[[str], None]] = None) -> Job:
    def job(ctx: TaskContext) -> TaskStatus:
        state = services.state
        ctx.log("generating report and manifest ...")

        def status(line: str) -> None:
            state.publish_state.set_status(line)
            ctx.log(line)
            if on_status is not None:
                on_status(line)

        report = services.publish_report(on_status=status)
        state.set_report_status(report)
        ctx.runner._safe_broadcast(ReportStatusChanged(state.evidence.session_id, report))
        ctx.runner._safe_broadcast(DataRefreshed(RefreshScope.REPORT, generation=state.refresh.generation))
        if report.state is ReportState.FAILED:
            return TaskStatus.PARTIAL
        return TaskStatus.SUCCEEDED

    return job


def evidence_job(services: Services) -> Job:
    def job(ctx: TaskContext) -> TaskStatus:
        state = services.state
        ctx.log("scanning evidence sessions ...")
        sessions = services.scan_evidence()
        state.evidence_sessions = sessions
        ctx.runner._safe_broadcast(DataRefreshed(RefreshScope.EVIDENCE, generation=state.refresh.generation))
        ctx.log(f"{len(sessions)} session(s) found")
        return TaskStatus.SUCCEEDED

    return job


def make_task(
    kind: TaskKind,
    title: str,
    *,
    controller_index: Optional[int] = None,
    disk_ids: Optional[Iterable[str]] = None,
    volume_id: str = "",
    epoch: int = 0,
) -> TaskItem:
    return TaskItem(
        id=uuid.uuid4().hex[:10],
        kind=kind,
        title=title,
        controller_index=controller_index,
        disk_ids=list(disk_ids or []),
        volume_id=volume_id,
        epoch=epoch,
    )


__all__ = [
    "Job",
    "TaskContext",
    "TaskRunner",
    "erase_job",
    "evidence_job",
    "make_task",
    "raid_job",
    "refresh_job",
    "report_job",
]
