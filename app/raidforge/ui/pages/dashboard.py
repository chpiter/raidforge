"""Dashboard: four status cards plus recent tasks."""

from __future__ import annotations

import socket
from typing import List

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Grid

from ..messages import NavigateRequested
from ..models import HotkeyHint, PageId, RefreshScope, StatusKind
from ..palette import status_icon, status_style
from ..widgets.cards import RecentTasksPanel, StatCard
from .base import StatefulPage


class DashboardPage(StatefulPage):
    PAGE = PageId.DASHBOARD
    SCOPES = frozenset(
        {
            RefreshScope.ALL,
            RefreshScope.CONTROLLER,
            RefreshScope.DISKS,
            RefreshScope.TASKS,
            RefreshScope.REPORT,
        }
    )

    def compose(self) -> ComposeResult:
        with Grid(id="dashboard-cards"):
            yield StatCard("Server", id="card-server", classes="card--server")
            yield StatCard("Controllers", id="card-controllers", classes="card--controllers")
            yield StatCard("Storage", id="card-storage", classes="card--storage")
            yield StatCard("Health", id="card-health", classes="card--health")
        yield RecentTasksPanel(self._open_task)

    def hotkeys(self) -> List[HotkeyHint]:
        return [
            HotkeyHint("Enter", "open task"),
            HotkeyHint("F5", "refresh"),
            HotkeyHint("Ctrl+P", "commands"),
            HotkeyHint("Ctrl+Q", "quit"),
        ]

    def focus_primary(self) -> None:
        try:
            self.query_one(RecentTasksPanel).query_one(DataTable).focus()
        except Exception:  # pragma: no cover
            pass

    def on_task_state_changed(self, message) -> None:
        self.refresh_data()
        self.refresh_chrome()

    def on_report_status_changed(self, message) -> None:
        self.refresh_data()
        self.refresh_chrome()

    def refresh_data(self) -> None:
        if not self.is_mounted:
            return
        self._server_card()
        self._controllers_card()
        self._storage_card()
        self._health_card()
        try:
            self.query_one(RecentTasksPanel).load(self.state.recent_tasks)
        except Exception:  # pragma: no cover - panel may not be composed yet
            pass

    def _server_card(self) -> None:
        state = self.state
        tool = getattr(state.detection, "tool", "-") or "-"
        net = state.network_detail or state.ip_address or "-"
        text = Text()
        text.append("SN     ", style="bold")
        text.append(state.server_sn or "-")
        text.append("\nHost   ", style="bold")
        text.append(_hostname())
        text.append("\nSession\n", style="bold")
        text.append(state.evidence.session_id)
        text.append("\nTool   ", style="bold")
        text.append(str(tool))
        text.append("   Net ")
        text.append(str(net))
        self.query_one("#card-server", StatCard).update_body(text)

    def _controllers_card(self) -> None:
        state = self.state
        total_vd = len(state.virtual_disks)
        caps = 0
        for controller in state.controllers:
            caps += int(bool(state.raid_capabilities(controller.index).create))
        text = Text()
        text.append(f"{len(state.controllers)} controller(s)\n", style="bold")
        for controller in state.controllers[:3]:
            text.append(f"c{controller.index}  {controller.vendor.label}  {controller.model or '-'}\n")
        text.append(f"VD {total_vd}   create-capable {caps}")
        self.query_one("#card-controllers", StatCard).update_body(text)

    def _storage_card(self) -> None:
        summary = self.state.storage_summary()
        media = summary["by_media"]
        text = Text()
        text.append(f"PD {summary['disks']}   VD {summary['volumes']}\n", style="bold")
        text.append(f"HDD {media['HDD']}   SSD {media['SSD']}   UNK {media['UNKNOWN']}\n")
        text.append(f"Free {summary['free']}   Raw {_human(summary['total_bytes'])}")
        self.query_one("#card-storage", StatCard).update_body(text)

    def _health_card(self) -> None:
        kind, issues = self.state.health_summary()
        icon = status_icon(kind)
        text = Text()
        text.append(f"{icon} {kind.value.upper()}\n", style=status_style(kind, self.app.theme))
        for issue in issues[:3]:
            text.append(f"• {issue}\n")
        text.append(f"Report: {self.state.report_status.state.value}")
        self.query_one("#card-health", StatCard).update_body(text)

    def _open_task(self, task_id: str) -> None:
        self.state.task_cursor = task_id
        self.post_message(NavigateRequested(PageId.TASKS))


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:  # pragma: no cover
        return "-"


def _human(value: object) -> str:
    number = float(value or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if number < 1024.0 or unit == "T":
            return f"{number:.1f}{unit}"
        number /= 1024.0
    return f"{number:.1f}T"


__all__ = ["DashboardPage"]
