"""Headless smoke test for the multi-module shell.

Run with the shipped interpreter:

    python tests/ui_smoke.py
    UI_SMOKE_SIZE=80x25 python tests/ui_smoke.py

It drives the top tabs, the Physical multi-select table, the shared
ConfirmModal and the task queue in dry-run mode, so it never touches a disk.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_here = Path(__file__).resolve()
_candidates = [
    Path(os.environ["RAIDFORGE_APP_ROOT"]) if os.environ.get("RAIDFORGE_APP_ROOT") else None,
    _here.parent.parent / "app",
    Path("/opt/raidforge"),
    Path("/root/raidforge/app"),
]
for candidate in _candidates:
    if candidate and (candidate / "raidforge" / "__init__.py").is_file():
        sys.path.insert(0, str(candidate))
        break
else:  # pragma: no cover - misconfigured checkout
    raise SystemExit("cannot locate app/raidforge; set RAIDFORGE_APP_ROOT")

from textual.widgets import ContentSwitcher, Input, Select  # noqa: E402

from raidforge.config import load_config  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.models import DiskInfo, Medium, Protocol, Vendor  # noqa: E402
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.ui.app import create_app  # noqa: E402
from raidforge.ui.pages.physical import PhysicalPage  # noqa: E402
from raidforge.ui.screens.confirm import ConfirmModal  # noqa: E402
from raidforge.ui.screens.main import MainScreen  # noqa: E402
from raidforge.ui.state import AppState  # noqa: E402
from raidforge.ui.widgets.disk_table import MultiSelectTable  # noqa: E402
from raidforge.ui.widgets.nav_bar import NavItem  # noqa: E402


async def main() -> int:
    failures: list[str] = []

    cfg = load_config(
        overrides={
            "evidence": {"root": "/tmp/ui-smoke-evidence"},
            "report": {"enabled": False},
            "tools": {"command_timeout": 5},
        }
    )
    cfg.dry_run = True
    sid = resolve_server_id(force_values={"system_serial": "SN-HOST-TEST-01"})
    ev = EvidenceSession(cfg, sid.serial)
    state = AppState(cfg=cfg, evidence=ev, server_id=sid)
    app = create_app(state)

    cols, rows = (int(part) for part in os.environ.get("UI_SMOKE_SIZE", "80x25").lower().split("x"))
    async with app.run_test(size=(cols, rows)) as pilot:
        await pilot.pause(0.8)
        for _ in range(50):
            if state.active_task_id is None:
                break
            await pilot.pause(0.2)

        screen = app.screen
        if not isinstance(screen, MainScreen):
            failures.append(f"root screen is {type(screen).__name__}, expected MainScreen")
        if len(screen.query(NavItem)) != 7:
            failures.append("top navigation does not contain 7 tabs")
        for selector, height in (
            ("#status-bar", 1),
            ("#nav-bar", 3),
            ("#breadcrumb", 1),
            ("#hotkey-bar", 1),
        ):
            if screen.query_one(selector).size.height != height:
                failures.append(f"{selector} is not {height} row high")
        if screen.query_one("#pages", ContentSwitcher).size.height != rows - 6:
            failures.append("ContentSwitcher does not fill the remaining height")

        for index in range(1, 8):
            await pilot.press(f"ctrl+{index}")
            await pilot.pause(0.08)
        await pilot.press("ctrl+3")
        await pilot.pause(0.2)
        if state.active_page.value != "physical":
            failures.append("Ctrl+3 did not open Physical")

        state.disks = [
            DiskInfo(
                disk_id="c0e32s0",
                controller_index=0,
                vendor=Vendor.LSI,
                medium=Medium.HDD,
                protocol=Protocol.SAS,
                enclosure=32,
                slot=0,
                model="ST2000NM0008",
                serial="ZTN1",
                size_bytes=2_000_398_934_016,
                raid_state="Onln",
            ),
            DiskInfo(
                disk_id="c0e32s1",
                controller_index=0,
                vendor=Vendor.LSI,
                medium=Medium.SSD,
                protocol=Protocol.SATA,
                enclosure=32,
                slot=1,
                model="S4510",
                serial="S2",
                size_bytes=480_000_000_000,
                raid_state="UGood",
            ),
        ]
        page = screen.query_one(PhysicalPage)
        page.refresh_data()
        await pilot.pause(0.3)
        table = page.query_one(MultiSelectTable)
        if table.row_count != 2:
            failures.append(f"physical table has {table.row_count} rows, expected 2")
        if table.size.width != cols:
            failures.append(f"physical table width is {table.size.width}, expected {cols}")
        if table.virtual_size.width > cols or table.scroll_x:
            failures.append("physical table needs horizontal scrolling")

        await pilot.press("space")
        await pilot.pause(0.2)
        if "c0e32s0" not in state.physical_selection:
            failures.append("Space did not select the cursor row")
        await pilot.press("f8")
        await pilot.pause(0.3)
        if not isinstance(app.screen, ConfirmModal):
            failures.append(f"F8 opened {type(app.screen).__name__}, expected ConfirmModal")
        else:
            for char in "no":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause(0.2)
            if not isinstance(app.screen, ConfirmModal):
                failures.append("wrong acknowledgement was accepted")
            for char in "YES":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause(1.0)
            if not state.erase_history:
                failures.append("no EraseRun recorded after the correct acknowledgement")

        await pilot.press("ctrl+5")
        await pilot.pause(0.2)
        if screen.query_one("TaskTable").row_count < 2:
            failures.append("task queue does not show the refresh and erase tasks")
        await pilot.press("ctrl+6")
        await pilot.pause(0.2)
        if screen.query_one("#page-evidence").size.width != cols:
            failures.append("evidence page does not fill the width")

        await pilot.press("ctrl+7")
        await pilot.pause(0.2)
        settings = screen.query_one("#page-settings")
        await settings._show("appearance")
        await pilot.pause(0.2)
        theme_select = settings.query_one("#settings-theme", Select)
        theme_select.value = "raidforge-mono"
        await pilot.pause(0.2)
        if app.theme != "raidforge-mono":
            failures.append(f"theme switch did not apply (app.theme={app.theme!r})")

    manifest = ev.finalize()
    names = [item["path"] for item in manifest["files"]]
    if not any(name.endswith(".jsonl") for name in names):
        failures.append("events.jsonl missing from evidence")
    if not any(name.endswith(".svg") for name in names):
        failures.append("no SVG snapshots in evidence")

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(" -", failure)
        return 1
    print("UI SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
