"""Pushed screen showing the physical members of one virtual drive."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

from ..focus import FocusLabelMixin, capture_focus
from ..models import HotkeyHint
from ..state import AppState
from ..widgets.breadcrumb import Breadcrumb
from ..widgets.disk_table import MultiSelectTable
from ..widgets.hotkey_bar import HotkeyBar


class VolumeMembersScreen(FocusLabelMixin, Screen):
    BINDINGS = [Binding("escape", "back", "Back", show=False)]

    def __init__(self, state: AppState, controller_index: int, volume_id: str) -> None:
        super().__init__()
        self.state = state
        self.controller_index = controller_index
        self.volume_id = volume_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="chrome-screen"):
            yield Breadcrumb("", id="members-breadcrumb")
            with Vertical(id="detail-body"):
                yield Static("", id="members-kv")
                yield MultiSelectTable(
                    id="members-table",
                    row_allowed=lambda disk: (True, ""),
                    vd_for=self._vd_label,
                )
            yield HotkeyBar("", id="members-hotkeys")

    def on_mount(self) -> None:
        self._focus = capture_focus()
        self.state.push_breadcrumb(f"{self.volume_id} members")
        self._load()
        self._snapshot("13b-volume-members")

    def on_unmount(self) -> None:
        self._focus.restore()
        self.state.pop_breadcrumb()

    def _load(self) -> None:
        view = self.state.volume_by_id(self.volume_id, self.controller_index)
        members = self.state.members_for_volume(view) if view else []
        if view is None:
            self.query_one("#members-kv", Static).update("Virtual drive no longer present.")
            self.query_one(MultiSelectTable).load_disks([])
        else:
            lines = [
                f"VD       : {view.volume_id}",
                f"Controller: c{view.controller_index}   Level: {view.level_label}   State: {view.state}",
                f"Name     : {view.name}",
                f"Capacity : {view.size_human}",
                f"Members  : {view.members_label}",
                f"Spans    : {len(view.spans)}",
            ]
            for index, span in enumerate(view.spans, start=1):
                lines.append(f"  span {index}: {', '.join(span)}")
            self.query_one("#members-kv", Static).update("\n".join(lines))
            self.query_one(MultiSelectTable).load_disks(members, set(), None)
        self._chrome(members)

    def _vd_label(self, disk) -> str:
        return self.state.disk_membership().get(disk.disk_id, "free")

    def _chrome(self, members) -> None:
        try:
            self.query_one(Breadcrumb).update_items(self.state.breadcrumb_items())
            hints = [
                HotkeyHint("Esc", "back"),
                HotkeyHint("Ctrl+Q", "quit"),
            ]
            self.query_one(HotkeyBar).update_hints(hints, self.app.theme)
        except Exception:  # pragma: no cover
            pass

    def on_data_refreshed(self, message) -> None:
        self._load()

    def action_back(self) -> None:
        self.app.pop_screen()

    def _snapshot(self, name: str) -> None:
        if not self.state.cfg.evidence.snapshot_on_screen_change:
            return

        async def _capture() -> None:
            try:
                await self.app.take_snapshot(name)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass

        self.run_worker(_capture, name=f"snapshot-{name}")


__all__ = ["VolumeMembersScreen"]
