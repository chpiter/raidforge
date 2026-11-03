"""Dashboard keyboard contract: Enter activates the focused button."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="needs Textual to run the app headless")

import textual  # noqa: E402

from raidforge.config import load_config  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.models import DetectionResult, Vendor  # noqa: E402
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.ui.app import create_app  # noqa: E402
from raidforge.ui.screens import dashboard as dashboard_mod  # noqa: E402
from raidforge.ui.screens.disk_list import DiskListScreen  # noqa: E402
from raidforge.ui.screens.settings import SettingsScreen  # noqa: E402
from raidforge.ui.state import AppContext  # noqa: E402
from textual.widgets import Button  # noqa: E402

PINNED_TEXTUAL = "0.89.1"
needs_pinned_textual = pytest.mark.skipif(
    getattr(textual, "__version__", "") != PINNED_TEXTUAL,
    reason=f"verified against textual {PINNED_TEXTUAL}; running {getattr(textual, '__version__', '?')}",
)


def _context(tmp_path) -> AppContext:
    cfg = load_config(
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {"enabled": False, "wait_for_route_seconds": 0},
            "tools": {"command_timeout": 5},
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-DASH-TEST"})
    return AppContext(cfg=cfg, evidence=EvidenceSession(cfg, sid.serial), server_id=sid)


def _quiet_dashboard(monkeypatch, tmp_path):
    scans = []
    retries = []

    def scan(cfg):
        scans.append(True)
        return DetectionResult(tool="test", vendor=Vendor.LSI, controllers=[])

    monkeypatch.setattr(dashboard_mod.detect_mod, "detect_controllers", scan)
    monkeypatch.setattr(dashboard_mod.enum_mod, "enumerate_disks", lambda cfg, detection: ([], []))
    monkeypatch.setattr(dashboard_mod, "wait_for_default_route", lambda timeout: (True, 0))
    monkeypatch.setattr(
        dashboard_mod, "retry_pending", lambda cfg: retries.append(True) or []
    )
    return _context(tmp_path), scans, retries


@needs_pinned_textual
def test_focused_buttons_receive_enter(monkeypatch, tmp_path):
    ctx, scans, retries = _quiet_dashboard(monkeypatch, tmp_path)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.4)
            screen = app.screen
            assert screen.query_one("#go-disks", Button).has_focus

            # Default focus -> DiskList.
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, DiskListScreen)
            assert len(app.screen_stack) == 3
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert app.screen is screen

            # Rescan button -> stays on Dashboard, starts the scan.
            screen.query_one("#rescan", Button).focus()
            before = len(scans)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, dashboard_mod.DashboardScreen)
            assert len(scans) == before + 1

            # Upload button -> stays on Dashboard, invokes retry_pending.
            screen.query_one("#upload", Button).focus()
            before = len(retries)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, dashboard_mod.DashboardScreen)
            assert len(retries) == before + 1

    asyncio.run(scenario())


@needs_pinned_textual
def test_no_focus_enter_opens_disk_list(monkeypatch, tmp_path):
    ctx, _scans, _retries = _quiet_dashboard(monkeypatch, tmp_path)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            app.set_focus(None)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, DiskListScreen)
            assert len(app.screen_stack) == 3

    asyncio.run(scenario())


@needs_pinned_textual
def test_quit_button_receives_enter(monkeypatch, tmp_path):
    ctx, _scans, _retries = _quiet_dashboard(monkeypatch, tmp_path)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            app.screen.query_one("#quit", Button).focus()
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert app.return_code == 0

    asyncio.run(scenario())


@needs_pinned_textual
def test_r_u_s_shortcuts_still_work(monkeypatch, tmp_path):
    ctx, scans, retries = _quiet_dashboard(monkeypatch, tmp_path)

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.3)
            before = len(scans)
            await pilot.press("r")
            await pilot.pause(0.2)
            assert len(scans) == before + 1

            before = len(retries)
            await pilot.press("u")
            await pilot.pause(0.2)
            assert len(retries) == before + 1

            await pilot.press("s")
            await pilot.pause(0.2)
            assert isinstance(app.screen, SettingsScreen)
            await pilot.press("escape")
            await pilot.pause(0.2)

    asyncio.run(scenario())
