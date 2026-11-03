"""Unit tests for the pure layers: parsing, command construction, evidence.

These tests never execute a vendor tool and never touch a disk - every input is
read from ``tests/fixtures``, which makes them safe to run on any machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raidforge.config import default_config
from raidforge.core import commands as cmd_mod
from raidforge.core import enum_disks
from raidforge.core.evidence import EvidenceSession
from raidforge.core.models import (
    ControllerInfo,
    DiskInfo,
    Medium,
    Protocol,
    SubmitStatus,
    Vendor,
)
from raidforge.core.serverid import resolve_server_id
from raidforge.core.utils import parse_decimal_size

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_text(name: str) -> str:
    """Read one fixture file as text."""

    return (FIXTURES / name).read_text(encoding="utf-8")


def controller(vendor: Vendor, index: int = 0) -> ControllerInfo:
    """Build a controller id expected by the parsers."""

    return ControllerInfo(index=index, vendor=vendor, model="test controller")


# ---------------------------------------------------------------------------
# utils
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.818 TB", 1818000000000),
        ("558.406 GB", 558406000000),
        ("480 GB", 480000000000),
        ("960197124096 bytes [960 GB]", 0),
        ("", 0),
        (None, 0),
        ("weird", 0),
    ],
)
def test_parse_decimal_size(raw, expected):
    """Vendor size strings must reduce to a byte count."""

    assert parse_decimal_size(raw) == expected


# ---------------------------------------------------------------------------
# LSI / storcli
# ---------------------------------------------------------------------------
def test_parse_storcli_json():
    """Three disks: HDD, SED-capable SSD and one with an unknown medium."""

    disks = enum_disks.parse_storcli_json(
        fixture_text("storcli_sall_show_J.json"), controller(Vendor.LSI)
    )
    assert [d.disk_id for d in disks] == ["c0e32s0", "c0e32s1", "c0e32s2"]

    hdd, ssd, mystery = disks
    assert hdd.medium is Medium.HDD
    assert hdd.protocol is Protocol.SAS
    assert hdd.serial == "ZTN1ABCDEFG"
    assert hdd.firmware == "SN02"
    assert hdd.firmware_or_raid == "RAID Onln / FW SN02"
    assert hdd.size_bytes == 1818000000000
    assert hdd.selectable is True

    assert ssd.medium is Medium.SSD
    assert ssd.protocol is Protocol.SATA
    assert ssd.firmware == "XCV10120"
    assert ssd.raw.get("SED") == "Y"

    # Constraint 8: no medium -> no erase, ever.
    assert mystery.medium is Medium.UNKNOWN
    assert mystery.selectable is False


def test_storcli_commands():
    """Dry-run text is what ConfirmModal shows and execute() submits."""

    cfg = default_config()
    disks = enum_disks.parse_storcli_json(
        fixture_text("storcli_sall_show_J.json"), controller(Vendor.LSI)
    )
    plans = [
        cmd_mod.build_command(disk, cmd_mod.choose_default_method(disk, cfg), cfg)
        for disk in disks
        if disk.selectable
    ]
    assert plans[0].dry_run_text == "storcli /c0/e32/s0 start erase thorough"
    # SED capable -> sanitize; otherwise SSDs fall back to block erase.
    assert plans[1].dry_run_text == "storcli /c0/e32/s1 start erase secure"
    assert all(plan.argv[0] == "storcli" for plan in plans)


def test_unknown_media_refuses_every_method():
    """No method may ever be produced for an UNKNOWN disk."""

    cfg = default_config()
    mystery = DiskInfo(
        disk_id="c0e32s2", controller_index=0, vendor=Vendor.LSI, medium=Medium.UNKNOWN
    )
    with pytest.raises(ValueError):
        cmd_mod.choose_default_method(mystery, cfg)
    with pytest.raises(ValueError):
        cmd_mod.build_command(mystery, "thorough", cfg)
    assert cmd_mod.plan_for_selection([mystery], cfg) == []


# ---------------------------------------------------------------------------
# PMC / arcconf
# ---------------------------------------------------------------------------
def test_parse_arcconf_json():
    """The mock arcconf config carries one HDD and one SSD."""

    disks = enum_disks.parse_arcconf_json(
        fixture_text("arcconf_getconfigjson.json"), ControllerInfo(index=1, vendor=Vendor.PMC)
    )
    assert len(disks) == 2
    assert disks[0].disk_id == "c1-ch0-dev0"
    assert disks[0].medium is Medium.HDD
    assert disks[0].protocol is Protocol.SAS
    assert disks[0].size_bytes == 1818000000000

    assert disks[1].medium is Medium.SSD
    assert disks[1].protocol is Protocol.SATA


def test_arcconf_commands():
    """Non-SED SSDs use the block erase path - we never guess at crypto erase."""

    cfg = default_config()
    disks = enum_disks.parse_arcconf_json(
        fixture_text("arcconf_getconfigjson.json"), ControllerInfo(index=1, vendor=Vendor.PMC)
    )
    plans = [cmd_mod.build_command(d, cmd_mod.choose_default_method(d, cfg), cfg) for d in disks]
    assert plans[0].argv == ["arcconf", "TASK", "START", "1", "DEVICE", "0", "0", "erase", "NOPROMPT"]
    assert plans[0].kind == "hdd_thorough"
    assert plans[1].kind == "ssd_blockerase"
    assert plans[1].argv[-1] == "NOPROMPT"


# ---------------------------------------------------------------------------
# HPE / ssacli
# ---------------------------------------------------------------------------
def test_parse_ssacli_detail():
    """The third fixture disk has no rotational speed -> UNKNOWN."""

    disks = enum_disks.parse_ssacli_detail(
        fixture_text("ssacli_pd_all_show_detail.txt"), controller(Vendor.HPE)
    )
    assert [d.disk_id for d in disks] == ["c0-1I:1:1", "c0-1I:1:2", "c0-2I:1:5"]
    assert disks[0].medium is Medium.HDD
    assert disks[0].size_bytes == 2000000000000
    assert disks[0].firmware == "HPD5"
    assert disks[0].firmware_or_raid == "RAID OK / FW HPD5"
    assert disks[1].medium is Medium.SSD
    assert disks[1].serial == "PHLJ9123456789"
    assert disks[2].medium is Medium.UNKNOWN
    assert disks[2].selectable is False


def test_ssacli_commands_use_the_pd_reference():
    cfg = default_config()
    disks = enum_disks.parse_ssacli_detail(
        fixture_text("ssacli_pd_all_show_detail.txt"), controller(Vendor.HPE)
    )
    plan = cmd_mod.build_command(disks[0], "thorough", cfg)
    # HPE CLI reference: <target> modify erase [erasepattern=...] - the `erase`
    # verb is mandatory, a bare "modify erasepattern=..." is rejected.
    assert plan.argv == [
        "ssacli", "ctrl", "slot=0", "pd", "1I:1:1", "modify", "erase",
        "erasepattern=overwrite", "forced",
    ]


# ---------------------------------------------------------------------------
# generic
# ---------------------------------------------------------------------------
def test_lsscsi_and_smartctl_medium_detection():
    # The NVMe line uses a non-numeric host adapter label and must be ignored by
    # the SCSI parser - NVMe devices arrive through `nvme list -o json` instead.
    rows = enum_disks.parse_lsscsi(fixture_text("lsscsi.txt"))
    assert [row["node"] for row in rows] == ["/dev/sda", "/dev/sdb", "/dev/sg12"]
    assert rows[-1]["type"] == "enclosu"

    ssd_fields = enum_disks.parse_smartctl_identity(fixture_text("smartctl_ssd.txt"))
    assert enum_disks.medium_from_smartctl(ssd_fields) == (Medium.SSD, Protocol.SATA)

    hdd_fields = enum_disks.parse_smartctl_identity(fixture_text("smartctl_hdd.txt"))
    assert enum_disks.medium_from_smartctl(hdd_fields) == (Medium.HDD, Protocol.SAS)


def test_generic_command_per_protocol():
    cfg = default_config()
    cases = [
        (Protocol.SATA, Medium.SSD, "/dev/sda", "blkdiscard --force /dev/sda"),
        (Protocol.SAS, Medium.HDD, "/dev/sdb", "sg_format --format --size=512 --verify /dev/sdb"),
        (Protocol.NVME, Medium.SSD, "/dev/nvme0n1", "nvme format /dev/nvme0n1 --force --ses=2"),
    ]
    for protocol, medium, node, expected in cases:
        disk = DiskInfo(
            disk_id=node.rsplit("/", 1)[-1],
            controller_index=0,
            vendor=Vendor.GENERIC,
            medium=medium,
            protocol=protocol,
            device_node=node,
        )
        plan = cmd_mod.build_command(disk, cmd_mod.choose_default_method(disk, cfg), cfg)
        assert plan.dry_run_text == expected


def test_parse_nvme_list_json():
    payload = json.dumps(
        {
            "Devices": [
                {
                    "DevicePath": "/dev/nvme0n1",
                    "ModelNumber": "Micron",
                    "SerialNumber": "ERASE01",
                    "Firmware": "7.2.22",
                    "PhysicalSize": 960197124096,
                }
            ]
        }
    )
    disks = enum_disks.parse_nvme_list(payload)
    assert len(disks) == 1
    assert disks[0].medium is Medium.SSD
    assert disks[0].protocol is Protocol.NVME
    assert disks[0].size_bytes == 960197124096
    assert disks[0].firmware == "7.2.22"
    assert disks[0].raid_state == ""
    assert disks[0].firmware_or_raid == "FW 7.2.22"


def test_firmware_or_raid_labels():
    disk = DiskInfo(disk_id="x", controller_index=0, vendor=Vendor.LSI)
    assert disk.firmware_or_raid == "-"
    disk.firmware = "SN02"
    assert disk.firmware_or_raid == "FW SN02"
    disk.raid_state = "Onln"
    assert disk.firmware_or_raid == "RAID Onln / FW SN02"
    disk.firmware = ""
    assert disk.firmware_or_raid == "RAID Onln"


@pytest.mark.parametrize("payload", ["[]", '{"Devices": []}', "{}", "null", '"text"', "not json"])
def test_parse_nvme_list_empty_shapes(payload):
    """``nvme list -o json`` prints a bare ``[]`` when no device exists.

    Assuming a mapping here crashed the whole enumeration on machines without
    NVMe drives ('list' object has no attribute 'get').
    """

    assert enum_disks.parse_nvme_list(payload) == []


def test_parse_nvme_list_bare_array():
    """Some builds emit a top-level array; it must still yield usable disks."""

    payload = json.dumps(
        [{"DevicePath": "/dev/nvme1n1", "ModelNumber": "Samsung", "PhysicalSize": 512110190592}]
    )
    disks = enum_disks.parse_nvme_list(payload)
    assert len(disks) == 1
    assert disks[0].disk_id == "nvme1n1"
    assert disks[0].size_bytes == 512110190592


# ---------------------------------------------------------------------------
# server identity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "forced,expected_source",
    [
        ({"system_serial": "CZJ12345"}, "dmi.system-serial-number"),
        ({"system_serial": "To be filled by O.E.M.", "baseboard_serial": "BB-8899"}, "dmi.baseboard-serial-number"),
    ],
)
def test_server_id_ladder(forced, expected_source):
    """Placeholder DMI values must fall through to the next rung."""

    sid = resolve_server_id(force_values={"macs": ["aa:bb:cc:dd:ee:ff"], **forced})
    assert sid.source == expected_source
    assert sid.confirm_token == f"CONFIRM-{sid.serial}"


def test_server_id_derived_when_everything_is_broken():
    sid = resolve_server_id(
        force_values={
            "system_serial": "Not Specified",
            "baseboard_serial": "0",
            "system_uuid": "Not Settable",
            "macs": ["11:22:33:44:55:66"],
        }
    )
    assert sid.source.startswith("derived")
    assert sid.is_fallback is True
    assert sid.serial.startswith("UNK")


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------
def test_evidence_session_writes_manifest(tmp_path):
    cfg = default_config()
    cfg.evidence.root = str(tmp_path)
    session = EvidenceSession(cfg, "SN-TEST-1", session_id="SN-TEST-1_20260101T000000Z")

    session.emit("hello", value=1)
    session.emit("world", value=2)
    session.write_controller_raw([("storcli.txt", "raw output")])
    session.save_svg("screen", "<svg/>")

    lines = session.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "hello"
    assert json.loads(lines[1])["seq"] == 2

    manifest = session.finalize({"tool": "storcli"})
    names = {item["path"] for item in manifest["files"]}
    assert names == {"events.jsonl", "controller_raw/storcli.txt", "snapshots/screen.svg"}
    assert manifest["tool"] == "storcli"
    # The manifest itself is never hashed into itself.
    assert all(item["path"] != "manifest.json" for item in manifest["files"])
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_submit_result_semantics():
    """A zero return code only means 'accepted', never 'completed'."""

    from raidforge.core.models import SubmitResult

    result = SubmitResult(
        disk_id="c0e0s0", kind="hdd_thorough", status=SubmitStatus.ASYNC_SUBMITTED,
        return_code=0, duration_sec=0.4,
    )
    assert result.ok is True
    failed = SubmitResult(
        disk_id="c0e0s0", kind="hdd_thorough", status=SubmitStatus.FAILED,
        return_code=1, duration_sec=0.1,
    )
    assert failed.ok is False
