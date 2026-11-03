"""Headless UI smoke test - runs in the *same* interpreter that ships in the ISO.

Pushed to the build host by tools/rssh.py during deployment debugging:

    /root/tv/bin/python /root/ui_smoke.py

It drives Dashboard -> DiskList -> Confirm -> Progress through Textual's
run_test() pilot and asserts the safety gates actually fire.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Works in-repo (tests/../app), on a bare host next to a checkout, and inside
# the built image (RAIDFORGE_APP_ROOT=/opt/raidforge).
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

from raidforge.config import load_config
from raidforge.core.evidence import EvidenceSession
from raidforge.core.models import DiskInfo, Medium, Protocol, Vendor
from raidforge.core.serverid import resolve_server_id
from raidforge.ui.app import create_app
from raidforge.ui.state import AppContext


async def main() -> int:
    cfg = load_config(
        overrides={
            "evidence": {"root": "/tmp/ui-smoke-evidence"},
            "report": {"enabled": False},
            "tools": {"command_timeout": 5},
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-HOST-TEST-01"})
    ev = EvidenceSession(cfg, sid.serial)
    ctx = AppContext(cfg=cfg, evidence=ev, server_id=sid)
    app = create_app(ctx)

    failures: list[str] = []

    # 120x32 by default; set UI_SMOKE_SIZE=80x25 to rehearse the shipped console.
    cols, rows = (int(part) for part in os.environ.get("UI_SMOKE_SIZE", "120x32").lower().split("x"))
    async with app.run_test(size=(cols, rows)) as pilot:
        await pilot.pause(0.8)
        print("screen:", type(app.screen).__name__)

        # --- settings screen: transport, IPv4 and port -----------------------
        import raidforge.config as _config_mod
        import raidforge.ui.screens.settings as _settings_mod
        from raidforge.core import report as _report_mod
        from textual.widgets import Button as _Button
        from textual.widgets import Input as _Input

        # Never write the host's real /etc from a smoke test.
        _config_mod.CONFIG_DIR = "/tmp/ui-smoke-config.d"
        _config_mod.SITE_OVERRIDE_PATH = "/tmp/ui-smoke-config.d/90-site.yaml"
        # Never open a real ssh/http connection from a smoke test either; the
        # probes themselves are covered by test_report_ssh/test_report_http.
        _settings_mod.report_mod.check_ssh = (
            lambda cfg, timeout=5.0: _report_mod.SshCheck(True, "smoke: ssh reachable")
        )
        _settings_mod.report_mod.check_http = (
            lambda cfg, timeout=5.0: _report_mod.ProbeCheck(True, "smoke: http reachable")
        )
        await pilot.press("s")
        await pilot.pause(0.3)
        if type(app.screen).__name__ != "SettingsScreen":
            failures.append(f"S opened {type(app.screen).__name__}, expected SettingsScreen")
        else:
            app.screen.query_one("#ssh-host", _Input).value = "10.9.8.7"
            await pilot.press("enter")
            await pilot.pause(0.5)
            if ctx.cfg.report.ssh_host != "10.9.8.7":
                failures.append("settings did not update cfg.report.ssh_host")
            if ctx.cfg.report.ssh_user != "evidence":
                failures.append("settings changed the fixed ssh_user")
            if "reachable" not in ctx.last_message:
                failures.append("settings probe result did not reach the status line")

            # Switch the preferred transport to HTTP; the port follows it.
            app.screen.query_one("#transport-http", _Button).press()
            await pilot.pause(0.2)
            if app.screen.query_one("#port", _Input).value != "8080":
                failures.append("HTTP mode did not select port 8080")
            await pilot.press("enter")
            await pilot.pause(0.5)
            if ctx.cfg.report.transport != "http":
                failures.append("settings did not switch cfg.report.transport to http")
            if ctx.cfg.report.http_base_url != "http://10.9.8.7:8080":
                failures.append(
                    f"derived http_base_url is {ctx.cfg.report.http_base_url!r}"
                )
            await pilot.press("escape")
            await pilot.pause(0.3)
            print("screen:", type(app.screen).__name__)

        ctx.disks = [
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
                size_bytes=2000398934016,
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
                raw={"SED": "Y"},
            ),
            DiskInfo(
                disk_id="c0e32s2",
                controller_index=0,
                vendor=Vendor.LSI,
                medium=Medium.UNKNOWN,
                enclosure=32,
                slot=2,
                model="MYSTERY",
            ),
        ]
        ctx.inventory = list(ctx.disks)

        if getattr(app.screen, "focused", None) is None or app.screen.focused.id != "go-disks":
            failures.append("dashboard default focus is not #go-disks")
        await pilot.press("enter")
        await pilot.pause(0.4)
        print("screen:", type(app.screen).__name__)

        # --- row cursor: focus, movement, visible styling -------------------
        from raidforge.ui.theme import build_css
        from raidforge.ui.widgets import DiskTable

        table = app.screen.query_one(DiskTable)
        if not table.has_focus:
            failures.append("disk table did not take keyboard focus on mount")
        if table.cursor_type != "row":
            failures.append(f"cursor_type={table.cursor_type}, expected 'row'")
        start_row = table.cursor_row
        await pilot.press("down")
        await pilot.pause(0.1)
        if table.cursor_row != start_row + 1:
            failures.append(f"down did not move the row cursor ({start_row} -> {table.cursor_row})")
        await pilot.press("up")
        await pilot.pause(0.1)
        if table.cursor_row != start_row:
            failures.append("up did not move the row cursor back")
        css = build_css(cfg)
        header_rule = css.split("DataTable > .datatable--header", 1)[-1].split("}", 1)[0]
        for needle in ("background: $foreground", "color: $background"):
            if needle not in header_rule:
                failures.append(f"table header rule is missing {needle!r}: {header_rule.strip()!r}")

        # The theme must actually be selected, not just registered.
        theme_name = getattr(app, "theme", None)
        if theme_name != "raidforge-mono":
            failures.append(f"monochrome theme is not active (app.theme={theme_name!r})")
        print("theme:", theme_name)

        # The cursor is a text marker in the first column: colour-based
        # highlighting proved unreliable on the 16-colour console, a ">" does not.
        from textual.coordinate import Coordinate

        def marker(row: int) -> str:
            return str(table.get_cell_at(Coordinate(row, 0))).strip()

        if marker(0) != ">":
            failures.append(f"cursor marker missing on row 0 (got {marker(0)!r})")
        print("cursor marker row0:", repr(marker(0)), "row1:", repr(marker(1)))

        await pilot.press("space")
        await pilot.pause(0.15)
        if len(ctx.selection) != 1:
            failures.append(f"space did not select the first row (selection={ctx.selection})")

        await pilot.press("down", "space")
        await pilot.pause(0.15)
        if len(ctx.selection) != 2:
            failures.append(f"second row not selectable (selection={ctx.selection})")

        # UNKNOWN medium must be refused (constraint 8).
        await pilot.press("down", "space")
        await pilot.pause(0.15)
        if "c0e32s2" in ctx.selection:
            failures.append("UNKNOWN medium disk was selectable")

        await pilot.press("enter")
        await pilot.pause(0.5)
        if type(app.screen).__name__ != "ConfirmModal":
            failures.append(f"expected ConfirmModal, got {type(app.screen).__name__}")

        # Wrong acknowledgement must not proceed.
        for ch in "no":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.3)
        if type(app.screen).__name__ != "ConfirmModal":
            failures.append("wrong acknowledgement was accepted")

        for ch in "YES":  # case-insensitive on purpose
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(1.2)
        print("screen after ack:", type(app.screen).__name__)
        if not ctx.results:
            failures.append("no submission results recorded after correct acknowledgement")

        for row in ctx.results:
            print(f"  {row.disk_id}: {row.status.value} rc={row.return_code}")

        if not ctx.erase_history or not ctx.erase_history[-1].disks:
            failures.append("EraseRun did not keep the plan-time disk snapshot")

        # The per-run publisher must create the DOCX before Q/exit.
        for _ in range(80):
            if ctx.publish_state.generation >= 1:
                break
            await pilot.pause(0.1)
        if ctx.publish_state.generation < 1:
            failures.append(f"per-run report was not published: {ctx.publish_state.status!r}")
        if not list(ev.root.glob("*-raidforge-report-*.docx")):
            failures.append("no DOCX report before exit")

        await pilot.press("escape")
        await pilot.pause(0.3)

    manifest = ev.finalize()
    names = [f["path"] for f in manifest["files"]]
    print("evidence files:", names)
    if not any(n.endswith(".jsonl") for n in names):
        failures.append("events.jsonl missing from evidence")
    if not any(n.endswith(".svg") for n in names):
        failures.append("no SVG snapshots in evidence")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nUI SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
