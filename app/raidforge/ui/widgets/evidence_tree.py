"""Evidence file tree and report/upload status panel."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from rich.text import Text
from textual.widgets import Static, Tree

from ..models import EvidenceSessionView, ReportState


class EvidenceTree(Tree):
    def __init__(self, **kwargs) -> None:
        super().__init__("Evidence", **kwargs)

    def load(self, session: EvidenceSessionView | None) -> None:
        try:
            self.root.remove_children()
        except Exception:  # pragma: no cover - Tree API drift
            pass
        if session is None:
            self.root.label = Text("Evidence")
            return
        self.root.label = Text(session.session_id)
        groups = defaultdict(list)
        for item in session.files:
            groups[item.kind].append(item)
        for kind in ("events", "snapshot", "report", "json", "recording", "file"):
            items = groups.get(kind)
            if not items:
                continue
            node = self.root.add(f"{kind} ({len(items)})", expand=kind in ("events", "snapshot"))
            for item in items[:200]:
                node.add_leaf(item.rel_path)
        self.root.expand()


class ReportStatusPanel(Static):
    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)

    def update_session(self, session: EvidenceSessionView | None) -> None:
        if session is None:
            self.content = Text("No evidence session selected.")
            return
        lines = [
            ("Session", session.session_id),
            ("Path", session.path),
            ("Files", str(session.file_count)),
            ("Size", session.size_human),
            ("Manifest", "✓ present" if session.manifest_ok else "✗ missing"),
            ("Report", f"{session.report_state.value} {session.report_path or '-'}"),
            ("Upload", session.upload or "not attempted"),
        ]
        text = Text()
        for index, (key, value) in enumerate(lines):
            if index:
                text.append("\n")
            text.append(f"{key:<9}", style="bold")
            text.append(str(value))
        self.content = text


__all__ = ["EvidenceTree", "ReportStatusPanel"]
