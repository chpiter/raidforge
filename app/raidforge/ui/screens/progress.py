"""Progress screen: serial submission of every planned erase command.

IMPORTANT (contract 12): the table below reports *submission* results only.
A zero exit code means the controller accepted the job; the erase itself keeps
running inside the controller firmware after this screen disappears.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Label, Static

from ...core.erase import execute_plans
from ...core.models import CommandPlan, DiskInfo, EraseRun, SubmitResult, SubmitStatus, utc_now_iso
from ..state import AppContext
from .base import BaseScreen

SCREEN_NAME = "Erase progress"


class ProgressScreen(BaseScreen):
    """Runs the plans serially and shows one row per submitted command."""

    SCREEN_NAME = SCREEN_NAME
    HINTS = [("Q", "stop run"), ("Esc", "back")]

    #: Q on this screen stops the run instead of quitting the application:
    #: killing the process would orphan the vendor command that is currently
    #: running and its result would never reach the evidence log.
    BINDINGS = [Binding("q", "cancel_run", "Stop run")]

    COLUMNS: List[str] = ["#", "Disk", "Method", "Status", "RC", "Sec", "Detail"]
    COLUMN_KEYS: List[str] = ["n", "disk", "method", "status", "rc", "sec", "detail"]

    def __init__(
        self,
        ctx: AppContext,
        plans: List[CommandPlan],
        disks: Optional[Dict[str, DiskInfo]] = None,
    ) -> None:
        super().__init__(ctx)
        self.plans = plans
        #: Plan-time snapshot of the selected disks.  Never read ctx.disks at
        #: finish time: a background rescan can replace it mid-run.
        self.disks = dict(disks or {})
        #: True between on_mount (worker spawned) and _finish.  Q must only
        #: take the "cancel" branch while this is set - once the run is over
        #: there is nothing to cancel, and faking a "cancelling" state that
        #: nothing will ever clear is exactly the frozen screen we shipped once.
        self._running = False
        self._summary = Static("waiting...")
        #: One EraseRun is recorded per screen, not per submit callback.
        self._recorded = False
        self._epoch = ctx.run_epoch
        self._started_at = utc_now_iso()
        self._dry_run = bool(ctx.cfg.dry_run)
        # No zebra stripes: alternating greys are another colour, and on a
        # 16-colour console they come from the theme's lighten/darken blends.
        self._table = DataTable(zebra_stripes=False)

    def build_content(self) -> ComposeResult:
        yield Label("[b]Submitting erase commands[/b]", classes="title")
        yield self._summary
        yield self._table
        yield self.status_line()

    def on_mount(self) -> None:
        """Prepare the table then start the serial runner in a worker thread."""

        self._table.clear(columns=True)
        for label, key in zip(self.COLUMNS, self.COLUMN_KEYS):
            self._table.add_column(label, key=key)
        for index, plan in enumerate(self.plans, start=1):
            self._table.add_row(
                str(index),
                plan.disk_id,
                plan.label,
                "queued",
                "-",
                "-",
                plan.notes,
                key=plan.disk_id,
            )
        self._summary.update(f"queued {len(self.plans)} command(s); submitting serially...")
        self.ctx.cancel_event.clear()
        self._started_at = utc_now_iso()
        self._running = True
        self.ctx.spawn(self._run, name="erase-run")

    def is_run_active(self) -> bool:
        """True while the worker is still submitting plans."""

        return self._running

    # -- execution ----------------------------------------------------------
    def _run(self) -> None:
        """Worker thread: the one and only executor."""

        def _on_progress(done: int, total: int, result: SubmitResult) -> None:
            self.app.call_ui(self._record_result, done, total, result)

        results = execute_plans(
            self.plans,
            self.ctx.evidence,
            self.ctx.cfg,
            _on_progress,
            should_stop=self.ctx.cancel_event.is_set,
        )
        self.ctx.results = results
        self.app.call_ui(self._finish, results)

    def _record_result(self, done: int, total: int, result: SubmitResult) -> None:
        """Paint one completed row (called on the UI thread)."""

        # State is carried by an ASCII marker, not by colour: the console has
        # 16 colours and the old [ok]/[muted]/[danger] tags were not even valid
        # Rich colour names (they rendered as nothing).
        marker = {
            SubmitStatus.ASYNC_SUBMITTED: "OK  ",
            SubmitStatus.SUBMITTED: "OK  ",
            SubmitStatus.SKIPPED: "SKIP",
            SubmitStatus.CANCELLED: "STOP",
        }.get(result.status, "FAIL")
        self._table.update_cell(result.disk_id, "status", f"{marker} {result.status.value}")
        self._table.update_cell(result.disk_id, "rc", str(result.return_code))
        self._table.update_cell(result.disk_id, "sec", f"{result.duration_sec:.2f}")
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        self._table.update_cell(result.disk_id, "detail", detail[-1][:80] if detail else "-")
        self._summary.update(
            f"submitted {done}/{total}  -  ctrl accepts the job and erases asynchronously"
        )
        self.refresh()

    def _finish(self, results: List[SubmitResult]) -> None:
        """Final summary once every plan has been attempted."""

        accepted = sum(1 for result in results if result.ok)
        failed = len(results) - accepted
        stopped = self.ctx.cancel_event.is_set()
        prefix = "STOPPED: " if stopped else "done: "
        self._summary.update(
            f"{prefix}{len(results)} attempts, {accepted} accepted by the controller, "
            f"{failed} rejected\n"
            "Note: 'accepted' is not 'finished' - erasure continues inside the controller."
        )
        not_submitted = [plan.disk_id for plan in self.plans if plan.disk_id not in {r.disk_id for r in results}]
        if not_submitted:
            self.set_message(f"NOT submitted: {', '.join(not_submitted)} - Esc to go back")
        else:
            self.set_message("run complete - Esc to go back, Q to quit")
        if not self._recorded:
            run = EraseRun(
                index=len(self.ctx.erase_history) + 1,
                run_epoch=self._epoch,
                started_at=self._started_at,
                finished_at=utc_now_iso(),
                dry_run=self._dry_run,
                cancelled=stopped,
                plans=list(self.plans),
                results=list(results),
                disks={
                    plan.disk_id: self.disks[plan.disk_id]
                    for plan in self.plans
                    if plan.disk_id in self.disks
                },
            )
            self.ctx.erase_history.append(run)
            self.ctx.evidence.emit(
                "erase_run_recorded",
                index=run.index,
                run_epoch=run.run_epoch,
                planned=len(run.plans),
                attempts=len(run.results),
                cancelled=stopped,
            )
            self._recorded = True
        self.ctx.cancel_event.clear()
        self._running = False
        starter = getattr(self.app, "start_run_publish", None)
        if callable(starter):
            starter(self._epoch)
        else:  # pragma: no cover - only direct screen construction in tests
            self.schedule_snapshot("05-progress", epoch=self._epoch)

    # -- cancellation -------------------------------------------------------
    def request_cancel(self) -> None:
        """Ask the worker to stop (called by the app when Q is pressed)."""

        self.ctx.cancel_event.set()
        self.set_message("cancelling - waiting for the current command (max 60s)")
        self._summary.update("STOP requested: no further disks will be submitted.")

    def action_cancel_run(self) -> None:
        """Q on this screen: stop submitting, do not kill the process.

        When the run is already finished there is nothing to cancel - Q then
        falls through to the normal application exit instead of parking the
        screen in a "cancelling" state that no worker will ever resolve.
        """

        if self._running:
            if self.ctx.cancel_event.is_set():
                return
            self.ctx.evidence.emit("erase_cancel_requested")
            self.request_cancel()
            return
        self.app.action_quit_app()

    def on_key(self, event) -> None:
        """Escape returns to the disk list."""

        if getattr(event, "key", "") == "escape":
            self.app.pop_screen()
