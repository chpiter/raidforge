"""Virtual drives: same full-width layout as Physical, Enter opens members."""

from __future__ import annotations

from typing import List

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable

from ...agent.queue import submit_local
from ...core.models import RaidAction
from ..models import ConfirmRequest, HotkeyHint, PageId, RefreshScope, TaskKind, VirtualDiskView
from ..tasks import make_task, raid_job
from ..widgets.detail_drawer import DetailDrawer
from ..widgets.filter_bar import VolumeFilterBar
from ..widgets.tables import VolumeTable
from .base import StatefulPage


class VirtualPage(StatefulPage):
    PAGE = PageId.VIRTUAL
    BINDINGS = [
        Binding("slash", "search", "Search", show=False),
        Binding("escape", "clear_search_or_back", "Back", show=False),
    ]
    SCOPES = frozenset({RefreshScope.ALL, RefreshScope.CONTROLLER})

    def compose(self) -> ComposeResult:
        yield VolumeFilterBar(self.state, self._on_filter)
        yield VolumeTable(id="virtual-table")
        yield DetailDrawer(id="virtual-drawer")

    def hotkeys(self) -> List[HotkeyHint]:
        view = self._selected_view()
        busy = self.active_task_running()
        can_delete = bool(view and self.state.raid_capabilities(view.controller_index).delete_vd)
        hints = [
            HotkeyHint("Enter", "members", enabled=view is not None),
            HotkeyHint("F7", "create", danger=True, enabled=not busy),
            HotkeyHint("F9", "delete", danger=True, enabled=can_delete and not busy),
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
        return hints

    def focus_primary(self) -> None:
        try:
            self.query_one(VolumeTable).focus()
        except Exception:  # pragma: no cover
            pass

    # -- filters ------------------------------------------------------------
    def _controllers(self) -> List[int]:
        return sorted({view.controller_index for view in self.state.virtual_disks})

    def _on_filter(self) -> None:
        self.refresh_data()

    def _visible(self) -> List[VirtualDiskView]:
        f = self.state.filter_for("virtual")
        result: List[VirtualDiskView] = []
        query = f.query.lower()
        for view in self.state.virtual_disks:
            if f.ctrl != "all" and str(view.controller_index) != f.ctrl:
                continue
            if f.level != "all" and view.level.upper() != f.level:
                continue
            if f.state != "all" and view.status_kind.value != f.state:
                continue
            haystack = " ".join([view.volume_id, view.name, view.members_label, view.level]).lower()
            if query and query not in haystack:
                continue
            result.append(view)
        return result

    # -- data ---------------------------------------------------------------
    def apply_layout_mode(self, mode) -> None:
        try:
            self.query_one(VolumeTable).set_layout_mode(mode)
        except Exception:  # pragma: no cover - page may not be mounted yet
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        if not self.is_mounted:
            return
        bar = self.query_one(VolumeFilterBar)
        bar.set_controllers(self._controllers())
        views = self._visible()
        table = self.query_one(VolumeTable)
        table.load(views, self.state.virtual_cursor)
        bar.set_count(len(views), len(self.state.virtual_disks))
        if views:
            self._show_drawer(views[0] if self.state.virtual_cursor is None else self._selected_view())
        else:
            self.query_one(DetailDrawer).hide()
        self.refresh_chrome()

    def _selected_view(self) -> VirtualDiskView | None:
        if self.state.virtual_cursor:
            for view in self.state.virtual_disks:
                if view.key == self.state.virtual_cursor:
                    return view
        if self.state.active_volume_id:
            return self.state.volume_by_id(self.state.active_volume_id)
        return None

    def _show_drawer(self, view: VirtualDiskView | None) -> None:
        drawer = self.query_one(DetailDrawer)
        if view is None:
            drawer.hide()
            return
        text = Text()
        text.append(f"{view.volume_id}  c{view.controller_index}  {view.level_label}  {view.state}  {view.size_human}\n", style="bold")
        text.append("Name    ", style="bold")
        text.append(f"{view.name}   ")
        text.append("Spans ", style="bold")
        text.append(f"{len(view.spans)}   ")
        text.append("Members ", style="bold")
        text.append(f"{view.members_label}\n")
        text.append("Disks   ", style="bold")
        text.append(", ".join(view.disk_ids) or "-")
        drawer.show(text)

    # -- events -------------------------------------------------------------
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "virtual-table":
            return
        key = str(event.row_key.value)
        self.state.virtual_cursor = key
        view = next((item for item in self.state.virtual_disks if item.key == key), None)
        if view is not None:
            self.state.active_volume_id = view.volume_id
        self._show_drawer(view)
        self.refresh_chrome()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "virtual-table":
            return
        view = next((item for item in self.state.virtual_disks if item.key == str(event.row_key.value)), None)
        if view is not None:
            self.open_members(view)

    # -- actions ------------------------------------------------------------
    def open_members(self, view: VirtualDiskView) -> None:
        from ..screens.volume_members import VolumeMembersScreen

        self.app.push_screen(
            VolumeMembersScreen(self.state, view.controller_index, view.volume_id),
            self._after_members,
        )

    def _after_members(self, _result=None) -> None:
        self.refresh_chrome()

    def action_search(self) -> None:
        self.query_one(VolumeFilterBar).focus_search()

    def action_clear_search_or_back(self) -> None:
        bar = self.query_one(VolumeFilterBar)
        if self.state.filter_for("virtual").query:
            bar.clear_search()
            self.refresh_data()
            return
        back = getattr(self.app.screen, "action_back", None)
        if callable(back):
            back()

    def action_create(self) -> None:
        view = self._selected_view()
        index = view.controller_index if view else (self.state.controller_cursor or (self._controllers() or [None])[0])
        if index is None:
            self.toast("no controller available")
            return
        from ..screens.raid_wizard import RaidWizardScreen

        self.app.push_screen(RaidWizardScreen(self.state, index), self._after_wizard)

    def _after_wizard(self, _result=None) -> None:
        self.refresh_data()
        self.refresh_chrome()

    def action_delete(self) -> None:
        if self.active_task_running():
            self.toast("a task is already running - Ctrl+Q cancels", "warn")
            return
        view = self._selected_view()
        if view is None:
            self.toast("select a virtual drive first")
            return
        caps = self.state.raid_capabilities(view.controller_index)
        if not caps.delete_vd:
            self.toast("deleting virtual drives is not available")
            return
        services = self.state.services
        try:
            plans = services.plan_delete(view.controller_index, view.volume_id)  # type: ignore[union-attr]
        except Exception as exc:
            self.toast(f"cannot build delete plan: {exc}")
            return
        epoch = self.state.raid_epoch + 1
        self.state.raid_epoch = epoch
        request = ConfirmRequest(
            title=f"Delete virtual drive {view.volume_id} on c{view.controller_index}",
            action="delete",
            ack_word="yes",
            ack_label="yes",
            audit_token=self.state.audit_token(),
            commands=[plan.dry_run_text for plan in plans],
            summary=[
                f"VD {view.volume_id}  {view.level_label}  {view.state}  {view.size_human}",
                f"Members: {view.members_label}",
                "Member disks become free after deletion.",
            ],
            warnings=["IRREVERSIBLE: member data becomes inaccessible immediately."],
            danger=True,
            epoch=epoch,
            snapshot_name="04-raid-confirm",
            event_prefix="raid_confirm",
        )

        def after(approved: bool) -> None:
            if not approved:
                self.toast("delete cancelled")
                return
            task = make_task(
                TaskKind.RAID_DELETE,
                f"Delete {view.volume_id}",
                controller_index=view.controller_index,
                volume_id=view.volume_id,
                epoch=epoch,
            )
            submit_local(
                self.state,
                task,
                raid_job(services, plans, RaidAction.DELETE_VD, view.controller_index, epoch),
            )
            self.toast(f"delete queued as {task.id}")

        from ..screens.confirm import ConfirmModal

        self.app.push_screen(ConfirmModal(self.state, request), after)


__all__ = ["VirtualPage"]
