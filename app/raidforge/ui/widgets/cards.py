"""Dashboard cards and the recent-task panel."""

from __future__ import annotations

from typing import Iterable, Union

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from ..models import TaskItem


class StatCard(Vertical):
    """A round-bordered card with a coloured title and a compact body."""

    def __init__(self, title: str, *, id: str, classes: str = "") -> None:
        super().__init__(id=id, classes=f"stat-card {classes}".strip())
        self._title_text = title

    def compose(self) -> ComposeResult:
        yield Static(self._title_text, classes="card-title")
        yield Static("", classes="card-body")

    def update_body(self, value: Union[str, Text]) -> None:
        self.query_one(".card-body", Static).update(value)


class RecentTasksPanel(Vertical):
    """Bottom strip on the dashboard; Enter jumps to the Tasks page."""

    def __init__(self, on_open) -> None:
        super().__init__(id="recent-tasks")
        self._on_open = on_open
        self._table = DataTable(
            id="recent-tasks-table",
            cursor_type="row",
            zebra_stripes=True,
            show_row_labels=False,
            cell_padding=1,
        )

    def compose(self) -> ComposeResult:
        yield Static("Recent tasks", classes="section-title")
        yield self._table

    def on_mount(self) -> None:
        self._table.add_column("Time", width=7)
        self._table.add_column("Task", width=14)
        self._table.add_column("Target", width=11)
        self._table.add_column("State", width=10)
        self._table.add_column("Detail", width=24)

    def load(self, tasks: Iterable[TaskItem]) -> None:
        self._table.clear()
        for task in tasks:
            self._table.add_row(
                (task.finished_at or task.created_at or "-")[11:19],
                _clip(task.title, 15),
                _clip(task.target, 12),
                f"{task.status_icon} {task.status.value}",
                _clip(task.error or task.progress_text, 26),
                key=task.id,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key is not None:
            self._on_open(str(event.row_key.value))


def _clip(text: str, width: int) -> str:
    text = str(text or "-")
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


__all__ = ["RecentTasksPanel", "StatCard"]
