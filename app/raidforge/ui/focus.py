"""Focus capture/restore and focus labels for pushed screens and modals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from textual.css.query import NoMatches
from textual.widget import Widget

#: Short, operator-readable names for the widgets that can take focus.
_FOCUS_LABELS = {
    "nav-bar": "nav",
    "physical-table": "disk table",
    "virtual-table": "volume table",
    "controller-list": "controllers",
    "controller-volumes": "volumes",
    "controller-summary": "controller",
    "task-queue": "task queue",
    "task-log": "task log",
    "evidence-sessions": "sessions",
    "evidence-tree": "evidence",
    "settings-categories": "settings",
    "settings-form": "settings form",
    "filter-search": "search",
    "vfilter-search": "search",
    "detail-disks": "disk table",
    "detail-volumes": "volumes",
    "wizard-disks-table": "disk table",
    "confirm-ack": "confirmation",
    "palette-input": "commands",
}


def focus_label(widget: Optional[Widget]) -> str:
    """Return a short label describing the focused widget."""

    if widget is None:
        return ""
    widget_id = str(getattr(widget, "id", "") or "")
    label = _FOCUS_LABELS.get(widget_id)
    if label:
        return label
    if widget_id:
        return widget_id[:16]
    return type(widget).__name__[:16]


class FocusLabelMixin:
    """Keep a screen's breadcrumb in sync with its focused child."""

    def on_descendant_focus(self, event) -> None:
        try:
            breadcrumb = self.query_one("Breadcrumb")  # type: ignore[attr-defined]
        except (NoMatches, AttributeError):
            return
        try:
            breadcrumb.set_focus_label(focus_label(getattr(event, "widget", None)))
        except Exception:  # pragma: no cover - breadcrumb may be mid-mount
            return


@dataclass
class FocusMemory:
    widget: Optional[Widget]
    fallback: Optional[Callable[[], None]] = None

    def restore(self) -> None:
        target = self.widget
        if target is not None:
            try:
                if target.is_mounted:
                    target.focus()
                    return
            except Exception:  # pragma: no cover - widget disposed
                pass
        if self.fallback is not None:
            try:
                self.fallback()
            except Exception:  # pragma: no cover - page may be hidden
                pass


def capture_focus(fallback: Optional[Callable[[], None]] = None) -> FocusMemory:
    """Capture the app's focused widget from inside a screen's on_mount."""

    # Imported lazily: focus.py is also imported by tests without an active app.
    try:
        from textual.app import active_app

        app = active_app.get()
        return FocusMemory(app.focused, fallback)
    except Exception:  # pragma: no cover - no active app
        return FocusMemory(None, fallback)


__all__ = ["FocusLabelMixin", "FocusMemory", "capture_focus", "focus_label"]
