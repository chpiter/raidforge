"""Provider collection and new-RAID eligibility (free vs configured disks)."""

from __future__ import annotations

import json
from pathlib import Path

from raidforge.config import default_config
from raidforge.core import enum_disks
from raidforge.core.models import ControllerInfo, DiskInfo, Medium, Protocol, Vendor
from raidforge.core.raid.adaptec import AdaptecProvider
from raidforge.core.raid.hpe import HpeProvider
from raidforge.core.raid.lsi import LsiProvider
from raidforge.core.utils import CmdResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeShell:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, cmd, timeout=60.0, **kwargs):
        argv = list(cmd) if not isinstance(cmd, str) else cmd.split()
        key = " ".join(argv)
        self.calls.append(key)
        if key in self.table:
            return CmdResult(0, self.table[key], "", 0.01, argv)
        return CmdResult(127, "", f"no such file or directory: {key}", 0.001, argv)


# ---------------------------------------------------------------------------
# LSI
# ---------------------------------------------------------------------------
def test_lsi_collect_marks_free_and_blocked_disks(monkeypatch):
    controller = ControllerInfo(index=0, vendor=Vendor.LSI, binary="storcli")
    controller.raw_json = json.loads(text("storcli_c0_show_J.json"))
    disks = enum_disks.parse_storcli_json(text("storcli_sall_show_J_raid.json"), controller)
    shell = FakeShell({"storcli /c0/vall show all J": text("storcli_vall_show_all_J.json")})
    monkeypatch.setattr("raidforge.core.raid.lsi.run_cmd", shell)

    topology, raw = LsiProvider("storcli").collect(default_config(), controller, disks)
    assert topology.error == ""
    assert topology.free_disk_ids == ["c0e32s6"]
    assert topology.membership["c0e32s0"] == "v0"
    assert "member of v1" in topology.blocked["c0e32s2"]
    assert "Frgn" in topology.blocked["c0e32s7"]
    assert "Failed" in topology.blocked["c0e32s8"]
    assert controller.details["Product Name"] == "MegaRAID Tri-Mode SAS3108"
    assert raw and raw[0][0].startswith("raid_lsi_c0_")


def test_lsi_collect_blocks_everything_when_members_unparsable(monkeypatch):
    controller = ControllerInfo(index=0, vendor=Vendor.LSI, binary="storcli")
    disks = enum_disks.parse_storcli_json(text("storcli_sall_show_J_raid.json"), controller)
    broken = '{"Controllers":[{"Response Data":{"Virtual Drives":[{"DG/VD":"0/0","TYPE":"RAID1","State":"Optl"}]}}]}'
    shell = FakeShell(
        {
            "storcli /c0/vall show all J": broken,
            "storcli /c0/vall show": "",
        }
    )
    monkeypatch.setattr("raidforge.core.raid.lsi.run_cmd", shell)
    topology, _raw = LsiProvider("storcli").collect(default_config(), controller, disks)
    assert topology.free_disk_ids == []
    assert all("unparsable" in reason for reason in topology.blocked.values())


# ---------------------------------------------------------------------------
# Adaptec
# ---------------------------------------------------------------------------
def test_adaptec_collect_marks_ready_disks_free(monkeypatch):
    controller = ControllerInfo(index=1, vendor=Vendor.PMC, binary="arcconf")
    controller.raw_json = json.loads(text("arcconf_getconfigjson_raid.json"))
    disks = enum_disks.parse_arcconf_json(text("arcconf_getconfigjson_raid.json"), controller)
    shell = FakeShell({"arcconf getconfigjson 1": text("arcconf_getconfigjson_raid.json")})
    monkeypatch.setattr("raidforge.core.raid.adaptec.run_cmd", shell)

    topology, raw = AdaptecProvider("arcconf").collect(default_config(), controller, disks)
    assert topology.error == ""
    assert topology.free_disk_ids == ["c1-ch0-dev6", "c1-ch0-dev7"]
    assert topology.membership["c1-ch0-dev0"] == "0"
    assert "member of logical device 1" in topology.blocked["c1-ch0-dev2"]
    assert controller.details["ControllerName"] == "Adaptec SmartRAID 3154-8i"
    assert raw and raw[0][0].startswith("raid_pmc_c1_")


# ---------------------------------------------------------------------------
# HPE
# ---------------------------------------------------------------------------
def hpe_disks() -> list[DiskInfo]:
    refs = ["1I:1:1", "1I:1:2", "2I:1:1", "2I:1:2", "2I:1:3", "2I:1:4", "1I:1:3", "1I:1:4"]
    return [
        DiskInfo(
            disk_id=f"c0-{ref}",
            controller_index=0,
            vendor=Vendor.HPE,
            medium=Medium.HDD,
            protocol=Protocol.SAS,
            controller_ref=ref,
            raid_state="OK",
            size_bytes=600 * 10**9,
        )
        for ref in refs
    ]


def test_hpe_collect_uses_unassigned_group(monkeypatch):
    controller = ControllerInfo(index=0, vendor=Vendor.HPE, binary="ssacli")
    shell = FakeShell(
        {
            "ssacli ctrl slot=0 ld all show detail": text("ssacli_ld_all_show_detail.txt"),
            "ssacli ctrl slot=0 pd all show": text("ssacli_pd_all_show.txt"),
            "ssacli ctrl slot=0 show detail": text("ssacli_ctrl_all_show.txt"),
        }
    )
    monkeypatch.setattr("raidforge.core.raid.hpe.run_cmd", shell)
    topology, raw = HpeProvider("ssacli").collect(default_config(), controller, hpe_disks())
    assert topology.error == ""
    assert topology.free_disk_ids == ["c0-1I:1:3", "c0-1I:1:4"]
    assert topology.membership["c0-1I:1:1"] == "1"
    assert "member of logical drive 2" in topology.blocked["c0-2I:1:1"]
    assert controller.details["Serial Number"] == "PDNLH0BRH9P0RF"
    assert raw and any(name.startswith("raid_hpe_c0_") for name, _ in raw)


def test_hpe_collect_blocks_everything_without_pd_grouping(monkeypatch):
    controller = ControllerInfo(index=0, vendor=Vendor.HPE, binary="ssacli")
    shell = FakeShell(
        {
            "ssacli ctrl slot=0 ld all show detail": text("ssacli_ld_all_show_detail.txt"),
            "ssacli ctrl slot=0 pd all show": "",
            "ssacli ctrl slot=0 show detail": "",
        }
    )
    monkeypatch.setattr("raidforge.core.raid.hpe.run_cmd", shell)
    topology, _raw = HpeProvider("ssacli").collect(default_config(), controller, hpe_disks())
    assert topology.free_disk_ids == []
    assert all("grouping unavailable" in reason for reason in topology.blocked.values())
