"""hiraidadm parsers, disk enumeration and HiRAID provider eligibility."""

from __future__ import annotations

from pathlib import Path

from raidforge.config import default_config
from raidforge.core import enum_disks
from raidforge.core import hiraidadm as h
from raidforge.core.models import ControllerInfo, Medium, Protocol, Vendor
from raidforge.core.raid.huawei import HuaweiProvider
from raidforge.core.utils import CmdResult

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hiraidadm"


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


def controller() -> ControllerInfo:
    return ControllerInfo(index=0, vendor=Vendor.HUAWEI, binary="hiraidadm")


def test_banner_and_status() -> None:
    ok = text("show_allctrl.txt")
    assert h.status_code(ok) == 0
    assert h.is_success(ok)
    assert not h.is_success(text("status_error.txt"))
    assert h.status_code(text("status_error.txt")) == 1


def test_controller_list_and_fields() -> None:
    entries = h.controller_entries(text("show_allctrl.txt"))
    assert len(entries) == 1
    assert entries[0]["controller id"] == "0"
    assert entries[0]["pci address"] == "0000:85:00.0"
    assert entries[0]["device name"] == "/dev/bsg/hiraid0"

    fields = h.controller_fields(text("c0_show.txt"))
    assert fields == {
        "model": "SP686C-M-16i",
        "serial": "2102312XYZ",
        "firmware": "2.0.4.1",
        "sas_address": "0x50c31dc5b71e8000",
    }


def test_pdlist_two_line_header() -> None:
    rows = h.parse_pdlist(text("c0_pdlist.txt"))
    assert len(rows) == 3
    first = rows[0]
    assert first["index"] == "0"
    assert first["enc"] == "0"
    assert first["slot"] == "0"
    assert first["did"] == "5"
    assert first["intf"] == "SAS"
    assert first["media type"] == "HDD"
    assert first["status"] == "Unconfigured Good"
    assert first["capacity"] == "1.200TB"
    assert first["spin"] == "7200"
    assert first["model"] == "ST1200MM0009"
    assert rows[1]["media type"] == "SSD"
    assert rows[2]["status"] == "Online"


def test_vdlist_and_members() -> None:
    assert h.vd_ids(text("c0_vdlist.txt")) == ["0"]
    rows = h.parse_vdlist(text("c0_vdlist.txt"))
    assert rows[0]["name"] == "vd0"
    assert rows[0]["raid level"] == "1"

    members = h.parse_pdarray(text("c0_vd0_pdarray.txt"))
    assert [(row["enc"], row["slot"]) for row in members] == [("0", "0"), ("0", "1")]


def test_detail_kv() -> None:
    fields = h.parse_kv(text("c0_e0_s0_show.txt"))
    assert fields["serial number"] == "Z1ABC123"
    assert fields["raid group id"] == "NA"
    assert h.erase_progress(text("c0_show_erase.txt"))["erase progress"] == "45%"


def test_parse_hiraidadm_pd_medium_and_protocol() -> None:
    disks = enum_disks.parse_hiraidadm_pd(text("c0_pdlist.txt"), controller())
    assert [disk.disk_id for disk in disks] == ["c0e0s0", "c0e0s1", "c0e0s2"]
    assert disks[0].medium is Medium.HDD
    assert disks[1].medium is Medium.SSD
    assert disks[0].protocol is Protocol.SAS
    assert disks[0].size_bytes > 0
    assert disks[0].device_node == ""
    assert disks[2].raid_state == "Online"


def test_augment_serial_from_detail(monkeypatch) -> None:
    disks = enum_disks.parse_hiraidadm_pd(text("c0_pdlist.txt"), controller())
    shell = FakeShell(
        {
            "hiraidadm c0:e0:s0 show": text("c0_e0_s0_show.txt"),
            "hiraidadm c0:e0:s1 show": text("c0_e0_s0_show.txt"),
            "hiraidadm c0:e0:s2 show": text("c0_e0_s0_show.txt"),
        }
    )
    monkeypatch.setattr(enum_disks, "run_cmd", shell)
    raw = []
    enum_disks.augment_hiraidadm_details(default_config(), "hiraidadm", disks, raw)
    assert disks[0].serial == "Z1ABC123"
    assert disks[0].firmware == "4.10"
    assert len(raw) == 3


def _collect(monkeypatch, pdarray: str):
    disks = enum_disks.parse_hiraidadm_pd(text("c0_pdlist.txt"), controller())
    shell = FakeShell(
        {
            "hiraidadm c0 show pdlist": text("c0_pdlist.txt"),
            "hiraidadm c0 show vdlist": text("c0_vdlist.txt"),
            "hiraidadm c0 show": text("c0_show.txt"),
            "hiraidadm c0:vd0 show": text("c0_vd0_show.txt"),
            "hiraidadm c0:vd0 show pdarray": pdarray,
        }
    )
    monkeypatch.setattr("raidforge.core.raid.huawei.run_cmd", shell)
    return HuaweiProvider("hiraidadm").collect(default_config(), controller(), disks)


def test_provider_collect_membership(monkeypatch) -> None:
    topology, raw = _collect(monkeypatch, text("c0_vd0_pdarray.txt"))
    assert topology.error == ""
    assert len(topology.volumes) == 1
    volume = topology.volumes[0]
    assert volume.volume_id == "vd0"
    assert volume.level == "RAID1"
    assert volume.disk_ids == ["c0e0s0", "c0e0s1"]
    assert topology.membership == {"c0e0s0": "vd0", "c0e0s1": "vd0"}
    assert topology.free_disk_ids == []
    assert "state 'Online' is not free" in topology.blocked["c0e0s2"]
    assert raw and raw[0][0] == "raid_huawei_c0_pdlist.txt"


def test_provider_blocks_everything_when_members_unparsable(monkeypatch) -> None:
    topology, _raw = _collect(monkeypatch, "")
    assert topology.free_disk_ids == []
    assert all("unparsable" in reason for reason in topology.blocked.values())
