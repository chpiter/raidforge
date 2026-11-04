"""Immediate per-run report generation and background upload."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="legacy progress screen; report task covered by tests/test_ui_tasks.py")

pytest.importorskip("textual", reason="needs Textual to run the app headless")
pytest.importorskip("docx", reason="needs python-docx to build the report")
pytest.importorskip("PIL", reason="needs Pillow to embed screenshots")

import textual  # noqa: E402

from docx import Document  # noqa: E402

from raidforge.config import load_config  # noqa: E402
from raidforge.core import publish as publish_mod  # noqa: E402
from raidforge.core.evidence import EvidenceSession  # noqa: E402
from raidforge.core.models import DetectionResult, DiskInfo, Medium, Protocol, Vendor  # noqa: E402
from raidforge.core.report import UploadResult  # noqa: E402
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
        firmware="SN02",
        size_bytes=2000398934016,
        raid_state="Onln",
    )


def _context(tmp_path) -> AppContext:
    cfg = load_config(
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {
                "enabled": True,
                "wait_for_route_seconds": 0,
                "run_upload_seconds": 5,
                "exit_upload_seconds": 5,
            },
            "tools": {"command_timeout": 5},
            "dry_run": True,
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-PUBLISH-TEST"})
    return AppContext(cfg=cfg, evidence=EvidenceSession(cfg, sid.serial), server_id=sid)


async def _wait(pilot, predicate, timeout: float = 10.0, step: float = 0.1) -> bool:
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


class _UploadRecorder:
    def __init__(self) -> None:
        self.calls = []

    def __call__(
        self,
        cfg,
        session_dir,
        deadline=None,
        on_status=None,
        queue_on_failure=True,
        skip_route_wait=False,
    ):
        self.calls.append(
            {
                "queue_on_failure": queue_on_failure,
                "deadline": deadline,
                "skip_route_wait": skip_route_wait,
            }
        )
        if on_status:
            on_status("fake upload")
        return UploadResult("rsync", True, "fake upload")


def _document_text(document) -> str:
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


@needs_pinned_textual
def test_each_run_publishes_before_quit(tmp_path, monkeypatch):
    uploads = _UploadRecorder()
    monkeypatch.setattr(publish_mod, "upload_session", uploads)
    empty = DetectionResult(tool="test", vendor=Vendor.LSI, controllers=[])
    monkeypatch.setattr(dashboard_mod.detect_mod, "detect_controllers", lambda cfg: empty)
    monkeypatch.setattr(
        dashboard_mod.enum_mod, "enumerate_disks", lambda cfg, detection: ([], [])
    )

    ctx = _context(tmp_path)
    disks = [
        _disk("c0e32s0", Medium.HDD, "ZTN1"),
        _disk("c0e32s1", Medium.SSD, "BTWL1234"),
    ]

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause(0.4)
            ctx.disks = list(disks)
            ctx.inventory = list(disks)

            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, DiskListScreen)
            await pilot.press("space")
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmModal)
            await _approve(pilot)
            assert await _wait(
                pilot,
                lambda: isinstance(app.screen, ProgressScreen)
                and ctx.publish_state.generation >= 1,
            ), f"run 1 did not publish: {ctx.publish_state.status!r}"
            await pilot.pause(0.2)

            reports = list(ctx.evidence.root.glob("*-raidforge-report-*.docx"))
            assert len(reports) == 1, [p.name for p in reports]
            first = Document(str(reports[0]))
            first_text = _document_text(first)
            assert first_text.count("擦除记录") == 1
            assert len(first.inline_shapes) >= 2, "confirm/progress screenshots missing"
            assert uploads.calls and uploads.calls[0]["queue_on_failure"] is False

            # Second run while still on the Progress screen, then back.
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert isinstance(app.screen, DiskListScreen)
            await pilot.press("space")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmModal)
            await _approve(pilot)
            assert await _wait(
                pilot,
                lambda: isinstance(app.screen, ProgressScreen)
                and ctx.publish_state.generation >= 2,
            ), f"run 2 did not publish: {ctx.publish_state.status!r}"
            await pilot.pause(0.2)

            reports = list(ctx.evidence.root.glob("*-raidforge-report-*.docx"))
            assert len(reports) == 1, [p.name for p in reports]
            second = Document(str(reports[0]))
            assert _document_text(second).count("擦除记录") == 2
            assert len(second.inline_shapes) >= 4
            assert [call["queue_on_failure"] for call in uploads.calls[:2]] == [False, False]

            await pilot.press("escape")
            await pilot.pause(0.2)
            await pilot.press("q")
            await pilot.pause(1.0)
            assert app.return_code == 0

    asyncio.run(scenario())

    # Exit path publishes again and is allowed to queue.
    manifest = finalize_session(ctx)
    assert "report_file" in manifest, sorted(manifest.keys())
    assert len(manifest["erase_runs"]) == 2
    assert uploads.calls[-1]["queue_on_failure"] is True
