"""Disk list screen: inventory table with row selection and erase trigger."""

from __future__ import annotations

from dataclasses import replace
from typing import List

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import DataTable, Label, Static

from ...core import commands as cmd_mod
from ...core.models import CommandPlan, DiskInfo, Medium
from ..state import AppContext
from ..widgets import DiskTable, KeyBar, SessionStatusBar
from .base import BaseScreen

SCREEN_NAME = "Disk list"


class DiskListScreen(BaseScreen):
    """Shows every discovered disk; arrows move the cursor, Space toggles."""

    SCREEN_NAME = SCREEN_NAME
    HINTS = [
        ("Up/Dn", "move"),
        ("Space", "toggle"),
        ("A", "all"),
        ("N", "none"),
        ("Enter", "erase"),
        ("Esc", "back"),
        ("Q", "quit"),
    ]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self._headline = Static("", classes="muted")
        # The table owns the Space binding; it reports back so this screen can
        # repaint its headline and status line without duplicating the toggle.
        self._table = DiskTable(on_change=self._on_selection_changed)

    def compose(self) -> ComposeResult:  # noqa: D102 - see BaseScreen.compose
        yield SessionStatusBar(self.ctx, SCREEN_NAME)
        with Container(id="content"):
            yield Label("[b]Physical disks[/b]", classes="title")
            yield self._headline
            yield self._table
            yield self.status_line()
        yield KeyBar(self.HINTS)

    def on_mount(self) -> None:
        """Load the inventory built by the dashboard and take keyboard focus."""

        self._table.load_disks(self.ctx.disks, self.ctx.selection)
        # Without an explicit focus the DataTable never sees Up/Down/Space, so
        # the row cursor looks dead until the operator happens to press Tab.
        self._table.focus()
        self._render_headline()
        self.set_message(f"{len(self.ctx.disks)} disks - Up/Down move, Space toggle, Enter erase")
        self.schedule_snapshot("03-disk-list")

    # -- rendering ----------------------------------------------------------
    def _render_headline(self) -> None:
        disks = self.ctx.disks
        hdd = sum(1 for d in disks if d.medium is Medium.HDD)
        ssd = sum(1 for d in disks if d.medium is Medium.SSD)
        unknown = sum(1 for d in disks if d.medium is Medium.UNKNOWN)
        self._headline.update(
            f"HDD {hdd} | SSD {ssd} | UNKNOWN {unknown} (not selectable)  selected={len(self._table.selected_ids)}"
        )

    def _sync_selection(self) -> None:
        """Push the table's selection back into the shared context."""

        self.ctx.selection = list(self._table.selected_ids)
        self._render_headline()

    # -- input --------------------------------------------------------------
    def on_key(self, event) -> None:
        """Keyboard shortcuts.

        Space is handled by :class:`DiskTable` itself (its Space binding fires
        before the key bubbles here), so it must NOT be toggled again here.
        Enter is handled through :meth:`on_data_table_row_selected`, because
        DataTable owns that key (``enter -> select_cursor``).
        """

        key = getattr(event, "key", "")
        if key in ("a",):
            count = self._table.select_all()
            self._sync_selection()
            self.set_message(f"{count} disks selected")
            event.stop()
        elif key in ("n",):
            self._table.clear_selection()
            self._sync_selection()
            self.set_message("selection cleared")
            event.stop()
        elif key == "escape":
            self.app.pop_screen()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on the focused row starts the erase flow (constraint 8 order)."""

        event.stop()
        self.start_erase()

    def _on_selection_changed(self, message: str) -> None:
        """Callback triggered by the disk table after every toggle."""

        self._sync_selection()
        self.set_message(message)

    # -- erase flow ---------------------------------------------------------
    def start_erase(self) -> None:
        """Validate the selection and open the confirmation modal.

        Nothing is executed here: the operator must first review the dry run
        and type the confirmation word (constraint 8).
        """

        selected: List[DiskInfo] = self._table.selected_disks()
        self._sync_selection()
        if not selected:
            self.set_message("nothing selected")
            return

        unselectable = [disk.disk_id for disk in selected if not disk.selectable]
        if unselectable:
            self.set_message(f"refusing: unknown media -> {', '.join(unselectable)}")
            return

        try:
            plans: List[CommandPlan] = cmd_mod.plan_for_selection(selected, self.ctx.cfg)
        except ValueError as exc:
            self.set_message(f"cannot build commands: {exc}")
            return

        # Freeze the DiskInfo objects at plan time.  A background rescan can
        # replace ctx.disks while the vendor command is running; the report must
        # still describe the disks the operator actually selected.
        snapshot = {disk.disk_id: replace(disk) for disk in selected}

        self.ctx.run_epoch += 1
        self.ctx.evidence.emit(
            "selection_confirmed_ui",
            run_epoch=self.ctx.run_epoch,
            disks=[disk.disk_id for disk in selected],
            planned=[plan.to_json_obj() for plan in plans],
        )

        from .confirm import ConfirmModal
        from .progress import ProgressScreen

        def _after_confirm(approved: bool) -> None:
            if not approved:
                self.set_message("erase cancelled")
                return
            self.app.push_screen(ProgressScreen(self.ctx, plans, snapshot))

        self.app.push_screen(ConfirmModal(self.ctx, plans, snapshot), _after_confirm)
