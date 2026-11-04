"""RAID write progress: serial submission, then an honest rescan verdict."""

from __future__ import annotations

from typing import List, Optional, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Label, Static

from ...core.models import (
    RaidAction,
    RaidActionRun,
    RaidActionResult,
    RaidCommandPlan,
    RaidStatus,
    RaidTopology,
    utc_now_iso,
)
from ...core.raid.execute import execute_raid_plans
from ..scan import run_scan
from ..state import AppContext
from .base import BaseScreen

SCREEN_NAME = "RAID progress"

COLUMNS = ["#", "Action", "Target", "Status", "RC", "Sec", "Detail"]
COLUMN_KEYS = ["n", "action", "target", "status", "rc", "sec", "detail"]


class RaidProgressScreen(BaseScreen):
    """Run the confirmed plans serially, then rescan and report what is visible."""

    SCREEN_NAME = SCREEN_NAME
    HINTS = [("Q", "stop run"), ("Esc", "back")]
    BINDINGS = [Binding("q", "cancel_run", "Stop run")]

    def __init__(
        self,
        ctx: AppContext,
        plans: List[RaidCommandPlan],
        action: RaidAction,
        controller_index: int,
        epoch: Optional[int] = None,
    ) -> None:
        super().__init__(ctx)
        self.plans = plans
        self.action = action
        self.controller_index = controller_index
        self._epoch = ctx.raid_epoch if epoch is None else epoch
        self._summary = Static("waiting...")
        self._table = DataTable(zebra_stripes=False)
        self._running = False
        self._recorded = False
        self._started_at = utc_now_iso()
        self._dry_run = bool(ctx.cfg.dry_run)
        self._topology_before = self._topology_dict()

    def _topology_dict(self) -> dict:
        topology = self.ctx.raid_topology(self.controller_index)
        return topology.to_json_obj() if topology else {}

    def build_content(self) -> ComposeResult:
        yield Label("[b]Submitting RAID commands[/b]", classes="title")
        yield self._summary
        yield self._table
        yield self.status_line()

    def on_mount(self) -> None:
        self._table.clear(columns=True)
        for label, key in zip(COLUMNS, COLUMN_KEYS):
            self._table.add_column(label, key=key)
        for index, plan in enumerate(self.plans, start=1):
            self._table.add_row(
                str(index),
                plan.action.value,
                plan.volume_id or (plan.disk_ids[0] if len(plan.disk_ids) == 1 else ",".join(plan.disk_ids)),
                "queued",
                "-",
                "-",
                plan.notes,
                key=str(index),
            )
        self._summary.update(f"queued {len(self.plans)} command(s); submitting serially...")
        self.ctx.cancel_event.clear()
        self._running = True
        self.ctx.spawn(self._run, name="raid-run")

    def is_run_active(self) -> bool:
        return self._running

    # -- execution ----------------------------------------------------------
    def _run(self) -> None:
        def _on_progress(done: int, total: int, result: RaidActionResult) -> None:
            self.app.call_ui(self._record_result, done, total, result)

        results = execute_raid_plans(
            self.plans,
            self.ctx.evidence,
            self.ctx.cfg,
            _on_progress,
            should_stop=self.ctx.cancel_event.is_set,
        )
        scan = None
        scan_error = ""
        try:
            scan = run_scan(self.ctx)
        except Exception as exc:  # pragma: no cover - rescan failure must not hide results
            scan_error = str(exc)
        self.app.call_ui(self._finish, results, scan_error)

    def _record_result(self, done: int, total: int, result: RaidActionResult) -> None:
        marker = {
            RaidStatus.ACCEPTED: "OK  ",
            RaidStatus.SKIPPED: "SKIP",
            RaidStatus.CANCELLED: "STOP",
        }.get(result.status, "FAIL")
        key = str(done)
        try:
            self._table.update_cell(key, "status", f"{marker} {result.status.value}")
            self._table.update_cell(key, "rc", str(result.return_code))
            self._table.update_cell(key, "sec", f"{result.duration_sec:.2f}")
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            self._table.update_cell(key, "detail", detail[-1][:90] if detail else "-")
        except Exception:  # pragma: no cover - row may already be gone
            pass
        self._summary.update(f"submitted {done}/{total} - the controller decides when the work finishes")
        self.refresh()

    def _finish(self, results: List[RaidActionResult], scan_error: str) -> None:
        topology_after = self.ctx.raid_topology(self.controller_index)
        observed, observed_detail = self._observe(results, topology_after, scan_error)
        accepted = sum(1 for result in results if result.status is RaidStatus.ACCEPTED)
        failed = len(results) - accepted
        # A Q that arrives after the last submission but during the rescan must
        # not relabel a completed run as STOPPED.
        stopped = self.ctx.cancel_event.is_set() and len(results) < len(self.plans)
        prefix = "STOPPED: " if stopped else "done: "
        self._summary.update(
            f"{prefix}{len(results)} attempts, {accepted} accepted by the controller, {failed} not accepted\n"
            f"rescan: {observed} - {observed_detail}\n"
            "Note: 'accepted' is not 'finished'; the controller may still be initialising the volume."
        )
        if not self._recorded:
            run = RaidActionRun(
                index=len(self.ctx.raid_history) + 1,
                epoch=self._epoch,
                action=self.action,
                started_at=self._started_at,
                finished_at=utc_now_iso(),
                dry_run=self._dry_run,
                cancelled=stopped,
                controller_index=self.controller_index,
                plans=list(self.plans),
                results=list(results),
                topology_before=self._topology_before,
                topology_after=topology_after.to_json_obj() if topology_after else {},
            )
            self.ctx.raid_history.append(run)
            self.ctx.evidence.write_json(
                "raid_actions.json",
                [record.to_json_obj() for record in self.ctx.raid_history],
            )
            self.ctx.evidence.emit(
                "raid_run_recorded",
                index=run.index,
                epoch=run.epoch,
                action=run.action.value,
                planned=len(run.plans),
                attempts=len(run.results),
                cancelled=stopped,
                observed=observed,
                observed_detail=observed_detail,
            )
            self.ctx.evidence.emit(
                "raid_rescan_after",
                action=self.action.value,
                controller_index=self.controller_index,
                observed=observed,
                detail=observed_detail,
            )
            self._recorded = True
        self.ctx.cancel_event.clear()
        self._running = False
        starter = getattr(self.app, "start_run_publish", None)
        if callable(starter):
            starter(self._epoch)
        else:  # pragma: no cover - direct screen construction in tests
            self.schedule_snapshot("05-raid-progress", epoch=self._epoch)
        self.set_message(f"RAID {self.action.value}: {observed} - Esc to go back, Q to quit")

    def _observe(
        self,
        results: List[RaidActionResult],
        topology: Optional[RaidTopology],
        scan_error: str,
    ) -> Tuple[str, str]:
        if scan_error:
            return "not_checked", f"rescan failed: {scan_error[:120]}"
        if topology is None:
            return "not_checked", "no topology after the run"
        if topology.error:
            return "not_checked", topology.error[:160]
        if self.action is RaidAction.CREATE:
            wanted = [(plan.level, set(plan.disk_ids)) for plan in self.plans]
            actual = [(volume.level, set(volume.disk_ids)) for volume in topology.volumes]
            matched = sum(
                1 for level, disks in wanted if any(a_level == level and disks <= a_disks for a_level, a_disks in actual)
            )
            detail = f"{matched}/{len(wanted)} target volume(s) visible; now {len(topology.volumes)} volume(s)"
            if matched == len(wanted):
                return "confirmed", detail
            if matched == 0:
                return "not_visible", detail + " (the controller may still be initialising)"
            return "mismatch", detail
        if self.action is RaidAction.DELETE_VD:
            wanted = {plan.volume_id for plan in self.plans}
            remaining = [volume.volume_id for volume in topology.volumes if volume.volume_id in wanted]
            if not remaining:
                return "confirmed", f"deleted {', '.join(sorted(wanted))}; no target VD remains"
            return "mismatch", f"still visible: {', '.join(remaining)}"
        if self.action is RaidAction.CLEAR_CONFIG:
            foreign = [
                disk_id
                for disk_id, reason in topology.blocked.items()
                if "frgn" in reason.lower() or "foreign" in reason.lower()
            ]
            if not topology.volumes and not foreign:
                return "confirmed", "no virtual drives or foreign configuration remain"
            if foreign:
                return "mismatch", f"foreign metadata still present on: {', '.join(foreign)}"
            return "mismatch", f"{len(topology.volumes)} virtual drive(s) still visible"
        return "not_checked", "unknown action"

    # -- cancellation -------------------------------------------------------
    def request_cancel(self) -> None:
        self.ctx.cancel_event.set()
        self.set_message("cancelling - waiting for the current command (max 120s)")
        self._summary.update("STOP requested: no further RAID commands will be submitted.")

    def action_cancel_run(self) -> None:
        if self._running:
            if self.ctx.cancel_event.is_set():
                return
            self.ctx.evidence.emit("raid_cancel_requested", action=self.action.value)
            self.request_cancel()
            return
        self.app.exit()

    def on_key(self, event) -> None:
        if getattr(event, "key", "") != "escape":
            return
        if self._running:
            event.stop()
            self.request_cancel()
            return
        self.app.pop_screen()


__all__ = ["RaidProgressScreen"]
