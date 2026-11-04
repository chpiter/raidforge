"""Live report path: drive two erase rounds through the real Textual app.

This is the test that catches integration drift: Textual's SVG writer, the
snapshot tagging, the ``done -> Esc -> erase again`` loop and the DOCX
generation all have to line up before the report is useful.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="legacy progress screen; report task covered by tests/test_ui_tasks.py")

pytest.importorskip("textual", reason="needs Textual to run the app headless")
pytest.importorskip("docx", reason="needs python-docx to build the report")
pytest.importorskip("PIL", reason="needs Pillow to embed screenshots")

import textual  # noqa: E402

from docx import Document  # noqa: E402

from raidforge.config import load_config  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.models import DetectionResult, DiskInfo, Medium, Protocol, Vendor  # noqa: E402
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.main import finalize_session  # noqa: E402
from raidforge.ui.app import create_app  # noqa: E402
from raidforge.ui.screens import dashboard as dashboard_mod  # noqa: E402
from raidforge.ui.screens.confirm import ConfirmModal  # noqa: E402
from raidforge.ui.screens.disk_list import DiskListScreen  # noqa: E402
from raidforge.ui.screens.progress import ProgressScreen  # noqa: E402
from raidforge.ui.state import AppContext  # noqa: E402

PINNED_TEXTUAL = "0.89.1"
needs_pinned_textual = pytest.mark.skipif(
    getattr(textual, "__version__", "") != PINNED_TEXTUAL,
    reason=f"verified against textual {PINNED_TEXTUAL}; running {getattr(textual, '__version__', '?')}",
)


def _disk(disk_id: str, medium: Medium, serial: str) -> DiskInfo:
    return DiskInfo(
        disk_id=disk_id,
        controller_index=0,
        vendor=Vendor.LSI,
        medium=medium,
        protocol=Protocol.SAS,
        enclosure=32,
        slot=0,
        model="TESTMODEL",
        serial=serial,
        size_bytes=2000398934016,
        raid_state="Onln",
    )


def _context(tmp_path: Path) -> AppContext:
    cfg = load_config(
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {"enabled": False, "wait_for_route_seconds": 0},
            "tools": {"command_timeout": 5},
            "dry_run": True,
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-LIVE-REPORT"})
    ev = EvidenceSession(cfg, sid.serial)
    return AppContext(cfg=cfg, evidence=ev, server_id=sid)


async def _wait(pilot, predicate, *, timeout: float = 8.0, step: float = 0.1) -> bool:
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        await pilot.pause(step)
        waited += step
    return predicate()


async def _approve(pilot) -> None:
    for char in "yes":
        await pilot.press(char)
    await pilot.press("enter")


@needs_pinned_textual
def test_two_rounds_produce_one_report(tmp_path, monkeypatch):
    # No vendor probing from a unit test: the scans are replaced by no-ops and
    # the inventory is injected after mount, exactly like ui_smoke.py does.
    empty = DetectionResult(tool="test", vendor=Vendor.LSI, controllers=[])
    monkeypatch.setattr(dashboard_mod.detect_mod, "detect_controllers", lambda cfg: empty)
    monkeypatch.setattr(dashboard_mod.enum_mod, "enumerate_disks", lambda cfg, detection: ([], []))

    ctx = _context(tmp_path)
    disks = [
        _disk("c0e32s0", Medium.HDD, "ZTN1"),
        _disk("c0e32s1", Medium.SSD, "BTWL1234"),
    ]

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.5)
            assert isinstance(app.screen, dashboard_mod.DashboardScreen)
            ctx.disks = list(disks)
            ctx.inventory = list(disks)

            # -- round 1: HDD ------------------------------------------------
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, DiskListScreen)
            await pilot.press("space")
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmModal)
            assert ctx.run_epoch == 1
            await _approve(pilot)
            assert await _wait(
                pilot, lambda: isinstance(app.screen, ProgressScreen) and len(ctx.erase_history) == 1
            ), f"round 1 never finished - screen {app.screen!r}"
            assert await _wait(
                pilot, lambda: ctx.publish_state.generation >= 1
            ), f"run 1 report was not published: {ctx.publish_state.status!r}"

            # -- back, switch to the SSD, run again --------------------------
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.screen, DiskListScreen)
            await pilot.press("space")  # deselect the HDD row
            await pilot.press("down")
            await pilot.press("space")  # select the SSD row
            await pilot.pause(0.1)
            assert ctx.selection == ["c0e32s1"], f"unexpected selection {ctx.selection}"

            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmModal)
            assert ctx.run_epoch == 2
            await _approve(pilot)
            assert await _wait(
                pilot, lambda: isinstance(app.screen, ProgressScreen) and len(ctx.erase_history) == 2
            ), f"round 2 never finished - screen {app.screen!r}"
            assert await _wait(
                pilot, lambda: ctx.publish_state.generation >= 2
            ), f"run 2 report was not published: {ctx.publish_state.status!r}"

            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.screen, DiskListScreen)
            await pilot.press("q")
            await pilot.pause(1.2)
            assert app.return_code == 0, f"app did not exit - screen {app.screen!r}"

    asyncio.run(scenario())

    assert len(ctx.erase_history) == 2
    assert ctx.erase_history[0].disks, "plan-time disk snapshot was not recorded"
    assert ctx.erase_history[0].disks["c0e32s0"].serial == "ZTN1"
    tagged = {(record.stem, record.run_epoch) for record in ctx.snapshot_log}
    assert ("confirm", 1) in tagged and ("progress", 1) in tagged
    assert ("confirm", 2) in tagged and ("progress", 2) in tagged

    manifest = finalize_session(ctx, publish=True)
    reports = list(ctx.evidence.root.glob("*-raidforge-report-*.docx"))
    assert len(reports) == 1, [path.name for path in reports]
    assert manifest["report_file"] == reports[0].name
    assert len(manifest["erase_runs"]) == 2

    document = Document(str(reports[0]))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    text = "\n".join(parts)
    assert "擦除记录" in text
    assert "ZTN1" in text and "BTWL1234" in text
    assert "本次擦除" in text and "固件/RAID" in text
    for removed in ("第 1 次", "第 2 次", "第 1 轮", "第 2 轮", "操作模式", "擦除轮次"):
        assert removed not in text
    # Real Textual SVGs must have survived the rasteriser.
    assert len(document.inline_shapes) >= 4

    files = {entry["path"] for entry in manifest["files"]}
    assert reports[0].name in files
