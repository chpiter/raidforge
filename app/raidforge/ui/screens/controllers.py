"""Controller overview: tool, model, firmware and RAID volume counts."""

from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import DataTable, Label, Static

from ...core.models import ControllerInfo
from ..scan import run_scan
from ..state import AppContext
from ..widgets import KeyBar, SessionStatusBar
from ..widgets.raid_tables import CursorTable
from .base import BaseScreen

SCREEN_NAME = "Controllers"

COLUMNS = ["", "Ctrl", "Tool", "Model", "FW", "VDs", "PDs", "Cap"]
COLUMN_KEYS = ["cur", "ctrl", "tool", "model", "fw", "vds", "pds", "cap"]


class ControllersScreen(BaseScreen):
    """List every detected RAID controller and open its detail view."""

    SCREEN_NAME = SCREEN_NAME
    HINTS = [
        ("Enter", "details"),
        ("R", "rescan"),
        ("E", "erase disks"),
        ("Esc", "back"),
        ("Q", "quit"),
    ]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self._info = Static("", classes="muted")
        self._table = CursorTable()
        self._controllers: List[ControllerInfo] = []
        self._scanning = False

    def compose(self) -> ComposeResult:
        yield SessionStatusBar(self.ctx, SCREEN_NAME)
        with Container(id="content"):
            yield Label("[b]RAID controllers[/b]", classes="title")
            yield self._info
            yield self._table
            yield self.status_line()
        yield KeyBar(self.HINTS)

    def on_mount(self) -> None:
        self._table.focus()
        self._load()
        self.schedule_snapshot("02-controllers")
        if self.ctx.detection is None or self.ctx.raid is None:
            self._spawn_scan()

    # -- rendering ----------------------------------------------------------
    def _load(self) -> None:
        detection = self.ctx.detection
        self._controllers = list(detection.controllers) if detection else []
        self._table.clear(columns=True)
        for label, key in zip(COLUMNS, COLUMN_KEYS):
            self._table.add_column(label, key=key)
        self._table._row_keys = []

        if detection is None:
            info_lines = ["No scan result yet."]
        else:
            binary = f" ({detection.binary})" if detection.binary else ""
            info_lines = [
                f"Tool: {detection.tool}{binary}   controllers: {len(self._controllers)}",
                f"{self.ctx.evidence.root}",
            ]
        if self.ctx.raid and self.ctx.raid.error:
            info_lines.append(f"RAID issue: {self.ctx.raid.error[:120]}")
        self._info.update("\n".join(info_lines))

        for controller in self._controllers:
            topology = self.ctx.raid_topology(controller.index)
            caps = self.ctx.raid_capabilities(controller.index)
            vds = len(topology.volumes) if topology else 0
            pds = len(self.ctx.controller_disks(controller.index))
            cap = "".join(
                [
                    "C" if caps.create else "-",
                    "D" if caps.delete_vd else "-",
                    "X" if caps.clear_config else "-",
                ]
            )
            key = controller.controller_id
            self._table._row_keys.append(key)
            self._table.add_row(
                "",
                key,
                controller.tool or "-",
                (controller.model or "-")[:26],
                (controller.firmware or "-")[:12],
                str(vds),
                str(pds),
                cap,
                key=key,
            )
        self._table.move_to_first()
        if self._controllers:
            self.set_message("Enter opens the controller; R rescans; E opens disk erase")
        else:
            self.set_message("no managed RAID controller - check vendor CLI binaries in /usr/local/bin")

    def _spawn_scan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self.set_message("scanning controllers and RAID topology...")

        def _worker() -> None:
            try:
                run_scan(self.ctx)
            except Exception as exc:  # pragma: no cover - scans must not kill the UI
                self.app.call_ui(lambda: self.set_message(f"scan failed: {exc}"))
            finally:
                self.app.call_ui(self._after_scan)

        self.ctx.spawn(_worker, name="raid-rescan")

    def _after_scan(self) -> None:
        self._scanning = False
        self._load()
        self.refresh()

    # -- input --------------------------------------------------------------
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self._open_detail()

    def on_key(self, event) -> None:
        key = getattr(event, "key", "")
        if key == "r":
            event.stop()
            self._spawn_scan()
        elif key == "e":
            event.stop()
            self._go_disks()
        elif key == "escape":
            self.app.pop_screen()

    def _open_detail(self) -> None:
        try:
            row = self._table.cursor_row
        except Exception:  # pragma: no cover - defensive
            return
        if not (0 <= row < len(self._controllers)):
            return
        controller = self._controllers[row]
        self.ctx.active_controller = controller.index
        from .controller_detail import ControllerDetailScreen

        self.app.push_screen(ControllerDetailScreen(self.ctx, controller.index))

    def _go_disks(self) -> None:
        from .disk_list import DiskListScreen

        self.app.push_screen(DiskListScreen(self.ctx))


__all__ = ["ControllersScreen"]
