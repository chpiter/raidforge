"""Row-selectable disk table with a visible cursor (constraint 4 / UI hints).

The table owns the selection state.  Every row keeps its own ``selected`` flag;
wherever the running Textual version exposes ``Row.meta`` we mirror the flag
there too, so the state survives operations that rebuild individual cells.

The cursor is a literal ``>`` in a dedicated first column, moved by
:meth:`DiskTable.watch_cursor_coordinate`.  Textual's own row cursor
(``.datatable--cursor``) only paints the last cell of the row on the 16-colour
console, which reads as a rendering fault; ``ui/theme.py`` neutralises it.
``show_cursor`` therefore stays enabled - turning it off also disables the
arrow-key actions.

An earlier version painted the cursor row cell-by-cell with Rich styles; that
fought with the widget's own rendering and only the last cell kept the colour,
so it is gone.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from textual.binding import Binding
from textual.widgets import DataTable

from ...core.models import DiskInfo

# First column is a plain-text cursor marker: the console has 16 colours and no
# reliable way to paint a highlight band, but a ">" is impossible to miss.
COLUMNS: List[str] = ["", "Sel", "ID", "Location", "Media", "Proto", "Model", "Serial", "Size", "FW/RAID"]
# Explicit keys: `add_column` derives the key from the ``key`` argument, not
# from the label, so leaving it unset makes every cell unaddressable.
COLUMN_KEYS: List[str] = ["cur"] + [label.lower() for label in COLUMNS[1:]]
STATUS_COL = "fw/raid"
SEL_COL = "sel"
CUR_COL = "cur"

CHECKED = "[X]"
UNCHECKED = "[ ]"



class DiskTable(DataTable):
    """A :class:`~textual.widgets.DataTable` with a row cursor and Space toggles.

    Keyboard contract:

    * ``Up`` / ``Down`` / ``PgUp`` / ``PgDn`` / ``Home`` / ``End`` move the row
      cursor (built into :class:`DataTable`);
    * ``Tab`` moves focus onto the table, so the cursor keys start working
      without touching the mouse;
    * ``Space`` toggles the row under the cursor.

    ``Enter`` is deliberately *not* bound here: it has to bubble up to the
    screen, which opens the confirmation dialog.
    """

    BINDINGS = [
        Binding("space", "toggle_selection", "Toggle", show=False),
    ]

    def __init__(
        self,
        on_change: Optional[Callable[[str], None]] = None,
        row_allowed: Optional[Callable[[DiskInfo], Tuple[bool, str]]] = None,
        **kwargs,
    ) -> None:
        # ``cursor_type="row"`` is what makes Textual post RowSelected on Enter
        # and keep the cursor on a whole row rather than a single cell.  Note
        # that ``show_cursor=False`` is NOT an option: Textual gates its own
        # action_cursor_up/down on that flag, so the arrow keys would die too.
        # No zebra stripes: on the 16-colour console they fight with the cursor
        # style (only the last cell of the highlighted row kept the colour), and
        # alternating greys add nothing on a black background.
        super().__init__(cursor_type="row", zebra_stripes=False, **kwargs)
        self.on_change = on_change
        self._row_allowed = row_allowed or self.default_row_allowed
        self._row_keys: List[str] = []
        self._rows_meta: Dict[str, Dict[str, object]] = {}
        self._cells: Dict[str, Dict[str, str]] = {}

    def default_row_allowed(self, disk: DiskInfo) -> Tuple[bool, str]:
        """Default rule: UNKNOWN media can never be selected."""

        if not disk.selectable:
            return False, f"disk {disk.disk_id}: media type UNKNOWN - selection forbidden"
        return True, ""

    def allowed(self, disk: DiskInfo) -> Tuple[bool, str]:
        """Return ``(allowed, reason)`` for a row (overridable via ctor)."""

        try:
            return self._row_allowed(disk)
        except Exception:  # pragma: no cover - callbacks must not break the table
            return False, "eligibility check failed"

    # -- population ---------------------------------------------------------
    def load_disks(self, disks: List[DiskInfo], selection: Optional[List[str]] = None) -> None:
        """(Re)populate the table from an inventory list.

        Args:
            disks: Full inventory in display order.
            selection: Disk ids that must start out selected.
        """

        selection = list(selection or [])
        self.clear(columns=True)
        for label, key in zip(COLUMNS, COLUMN_KEYS):
            self.add_column(label, key=key)
        self._row_keys = []
        self._rows_meta = {}
        self._cells = {}

        for disk in disks:
            key = disk.disk_id
            can_select, _reason = self.allowed(disk)
            selected = key in selection and can_select
            self._row_keys.append(key)
            self._rows_meta[key] = {"disk": disk, "selected": selected}
            values = {
                "cur": " ",
                "sel": CHECKED if selected else (UNCHECKED if can_select else "--"),
                "id": disk.disk_id,
                "location": disk.location,
                "media": disk.medium.value,
                "proto": disk.protocol.value,
                "model": disk.model or "-",
                "serial": disk.serial or "-",
                "size": disk.size_human,
                "fw/raid": disk.firmware_or_raid,
            }
            self._cells[key] = values
            self.add_row(*(values[column] for column in COLUMN_KEYS), key=key)
            self._mirror_meta(key, selected)
        if self._row_keys:
            self.move_cursor(row=0)
            self._move_marker(None, self._row_keys[0])

    # -- selection ----------------------------------------------------------
    @property
    def selected_ids(self) -> List[str]:
        """Disk ids currently ticked."""

        return [key for key in self._row_keys if self._rows_meta.get(key, {}).get("selected")]

    def selected_disks(self) -> List[DiskInfo]:
        """Resolved :class:`DiskInfo` objects for the ticked rows."""

        return [self._rows_meta[key]["disk"] for key in self.selected_ids]  # type: ignore[misc]

    def toggle_selection(self) -> str:
        """Toggle the row under the cursor.

        Returns:
            A short status message suitable for the screen's status line.  Rows
            whose media type is UNKNOWN are never toggled (constraint 8).
        """

        key = self.current_row_key
        if key is None:
            return "no row selected"
        meta = self._rows_meta.get(key)
        if meta is None:  # pragma: no cover - defensive
            return "unknown row"
        disk: DiskInfo = meta["disk"]  # type: ignore[assignment]
        can_select, reason = self.allowed(disk)
        if not can_select:
            return reason or f"disk {disk.disk_id}: selection forbidden"

        selected = not bool(meta.get("selected"))
        meta["selected"] = selected
        self._set_cell(key, SEL_COL, CHECKED if selected else UNCHECKED)
        self._mirror_meta(key, selected)
        self._notify(f"{disk.disk_id} {'selected' if selected else 'deselected'}")
        return f"{disk.disk_id} {'selected' if selected else 'deselected'}"

    def select_all(self, medium: Optional[str] = None) -> int:
        """Select every selectable row (optionally restricted to one medium).

        Returns:
            How many rows ended up selected.
        """

        for key in self._row_keys:
            disk: DiskInfo = self._rows_meta[key]["disk"]  # type: ignore[assignment]
            can_select, _reason = self.allowed(disk)
            if not can_select:
                continue
            if medium and disk.medium.value != medium.upper():
                continue
            self._rows_meta[key]["selected"] = True
            self._set_cell(key, SEL_COL, CHECKED)
            self._mirror_meta(key, True)
        return len(self.selected_ids)

    def clear_selection(self) -> None:
        """Untick every row."""

        for key in self._row_keys:
            self._rows_meta[key]["selected"] = False
            self._set_cell(key, SEL_COL, UNCHECKED)
            self._mirror_meta(key, False)

    # -- helpers ------------------------------------------------------------
    @property
    def current_row_key(self) -> Optional[str]:
        """Row key under the cursor, tolerant of Textual API drift."""

        try:
            index = self.cursor_row
            if 0 <= index < len(self._row_keys):
                return self._row_keys[index]
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            return str(self.coordinate_to_cell_key(self.cursor_coordinate).row.value)
        except Exception:
            return None

    def watch_cursor_coordinate(self, old_coordinate, new_coordinate) -> None:
        """Keep the ">" marker on the row the cursor is sitting on."""

        super().watch_cursor_coordinate(old_coordinate, new_coordinate)
        old_key = None
        if 0 <= old_coordinate.row < len(self._row_keys):
            old_key = self._row_keys[old_coordinate.row]
        self._move_marker(old_key, self.current_row_key)

    def _move_marker(self, old_key: Optional[str], new_key: Optional[str]) -> None:
        if old_key and old_key != new_key:
            self._set_cell(old_key, CUR_COL, " ")
        if new_key:
            self._set_cell(new_key, CUR_COL, ">")

    def _set_cell(self, key: str, column: str, value: str) -> None:
        """Write one cell, keeping the cached text in sync with what is shown."""

        self._cells.setdefault(key, {})[column] = value
        self.update_cell(key, column, value)

    def _mirror_meta(self, key: str, selected: bool) -> None:
        """Write the flag to ``Row.meta`` when that attribute exists."""

        try:
            row = self.rows.get(key) if hasattr(self, "rows") else None
        except Exception:  # pragma: no cover
            row = None
        if row is not None and hasattr(row, "meta"):
            try:
                row.meta = {"selected": selected}  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass

    def _notify(self, message: str) -> None:
        """Report a change to whoever owns the table (usually the screen)."""

        if callable(self.on_change):
            try:
                self.on_change(message)
            except Exception:  # pragma: no cover - UI callbacks must not raise
                pass

    def action_toggle_selection(self) -> None:
        """Binding target for Space."""

        self.toggle_selection()
