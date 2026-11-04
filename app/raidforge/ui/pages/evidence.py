"""Evidence: session list, file tree and report/upload status."""

from __future__ import annotations

from typing import List

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable

from ..models import EvidenceSessionView, HotkeyHint, PageId, RefreshScope, TaskKind
from ..tasks import evidence_job, make_task
from ..widgets.evidence_tree import EvidenceTree, ReportStatusPanel
from ..widgets.tables import EvidenceSessionTable
from .base import StatefulPage


class EvidencePage(StatefulPage):
    PAGE = PageId.EVIDENCE
    BINDINGS = [Binding("r", "refresh_evidence", "Rescan", show=False)]
    SCOPES = frozenset({RefreshScope.EVIDENCE, RefreshScope.REPORT})

    def compose(self) -> ComposeResult:
        yield EvidenceSessionTable(id="evidence-sessions")
        with Vertical(id="evidence-detail"):
            yield EvidenceTree(id="evidence-tree")
            yield ReportStatusPanel(id="report-status", classes="status-line")

    def on_mount(self) -> None:
        super().on_mount()
        if not self.state.evidence_sessions:
            self._scan()

    def hotkeys(self) -> List[HotkeyHint]:
        return [
            HotkeyHint("R", "rescan evidence"),
            HotkeyHint("Enter", "expand"),
            HotkeyHint("F5", "refresh"),
            HotkeyHint("Ctrl+P", "commands"),
            HotkeyHint("Ctrl+Q", "quit"),
        ]

    def focus_primary(self) -> None:
        try:
            self.query_one(EvidenceSessionTable).focus()
        except Exception:  # pragma: no cover
            pass

    def apply_layout_mode(self, mode) -> None:
        try:
            self.query_one(EvidenceSessionTable).set_layout_mode(mode)
        except Exception:  # pragma: no cover - page may not be mounted yet
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        if not self.is_mounted:
            return
        table = self.query_one(EvidenceSessionTable)
        table.load(self.state.evidence_sessions, self.state.evidence_cursor)
        session = self._selected_session()
        if session is not None:
            self.state.evidence_cursor = session.session_id
        self.query_one(EvidenceTree).load(session)
        self.query_one(ReportStatusPanel).update_session(session)
        self.refresh_chrome()

    def _selected_session(self) -> EvidenceSessionView | None:
        cursor = self.state.evidence_cursor
        if cursor:
            for session in self.state.evidence_sessions:
                if session.session_id == cursor:
                    return session
        return self.state.evidence_sessions[0] if self.state.evidence_sessions else None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "evidence-sessions":
            return
        self.state.evidence_cursor = str(event.row_key.value)
        session = self._selected_session()
        self.query_one(EvidenceTree).load(session)
        self.query_one(ReportStatusPanel).update_session(session)
        self.refresh_chrome()

    def on_report_status_changed(self, message) -> None:
        session = self._selected_session()
        self.query_one(ReportStatusPanel).update_session(session)
        self.refresh_chrome()

    # -- actions ------------------------------------------------------------
    def _scan(self) -> None:
        runner = self.state.task_runner
        if runner is None:
            return
        task = make_task(TaskKind.EVIDENCE_SCAN, "Scan evidence sessions")
        runner.submit(task, evidence_job(self.state.services))  # type: ignore[arg-type]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "evidence-sessions":
            self.query_one(EvidenceTree).focus()

    def action_refresh_evidence(self) -> None:
        self._scan()
        self.toast("evidence scan queued")

    def action_refresh(self) -> None:
        self._scan()
        self.toast("evidence scan queued")


__all__ = ["EvidencePage"]
