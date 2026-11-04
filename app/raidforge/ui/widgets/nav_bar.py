"""Top navigation bar: seven horizontal tabs, keyboard driven.

The active tab uses Textual 7.5.0's ``border: tab`` (a three-row border).  The
bar itself is the only focusable element; individual tabs are non-focusable so
Tab does not walk through all seven of them.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Static

from ..messages import NavigateRequested
from ..models import PageId

NAV_PAGES = (
    PageId.DASHBOARD,
    PageId.CONTROLLERS,
    PageId.PHYSICAL,
    PageId.VIRTUAL,
    PageId.TASKS,
    PageId.EVIDENCE,
    PageId.SETTINGS,
)


class NavItem(Static):
    """One tab; activation is driven by NavBar's Enter binding."""

    def __init__(self, page: PageId) -> None:
        super().__init__(f" {page.title} ", id=f"nav-{page.value}", classes="nav-item")
        self.page = page


class NavBar(Horizontal):
    """Focusable seven-module tab strip."""

    can_focus = True

    BINDINGS = [
        Binding("left", "cursor_prev", "Previous tab", show=False),
        Binding("right", "cursor_next", "Next tab", show=False),
        Binding("home", "cursor_first", "First tab", show=False),
        Binding("end", "cursor_last", "Last tab", show=False),
        Binding("enter", "activate", "Open tab", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cursor_index = 0

    def compose(self) -> ComposeResult:
        for page in NAV_PAGES:
            yield NavItem(page)

    # -- state --------------------------------------------------------------
    def items(self) -> list[NavItem]:
        return list(self.query(NavItem))

    def set_active(self, page: PageId) -> None:
        items = self.items()
        for index, item in enumerate(items):
            item.set_class(item.page is page, "-active")
            if item.page is page:
                self.set_cursor(index)

    def set_cursor(self, index: int) -> None:
        items = self.items()
        if not items:
            return
        self.cursor_index = max(0, min(index, len(items) - 1))
        for position, item in enumerate(items):
            item.set_class(position == self.cursor_index, "-cursor")

    @property
    def cursor_page(self) -> PageId:
        items = self.items()
        if not items:
            return PageId.DASHBOARD
        return items[self.cursor_index].page

    # -- actions ------------------------------------------------------------
    def action_cursor_prev(self) -> None:
        self.set_cursor(self.cursor_index - 1)

    def action_cursor_next(self) -> None:
        self.set_cursor(self.cursor_index + 1)

    def action_cursor_first(self) -> None:
        self.set_cursor(0)

    def action_cursor_last(self) -> None:
        self.set_cursor(len(self.items()) - 1)

    def action_activate(self) -> None:
        self.post_message(NavigateRequested(self.cursor_page))


__all__ = ["NAV_PAGES", "NavBar", "NavItem"]
