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
from raidforge.ui.models import PageId  # noqa: E402
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
from textual.widgets import ContentSwitcher, Input, Select  # noqa: E402


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

            await pilot.press("ctrl+3")
            await pilot.pause(0.25)
            assert state.active_page is PageId.PHYSICAL
            page = screen.query_one(PhysicalPage)
            search = page.query_one("#filter-search", Input)
            search.value = "abc"
            await pilot.pause(0.1)
            await pilot.press("ctrl+1")
            await pilot.pause(0.1)
            await pilot.press("ctrl+3")
            await pilot.pause(0.25)
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
            await pilot.press("ctrl+3")
            await pilot.pause(0.3)
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
            await pilot.press("ctrl+3")
            await pilot.pause(0.3)
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
            await pilot.press("ctrl+5")
            await pilot.pause(0.3)
            assert app.screen.query_one("TaskTable").row_count >= 2

    asyncio.run(scenario())


def test_navbar_start_focus_arrows_and_enter(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(1.0)
            from raidforge.ui.widgets.nav_bar import NavBar
            assert isinstance(app.focused, NavBar)
            await pilot.press("right")
            await pilot.pause(0.1)
            assert app.screen.query_one("#nav-bar").cursor_page is PageId.CONTROLLERS
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert state.active_page is PageId.CONTROLLERS
            from raidforge.ui.widgets.tables import ControllerTable
            assert isinstance(app.focused, ControllerTable)
            assert app.screen.query_one("HotkeyBar").can_focus is False
    asyncio.run(scenario())


def test_physical_ctrl_a_ctrl_i_and_jk(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("ctrl+3")
            await pilot.pause(0.3)
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
            await pilot.press("ctrl+3")
            await pilot.pause(0.3)
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
            await pilot.press("ctrl+2")
            await pilot.pause(0.3)
            assert isinstance(app.focused, ControllerTable)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert type(app.screen).__name__ == "ControllerDetailScreen"
            await pilot.press("escape")
            await pilot.pause(0.4)
            assert isinstance(app.focused, ControllerTable)
    asyncio.run(scenario())


def test_responsive_columns_standard_and_wide(monkeypatch, tmp_path):
    state = _state(tmp_path)
    _install_fake_scan(monkeypatch, state)
    async def scenario() -> None:
        app = create_app(state)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(1.0)
            await pilot.press("ctrl+3")
            await pilot.pause(0.3)
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
            await pilot.press("ctrl+3")
            await pilot.pause(0.3)
            table = app.screen.query_one("#physical-table", MultiSelectTable)
            labels = {str(column.label) for column in table.columns.values()}
            assert {"Proto", "FW", "Serial"} <= labels
            assert table.virtual_size.width <= 120
            assert table.scroll_x == 0
    asyncio.run(wide())
