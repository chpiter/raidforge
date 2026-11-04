"""Common behaviour for the seven persistent pages."""

from __future__ import annotations

from typing import List, Optional

from textual.widget import Widget

from ..messages import BreadcrumbChanged, HotkeysChanged, Toast
from ..models import HotkeyHint, PageId, RefreshScope
from ..state import AppState


class StatefulPage(Widget):
    """Base class for pages mounted permanently inside ``ContentSwitcher``.

    Pages are widgets, not screens: switching tabs never destroys them.  Scroll
    offsets are saved explicitly because ``ContentSwitcher`` hides a page with
    ``display: none``, which can reset the scroll position.
    """

    PAGE: PageId = PageId.DASHBOARD
    SCOPES = frozenset(RefreshScope)

    def __init__(self, state: AppState) -> None:
        super().__init__(id=f"page-{self.PAGE.value}", classes="stateful-page")
        self.state = state

    # -- lifecycle ----------------------------------------------------------
    def on_mount(self) -> None:
        self.restore_scroll()
        self.refresh_data()
        self.refresh_chrome()

    # -- data hooks ---------------------------------------------------------
    def refresh_data(self) -> None:
        """Rebuild the page's widgets from AppState."""

    def hotkeys(self) -> List[HotkeyHint]:
        return [HotkeyHint("Ctrl+P", "commands"), HotkeyHint("Ctrl+Q", "quit")]

    def focus_primary(self) -> None:
        """Focus the widget that should receive arrows/Space on page entry."""

    def apply_layout_mode(self, mode) -> None:
        """Adjust column visibility for Standard/Wide terminals."""



    def refresh_chrome(self) -> None:
        if not self.is_mounted:
            return
        self.post_message(HotkeysChanged(self.hotkeys(), self.PAGE))
        self.post_message(BreadcrumbChanged(self.state.breadcrumb_items(), self.PAGE))

    def toast(self, text: str, level: str = "info") -> None:
        self.state.last_message = text
        self.post_message(Toast(text, level))

    # -- messages -----------------------------------------------------------
    def on_data_refreshed(self, message) -> None:
        if message.scope not in self.SCOPES:
            return
        self.refresh_data()
        self.refresh_chrome()

    def on_task_state_changed(self, message) -> None:
        self.refresh_chrome()

    def on_report_status_changed(self, message) -> None:
        self.refresh_chrome()

    # -- scroll retention ---------------------------------------------------
    def _scroll_key(self, widget: Widget) -> str:
        name = widget.id or widget.__class__.__name__
        return f"{self.PAGE.value}:{name}"

    def save_scroll(self) -> None:
        for widget in self.query("*"):
            try:
                if hasattr(widget, "scroll_y") and hasattr(widget, "scroll_x"):
                    self.state.scroll_positions[self._scroll_key(widget)] = (
                        int(widget.scroll_x),
                        int(widget.scroll_y),
                    )
            except Exception:  # pragma: no cover - widgets may be mid-removal
                continue

    def restore_scroll(self) -> None:
        def _apply() -> None:
            for widget in self.query("*"):
                key = self._scroll_key(widget)
                position = self.state.scroll_positions.get(key)
                if position is None:
                    continue
                try:
                    widget.scroll_to(x=position[0], y=position[1], animate=False)
                except Exception:  # pragma: no cover - some widgets are not scrollable
                    continue

        self.call_after_refresh(_apply)

    # -- small helpers ------------------------------------------------------
    def active_task_running(self) -> bool:
        return self.state.active_task_id is not None


__all__ = ["StatefulPage"]
