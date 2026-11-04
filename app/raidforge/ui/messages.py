"""Textual messages used by the multi-module shell.

The three domain messages are the contract between workers/pages:

``DataRefreshed``
    Something in AppState changed; the ``scope`` says what.
``TaskStateChanged``
    A queued/running task moved or produced a log line.
``ReportStatusChanged``
    Report/manifest/upload state changed.

Navigation, breadcrumb, hotkey and toast notifications are UI-only messages;
they carry no domain state.
"""

from __future__ import annotations

from typing import FrozenSet, List, Optional

from textual.message import Message

from .models import (
    BreadcrumbItem,
    ConfirmRequest,
    HotkeyHint,
    PageId,
    RefreshScope,
    ReportStatus,
    TaskStatus,
)


class DataRefreshed(Message):
    """Broadcast after a scan or a targeted refresh."""

    def __init__(
        self,
        scope: RefreshScope,
        controller_index: Optional[int] = None,
        disk_ids: Optional[FrozenSet[str]] = None,
        generation: int = 0,
    ) -> None:
        super().__init__()
        self.scope = scope
        self.controller_index = controller_index
        self.disk_ids = disk_ids or frozenset()
        self.generation = generation

    def clone(self) -> "DataRefreshed":
        return DataRefreshed(self.scope, self.controller_index, self.disk_ids, self.generation)


class TaskStateChanged(Message):
    """Broadcast after a task transition or a new log line."""

    def __init__(
        self,
        task_id: str,
        status: TaskStatus,
        done: int = 0,
        total: int = 0,
        message: str = "",
        log_line: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.task_id = task_id
        self.status = status
        self.done = done
        self.total = total
        self.message = message
        self.log_line = log_line

    def clone(self) -> "TaskStateChanged":
        return TaskStateChanged(
            self.task_id, self.status, self.done, self.total, self.message, self.log_line
        )


class ReportStatusChanged(Message):
    """Broadcast after report generation or an upload attempt."""

    def __init__(self, session_id: Optional[str], status: ReportStatus) -> None:
        super().__init__()
        self.session_id = session_id
        self.status = status

    def clone(self) -> "ReportStatusChanged":
        return ReportStatusChanged(self.session_id, self.status)


class NavigateRequested(Message):
    """Asked by a page or command palette to switch tabs."""

    def __init__(
        self,
        page: PageId,
        push_history: bool = True,
        focus_content: bool = True,
    ) -> None:
        super().__init__()
        self.page = page
        self.push_history = push_history
        #: Arrow-driven tab switches keep the NavBar focused; Enter opens the page.
        self.focus_content = focus_content


class BreadcrumbChanged(Message):
    """Ask the shell to repaint the breadcrumb."""

    def __init__(self, items: List[BreadcrumbItem], page: Optional[PageId] = None) -> None:
        super().__init__()
        self.items = items
        self.page = page


class HotkeysChanged(Message):
    """Ask the shell to replace the bottom bar."""

    def __init__(self, hints: List[HotkeyHint], page: Optional[PageId] = None) -> None:
        super().__init__()
        self.hints = hints
        self.page = page


class ConfirmRequested(Message):
    """A page wants ConfirmModal; the shell owns the actual push."""

    def __init__(self, request: ConfirmRequest) -> None:
        super().__init__()
        self.request = request


class Toast(Message):
    """Short non-blocking status text."""

    def __init__(self, text: str, level: str = "info", timeout: float = 3.0) -> None:
        super().__init__()
        self.text = text
        self.level = level
        self.timeout = timeout


__all__ = [
    "BreadcrumbChanged",
    "ConfirmRequested",
    "DataRefreshed",
    "HotkeysChanged",
    "NavigateRequested",
    "ReportStatusChanged",
    "TaskStateChanged",
    "Toast",
]
