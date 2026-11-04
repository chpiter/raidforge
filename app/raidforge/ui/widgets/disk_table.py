"""Keyboard-first multi-select disk table for Physical and the RAID wizard.

Textual 7.5.0 paints the row cursor across the full row, so there is no custom
``>`` marker column.  Standard mode hides Firmware/Protocol/Serial; Wide mode
restores them.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple

from rich.style import Style
from rich.text import Text
from textual.binding import Binding

from ...core.models import DiskInfo
from ..models import ICON_SELECTED, LayoutMode
from ..palette import THEME_MONO
from .keyboard_table import KeyboardTable

#: (label, width, key)
STANDARD_COLUMNS: List[Tuple[str, int, str]] = [
    ("", 2, "sel"),
    ("ID", 10, "id"),
    ("Ctl", 4, "ctrl"),
    ("Loc", 6, "loc"),
    ("Media", 5, "media"),
    ("Model", 10, "model"),
    ("Size", 7, "size"),
    ("State", 7, "state"),
    ("VD", 5, "vd"),
]

WIDE_COLUMNS: List[Tuple[str, int, str]] = [
    ("", 2, "sel"),
    ("ID", 10, "id"),
    ("Ctl", 4, "ctrl"),
    ("Loc", 6, "loc"),
    ("Media", 5, "media"),
    ("Proto", 5, "proto"),
    ("Model", 8, "model"),
    ("FW", 8, "fw"),
    ("Serial", 10, "serial"),
    ("Size", 7, "size"),
    ("State", 7, "state"),
    ("VD", 5, "vd"),
]

SEL_COL = "sel"
CHECKED = ICON_SELECTED
UNCHECKED = "○"
BLOCKED = "—"


class MultiSelectTable(KeyboardTable):
    """KeyboardTable with Space toggling, selection and responsive columns."""

    BINDINGS = [
        Binding("space", "toggle_selection", "Toggle", show=False),
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("a", "select_all", "Select all", show=False),
        Binding("ctrl+i", "invert_selection", "Invert", show=False),
        Binding("i", "invert_selection", "Invert", show=False),
    ]

    def __init__(
        self,
        on_change: Optional[Callable[[str], None]] = None,
        on_cursor: Optional[Callable[[Optional[str]], None]] = None,
        row_allowed: Optional[Callable[[DiskInfo], Tuple[bool, str]]] = None,
        vd_for: Optional[Callable[[DiskInfo], str]] = None,
        layout_mode: LayoutMode = LayoutMode.STANDARD,
        **kwargs,
    ) -> None:
        kwargs.setdefault("zebra_stripes", True)
        kwargs.setdefault("show_row_labels", False)
        kwargs.setdefault("cell_padding", 1)
        super().__init__(**kwargs)
        self.on_change = on_change
        self.on_cursor = on_cursor
        self._row_allowed = row_allowed or self.default_row_allowed
        self._vd_for = vd_for or (lambda disk: "free" if disk.selectable else "blocked")
        self.layout_mode = layout_mode
        self._row_keys: List[str] = []
        self._meta: Dict[str, Dict[str, object]] = {}
        self._disks: List[DiskInfo] = []
        self._selection: Set[str] = set()

    # -- eligibility --------------------------------------------------------
    def default_row_allowed(self, disk: DiskInfo) -> Tuple[bool, str]:
        if not disk.selectable:
            return False, "media type UNKNOWN - selection forbidden"
        return True, ""

    def allowed(self, disk: DiskInfo) -> Tuple[bool, str]:
        try:
            return self._row_allowed(disk)
        except Exception:  # pragma: no cover - callbacks must not break the table
            return False, "eligibility check failed"

    def columns_for_mode(self) -> List[Tuple[str, int, str]]:
        return WIDE_COLUMNS if self.layout_mode is LayoutMode.WIDE else STANDARD_COLUMNS

    def set_layout_mode(self, mode: LayoutMode) -> None:
        """Change column set; the page calls load_disks() afterwards."""

        self.layout_mode = mode

    # -- population ---------------------------------------------------------
    def load_disks(
        self,
        disks: List[DiskInfo],
        selected: Optional[Set[str]] = None,
        cursor_id: Optional[str] = None,
    ) -> None:
        selected = set(selected or set())
        self._disks = list(disks)
        self._selection = selected
        self._row_keys = []
        self._meta = {}
        self.clear(columns=True)
        for label, width, key in self.columns_for_mode():
            self.add_column(label, width=width, key=key)

        for disk in disks:
            key = disk.disk_id
            can_select, reason = self.allowed(disk)
            is_selected = key in selected and can_select
            self._row_keys.append(key)
            self._meta[key] = {"disk": disk, "selected": is_selected, "allowed": can_select}
            cells = self._cells_for(disk, is_selected, can_select, reason)
            self.add_row(
                *(cells[key_] for _, _, key_ in self.columns_for_mode()),
                key=key,
            )

        if cursor_id and cursor_id in self._row_keys:
            index = self._row_keys.index(cursor_id)
        else:
            index = 0 if self._row_keys else None
        if index is not None:
            self.move_cursor(row=index)
            self._notify_cursor(self._row_keys[index])

    def _cells_for(self, disk: DiskInfo, selected: bool, can_select: bool, reason: str) -> Dict[str, Text]:
        mono = getattr(self.app, "theme", "") == THEME_MONO
        dim = Style(dim=True) if not mono else Style()
        try:
            vd_text = str(self._vd_for(disk) or ("free" if can_select else "blocked"))
        except Exception:  # pragma: no cover - display callbacks must not break the table
            vd_text = "free" if can_select else "blocked"
        raw = {
            "sel": CHECKED if selected else (UNCHECKED if can_select else BLOCKED),
            "id": disk.disk_id,
            "ctrl": f"c{disk.controller_index}",
            "loc": disk.location,
            "media": disk.medium.value,
            "proto": disk.protocol.value,
            "model": disk.model or "-",
            "fw": disk.firmware_or_raid or "-",
            "serial": disk.serial or "-",
            "size": disk.size_human,
            "state": disk.firmware_or_raid or "-",
            "vd": vd_text,
        }
        style = None if can_select else dim
        return {key: Text(str(value), style=style) for key, value in raw.items()}

    # -- selection ----------------------------------------------------------
    @property
    def selected_ids(self) -> List[str]:
        return [key for key in self._row_keys if self._meta.get(key, {}).get("selected")]

    @property
    def current_row_key(self) -> Optional[str]:
        try:
            index = self.cursor_row
            if 0 <= index < len(self._row_keys):
                return self._row_keys[index]
        except Exception:  # pragma: no cover - defensive
            pass
        return None

    def toggle_selection(self) -> str:
        key = self.current_row_key
        if key is None:
            return "no row under the cursor"
        meta = self._meta.get(key)
        if meta is None:  # pragma: no cover - defensive
            return "unknown row"
        disk: DiskInfo = meta["disk"]  # type: ignore[assignment]
        can_select, reason = self.allowed(disk)
        if not can_select:
            return reason or f"disk {disk.disk_id}: selection forbidden"
        selected = not bool(meta.get("selected"))
        meta["selected"] = selected
        self.update_cell(key, SEL_COL, Text(CHECKED if selected else UNCHECKED))
        self._notify(f"{disk.disk_id} {'selected' if selected else 'deselected'}")
        self._notify_cursor(key)
        return f"{disk.disk_id} {'selected' if selected else 'deselected'}"

    def select_all(self) -> int:
        for key in self._row_keys:
            meta = self._meta[key]
            disk: DiskInfo = meta["disk"]  # type: ignore[assignment]
            can_select, _reason = self.allowed(disk)
            if not can_select:
                continue
            meta["selected"] = True
            self.update_cell(key, SEL_COL, Text(CHECKED))
        self._notify(f"{len(self.selected_ids)} disk(s) selected")
        return len(self.selected_ids)

    def invert_selection(self) -> int:
        for key in self._row_keys:
            meta = self._meta[key]
            disk: DiskInfo = meta["disk"]  # type: ignore[assignment]
            can_select, _reason = self.allowed(disk)
            if not can_select:
                continue
            meta["selected"] = not bool(meta.get("selected"))
            self.update_cell(
                key, SEL_COL, Text(CHECKED if meta["selected"] else UNCHECKED)
            )
        self._notify(f"{len(self.selected_ids)} disk(s) selected")
        return len(self.selected_ids)

    def clear_selection(self) -> None:
        for key in self._row_keys:
            meta = self._meta[key]
            disk: DiskInfo = meta["disk"]  # type: ignore[assignment]
            can_select, _reason = self.allowed(disk)
            meta["selected"] = False
            self.update_cell(key, SEL_COL, Text(UNCHECKED if can_select else BLOCKED))
        self._notify("selection cleared")

    # -- cursor -------------------------------------------------------------
    def watch_cursor_coordinate(self, old_coordinate, new_coordinate) -> None:
        super().watch_cursor_coordinate(old_coordinate, new_coordinate)
        self._notify_cursor(self.current_row_key)

    def _notify(self, message: str) -> None:
        if callable(self.on_change):
            try:
                self.on_change(message)
            except Exception:  # pragma: no cover
                pass

    def _notify_cursor(self, key: Optional[str]) -> None:
        if callable(self.on_cursor):
            try:
                self.on_cursor(key)
            except Exception:  # pragma: no cover
                pass

    def action_toggle_selection(self) -> None:
        self.toggle_selection()

    def action_select_all(self) -> None:
        self.select_all()

    def action_invert_selection(self) -> None:
        self.invert_selection()


#: Old import name kept for compatibility.
DiskTable = MultiSelectTable


__all__ = [
    "BLOCKED",
    "CHECKED",
    "DiskTable",
    "MultiSelectTable",
    "STANDARD_COLUMNS",
    "UNCHECKED",
    "WIDE_COLUMNS",
]
