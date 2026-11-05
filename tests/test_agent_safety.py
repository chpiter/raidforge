from __future__ import annotations

import json
import pytest
from pathlib import Path

from raidforge.agent.bootmedia import probe
from raidforge.agent.models import TaskRejected
from raidforge.agent.safety import check_boot_media
from raidforge.agent.topology import controller_identity
from raidforge.core.models import DiskInfo, Medium, Vendor

from agent_fakes import FakeRunner
from raidforge.core.utils import CmdResult


LSBLK = {
    "blockdevices": [
        {
            "name": "sda",
            "kname": "sda",
            "path": "/dev/sda",
            "type": "disk",
            "rm": False,
            "serial": "SYS-SERIAL",
            "wwn": "0x5000c500a1b2c3d4",
            "mountpoints": [None],
            "children": [
                {
                    "name": "sda2",
                    "kname": "sda2",
                    "path": "/dev/sda2",
                    "type": "part",
                    "mountpoints": ["/"],
                }
            ],
        },
        {
            "name": "sdb",
            "kname": "sdb",
            "path": "/dev/sdb",
            "type": "disk",
            "rm": True,
            "serial": "USB-SERIAL",
            "wwn": "0x5001a1b2c3d4e5f6",
            "mountpoints": ["/run/live/medium"],
        },
    ]
}


def _runner() -> FakeRunner:
    return FakeRunner(
        responses={
            "lsblk": CmdResult(0, json.dumps(LSBLK), "", 0.0, ["lsblk"]),
            "findmnt": CmdResult(0, "/dev/sda2\n", "", 0.0, ["findmnt"]),
        }
    )


def test_probe_protects_root_and_live_media(tmp_path: Path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "36 25 8:2 / / rw,relatime - ext4 /dev/sda2 rw\n"
        "37 25 8:18 / /run/live/medium ro - iso9660 /dev/sdb ro\n",
        encoding="utf-8",
    )
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("boot=live components", encoding="utf-8")
    boot = probe(_runner(), mountinfo_path=mountinfo, sys_block=tmp_path / "sysblock", cmdline_path=cmdline)
    assert boot.determined is True
    assert boot.is_protected("SYS-SERIAL", "0x5000c500a1b2c3d4")
    assert boot.is_protected("USB-SERIAL", "0x5001a1b2c3d4e5f6")
    assert boot.disk_for("USB-SERIAL", "").removable is True


def test_probe_fails_closed_without_identity(tmp_path: Path):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("", encoding="utf-8")
    runner = FakeRunner(responses={"lsblk": CmdResult(127, "", "missing", 0.0, ["lsblk"])})
    boot = probe(runner, mountinfo_path=tmp_path / "missing", sys_block=tmp_path / "sysblock", cmdline_path=cmdline)
    assert boot.determined is False


def test_safety_blocks_boot_and_member_disks(tmp_path: Path):
    from raidforge.agent.snapshot import HostSnapshot
    from raidforge.agent.bootmedia import BootMediaMap, ProtectedDisk

    boot = BootMediaMap(
        determined=True,
        disks=(ProtectedDisk("sda", "/dev/sda", "SYS-SERIAL", "0x5000c500a1b2c3d4", ("/",), False, "mounted"),),
        serials=frozenset({"sys-serial"}),
        wwns=frozenset({"0x5000c500a1b2c3d4"}),
    )
    snapshot = HostSnapshot(
        server_sn="SN",
        hardware={},
        controllers=(),
        pds=(),
        vds=(),
        capabilities={},
        boot_media=boot,
        storage={},
        projection={},
        topology_hash="x",
        collected_at="2026-11-04T10:00:00Z",
    )
    protected = DiskInfo(
        disk_id="c0e32s4",
        controller_index=0,
        vendor=Vendor.LSI,
        serial="SYS-SERIAL",
        wwn="5000C500A1B2C3D4",
        size_bytes=100,
        medium=Medium.HDD,
    )
    with pytest.raises(TaskRejected) as exc:
        check_boot_media(snapshot, [protected])
    assert exc.value.error_code == "boot_media_protected"


def test_controller_identity_without_serial_is_not_destructive():
    class C:
        index = 2
        vendor = Vendor.HPE
        serial = ""

    identity = controller_identity(C())
    assert identity["controller_id"] == "hpe:unknown-c2"
    assert identity["destructive_allowed"] is False

