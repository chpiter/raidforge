"""Read-only tables used by the RAID screens.

Both tables reuse the console-safe ">" cursor marker from :mod:`disk_table`:
Textual's own row highlight only paints the last cell on a 16-colour console.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from textual.widgets import DataTable

from ...core.models import DiskInfo, VolumeInfo

VOLUME_COLUMNS: List[str] = ["", "VD", "Level", "Name", "State", "Size", "Members"]
VOLUME_KEYS: List[str] = ["cur", "vd", "level", "name", "state", "size", "members"]

PD_COLUMNS: List[str] = ["", "ID", "Location", "Media", "Size", "State", "VD/Free"]
PD_KEYS: List[str] = ["cur", "id", "location", "media", "size", "state", "vd"]


class CursorTable(DataTable):
    """DataTable with a literal ``>`` cursor column and no selection."""

    CURSOR_COL = "cur"

    def __init__(self, **kwargs) -> None:
        super().__init__(cursor_type="row", zebra_stripes=False, **kwargs)
        self._row_keys: List[str] = []

    def watch_cursor_coordinate(self, old_coordinate, new_coordinate) -> None:
        super().watch_cursor_coordinate(old_coordinate, new_coordinate)
        old_key = self._key_at(old_coordinate.row)
        new_key = self._key_at(new_coordinate.row)
        if old_key and old_key != new_key:
            self._set_cell(old_key, self.CURSOR_COL, " ")
        if new_key:
            self._set_cell(new_key, self.CURSOR_COL, ">")

    def _key_at(self, row: int) -> Optional[str]:
        if 0 <= row < len(self._row_keys):
            return self._row_keys[row]
        return None

    def _set_cell(self, key: str, column: str, value: str) -> None:
        try:
            self.update_cell(key, column, value)
        except Exception:
            # clear(columns=True) resets the cursor before the new columns are
            # registered; the watcher must not crash the screen in that window.
            pass

    def move_to_first(self) -> None:
        if self._row_keys:
            self.move_cursor(row=0)
            self._set_cell(self._row_keys[0], self.CURSOR_COL, ">")


class VolumeTable(CursorTable):
    """Virtual-drive listing; the row under the cursor is the delete target."""

    def load_volumes(self, volumes: List[VolumeInfo]) -> None:
        self._volumes = list(volumes)
        self._row_keys = []
        self.clear(columns=True)
        for label, key in zip(VOLUME_COLUMNS, VOLUME_KEYS):
            self.add_column(label, key=key)
        self._row_keys = []
        for volume in volumes:
            key = volume.volume_id
            self._row_keys.append(key)
            self.add_row(
                "",
                volume.volume_id,
                volume.level_label,
                volume.name or "-",
                volume.state or "-",
                volume.size_human,
                volume.members_label,
                key=key,
            )
        self.move_to_first()

    @property
    def current_volume(self) -> Optional[VolumeInfo]:
        """Volume under the cursor, resolved from the table's source list."""

        try:
            row = self.cursor_row
        except Exception:  # pragma: no cover - defensive
            return None
        volumes = getattr(self, "_volumes", [])
        if 0 <= row < len(volumes):
            return volumes[row]
        return None

    def set_volumes(self, volumes: List[VolumeInfo]) -> None:
        self.load_volumes(volumes)


class RaidDiskTable(CursorTable):
    """Read-only physical-drive listing with a membership/free label."""

    def load_disks(
        self,
        disks: List[DiskInfo],
        membership: Dict[str, str],
        free_ids: List[str],
        blocked: Dict[str, str],
    ) -> None:
        self._row_keys = []
        self.clear(columns=True)
        for label, key in zip(PD_COLUMNS, PD_KEYS):
            self.add_column(label, key=key)
        self._row_keys = []
        free = set(free_ids)
        for disk in disks:
            key = disk.disk_id
            if disk.disk_id in membership:
                label = f"VD {membership[disk.disk_id]}"
            elif disk.disk_id in free:
                label = "free"
            elif disk.disk_id in blocked:
                label = "blocked"
            else:
                label = "-"
            self._row_keys.append(key)
            self.add_row(
                "",
                disk.disk_id,
                disk.location,
                disk.medium.value,
                disk.size_human,
                disk.raid_state or "-",
                label,
                key=key,
            )
        self.move_to_first()


__all__ = ["CursorTable", "RaidDiskTable", "VolumeTable"]
