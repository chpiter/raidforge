"""Controllers: list on the left, selected controller summary on the right."""

from __future__ import annotations

from typing import Dict, List

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from ...core.models import ControllerInfo, Vendor
from ..models import HotkeyHint, PageId, RefreshScope
from ..widgets.tables import ControllerTable, VolumeTable
from .base import StatefulPage


class ControllersPage(StatefulPage):
    PAGE = PageId.CONTROLLERS
    SCOPES = frozenset({RefreshScope.ALL, RefreshScope.CONTROLLER, RefreshScope.DISKS})

    def compose(self) -> ComposeResult:
        yield ControllerTable(id="controller-list")
        with Vertical(id="controller-summary"):
            yield Static("", id="controller-kv")
            yield Static("", id="controller-caps")
            yield VolumeTable(id="controller-volumes")

    def hotkeys(self) -> List[HotkeyHint]:
        return [
            HotkeyHint("Enter", "details"),
            HotkeyHint("F7", "create", danger=True),
            HotkeyHint("F5", "refresh"),
            HotkeyHint("Ctrl+P", "commands"),
            HotkeyHint("Ctrl+Q", "quit"),
        ]

    def focus_primary(self) -> None:
        try:
            self.query_one(ControllerTable).focus()
        except Exception:  # pragma: no cover
            pass

    # -- data ---------------------------------------------------------------
    def controllers(self) -> List[ControllerInfo]:
        if self.state.controllers:
            return list(self.state.controllers)
        indices = sorted({disk.controller_index for disk in self.state.physical_disks})
        controllers: List[ControllerInfo] = []
        for index in indices:
            disk = next((item for item in self.state.physical_disks if item.controller_index == index), None)
            controllers.append(
                ControllerInfo(
                    index=index,
                    vendor=disk.vendor if disk else Vendor.GENERIC,
                    tool=getattr(self.state.detection, "tool", "") if self.state.detection else "",
                    binary=getattr(self.state.detection, "binary", "") if self.state.detection else "",
                )
            )
        return controllers

    def apply_layout_mode(self, mode) -> None:
        try:
            self.query_one(ControllerTable).set_layout_mode(mode)
        except Exception:  # pragma: no cover - page may not be mounted yet
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        if not self.is_mounted:
            return
        controllers = self.controllers()
        vd_counts: Dict[int, int] = {}
        pd_counts: Dict[int, int] = {}
        for controller in controllers:
            topology = self.state.raid_topology(controller.index)
            vd_counts[controller.index] = len(topology.volumes) if topology else 0
            pd_counts[controller.index] = len(self.state.controller_disks(controller.index))
        table = self.query_one(ControllerTable)
        table.load(controllers, vd_counts, pd_counts, self.state.controller_cursor)
        if controllers and self.state.controller_cursor is None:
            self.state.controller_cursor = controllers[0].index
        self._update_summary()

    def _selected_controller(self) -> ControllerInfo | None:
        index = self.state.controller_cursor
        if index is None:
            return None
        return next((item for item in self.controllers() if item.index == index), None)

    def _update_summary(self) -> None:
        controller = self._selected_controller()
        if controller is None:
            self.query_one("#controller-kv", Static).update(Text("No controller selected."))
            self.query_one("#controller-caps", Static).update("")
            self.query_one(VolumeTable).load([])
            return
        topology = self.state.raid_topology(controller.index)
        caps = self.state.raid_capabilities(controller.index)
        lines = [
            f"Vendor   : {controller.vendor.label}",
            f"Tool     : {controller.tool or '-'} ({controller.binary or '-'})",
            f"Model    : {controller.model or '-'}",
            f"Firmware : {controller.firmware or '-'}",
            f"Serial   : {controller.serial or '-'}",
            f"Topology : {len(topology.volumes) if topology else 0} VD / "
            f"{len(topology.free_disk_ids) if topology else 0} free / "
            f"{len(topology.blocked) if topology else 0} blocked",
        ]
        if topology is not None and topology.error:
            lines.append(f"ERROR    : {topology.error}")
        self.query_one("#controller-kv", Static).update("\n".join(lines))
        self.query_one("#controller-caps", Static).update(
            f"Actions: create={'yes' if caps.create else 'no'}  "
            f"delete={'yes' if caps.delete_vd else 'no'}  "
            f"clear={'yes' if caps.clear_config else 'no'}"
        )
        views = [view for view in self.state.virtual_disks if view.controller_index == controller.index]
        self.query_one(VolumeTable).load(views)

    # -- events -------------------------------------------------------------
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "controller-list":
            return
        key = str(event.row_key.value)
        if key.startswith("c"):
            try:
                self.state.controller_cursor = int(key[1:])
            except ValueError:
                return
            self._update_summary()
            self.refresh_chrome()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "controller-list":
            return
        key = str(event.row_key.value)
        if key.startswith("c"):
            try:
                index = int(key[1:])
            except ValueError:
                return
            self.open_detail(index)

    def open_detail(self, index: int) -> None:
        from ..screens.controller_detail import ControllerDetailScreen

        self.state.active_controller = index
        self.state.controller_cursor = index
        self.app.push_screen(ControllerDetailScreen(self.state, index), self._after_detail)

    def _after_detail(self, _result=None) -> None:
        self.refresh_data()
        self.refresh_chrome()

    # -- actions ------------------------------------------------------------
    def action_create(self) -> None:
        index = self.state.controller_cursor
        if index is None:
            self.toast("select a controller first")
            return
        caps = self.state.raid_capabilities(index)
        if not caps.create:
            self.toast("RAID creation is not available for this controller")
            return
        from ..screens.raid_wizard import RaidWizardScreen

        self.app.push_screen(RaidWizardScreen(self.state, index), self._after_wizard)

    def _after_wizard(self, _result=None) -> None:
        self.refresh_data()
        self.refresh_chrome()


__all__ = ["ControllersPage"]
