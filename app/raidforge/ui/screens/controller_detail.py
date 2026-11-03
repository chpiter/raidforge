"""One controller: details, virtual drives, physical drives and RAID actions."""

from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Label, Static

from ...core.models import ControllerInfo, RaidCommandPlan, Vendor
from ...core.raid import provider_for
from ..scan import run_scan
from ..state import AppContext
from ..widgets import KeyBar, SessionStatusBar
from ..widgets.raid_tables import RaidDiskTable, VolumeTable
from .base import BaseScreen
from .raid_confirm import RaidConfirmModal
from .raid_progress import RaidProgressScreen

SCREEN_NAME = "Controller"


class ControllerDetailScreen(BaseScreen):
    """Read-only topology plus the three RAID write entry points."""

    SCREEN_NAME = SCREEN_NAME
    HINTS = [
        ("C", "create RAID"),
        ("D", "delete VD"),
        ("X", "clear config"),
        ("E", "erase disks"),
        ("R", "rescan"),
        ("Tab", "switch table"),
        ("Esc", "back"),
        ("Q", "quit"),
    ]

    def __init__(self, ctx: AppContext, controller_index: int) -> None:
        super().__init__(ctx)
        self.controller_index = controller_index
        self._info = Static("", classes="muted")
        self._volumes = VolumeTable(id="volume-table")
        self._disks = RaidDiskTable(id="pd-table")
        self._scanning = False

    def compose(self) -> ComposeResult:
        yield SessionStatusBar(self.ctx, SCREEN_NAME)
        with Container(id="content"):
            yield Label(f"[b]Controller c{self.controller_index}[/b]", classes="title")
            yield self._info
            yield Label("[b]Virtual drives[/b]")
            yield self._volumes
            yield Label("[b]Physical drives[/b]")
            yield self._disks
            yield self.status_line()
        yield KeyBar(self.HINTS)

    def on_mount(self) -> None:
        self._load()
        self._volumes.focus()
        self.schedule_snapshot("02b-controller-detail")

    def on_screen_resume(self) -> None:
        self._load()

    # -- data ---------------------------------------------------------------
    def _controller(self) -> ControllerInfo:
        existing = self.ctx.controller_by_index(self.controller_index)
        if existing is not None:
            return existing
        topology = self.ctx.raid_topology(self.controller_index)
        if topology is not None:
            vendor = topology.vendor
        elif self.ctx.detection is not None:
            vendor = self.ctx.detection.vendor
        else:
            vendor = Vendor.GENERIC
        return ControllerInfo(
            index=self.controller_index,
            vendor=vendor,
            tool=topology.tool if topology else "",
            binary=topology.binary if topology else "",
        )

    def _load(self) -> None:
        controller = self._controller()
        topology = self.ctx.raid_topology(self.controller_index)
        caps = self.ctx.raid_capabilities(self.controller_index)
        lines: List[str] = [
            f"Vendor   : {controller.vendor.label}",
            f"Tool     : {controller.tool or '-'} ({controller.binary or '-'})",
            f"Model    : {controller.model or '-'}",
            f"Firmware : {controller.firmware or '-'}",
            f"Serial   : {controller.serial or '-'}",
            "Actions  : "
            + " ".join(
                [
                    "create=yes" if caps.create else "create=no",
                    "delete=yes" if caps.delete_vd else "delete=no",
                    "clear=yes" if caps.clear_config else "clear=no",
                ]
            ),
        ]
        for key, value in list(controller.details.items())[:8]:
            lines.append(f"{key:<16}: {value}")
        if topology is None:
            lines.append("Topology : not collected yet - press R to rescan")
        elif topology.error:
            lines.append(f"Topology : ERROR {topology.error}")
        else:
            lines.append(
                f"Topology : {len(topology.volumes)} volume(s), "
                f"{len(topology.free_disk_ids)} free disk(s), {len(topology.blocked)} blocked"
            )
        self._info.update("\n".join(lines))

        volumes = topology.volumes if topology else []
        self._volumes.load_volumes(volumes)
        disks = self.ctx.controller_disks(self.controller_index)
        membership = topology.membership if topology else {}
        free = topology.free_disk_ids if topology else []
        blocked = topology.blocked if topology else {}
        self._disks.load_disks(disks, membership, free, blocked)
        self.set_message(
            f"c{self.controller_index}: {len(volumes)} VD(s), {len(free)} free disk(s) - "
            "C create, D delete VD, X clear config, E erase disks"
        )

    def _provider(self):
        if self.ctx.detection is None:
            self.set_message("no detection result - press R to rescan")
            return None
        return provider_for(self.ctx.detection)

    # -- actions ------------------------------------------------------------
    def _open_confirm(
        self,
        plans: List[RaidCommandPlan],
        title: str,
        ack_word: str,
        extra_lines: Optional[List[str]] = None,
    ) -> None:
        if not plans:
            self.set_message("nothing to do")
            return
        self.ctx.raid_epoch += 1
        epoch = self.ctx.raid_epoch
        action = plans[0].action
        controller_index = self.controller_index

        def _after(approved: bool) -> None:
            if not approved:
                self.set_message("RAID action cancelled")
                return
            self.app.push_screen(
                RaidProgressScreen(
                    self.ctx,
                    plans,
                    action,
                    controller_index,
                    epoch,
                )
            )

        self.app.push_screen(
            RaidConfirmModal(
                self.ctx,
                plans,
                title,
                ack_word,
                extra_lines=extra_lines,
                epoch=epoch,
            ),
            _after,
        )

    def _create(self) -> None:
        caps = self.ctx.raid_capabilities(self.controller_index)
        if not caps.create:
            self.set_message("RAID creation is not available for this controller")
            return
        from .raid_create import RaidCreateScreen

        self.app.push_screen(RaidCreateScreen(self.ctx, self.controller_index))

    def _delete(self) -> None:
        volume = self._volumes.current_volume
        if volume is None:
            self.set_message("no virtual drive under the cursor")
            return
        provider = self._provider()
        if provider is None:
            return
        controller = self._controller()
        try:
            plans = provider.delete_plans(controller, [volume])
        except ValueError as exc:
            self.set_message(f"cannot build delete command: {exc}")
            return
        self._open_confirm(
            plans,
            f"Delete virtual drive {volume.volume_id} on c{self.controller_index}",
            "yes",
            [
                f"VD {volume.volume_id} {volume.level_label} {volume.state or '-'} {volume.size_human}",
                f"Members: {volume.members_label}",
                "The member disks become free after the VD is deleted.",
            ],
        )

    def _clear(self) -> None:
        caps = self.ctx.raid_capabilities(self.controller_index)
        if not caps.clear_config or not self.ctx.cfg.raid.allow_clear_config:
            self.set_message("clearing the controller configuration is disabled")
            return
        provider = self._provider()
        if provider is None:
            return
        controller = self._controller()
        try:
            plans = provider.clear_plans(controller)
        except ValueError as exc:
            self.set_message(f"cannot build clear commands: {exc}")
            return
        self._open_confirm(
            plans,
            f"Clear ALL RAID configuration on c{self.controller_index}",
            controller.controller_id,
            [
                "This deletes every virtual drive and clears foreign/stale RAID metadata.",
                "Every disk behind this controller becomes unconfigured.",
                "The acknowledgement word is the controller id, not 'yes'.",
            ],
        )

    def _rescan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self.set_message("rescanning controller and RAID topology...")

        def _worker() -> None:
            try:
                run_scan(self.ctx)
            except Exception as exc:  # pragma: no cover - UI must survive scan errors
                self.app.call_ui(lambda: self.set_message(f"scan failed: {exc}"))
            finally:
                self.app.call_ui(self._after_rescan)

        self.ctx.spawn(_worker, name="raid-detail-rescan")

    def _after_rescan(self) -> None:
        self._scanning = False
        self._load()
        self.refresh()

    def _go_disks(self) -> None:
        from .disk_list import DiskListScreen

        self.app.push_screen(DiskListScreen(self.ctx))

    # -- input --------------------------------------------------------------
    def on_key(self, event) -> None:
        key = getattr(event, "key", "")
        if key == "c":
            event.stop()
            self._create()
        elif key == "d":
            event.stop()
            self._delete()
        elif key == "x":
            event.stop()
            self._clear()
        elif key == "r":
            event.stop()
            self._rescan()
        elif key == "e":
            event.stop()
            self._go_disks()
        elif key == "escape":
            self.app.pop_screen()


__all__ = ["ControllerDetailScreen"]
