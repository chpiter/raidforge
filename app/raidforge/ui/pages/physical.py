"""Physical disks: filter bar, full-width multi-select table, bottom drawer."""

from __future__ import annotations

from typing import List

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable

from ...core.models import DiskInfo, Vendor
from ..messages import NavigateRequested
from ..models import ConfirmRequest, HotkeyHint, PageId, RefreshScope, TaskKind
from ..tasks import erase_job, make_task
from ..widgets.detail_drawer import DetailDrawer
from ..widgets.disk_table import MultiSelectTable
from ..widgets.filter_bar import DiskFilterBar
from .base import StatefulPage


class PhysicalPage(StatefulPage):
    PAGE = PageId.PHYSICAL
    BINDINGS = [
        Binding("slash", "search", "Search", show=False),
        Binding("escape", "clear_search_or_back", "Back", show=False),
    ]
    SCOPES = frozenset({RefreshScope.ALL, RefreshScope.DISKS, RefreshScope.CONTROLLER})

    def compose(self) -> ComposeResult:
        yield DiskFilterBar(self.state, self._on_filter)
        yield MultiSelectTable(
            id="physical-table",
            on_change=self._on_selection,
            on_cursor=self._on_cursor,
            row_allowed=self._row_allowed,
            vd_for=self._vd_label,
        )
        yield DetailDrawer(id="physical-drawer")

    def hotkeys(self) -> List[HotkeyHint]:
        has_selection = bool(self.state.physical_selection)
        busy = self.active_task_running()
        hints = [
            HotkeyHint("Space", "toggle"),
            HotkeyHint("Enter", "confirm", enabled=not busy),
            HotkeyHint("F8", "erase", danger=True, enabled=has_selection and not busy),
            HotkeyHint("F5", "refresh", enabled=not busy),
            HotkeyHint("/", "search"),
            HotkeyHint("Ctrl+Q", "cancel" if busy else "quit"),
        ]
        try:
            width = self.app.size.width
        except Exception:  # pragma: no cover - not mounted yet
            width = 80
        if width >= 100:
            hints.append(HotkeyHint("Ctrl+P", "commands"))
        if width >= 130:
            hints.extend(
                [
                    HotkeyHint("Ctrl+A", "all"),
                    HotkeyHint("Ctrl+I", "invert"),
                ]
            )
        return hints

    def focus_primary(self) -> None:
        try:
            self.query_one(MultiSelectTable).focus()
        except Exception:  # pragma: no cover
            pass

    # -- filters ------------------------------------------------------------
    def _controllers(self) -> List[int]:
        return sorted({disk.controller_index for disk in self.state.physical_disks})

    def _on_filter(self) -> None:
        self.refresh_data()

    def _visible(self) -> List[DiskInfo]:
        f = self.state.filter_for("physical")
        membership = self.state.disk_membership()
        result: List[DiskInfo] = []
        query = f.query.lower()
        for disk in self.state.physical_disks:
            if f.ctrl != "all" and str(disk.controller_index) != f.ctrl:
                continue
            if f.media != "all" and disk.medium.value != f.media:
                continue
            member = disk.disk_id in membership
            blocked = bool(self.state.erase_blocked_reason(disk))
            if f.state == "free" and (member or blocked):
                continue
            if f.state == "vd" and not member:
                continue
            if f.state == "blocked" and not blocked:
                continue
            haystack = " ".join(
                [disk.disk_id, disk.serial, disk.model, disk.location, disk.device_node]
            ).lower()
            if query and query not in haystack:
                continue
            result.append(disk)
        return result

    # -- data ---------------------------------------------------------------
    def apply_layout_mode(self, mode) -> None:
        try:
            self.query_one(MultiSelectTable).set_layout_mode(mode)
        except Exception:  # pragma: no cover - page may not be mounted yet
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        if not self.is_mounted:
            return
        bar = self.query_one(DiskFilterBar)
        bar.set_controllers(self._controllers())
        disks = self._visible()
        self.state.prune_selection()
        table = self.query_one(MultiSelectTable)
        table.load_disks(disks, self.state.physical_selection, self.state.physical_cursor)
        bar.set_count(len(disks), len(self.state.physical_disks))
        if disks:
            self._on_cursor(table.current_row_key)
        else:
            self.query_one(DetailDrawer).hide()
        self.refresh_chrome()

    def _row_allowed(self, disk: DiskInfo):
        reason = self.state.erase_blocked_reason(disk)
        return (not reason, reason)

    def _vd_label(self, disk: DiskInfo) -> str:
        membership = self.state.disk_membership().get(disk.disk_id)
        if membership:
            return membership
        return "blocked" if self.state.erase_blocked_reason(disk) else "free"

    def _merge_visible_selection(self) -> None:
        table = self.query_one(MultiSelectTable)
        visible = {disk.disk_id for disk in self._visible()}
        self.state.physical_selection = (
            self.state.physical_selection - visible
        ) | set(table.selected_ids)

    def _on_selection(self, message: str) -> None:
        self._merge_visible_selection()
        self.toast(message)
        self.refresh_chrome()

    def _on_cursor(self, disk_id) -> None:
        self.state.physical_cursor = disk_id
        drawer = self.query_one(DetailDrawer)
        if not disk_id:
            drawer.hide()
            return
        disk = self.state.disk_by_id(disk_id)
        if disk is None:
            drawer.hide()
            return
        drawer.show(self._drawer_text(disk))
        self.refresh_chrome()

    def _drawer_text(self, disk: DiskInfo) -> Text:
        erase_block = self.state.erase_blocked_reason(disk)
        raid_block = self.state.blocked_reason(disk)
        text = Text()
        text.append(f"{disk.disk_id}", style="bold")
        text.append(f"  c{disk.controller_index}  {disk.location}  {disk.medium.value}  {disk.protocol.value}  {disk.size_human}\n")
        text.append("Model   ", style="bold")
        text.append(f"{disk.model or '-'}   ")
        text.append("Serial ", style="bold")
        text.append(f"{disk.serial or '-'}   ")
        text.append("FW ", style="bold")
        text.append(f"{disk.firmware or '-'}\n")
        text.append("State   ", style="bold")
        text.append(f"{disk.raid_state or ('direct' if disk.vendor is Vendor.GENERIC else '-')}   ")
        text.append("VD ", style="bold")
        text.append(f"{self.state.disk_membership().get(disk.disk_id, 'free')}   ")
        text.append("Erase ", style="bold")
        text.append("selectable" if not erase_block else erase_block)
        text.append("\nRAID    ", style="bold")
        text.append("free" if not raid_block else raid_block)
        return text

    # -- actions ------------------------------------------------------------
    def action_select_all(self) -> None:
        self.query_one(MultiSelectTable).select_all()
        self._merge_visible_selection()
        self.refresh_data()

    def action_clear_selection(self) -> None:
        self.query_one(MultiSelectTable).clear_selection()
        self.state.physical_selection.clear()
        self.refresh_data()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "physical-table":
            return
        if self.state.physical_selection:
            self.action_erase()
            return
        table = self.query_one(MultiSelectTable)
        key = table.current_row_key
        disk = self.state.disk_by_id(key) if key else None
        if disk is None:
            self.toast("no disk under the cursor", "warn")
            return
        reason = self.state.erase_blocked_reason(disk)
        if reason:
            self.toast(reason, "warn")
            return
        table.toggle_selection()
        self.action_erase()

    def action_clear_search_or_back(self) -> None:
        bar = self.query_one(DiskFilterBar)
        if self.state.filter_for("physical").query:
            bar.clear_search()
            self.refresh_data()
            return
        back = getattr(self.app.screen, "action_back", None)
        if callable(back):
            back()

    def action_search(self) -> None:
        self.query_one(DiskFilterBar).focus_search()

    def action_erase(self) -> None:
        if self.active_task_running():
            self.toast("a task is already running - Ctrl+Q cancels", "warn")
            return
        selected = [disk for disk in self.state.physical_disks if disk.disk_id in self.state.physical_selection]
        if not selected:
            self.toast("select at least one disk first - Space toggles, Enter confirms", "warn")
            return
        for disk in selected:
            reason = self.state.erase_blocked_reason(disk)
            if reason:
                self.toast(f"{disk.disk_id}: {reason}", "warn")
                return
        services = self.state.services
        try:
            plans, snapshot = services.plan_erase([disk.disk_id for disk in selected])  # type: ignore[union-attr]
        except Exception as exc:
            self.toast(f"cannot build erase plan: {exc}")
            return
        if not plans:
            self.toast("nothing to erase")
            return
        epoch = self.state.run_epoch + 1
        self.state.run_epoch = epoch
        controllers = {disk.controller_index for disk in selected}
        controller_index = next(iter(controllers)) if len(controllers) == 1 else None
        request = ConfirmRequest(
            title=f"Erase {len(plans)} disk(s)",
            action="erase",
            ack_word="yes",
            ack_label="yes",
            audit_token=self.state.audit_token(),
            commands=[plan.dry_run_text for plan in plans],
            summary=[
                f"{plan.disk_id}  {snapshot.get(plan.disk_id).medium.value if snapshot.get(plan.disk_id) else '-'}  "
                f"{snapshot.get(plan.disk_id).size_human if snapshot.get(plan.disk_id) else '-'}  {plan.label}"
                for plan in plans
            ],
            warnings=[
                "IRREVERSIBLE: the commands are submitted to the controller; the erase continues asynchronously.",
            ],
            danger=True,
            epoch=epoch,
            snapshot_name="04-confirm",
            event_prefix="confirm",
        )

        def after(approved: bool) -> None:
            if not approved:
                self.toast("erase cancelled")
                return
            self.state.evidence.emit(
                "selection_confirmed_ui",
                disks=[plan.disk_id for plan in plans],
                epoch=epoch,
            )
            task = make_task(
                TaskKind.ERASE,
                f"Erase {len(plans)} disk(s)",
                controller_index=controller_index,
                disk_ids=[plan.disk_id for plan in plans],
                epoch=epoch,
            )
            self.state.task_runner.submit(  # type: ignore[union-attr]
                task,
                erase_job(services, plans, snapshot, epoch, controller_index),
            )
            self.state.physical_selection.clear()
            self.toast(f"erase queued as {task.id}")
            self.refresh_data()

        from ..screens.confirm import ConfirmModal

        self.app.push_screen(ConfirmModal(self.state, request), after)


__all__ = ["PhysicalPage"]
