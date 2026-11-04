"""Keyboard tables shared by Controllers, Virtual, Tasks and Evidence."""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

from ...core.models import ControllerInfo
from ..models import EvidenceSessionView, LayoutMode, TaskItem, VirtualDiskView
from .keyboard_table import KeyboardTable


def _clip(text: object, width: int) -> str:
    value = str(text or "-")
    return value if len(value) <= width else value[: max(0, width - 1)] + "…"


ControllerStandard = [
    ("ID", 3, "id"),
    ("Tool", 5, "tool"),
    ("Model", 8, "model"),
    ("VD", 2, "vd"),
    ("PD", 2, "pd"),
]
ControllerWide = [
    ("ID", 3, "id"),
    ("Tool", 5, "tool"),
    ("Model", 7, "model"),
    ("FW", 6, "fw"),
    ("Serial", 7, "serial"),
    ("VD", 2, "vd"),
    ("PD", 2, "pd"),
]

VolumeStandard = [
    ("VD", 6, "vd"),
    ("Ctrl", 4, "ctrl"),
    ("Level", 7, "level"),
    ("State", 9, "state"),
    ("Size", 8, "size"),
    ("Members", 30, "members"),
]
VolumeWide = [
    ("VD", 6, "vd"),
    ("Ctrl", 4, "ctrl"),
    ("Level", 7, "level"),
    ("Name", 12, "name"),
    ("State", 9, "state"),
    ("Size", 8, "size"),
    ("Members", 30, "members"),
    ("Spans", 6, "spans"),
]

TaskStandard = [
    ("ID", 5, "id"),
    ("Kind", 6, "kind"),
    ("Target", 6, "target"),
    ("State", 5, "state"),
    ("Prog", 4, "prog"),
]
TaskWide = [
    ("ID", 5, "id"),
    ("Kind", 6, "kind"),
    ("Target", 6, "target"),
    ("State", 5, "state"),
    ("Prog", 4, "prog"),
    ("Started", 7, "started"),
    ("Duration", 5, "duration"),
]

EvidenceStandard = [
    ("Session", 12, "session"),
    ("Files", 4, "files"),
    ("Report", 6, "report"),
]
EvidenceWide = [
    ("Session", 10, "session"),
    ("Files", 3, "files"),
    ("Report", 5, "report"),
    ("Size", 7, "size"),
    ("Upload", 7, "upload"),
]


class _ResponsiveTable(KeyboardTable):
    standard_columns: List[Tuple[str, int, str]] = []
    wide_columns: List[Tuple[str, int, str]] = []

    def __init__(self, layout_mode: LayoutMode = LayoutMode.STANDARD, **kwargs) -> None:
        kwargs.setdefault("zebra_stripes", True)
        kwargs.setdefault("show_row_labels", False)
        kwargs.setdefault("cell_padding", 1)
        super().__init__(**kwargs)
        self.layout_mode = layout_mode

    def set_layout_mode(self, mode: LayoutMode) -> None:
        self.layout_mode = mode

    def columns_for_mode(self) -> List[Tuple[str, int, str]]:
        return self.wide_columns if self.layout_mode is LayoutMode.WIDE else self.standard_columns

    def _rebuild_columns(self) -> None:
        self.clear(columns=True)
        for label, width, key in self.columns_for_mode():
            self.add_column(label, width=width, key=key)


class ControllerTable(_ResponsiveTable):
    standard_columns = ControllerStandard
    wide_columns = ControllerWide

    def load(
        self,
        controllers: Iterable[ControllerInfo],
        vd_counts: Dict[int, int],
        pd_counts: Dict[int, int],
        cursor: int | None = None,
    ) -> None:
        self._rebuild_columns()
        keys: List[str] = []
        wide = self.layout_mode is LayoutMode.WIDE
        for controller in controllers:
            key = f"c{controller.index}"
            keys.append(key)
            values = {
                "id": key,
                "tool": _clip(controller.tool or controller.vendor.value, 5),
                "model": _clip(controller.model, 7 if wide else 8),
                "fw": _clip(controller.firmware, 6),
                "serial": _clip(controller.serial, 7),
                "vd": str(vd_counts.get(controller.index, 0)),
                "pd": str(pd_counts.get(controller.index, 0)),
            }
            self.add_row(
                *(values[key_] for _, _, key_ in self.columns_for_mode()),
                key=key,
            )
        if keys:
            index = keys.index(f"c{cursor}") if cursor is not None and f"c{cursor}" in keys else 0
            self.move_cursor(row=index)


class VolumeTable(_ResponsiveTable):
    standard_columns = VolumeStandard
    wide_columns = VolumeWide

    def load(self, views: Iterable[VirtualDiskView], cursor: str | None = None) -> None:
        self._rebuild_columns()
        keys: List[str] = []
        for view in views:
            keys.append(view.key)
            values = {
                "vd": view.volume_id,
                "ctrl": f"c{view.controller_index}",
                "level": _clip(view.level_label, 7),
                "name": _clip(view.name, 12),
                "state": _clip(f"{view.status_icon} {view.state}", 9),
                "size": view.size_human,
                "members": _clip(view.members_label, 30),
                "spans": str(len(view.spans)),
            }
            self.add_row(
                *(values[key_] for _, _, key_ in self.columns_for_mode()),
                key=view.key,
            )
        if keys:
            index = keys.index(cursor) if cursor in keys else 0
            self.move_cursor(row=index)


class TaskTable(_ResponsiveTable):
    standard_columns = TaskStandard
    wide_columns = TaskWide

    def load(self, tasks: Iterable[TaskItem], cursor: str | None = None) -> None:
        self._rebuild_columns()
        keys: List[str] = []
        for task in tasks:
            keys.append(task.id)
            values = {
                "id": task.id,
                "kind": _clip(task.kind.value, 6),
                "target": _clip(task.target, 6),
                "state": _clip(f"{task.status_icon} {task.status.value}", 5),
                "prog": task.progress_text,
                "started": _clip(task.started_at[11:19] if task.started_at else "-", 7),
                "duration": "-",
            }
            self.add_row(
                *(values[key_] for _, _, key_ in self.columns_for_mode()),
                key=task.id,
            )
        if keys:
            index = keys.index(cursor) if cursor in keys else 0
            self.move_cursor(row=index)

    def update_task(self, task: TaskItem) -> None:
        try:
            self.update_cell(task.id, "state", _clip(f"{task.status_icon} {task.status.value}", 5))
            self.update_cell(task.id, "prog", task.progress_text)
        except Exception:  # pragma: no cover - row may be gone
            pass


class EvidenceSessionTable(_ResponsiveTable):
    standard_columns = EvidenceStandard
    wide_columns = EvidenceWide

    def load(self, sessions: Iterable[EvidenceSessionView], cursor: str | None = None) -> None:
        self._rebuild_columns()
        keys: List[str] = []
        wide = self.layout_mode is LayoutMode.WIDE
        for session in sessions:
            keys.append(session.session_id)
            values = {
                "session": _clip(session.session_id, 10 if wide else 12),
                "files": str(session.file_count),
                "report": session.report_state.value,
                "size": session.size_human,
                "upload": _clip(session.upload or "-", 7),
            }
            self.add_row(
                *(values[key_] for _, _, key_ in self.columns_for_mode()),
                key=session.session_id,
            )
        if keys:
            index = keys.index(cursor) if cursor in keys else 0
            self.move_cursor(row=index)


__all__ = [
    "ControllerTable",
    "EvidenceSessionTable",
    "TaskTable",
    "VolumeTable",
]
