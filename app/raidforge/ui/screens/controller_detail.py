"""Pushed controller detail screen: KV, virtual drives and physical disks."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Static

from ...agent.queue import submit_local
from ...core.models import RaidAction
from ..focus import FocusLabelMixin, capture_focus
from ..models import ConfirmRequest, HotkeyHint, RefreshScope, TaskKind
from ..state import AppState
from ..tasks import make_task, raid_job, refresh_job
from ..widgets.breadcrumb import Breadcrumb
from ..widgets.disk_table import MultiSelectTable
from ..widgets.hotkey_bar import HotkeyBar
from ..widgets.tables import VolumeTable


class ControllerDetailScreen(FocusLabelMixin, Screen):
    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("f5", "refresh", "Refresh", show=False),
        Binding("f7", "create", "Create", show=False),
        Binding("f9", "delete_volume", "Delete VD", show=False),
        Binding("f10", "clear_config", "Clear config", show=False),
    ]

    def __init__(self, state: AppState, controller_index: int) -> None:
        super().__init__()
        self.state = state
        self.controller_index = controller_index

    def compose(self) -> ComposeResult:
        with Vertical(classes="chrome-screen"):
            yield Breadcrumb("", id="detail-breadcrumb")
            with Vertical(id="detail-body"):
                yield Static("", id="detail-kv")
                yield VolumeTable(id="detail-volumes")
                yield MultiSelectTable(
                    id="detail-disks",
                    row_allowed=lambda disk: (True, ""),
                    vd_for=self._vd_label,
                )
            yield HotkeyBar("", id="detail-hotkeys")

    def on_mount(self) -> None:
        self._focus = capture_focus()
        self.state.push_breadcrumb(f"c{self.controller_index}")
        self._load()
        try:
            self.query_one(VolumeTable).focus()
        except Exception:  # pragma: no cover
            pass
        self.state.evidence.emit("ui_controller_detail", controller_index=self.controller_index)
        self._snapshot("11b-controller-detail")

    def on_unmount(self) -> None:
        self._focus.restore()
        self.state.pop_breadcrumb()

    def on_data_refreshed(self, message) -> None:
        if message.scope in (RefreshScope.ALL, RefreshScope.CONTROLLER, RefreshScope.DISKS):
            self._load()

    # -- data ---------------------------------------------------------------
    def _controller(self):
        controller = self.state.controller_by_index(self.controller_index)
        if controller is not None:
            return controller
        from ...core.models import ControllerInfo, Vendor

        topology = self.state.raid_topology(self.controller_index)
        return ControllerInfo(
            index=self.controller_index,
            vendor=topology.vendor if topology else Vendor.GENERIC,
            tool=topology.tool if topology else "",
            binary=topology.binary if topology else "",
        )

    def _load(self) -> None:
        controller = self._controller()
        topology = self.state.raid_topology(self.controller_index)
        caps = self.state.raid_capabilities(self.controller_index)
        lines = [
            f"Vendor   : {controller.vendor.label}",
            f"Tool     : {controller.tool or '-'} ({controller.binary or '-'})",
            f"Model    : {controller.model or '-'}",
            f"Firmware : {controller.firmware or '-'}",
            f"Serial   : {controller.serial or '-'}",
            f"Actions  : create={'yes' if caps.create else 'no'}  "
            f"delete={'yes' if caps.delete_vd else 'no'}  clear={'yes' if caps.clear_config else 'no'}",
        ]
        if topology is None:
            lines.append("Topology : not collected - press F5")
        elif topology.error:
            lines.append(f"Topology : ERROR {topology.error}")
        else:
            lines.append(
                f"Topology : {len(topology.volumes)} VD / {len(topology.free_disk_ids)} free / "
                f"{len(topology.blocked)} blocked"
            )
        self.query_one("#detail-kv", Static).update("\n".join(lines))
        views = [view for view in self.state.virtual_disks if view.controller_index == self.controller_index]
        self.query_one(VolumeTable).load(views, self.state.virtual_cursor)
        disks = self.state.controller_disks(self.controller_index)
        self.query_one(MultiSelectTable).set_layout_mode(self.state.layout_mode)
        self.query_one(MultiSelectTable).load_disks(disks, set(), self.state.physical_cursor)
        self._chrome()

    def _vd_label(self, disk) -> str:
        membership = self.state.disk_membership().get(disk.disk_id)
        if membership:
            return membership
        return "blocked" if self.state.blocked_reason(disk) else "free"

    def _selected_view(self):
        cursor = self.state.virtual_cursor
        if cursor:
            for view in self.state.virtual_disks:
                if view.key == cursor and view.controller_index == self.controller_index:
                    return view
        for view in self.state.virtual_disks:
            if view.controller_index == self.controller_index:
                return view
        return None

    def _chrome(self) -> None:
        try:
            self.query_one(Breadcrumb).update_items(self.state.breadcrumb_items())
            hints = [
                HotkeyHint("F7", "create", danger=True),
                HotkeyHint("F9", "delete", danger=True, enabled=self._selected_view() is not None),
                HotkeyHint(
                    "F10",
                    "clear cfg",
                    danger=True,
                    enabled=self.state.raid_capabilities(self.controller_index).clear_config,
                ),
                HotkeyHint("F5", "refresh"),
                HotkeyHint("Esc", "back"),
                HotkeyHint("Ctrl+Q", "quit"),
            ]
            self.query_one(HotkeyBar).update_hints(hints, self.app.theme)
        except Exception:  # pragma: no cover - initial compose
            pass

    # -- events -------------------------------------------------------------
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "detail-volumes":
            self.state.virtual_cursor = str(event.row_key.value)
            self._chrome()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        runner = self.state.task_runner
        services = self.state.services
        if runner is None or services is None:
            return
        task = make_task(
            TaskKind.REFRESH,
            f"Refresh c{self.controller_index}",
            controller_index=self.controller_index,
        )
        runner.submit(  # type: ignore[union-attr]
            task,
            refresh_job(services, RefreshScope.CONTROLLER, self.controller_index),
        )

    def action_create(self) -> None:
        caps = self.state.raid_capabilities(self.controller_index)
        if not caps.create:
            self.state.last_message = "RAID creation is not available"
            return
        from .raid_wizard import RaidWizardScreen

        self.app.push_screen(RaidWizardScreen(self.state, self.controller_index), self._after_wizard)

    def _after_wizard(self, _result=None) -> None:
        self._load()

    def action_delete_volume(self) -> None:
        view = self._selected_view()
        if view is None:
            self.state.last_message = "no virtual drive selected"
            return
        services = self.state.services
        try:
            plans = services.plan_delete(self.controller_index, view.volume_id)  # type: ignore[union-attr]
        except Exception as exc:
            self.state.last_message = f"cannot build delete plan: {exc}"
            return
        epoch = self.state.raid_epoch + 1
        self.state.raid_epoch = epoch
        request = ConfirmRequest(
            title=f"Delete virtual drive {view.volume_id} on c{self.controller_index}",
            action="delete",
            ack_word="yes",
            ack_label="yes",
            audit_token=self.state.audit_token(),
            commands=[plan.dry_run_text for plan in plans],
            summary=[f"VD {view.volume_id}  {view.level_label}  {view.state}  {view.size_human}"],
            warnings=["IRREVERSIBLE: member data becomes inaccessible immediately."],
            danger=True,
            epoch=epoch,
            snapshot_name="04-raid-confirm",
            event_prefix="raid_confirm",
        )

        def after(approved: bool) -> None:
            if approved:
                task = make_task(
                    TaskKind.RAID_DELETE,
                    f"Delete {view.volume_id}",
                    controller_index=self.controller_index,
                    volume_id=view.volume_id,
                    epoch=epoch,
                )
                submit_local(
                    self.state,
                    task,
                    raid_job(services, plans, RaidAction.DELETE_VD, self.controller_index, epoch),
                )
                self.state.last_message = f"delete queued as {task.id}"

        from .confirm import ConfirmModal

        self.app.push_screen(ConfirmModal(self.state, request), after)

    def action_clear_config(self) -> None:
        caps = self.state.raid_capabilities(self.controller_index)
        if not caps.clear_config or not self.state.cfg.raid.allow_clear_config:
            self.state.last_message = "clearing the controller configuration is disabled"
            return
        services = self.state.services
        try:
            plans = services.plan_clear(self.controller_index)  # type: ignore[union-attr]
        except Exception as exc:
            self.state.last_message = f"cannot build clear plan: {exc}"
            return
        epoch = self.state.raid_epoch + 1
        self.state.raid_epoch = epoch
        request = ConfirmRequest(
            title=f"Clear ALL RAID configuration on c{self.controller_index}",
            action="clear",
            ack_word="yes",
            ack_label="yes",
            audit_token=self.state.audit_token(),
            commands=[plan.dry_run_text for plan in plans],
            summary=[
                "Deletes every virtual drive and clears foreign/stale RAID metadata.",
                "Every disk behind this controller becomes unconfigured.",
            ],
            warnings=["IRREVERSIBLE: type yes to clear every virtual drive and foreign metadata."],
            danger=True,
            epoch=epoch,
            snapshot_name="04-raid-confirm",
            event_prefix="raid_confirm",
        )

        def after(approved: bool) -> None:
            if approved:
                task = make_task(
                    TaskKind.RAID_CLEAR,
                    f"Clear config c{self.controller_index}",
                    controller_index=self.controller_index,
                    epoch=epoch,
                )
                submit_local(
                    self.state,
                    task,
                    raid_job(services, plans, RaidAction.CLEAR_CONFIG, self.controller_index, epoch),
                )
                self.state.last_message = f"clear queued as {task.id}"

        from .confirm import ConfirmModal

        self.app.push_screen(ConfirmModal(self.state, request), after)

    def _snapshot(self, name: str) -> None:
        if not self.state.cfg.evidence.snapshot_on_screen_change:
            return

        async def _capture() -> None:
            try:
                await self.app.take_snapshot(name)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass

        self.run_worker(_capture, name=f"snapshot-{name}")


__all__ = ["ControllerDetailScreen"]
