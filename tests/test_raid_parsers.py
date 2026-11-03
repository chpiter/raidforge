"""RAID topology parsers for storcli, arcconf and ssacli."""

from __future__ import annotations

from pathlib import Path

from raidforge.core import enum_disks
from raidforge.core.models import ControllerInfo, DiskInfo, Medium, Protocol, Vendor
from raidforge.core.raid import adaptec, hpe, lsi

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# LSI
# ---------------------------------------------------------------------------
def lsi_disks() -> list[DiskInfo]:
    controller = ControllerInfo(index=0, vendor=Vendor.LSI)
    return enum_disks.parse_storcli_json(text("storcli_sall_show_J_raid.json"), controller)


def test_lsi_vall_json_volumes_and_membership():
    controller = ControllerInfo(index=0, vendor=Vendor.LSI)
    disks = lsi_disks()
    volumes, membership, ambiguous = lsi.parse_vall_json(
        text("storcli_vall_show_all_J.json"), controller, disks
    )
    assert ambiguous is False
    assert [(volume.volume_id, volume.level, volume.state) for volume in volumes] == [
        ("v0", "RAID1", "Optl"),
        ("v1", "RAID10", "Optl"),
    ]
    assert volumes[0].name == "OS"
    assert volumes[0].disk_ids == ["c0e32s0", "c0e32s1"]
    assert volumes[1].spans == [
        ["c0e32s2", "c0e32s3"],
        ["c0e32s4", "c0e32s5"],
    ]
    assert membership["c0e32s0"] == "v0"
    assert membership["c0e32s5"] == "v1"


def test_lsi_text_fallback_uses_disk_groups():
    controller = ControllerInfo(index=0, vendor=Vendor.LSI)
    disks = lsi_disks()
    table = """
DG/VD TYPE  State Access Consist Cache Cac sCC       Size Name
0/0   RAID1 Optl  RW     Yes     RWBD  -   ON  1.818 TB OS
1/1   RAID10 Optl RW     Yes     RWBD  -   ON  3.636 TB DATA
"""
    volumes, membership, ambiguous = lsi.parse_vall_text(table, controller, disks)
    assert ambiguous is False
    assert [volume.volume_id for volume in volumes] == ["v0", "v1"]
    assert volumes[1].disk_ids == ["c0e32s2", "c0e32s3", "c0e32s4", "c0e32s5"]
    assert membership["c0e32s4"] == "v1"


def test_lsi_ambiguous_when_members_missing():
    controller = ControllerInfo(index=0, vendor=Vendor.LSI)
    payload = '{"Controllers":[{"Response Data":{"Virtual Drives":[{"DG/VD":"0/0","TYPE":"RAID1","State":"Optl","Size":"1 TB"}]}}]}'
    volumes, membership, ambiguous = lsi.parse_vall_json(payload, controller, [])
    assert len(volumes) == 1
    assert membership == {}
    assert ambiguous is True


# ---------------------------------------------------------------------------
# Adaptec
# ---------------------------------------------------------------------------
def pmc_disks() -> list[DiskInfo]:
    controller = ControllerInfo(index=1, vendor=Vendor.PMC)
    return enum_disks.parse_arcconf_json(text("arcconf_getconfigjson_raid.json"), controller)


def test_adaptec_json_volumes_and_membership():
    controller = ControllerInfo(index=1, vendor=Vendor.PMC)
    disks = pmc_disks()
    volumes, membership, ambiguous = adaptec.parse_ld_json(
        text("arcconf_getconfigjson_raid.json"), controller, disks
    )
    assert ambiguous is False
    assert [(volume.volume_id, volume.level) for volume in volumes] == [("0", "RAID1"), ("1", "RAID10")]
    assert volumes[0].disk_ids == ["c1-ch0-dev0", "c1-ch0-dev1"]
    assert volumes[1].spans == [
        ["c1-ch0-dev2", "c1-ch0-dev3"],
        ["c1-ch0-dev4", "c1-ch0-dev5"],
    ]
    assert membership["c1-ch0-dev2"] == "1"


def test_adaptec_text_fallback():
    controller = ControllerInfo(index=1, vendor=Vendor.PMC)
    disks = pmc_disks()
    volumes, membership, ambiguous = adaptec.parse_ld_text(
        text("arcconf_getconfig_ld.txt"), controller, disks
    )
    assert ambiguous is False
    assert [volume.volume_id for volume in volumes] == ["0", "1"]
    assert volumes[1].disk_ids == ["c1-ch0-dev2", "c1-ch0-dev3", "c1-ch0-dev4", "c1-ch0-dev5"]


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


def test_hpe_ld_detail_and_groups():
    controller = ControllerInfo(index=0, vendor=Vendor.HPE)
    disks = hpe_disks()
    volumes, membership, ambiguous = hpe.parse_ld_detail(
        text("ssacli_ld_all_show_detail.txt"), controller, disks
    )
    assert ambiguous is False
    assert [(volume.volume_id, volume.level) for volume in volumes] == [("1", "RAID1"), ("2", "RAID10")]
    assert volumes[0].disk_ids == ["c0-1I:1:1", "c0-1I:1:2"]
    assert volumes[1].spans == [
        ["c0-2I:1:1", "c0-2I:1:2"],
        ["c0-2I:1:3", "c0-2I:1:4"],
    ]
    assert membership["c0-2I:1:1"] == "2"

    groups = hpe.parse_pd_groups(text("ssacli_pd_all_show.txt"))
    assert groups["1I:1:1"] == "Array A"
    assert groups["2I:1:4"] == "Array B"
    assert groups["1I:1:3"] == "unassigned"


def test_hpe_ambiguous_when_members_missing():
    controller = ControllerInfo(index=0, vendor=Vendor.HPE)
    block = """
Smart Array P440ar in Slot 0 (Embedded)
   Array A
      Logical Drive 1 (1.09 TB, RAID 1, OK)
         Fault Tolerance: 1
         Status: OK
"""
    volumes, membership, ambiguous = hpe.parse_ld_detail(block, controller, hpe_disks())
    assert len(volumes) == 1
    assert membership == {}
    assert ambiguous is True
