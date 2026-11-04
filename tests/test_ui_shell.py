"""Headless tests for the multi-module shell, tables, wizard and tasks."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="needs Textual to run the app headless")

from raidforge.config import load_config  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.models import (  # noqa: E402
    ControllerInfo,
    DetectionResult,
    DiskInfo,
    Medium,
    Protocol,
    RaidBundle,
    RaidCapabilities,
    RaidTopology,
    Vendor,
    VolumeInfo,
)
from raidforge.core.scan import ScanResult  # noqa: E402
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.ui import services as services_mod  # noqa: E402
from raidforge.ui.app import create_app  # noqa: E402
from raidforge.ui.models import PageId, TaskItem, TaskKind, TaskStatus  # noqa: E402
from raidforge.ui.pages.physical import PhysicalPage  # noqa: E402
from raidforge.ui.screens.command_palette import Command, CommandPaletteModal  # noqa: E402
from raidforge.ui.screens.confirm import ConfirmModal  # noqa: E402
from raidforge.ui.screens.main import MainScreen  # noqa: E402
from raidforge.ui.screens.raid_wizard import RaidWizardScreen  # noqa: E402
from raidforge.ui.state import AppState  # noqa: E402
from raidforge.ui.widgets.breadcrumb import Breadcrumb  # noqa: E402
from raidforge.ui.widgets.detail_drawer import DetailDrawer  # noqa: E402
from raidforge.ui.widgets.disk_table import MultiSelectTable  # noqa: E402
from raidforge.ui.widgets.hotkey_bar import HotkeyBar  # noqa: E402
from raidforge.ui.widgets.nav_bar import NavBar, NavItem  # noqa: E402
from raidforge.ui.widgets.status_bar import StatusBar  # noqa: E402
from textual.coordinate import Coordinate  # noqa: E402
from textual.widgets import ContentSwitcher, DataTable, Input, Select  # noqa: E402


def _state(tmp_path) -> AppState:
    cfg = load_config(
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {"enabled": False},
            "tools": {"command_timeout": 2},
        }
    )
    cfg.ui.theme = "raidforge"
    cfg.dry_run = True
    sid = resolve_server_id(force_values={"system_serial": "SN-UI-TEST"})
    return AppState(cfg=cfg, evidence=EvidenceSession(cfg, sid.serial), server_id=sid)


def _disks() -> list[DiskInfo]:
    return [
        DiskInfo(
            disk_id="c0e32s0",
            controller_index=0,
            vendor=Vendor.LSI,
            medium=Medium.HDD,
            protocol=Protocol.SAS,
            enclosure=32,
            slot=0,
            model="ST2000",
            serial="ZTN1",
            size_bytes=2_000_398_934_016,
            raid_state="UGood",
        ),
        DiskInfo(
            disk_id="c0e32s1",
            controller_index=0,
            vendor=Vendor.LSI,
            medium=Medium.HDD,
            protocol=Protocol.SAS,
            enclosure=32,
            slot=1,
            model="ST2000",
            serial="ZTN2",
            size_bytes=2_000_398_934_016,
            raid_state="Onln",
        ),
        DiskInfo(
            disk_id="c0e32s2",
            controller_index=0,
            vendor=Vendor.LSI,
            medium=Medium.UNKNOWN,
            protocol=Protocol.UNKNOWN,
            enclosure=32,
            slot=2,
            model="MYSTERY",
        ),
    ]


def _install_fake_scan(monkeypatch, state: AppState, with_volume: bool = True) -> None:
    disks = _disks()
    controller = ControllerInfo(
        index=0,
        vendor=Vendor.LSI,
        model="LSI9361",
        firmware="1.0",
        tool="storcli",
        binary="storcli",
    )
    volumes = []
    membership = {}
    blocked = {}
    if with_volume:
        volumes = [
            VolumeInfo(
                volume_id="v0",
                controller_index=0,
                vendor=Vendor.LSI,
                level="RAID1",
                raw_level="1",
                name="data",
                state="Optl",
                size_bytes=2_000_398_934_016,
                disk_ids=["c0e32s1"],
                spans=[["c0e32s1"]],
            )
        ]
        membership = {"c0e32s1": "v0"}
    topology = RaidTopology(
        controller_index=0,
        vendor=Vendor.LSI,
        tool="storcli",
        binary="storcli",
        volumes=volumes,
        membership=membership,
        free_disk_ids=[disk.disk_id for disk in disks if disk.disk_id not in membership],
        blocked=blocked,
        disk_states={},
    )
    caps = RaidCapabilities(
        levels=["RAID0", "RAID1", "RAID5", "RAID10"],
        create=True,
        delete_vd=True,
        clear_config=True,
    )
    scan = ScanResult(
        detection=DetectionResult(
            tool="storcli",
            vendor=Vendor.LSI,
            controllers=[controller],
            binary="storcli",
            provider="lsi",
            attempts=[],
        ),
        disks=disks,
        raid=RaidBundle(
            tool="storcli",
            binary="storcli",
            topologies=[topology],
            capabilities={0: caps},
        ),
        raw_outputs=[],
    )

    def fake_scan(state_arg):
        state_arg.apply_scan(scan)
        return scan

    monkeypatch.setattr(services_mod, "run_scan", fake_scan)


def _generic_disks() -> list[DiskInfo]:
    return [
        DiskInfo(
            disk_id="sda",
            controller_index=0,
            vendor=Vendor.GENERIC,
            medium=Medium.SSD,
            protocol=Protocol.UNKNOWN,
            device_node="/dev/sda",
            model="VMware Virtual S",
            size_bytes=20_000_000_000,
        ),
        DiskInfo(
            disk_id="sdb",
            controller_index=0,
            vendor=Vendor.GENERIC,
            medium=Medium.SSD,
            protocol=Protocol.UNKNOWN,
            device_node="/dev/sdb",
            model="VMware Virtual S",
            size_bytes=20_000_000_000,
        ),
        DiskInfo(
            disk_id="sdc",
            controller_index=0,
            vendor=Vendor.GENERIC,
            medium=Medium.SSD,
            protocol=Protocol.UNKNOWN,
            device_node="/dev/sdc",
            model="VMware Virtual S",
            size_bytes=20_000_000_000,
        ),
    ]


def _install_generic_scan(monkeypatch, state: AppState) -> None:
    disks = _generic_disks()
    controller = ControllerInfo(
        index=0,
        vendor=Vendor.GENERIC,
        model="Direct attached",
        tool="sg3_utils",
        binary="sg_scan",
    )
    topology = RaidTopology(
        controller_index=0,
        vendor=Vendor.GENERIC,
        tool="sg3_utils",
        binary="sg_scan",
        volumes=[],
        membership={},
        free_disk_ids=[],
        blocked={
            disk.disk_id: "RAID management not supported on the generic (sg3_utils) path"
            for disk in disks
        },
        disk_states={disk.disk_id: "" for disk in disks},
    )
    scan = ScanResult(
        detection=DetectionResult(
            tool="sg3_utils",
            vendor=Vendor.GENERIC,
            controllers=[controller],
            binary="sg_scan",
            provider="generic",
            attempts=[],
        ),
        disks=disks,
        raid=RaidBundle(
            tool="sg3_utils",
            binary="sg_scan",
            topologies=[topology],
            capabilities={0: RaidCapabilities(create=False, delete_vd=False, clear_config=False)},
        ),
        raw_outputs=[],
    )

    def fake_scan(state_arg):
        state_arg.apply_scan(scan)
        return scan

    monkeypatch.setattr(services_mod, "run_scan", fake_scan)


_PAGE_ORDER = list(PageId)


async def _nav_to(pilot, app, page: PageId, enter_content: bool = False) -> None:
    """Drive the NavBar with arrows; optionally press Enter to enter content."""

    nav = app.screen.query_one(NavBar)
    nav.focus()
    await pilot.pause(0.1)
    await pilot.press("home")
    await pilot.pause(0.1)
    for _ in range(_PAGE_ORDER.index(page)):
        await pilot.press("right")
        await pilot.pause(0.05)
    await pilot.pause(0.25)
    assert app.screen.state.active_page is page
    if enter_content:
        await pilot.press("enter")
        await pilot.pause(0.25)


def test_top_nav_layout_and_state_retention(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            screen = app.screen
            assert isinstance(screen, MainScreen)
            assert len(screen.query(NavItem)) == 7
            assert screen.query_one("#nav-bar").size.height == 3
            assert screen.query_one(StatusBar).size.height == 1
            assert screen.query_one(Breadcrumb).size.height == 1
            assert screen.query_one(HotkeyBar).size.height == 1
            assert screen.query_one(ContentSwitcher).size.height == 19
            assert "SN" in str(screen.query_one("#status-sn").render())
            assert "User" in str(screen.query_one("#status-user").render())

            await _nav_to(pilot, app, PageId.PHYSICAL)
            page = screen.query_one(PhysicalPage)
            search = page.query_one("#filter-search", Input)
            search.value = "abc"
            await pilot.pause(0.1)
            await _nav_to(pilot, app, PageId.DASHBOARD)
            await _nav_to(pilot, app, PageId.PHYSICAL)
            assert page.query_one("#filter-search", Input).value == "abc"
            assert screen.query_one("#page-physical").size.width == 80

            await pilot.press("ctrl+p")
            await pilot.pause(0.25)
            assert isinstance(app.screen, CommandPaletteModal)
            await pilot.press("escape")
            await pilot.pause(0.2)

    asyncio.run(scenario())


def test_physical_full_width_selection_and_drawer(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.PHYSICAL, enter_content=True)
            page = app.screen.query_one(PhysicalPage)
            table = page.query_one("#physical-table", MultiSelectTable)
            drawer = page.query_one("#physical-drawer", DetailDrawer)
            assert table.row_count == 3
            assert table.size.width == 80
            assert table.virtual_size.width <= 80
            assert table.scroll_x == 0
            assert not drawer.has_class("-hidden")

            table.move_cursor(row=0)
            await pilot.pause(0.1)
            await pilot.press("space")
            await pilot.pause(0.1)
            assert "c0e32s0" in state.physical_selection
            assert table.get_cell_at(Coordinate(0, 0)).plain == "[x]"
            blocked = state.disk_by_id("c0e32s1")
            assert table._cells_for(blocked, False, False, "member of v0")["sel"].plain == " "

            table.move_cursor(row=1)
            await pilot.pause(0.1)
            await pilot.press("space")
            await pilot.pause(0.1)
            assert "c0e32s1" not in state.physical_selection

            table.move_cursor(row=2)
            await pilot.pause(0.1)
            await pilot.press("space")
            await pilot.pause(0.1)
            assert "c0e32s2" not in state.physical_selection

            state.filter_for("physical").state = "vd"
            page.refresh_data()
            await pilot.pause(0.2)
            assert table.row_count == 1
            state.filter_for("physical").state = "all"
            page.refresh_data()
            await pilot.pause(0.2)

            await pilot.press("f8")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmModal)
            assert app.screen.query_one("#confirm-ack", Input).placeholder == "yes"
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert isinstance(app.screen, MainScreen)

    asyncio.run(scenario())


def test_wizard_preview_and_confirm(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            app.push_screen(RaidWizardScreen(state, 0))
            await pilot.pause(0.3)
            wizard = app.screen
            assert isinstance(wizard, RaidWizardScreen)
            wizard.action_level("1")
            wizard._set_step(1)
            wizard.selected = {"c0e32s0", "c0e32s1"}
            wizard._build_preview()
            wizard._set_step(2)
            await pilot.pause(0.2)
            assert wizard.raid_layout is not None
            assert wizard.raid_layout.capacity_bytes > 0
            assert len(wizard.raid_layout.spans) == 1
            assert len(wizard.plans) == 1
            assert not wizard.warnings
            wizard._confirm()
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert isinstance(app.screen, RaidWizardScreen)

    asyncio.run(scenario())


def test_dry_run_erase_reaches_task_queue(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.PHYSICAL, enter_content=True)
            page = app.screen.query_one(PhysicalPage)
            table = page.query_one(MultiSelectTable)
            table.move_cursor(row=0)
            await pilot.pause(0.1)
            await pilot.press("space")
            await pilot.press("f8")
            await pilot.pause(0.3)
            for char in "yes":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause(1.0)
            kinds = [task.kind.value for task in state.task_queue]
            assert "erase" in kinds
            assert state.erase_history
            await _nav_to(pilot, app, PageId.TASKS)
            assert app.screen.query_one("TaskTable").row_count >= 2

    asyncio.run(scenario())


def test_navbar_start_focus_arrows_and_enter(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(1.0)
            from raidforge.ui.widgets.tables import ControllerTable
            assert isinstance(app.focused, NavBar)
            await pilot.press("right")
            await pilot.pause(0.3)
            assert state.active_page is PageId.CONTROLLERS
            assert isinstance(app.focused, NavBar)
            await pilot.press("left")
            await pilot.pause(0.3)
            assert state.active_page is PageId.DASHBOARD
            await pilot.press("right")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert state.active_page is PageId.CONTROLLERS
            assert isinstance(app.focused, ControllerTable)
            assert app.screen.query_one("HotkeyBar").can_focus is False
    asyncio.run(scenario())


def test_ctrl_number_shortcuts_removed(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.8)
            for binding in list(app.BINDINGS) + list(MainScreen.BINDINGS):
                key = str(binding.key)
                assert not (key.startswith("ctrl+") and key[5:].isdigit())
            await pilot.press("ctrl+3")
            await pilot.pause(0.2)
            assert state.active_page is PageId.DASHBOARD
    asyncio.run(scenario())


def test_palette_has_no_page_navigation(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.8)
            await pilot.press("ctrl+p")
            await pilot.pause(0.3)
            palette = app.screen
            assert isinstance(palette, CommandPaletteModal)
            labels = {command.label for command in palette.commands}
            assert not labels & {"Dashboard", "Controllers", "Physical", "Virtual", "Tasks", "Evidence", "Settings"}
            await pilot.press("escape")
            await pilot.pause(0.2)
    asyncio.run(scenario())


def test_escape_returns_focus_to_navbar(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.8)
            await _nav_to(pilot, app, PageId.PHYSICAL, enter_content=True)
            assert isinstance(app.focused, MultiSelectTable)
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.focused, NavBar)
            await pilot.press("right")
            await pilot.pause(0.3)
            assert state.active_page is PageId.VIRTUAL
    asyncio.run(scenario())


def test_navbar_focus_shows_nav_footer(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.8)
            await _nav_to(pilot, app, PageId.PHYSICAL)
            bar = app.screen.query_one(HotkeyBar)
            assert "←/→" in bar.content.plain
            assert "Enter" in bar.content.plain
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.focused, MultiSelectTable)
            assert "Space" in bar.content.plain
            assert "F8" in bar.content.plain
    asyncio.run(scenario())


def test_physical_ctrl_a_ctrl_i_and_jk(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(1.0)
            await _nav_to(pilot, app, PageId.PHYSICAL, enter_content=True)
            table = app.screen.query_one("#physical-table", MultiSelectTable)
            await pilot.press("ctrl+a")
            await pilot.pause(0.1)
            assert state.physical_selection == {"c0e32s0"}
            await pilot.press("ctrl+i")
            await pilot.pause(0.1)
            assert not state.physical_selection
            await pilot.press("i")
            await pilot.pause(0.1)
            assert state.physical_selection == {"c0e32s0"}
            start = table.cursor_row
            await pilot.press("j")
            await pilot.pause(0.1)
            assert table.cursor_row == start + 1
            await pilot.press("k")
            await pilot.pause(0.1)
            assert table.cursor_row == start
    asyncio.run(scenario())


def test_confirm_yes_error_clears(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(1.0)
            await _nav_to(pilot, app, PageId.PHYSICAL, enter_content=True)
            table = app.screen.query_one(MultiSelectTable)
            table.move_cursor(row=0)
            await pilot.press("space")
            await pilot.press("f8")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmModal)
            ack = app.screen.query_one("#confirm-ack", Input)
            assert ack.placeholder == "yes"
            for char in "no":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmModal)
            assert ack.value == ""
            for char in "YES":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause(1.0)
            assert state.erase_history
    asyncio.run(scenario())


def test_detail_focus_restore(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        from raidforge.ui.widgets.tables import ControllerTable
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(1.0)
            await _nav_to(pilot, app, PageId.CONTROLLERS, enter_content=True)
            assert isinstance(app.focused, ControllerTable)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "ControllerDetailScreen"
            await pilot.press("escape")
            await pilot.pause(0.4)
            assert isinstance(app.focused, ControllerTable)
    asyncio.run(scenario())


def test_generic_disks_select_and_open_confirm_at_80x25(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_generic_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.PHYSICAL, enter_content=True)
            page = app.screen.query_one(PhysicalPage)
            table = page.query_one("#physical-table", MultiSelectTable)
            assert table.row_count == 3
            assert page._vd_label(state.disk_by_id("sda")) == "free"
            disk = state.disk_by_id("sda")
            cells = table._cells_for(disk, False, True, "")
            assert cells["state"].plain == "direct"
            assert cells["vd"].plain == "free"
            assert cells["sel"].plain == " "
            labels = {str(column.label) for column in table.columns.values()}
            assert "Sel" in labels
            assert table.virtual_size.width <= 80
            assert table.get_cell_at(Coordinate(0, 0)).plain == " "

            await pilot.press("space")
            await pilot.pause(0.1)
            assert "sda" in state.physical_selection
            assert table.get_cell_at(Coordinate(0, 0)).plain == "[x]"
            await pilot.press("f8")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert isinstance(app.screen, MainScreen)

    asyncio.run(scenario())


def test_generic_erase_dry_run_reaches_task_queue(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_generic_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.PHYSICAL, enter_content=True)
            await pilot.press("space")
            await pilot.press("f8")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmModal)
            for char in "yes":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause(1.2)
            assert state.erase_history
            assert any(task.kind.value == "erase" for task in state.task_queue)

    asyncio.run(scenario())


def test_physical_enter_without_selection_selects_cursor_row(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_generic_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.PHYSICAL, enter_content=True)
            assert not state.physical_selection
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert "sda" in state.physical_selection
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("escape")
            await pilot.pause(0.2)

    asyncio.run(scenario())


def test_physical_enter_on_blocked_row_stays_on_table(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.PHYSICAL, enter_content=True)
            table = app.screen.query_one("#physical-table", MultiSelectTable)
            table.move_cursor(row=1)
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, ConfirmModal)
            assert isinstance(app.focused, MultiSelectTable)
            assert "member of v0" in state.last_message

    asyncio.run(scenario())


def test_detail_drawer_is_not_focusable():
    assert DetailDrawer.can_focus is False


def test_arrow_navigation_keeps_nav_focus_then_enter_selects(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_generic_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.PHYSICAL)
            assert isinstance(app.focused, NavBar)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.focused, MultiSelectTable)
            await pilot.press("space")
            await pilot.pause(0.1)
            assert "sda" in state.physical_selection

    asyncio.run(scenario())


def test_dashboard_focus_primary_reaches_recent_tasks(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.DASHBOARD)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.focused, DataTable)
            assert app.focused.id == "recent-tasks-table"

    asyncio.run(scenario())


def test_footer_shows_ctrl_q_and_fits_80_columns(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_generic_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.PHYSICAL)
            page = app.screen.query_one(PhysicalPage)
            keys = {hint.key for hint in page.hotkeys()}
            assert "Ctrl+Q" in keys
            bar = app.screen.query_one(HotkeyBar)
            assert "Ctrl+Q" in bar.content.plain
            assert len(bar.content.plain) <= 78

            await _nav_to(pilot, app, PageId.VIRTUAL)
            virtual = app.screen.query_one("VirtualPage")
            assert "Ctrl+Q" in {hint.key for hint in virtual.hotkeys()}

            await _nav_to(pilot, app, PageId.SETTINGS)
            settings = app.screen.query_one("SettingsPage")
            assert "Ctrl+Q" in {hint.key for hint in settings.hotkeys()}

    asyncio.run(scenario())


def test_breadcrumb_shows_focus_label(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_generic_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            await _nav_to(pilot, app, PageId.PHYSICAL)
            breadcrumb = app.screen.query_one(Breadcrumb)
            assert "Focus: nav" in breadcrumb.content.plain
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert "Focus: disk table" in breadcrumb.content.plain
            app.screen.query_one(NavBar).focus()
            await pilot.pause(0.2)
            assert "Focus: nav" in breadcrumb.content.plain

    asyncio.run(scenario())


def test_breadcrumb_shows_active_task(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_generic_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            task = TaskItem(
                id="task-erase",
                kind=TaskKind.ERASE,
                title="Erase 3 disk(s)",
                status=TaskStatus.RUNNING,
                done=1,
                total=3,
            )
            state.task_queue.append(task)
            state.active_task_id = task.id
            app.screen._update_chrome()
            await pilot.pause(0.1)
            breadcrumb = app.screen.query_one(Breadcrumb)
            assert "erase" in breadcrumb.content.plain
            assert "1/3" in breadcrumb.content.plain
            state.active_task_id = None

    asyncio.run(scenario())


def test_f5_while_busy_names_task_and_ctrl_q(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_generic_scan(monkeypatch, state)

    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.6)
            task = TaskItem(id="task-refresh", kind=TaskKind.REFRESH, title="Manual refresh")
            state.task_queue.append(task)
            state.active_task_id = task.id
            app.screen._update_chrome()
            await pilot.press("f5")
            await pilot.pause(0.2)
            assert "Manual refresh" in state.last_message
            assert "Ctrl+Q" in state.last_message
            state.active_task_id = None

    asyncio.run(scenario())


def test_responsive_columns_standard_and_wide(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(1.0)
            await _nav_to(pilot, app, PageId.PHYSICAL)
            table = app.screen.query_one("#physical-table", MultiSelectTable)
            labels = {str(column.label) for column in table.columns.values()}
            assert "Proto" not in labels and "FW" not in labels and "Serial" not in labels
            assert table.virtual_size.width <= 80
    asyncio.run(scenario())
    state2 = _state(tmp_path)
    _install_fake_scan(monkeypatch, state2)
    async def wide() -> None:
        app = create_app(state2)
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause(1.0)
            await _nav_to(pilot, app, PageId.PHYSICAL)
            table = app.screen.query_one("#physical-table", MultiSelectTable)
            labels = {str(column.label) for column in table.columns.values()}
            assert {"Proto", "FW", "Serial"} <= labels
            assert table.virtual_size.width <= 120
            assert table.scroll_x == 0
    asyncio.run(wide())
