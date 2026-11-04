"""Filter bars for the Physical and Virtual pages."""

from __future__ import annotations

from typing import Callable, Iterable, List, Tuple

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Select, Static

from ..models import FilterState


def _value(select: Select) -> str:
    value = select.value
    return "all" if value is None or value is Select.BLANK else str(value)


class DiskFilterBar(Horizontal):
    """Ctrl / Media / State selects + free-text search."""

    def __init__(self, state, on_change: Callable[[], None]) -> None:
        super().__init__(id="physical-filters", classes="filter-bar")
        self.state = state
        self.on_change = on_change
        self._filter = state.filter_for("physical")
        self._ctrl = Select([("All", "all")], allow_blank=False, value="all", compact=True, id="filter-ctrl")
        self._media = Select(
            [("All", "all"), ("HDD", "HDD"), ("SSD", "SSD"), ("UNK", "UNKNOWN")],
            allow_blank=False,
            value="all",
            compact=True,
            id="filter-media",
        )
        self._state = Select(
            [("All", "all"), ("Free", "free"), ("In VD", "vd"), ("Blocked", "blocked")],
            allow_blank=False,
            value="all",
            compact=True,
            id="filter-state",
        )
        self._search = Input(placeholder="/ search", compact=True, id="filter-search")
        self._count = Static("", id="filter-count")

    def compose(self) -> ComposeResult:
        yield self._ctrl
        yield self._media
        yield self._state
        yield self._search
        yield self._count

    def on_mount(self) -> None:
        self._apply_filter()

    def set_controllers(self, controllers: Iterable[int]) -> None:
        options: List[Tuple[str, str]] = [("All", "all")]
        options.extend((f"c{index}", str(index)) for index in controllers)
        try:
            self._ctrl.set_options(options)
            self._ctrl.value = self._filter.ctrl
        except Exception:  # pragma: no cover - Select API drift
            pass

    def _apply_filter(self) -> None:
        self._filter.ctrl = _value(self._ctrl)
        self._filter.media = _value(self._media)
        self._filter.state = _value(self._state)
        self._filter.query = self._search.value.strip()
        self.on_change()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        self._apply_filter()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-search":
            self._apply_filter()

    def set_count(self, visible: int, total: int) -> None:
        self._count.update(f"{visible}/{total}")

    def focus_search(self) -> None:
        self._search.focus()

    def clear_search(self) -> None:
        self._search.value = ""


class VolumeFilterBar(Horizontal):
    """Ctrl / Level / State selects + free-text search."""

    def __init__(self, state, on_change: Callable[[], None]) -> None:
        super().__init__(id="virtual-filters", classes="filter-bar")
        self.state = state
        self.on_change = on_change
        self._filter = state.filter_for("virtual")
        self._ctrl = Select([("All", "all")], allow_blank=False, value="all", compact=True, id="vfilter-ctrl")
        self._level = Select(
            [("All", "all"), ("RAID0", "RAID0"), ("RAID1", "RAID1"), ("RAID5", "RAID5"), ("RAID10", "RAID10")],
            allow_blank=False,
            value="all",
            compact=True,
            id="vfilter-level",
        )
        self._state = Select(
            [("All", "all"), ("Optimal", "ok"), ("Degraded", "warn"), ("Failed", "bad")],
            allow_blank=False,
            value="all",
            compact=True,
            id="vfilter-state",
        )
        self._search = Input(placeholder="/ search", compact=True, id="vfilter-search")
        self._count = Static("", id="vfilter-count")

    def compose(self) -> ComposeResult:
        yield self._ctrl
        yield self._level
        yield self._state
        yield self._search
        yield self._count

    def on_mount(self) -> None:
        self._apply_filter()

    def set_controllers(self, controllers: Iterable[int]) -> None:
        options: List[Tuple[str, str]] = [("All", "all")]
        options.extend((f"c{index}", str(index)) for index in controllers)
        try:
            self._ctrl.set_options(options)
            self._ctrl.value = self._filter.ctrl
        except Exception:  # pragma: no cover
            pass

    def _apply_filter(self) -> None:
        self._filter.ctrl = _value(self._ctrl)
        self._filter.level = _value(self._level)
        self._filter.state = _value(self._state)
        self._filter.query = self._search.value.strip()
        self.on_change()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        self._apply_filter()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "vfilter-search":
            self._apply_filter()

    def set_count(self, visible: int, total: int) -> None:
        self._count.update(f"{visible}/{total}")

    def focus_search(self) -> None:
        self._search.focus()

    def clear_search(self) -> None:
        self._search.value = ""


__all__ = ["DiskFilterBar", "VolumeFilterBar"]
