"""RAID UI flow and destructive gates (requires the pinned Textual version)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skip(reason="legacy RAID screens; replaced by tests/test_ui_wizard.py")

pytest.importorskip("textual", reason="needs Textual to run the app headless")

import textual  # noqa: E402

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
)
from raidforge.core.raid.commands import clear_plans  # noqa: E402
from raidforge.core.serverid import resolve_server_id  # noqa: E402
from raidforge.ui.app import create_app  # noqa: E402
from raidforge.ui.screens import dashboard as dashboard_mod  # noqa: E402
from raidforge.ui.screens.controller_detail import ControllerDetailScreen  # noqa: E402
from raidforge.ui.screens.controllers import ControllersScreen  # noqa: E402
from raidforge.ui.screens.raid_confirm import RaidConfirmModal  # noqa: E402
from raidforge.ui.screens.raid_create import RaidCreateScreen  # noqa: E402
from raidforge.ui.state import AppContext  # noqa: E402

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
            "raid": {"command_timeout": 5},
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-RAID-UI"})
    ctx = AppContext(cfg=cfg, evidence=EvidenceSession(cfg, sid.serial), server_id=sid)
    controller = ControllerInfo(
        index=0,
        vendor=Vendor.LSI,
        model="MegaRAID Tri-Mode SAS3108",
        firmware="5.230",
        serial="SV123",
        tool="storcli",
        binary="storcli",
    )
    disks = [
        DiskInfo(
            disk_id=f"c0e32s{index}",
            controller_index=0,
            vendor=Vendor.LSI,
            medium=Medium.HDD,
            protocol=Protocol.SAS,
            enclosure=32,
            slot=index,
            size_bytes=600 * 10**9,
            raid_state="UGood",
        )
        for index in range(4)
    ]
    ctx.detection = DetectionResult(
        tool="storcli",
        vendor=Vendor.LSI,
        controllers=[controller],
        binary="storcli",
        provider="lsi",
    )
    ctx.disks = disks
    ctx.raid = RaidBundle(
        tool="storcli",
        binary="storcli",
        topologies=[
            RaidTopology(
                controller_index=0,
                vendor=Vendor.LSI,
                tool="storcli",
                binary="storcli",
                volumes=[],
                membership={},
                free_disk_ids=[disk.disk_id for disk in disks],
                blocked={},
                disk_states={disk.disk_id: "UGood" for disk in disks},
            )
        ],
        capabilities={
            0: RaidCapabilities(
                levels=["RAID0", "RAID1", "RAID5", "RAID10"],
                create=True,
                delete_vd=True,
                clear_config=True,
                raid0_per_disk=True,
                name_supported=True,
                stripe_supported=True,
            )
        },
    )
    return ctx


@needs_pinned_textual
def test_dashboard_to_create_and_wrong_ack(monkeypatch, tmp_path):
    ctx = _context(tmp_path)
    monkeypatch.setattr(dashboard_mod, "run_scan", lambda _ctx: SimpleNamespace(detection=ctx.detection))

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("c")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ControllersScreen)

            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ControllerDetailScreen)

            await pilot.press("c")
            await pilot.pause(0.2)
            assert isinstance(app.screen, RaidCreateScreen)

            # Select all free disks, choose RAID0, then open the confirmation.
            await pilot.press("0")
            await pilot.press("a")
            await pilot.pause(0.2)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, RaidConfirmModal)

            # Wrong word must not dismiss the modal.
            await pilot.press("n", "o", "enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, RaidConfirmModal)

    asyncio.run(scenario())


@needs_pinned_textual
def test_clear_config_requires_controller_token(tmp_path):
    ctx = _context(tmp_path)
    controller = ctx.detection.controllers[0]
    plans = clear_plans(controller)
    approved = []

    async def scenario() -> None:
        app = create_app(ctx)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause(0.4)
            app.push_screen(
                RaidConfirmModal(ctx, plans, "Clear ALL RAID configuration on c0", "c0"),
                lambda value: approved.append(value),
            )
            await pilot.pause(0.3)
            # "yes" is deliberately not enough for a full controller clear.
            await pilot.press("y", "e", "s", "enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, RaidConfirmModal)
            await pilot.press("c", "0", "enter")
            await pilot.pause(0.2)
            assert approved == [True]

    asyncio.run(scenario())
