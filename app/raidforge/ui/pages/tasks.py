"""Task queue plus a live RichLog of the selected task."""

from __future__ import annotations

from typing import List

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, RichLog, Static

from ..models import HotkeyHint, PageId, RefreshScope, TaskItem, TaskStatus
from ..palette import colour_for
from ..widgets.tables import TaskTable
from .base import StatefulPage


class TasksPage(StatefulPage):
    PAGE = PageId.TASKS
    BINDINGS = [Binding("c", "cancel", "Cancel", show=False)]
    SCOPES = frozenset({RefreshScope.TASKS})

    def compose(self) -> ComposeResult:
        with Vertical(id="task-queue-pane"):
            yield TaskTable(id="task-queue")
            yield Static("", id="task-summary")
        yield RichLog(id="task-log", markup=False, wrap=True, auto_scroll=True)

    def hotkeys(self) -> List[HotkeyHint]:
        return [
            HotkeyHint("C", "cancel", enabled=self.state.active_task_id is not None),
            HotkeyHint("Enter", "log"),
            HotkeyHint("F5", "refresh"),
            HotkeyHint("Ctrl+P", "commands"),
            HotkeyHint("Ctrl+Q", "quit"),
        ]

    def focus_primary(self) -> None:
        try:
            self.query_one(TaskTable).focus()
        except Exception:  # pragma: no cover
            pass

    def apply_layout_mode(self, mode) -> None:
        try:
            self.query_one(TaskTable).set_layout_mode(mode)
        except Exception:  # pragma: no cover - page may not be mounted yet
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        if not self.is_mounted:
            return
        table = self.query_one(TaskTable)
        table.load(self.state.task_queue, self.state.task_cursor)
        self._summary()
        task = self._selected_task()
        if task is not None:
            self.state.task_cursor = task.id
            self._render_log(task)

    def _selected_task(self) -> TaskItem | None:
        cursor = self.state.task_cursor
        if cursor:
            for task in self.state.task_queue:
                if task.id == cursor:
                    return task
        return self.state.task_queue[-1] if self.state.task_queue else None

    def _summary(self) -> None:
        counts = {status: 0 for status in TaskStatus}
        for task in self.state.task_queue:
            counts[task.status] += 1
        self.query_one("#task-summary", Static).update(
            f"queued {counts[TaskStatus.QUEUED]}  running {counts[TaskStatus.RUNNING]}  "
            f"ok {counts[TaskStatus.SUCCEEDED]}  partial {counts[TaskStatus.PARTIAL]}  "
            f"failed {counts[TaskStatus.FAILED]}  cancelled {counts[TaskStatus.CANCELLED]}"
        )

    def _render_log(self, task: TaskItem) -> None:
        log = self.query_one(RichLog)
        log.clear()
        if not task.log_lines:
            log.write(Text("(no log lines yet)"))
            return
        for line in task.log_lines:
            log.write(Text(line, style=self._style_for(line)))

    def _style_for(self, line: str):
        if "FAIL" in line or "failed" in line.lower():
            return colour_for("bad", self.app.theme)
        if "OK" in line:
            return colour_for("ok", self.app.theme)
        if "⚠" in line or "cancel" in line.lower():
            return colour_for("warn", self.app.theme)
        return colour_for("muted", self.app.theme)

    # -- events -------------------------------------------------------------
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "task-queue":
            return
        self.state.task_cursor = str(event.row_key.value)
        task = self._selected_task()
        if task is not None:
            self._render_log(task)
        self.refresh_chrome()

    def on_task_state_changed(self, message) -> None:
        self.refresh_data()
        self.refresh_chrome()

    # -- actions ------------------------------------------------------------
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "task-queue":
            self.query_one(RichLog).focus()

    def action_cancel(self) -> None:
        runner = self.state.task_runner
        if runner is None or not runner.cancel_active():  # type: ignore[union-attr]
            self.toast("no running task")
            return
        self.toast("cancel requested; waiting for the current command")
        self.refresh_data()


__all__ = ["TasksPage"]
