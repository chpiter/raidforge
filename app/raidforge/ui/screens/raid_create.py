"""Create RAID0/1/5/10 with explicit RAID0 grouping and a dry-run preview."""

from __future__ import annotations

from typing import List, Optional, Tuple

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import DataTable, Input, Label, Static

from ...core.models import ControllerInfo, DiskInfo, Raid0Mode, RaidLevel, Vendor
from ...core.raid import provider_for
from ...core.raid.layout import build_layout, validate_layout
from ..state import AppContext
from ..widgets import DiskTable, KeyBar, SessionStatusBar
from .base import BaseScreen
from .raid_confirm import RaidConfirmModal
from .raid_progress import RaidProgressScreen

SCREEN_NAME = "Create RAID"

_LEVEL_KEYS = {
    "0": RaidLevel.RAID0,
    "1": RaidLevel.RAID1,
    "5": RaidLevel.RAID5,
    "t": RaidLevel.RAID10,
}


class RaidCreateScreen(BaseScreen):
    """Choose a level, eligible disks and RAID0 grouping, then confirm."""

    SCREEN_NAME = SCREEN_NAME
    HINTS = [
        ("0/1/5/T", "level"),
        ("G", "RAID0 mode"),
        ("Space", "toggle"),
        ("A", "all"),
        ("N", "none"),
        ("Enter", "create"),
        ("Esc", "back"),
    ]

    def __init__(self, ctx: AppContext, controller_index: int) -> None:
        super().__init__(ctx)
        self.controller_index = controller_index
        self.level = RaidLevel.RAID1
        self.mode = Raid0Mode.SINGLE
        self._blocked: dict[str, str] = {}
        self._free_ids: List[str] = []
        self._topology = None
        self._level_line = Static("", classes="muted")
        self._preview = Static("", classes="muted")
        self._name_input = Input(placeholder="optional VD name (A-Z, 0-9, _ -)", id="raid-name")
        self._table = DiskTable(on_change=self._on_selection_changed, row_allowed=self._allow_disk)

    def compose(self) -> ComposeResult:
        yield SessionStatusBar(self.ctx, SCREEN_NAME)
        with Container(id="content"):
            yield Label(f"[b]Create RAID on c{self.controller_index}[/b]", classes="title")
            yield self._level_line
            yield self._name_input
            yield self._table
            yield self._preview
            yield self.status_line()
        yield KeyBar(self.HINTS)

    def on_mount(self) -> None:
        self._load_disks()
        self._table.focus()
        self._refresh_header()
        self._update_preview()
        self.schedule_snapshot("02c-raid-create")

    def on_screen_resume(self) -> None:
        self._load_disks()
        self._update_preview()

    # -- data ---------------------------------------------------------------
    def _controller(self) -> Optional[ControllerInfo]:
        existing = self.ctx.controller_by_index(self.controller_index)
        if existing is not None:
            return existing
        topology = self.ctx.raid_topology(self.controller_index)
        if topology is None:
            return None
        return ControllerInfo(
            index=self.controller_index,
            vendor=topology.vendor or Vendor.GENERIC,
            tool=topology.tool,
            binary=topology.binary,
        )

    def _load_disks(self) -> None:
        self._topology = self.ctx.raid_topology(self.controller_index)
        self._blocked = dict(self._topology.blocked) if self._topology else {}
        self._free_ids = list(self._topology.free_disk_ids) if self._topology else []
        disks = self.ctx.controller_disks(self.controller_index)
        self._table.load_disks(disks, [])
        if self._topology is None:
            self.set_message("no RAID topology - go back and press R to rescan")
        elif self._topology.error:
            self.set_message(f"topology error: {self._topology.error}")
        elif not self._free_ids:
            self.set_message("no free disks on this controller - delete a VD or add disks")
        else:
            self.set_message(f"{len(self._free_ids)} free disk(s); Space toggles, Enter creates")

    def _allow_disk(self, disk: DiskInfo) -> Tuple[bool, str]:
        if disk.disk_id in self._free_ids:
            return True, ""
        return False, self._blocked.get(disk.disk_id, "not eligible for a new RAID")

    def _on_selection_changed(self, _message: str) -> None:
        self._update_preview()

    # -- rendering ----------------------------------------------------------
    def _refresh_header(self) -> None:
        stripe = (
            f"{self.ctx.cfg.raid.stripe_kb}K"
            if self.ctx.cfg.raid.stripe_kb
            else "controller default"
        )
        self._level_line.update(
            f"Level: {self.level.value}   [0] RAID0  [1] RAID1  [5] RAID5  [T] RAID10\n"
            f"RAID0 mode: {self.mode.value}   (G toggles single VD / one VD per disk)\n"
            f"Stripe: {stripe}   Init: {self.ctx.cfg.raid.init}   "
            f"Selected: {len(self._table.selected_ids)}"
        )

    def _selected_disks(self) -> List[DiskInfo]:
        return self._table.selected_disks()

    def _update_preview(self) -> None:
        selected = self._selected_disks()
        layout = build_layout(
            self.level,
            selected,
            mode=self.mode,
            name=self._name_input.value,
            stripe_kb=self.ctx.cfg.raid.stripe_kb,
            name_max_len=self.ctx.cfg.raid.name_max_len,
            init=self.ctx.cfg.raid.init,
        )
        errors = validate_layout(layout)
        self._refresh_header()
        if errors:
            self._preview.update("[b reverse]!! " + " ; ".join(errors) + "[/b reverse]")
            return
        spans = "  ".join(
            f"{index}) " + " + ".join(self._location(disk_id) for disk_id in span)
            for index, span in enumerate(layout.spans, start=1)
        )
        volume_count = len(layout.volume_names) if self.mode is Raid0Mode.PER_DISK else 1
        lines = [
            f"Spans   : {spans or '-'}",
            f"Volumes : {volume_count} x {layout.level.value}, usable ~{layout.capacity_human}",
            "Members : " + ", ".join(f"{d.disk_id}@{d.location}" for d in selected),
        ]
        if layout.name:
            lines.append(f"Name    : {layout.name}")
        lines.extend(f"Warning : {warning}" for warning in layout.warnings)
        self._preview.update("\n".join(lines))

    def _location(self, disk_id: str) -> str:
        disk = self.ctx.disk_by_id(disk_id)
        return disk.location if disk else disk_id

    # -- input --------------------------------------------------------------
    def _typing(self) -> bool:
        focused = getattr(self.app, "focused", None)
        return isinstance(focused, Input)

    def on_key(self, event) -> None:
        key = getattr(event, "key", "")
        if key == "escape":
            self.app.pop_screen()
            return
        if self._typing():
            return
        if key in _LEVEL_KEYS:
            event.stop()
            self.level = _LEVEL_KEYS[key]
            if self.level is not RaidLevel.RAID0:
                self.mode = Raid0Mode.SINGLE
            self._update_preview()
        elif key == "g":
            event.stop()
            if self.level is not RaidLevel.RAID0:
                self.set_message("RAID0 mode only applies to RAID0")
                return
            self.mode = Raid0Mode.PER_DISK if self.mode is Raid0Mode.SINGLE else Raid0Mode.SINGLE
            self._update_preview()
        elif key == "a":
            event.stop()
            self._table.select_all()
            self._update_preview()
            self.set_message(f"{len(self._table.selected_ids)} disks selected")
        elif key == "n":
            event.stop()
            self._table.clear_selection()
            self._update_preview()
            self.set_message("selection cleared")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self._start_confirm()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "raid-name":
            self._start_confirm()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "raid-name":
            self._update_preview()

    # -- confirm ------------------------------------------------------------
    def _start_confirm(self) -> None:
        selected = self._selected_disks()
        if not selected:
            self.set_message("select at least one free disk")
            return
        layout = build_layout(
            self.level,
            selected,
            mode=self.mode,
            name=self._name_input.value,
            stripe_kb=self.ctx.cfg.raid.stripe_kb,
            name_max_len=self.ctx.cfg.raid.name_max_len,
            init=self.ctx.cfg.raid.init,
        )
        errors = validate_layout(layout)
        if errors:
            self._preview.update("[b reverse]!! " + " ; ".join(errors) + "[/b reverse]")
            self.set_message("layout is invalid - nothing was submitted")
            return

        controller = self._controller()
        if controller is None or self.ctx.detection is None:
            self.set_message("no controller detection result - go back and press R")
            return
        provider = provider_for(self.ctx.detection)
        try:
            plans = provider.create_plans(controller, layout)
        except ValueError as exc:
            self.set_message(f"cannot build create commands: {exc}")
            return

        self.ctx.raid_epoch += 1
        epoch = self.ctx.raid_epoch
        stripe = f"{layout.stripe_kb}K" if layout.stripe_kb else "controller default"
        extra = [
            f"Level {layout.level.value}, mode {layout.mode.value}, "
            f"stripe {stripe}, init {layout.init}",
            f"Disks: " + ", ".join(f"{d.disk_id}@{d.location}" for d in selected),
            f"Usable capacity: ~{layout.capacity_human}",
        ]
        extra.extend(f"Warning: {warning}" for warning in layout.warnings)

        def _after(approved: bool) -> None:
            if not approved:
                self.set_message("RAID creation cancelled")
                return
            self.app.push_screen(
                RaidProgressScreen(self.ctx, plans, plans[0].action, self.controller_index, epoch)
            )

        self.app.push_screen(
            RaidConfirmModal(
                self.ctx,
                plans,
                f"Create {layout.level.value} on c{self.controller_index}",
                "yes",
                extra_lines=extra,
                epoch=epoch,
            ),
            _after,
        )


__all__ = ["RaidCreateScreen"]
