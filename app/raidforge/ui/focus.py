"""Focus capture/restore for pushed screens and modals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from textual.widget import Widget


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


__all__ = ["FocusMemory", "capture_focus"]
