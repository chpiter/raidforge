"""End-to-end tests for the detection -> enumeration pipeline.

``core.utils.run_cmd`` and ``core.detect.which`` are replaced by fixtures, so
the whole ladder and every branch of :func:`raidforge.core.enum_disks.enumerate_disks`
can be exercised on any platform (including CI servers with no RAID card).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raidforge.config import default_config
from raidforge.core import detect as detect_mod
from raidforge.core import enum_disks as enum_mod
from raidforge.core.models import Medium, Protocol, Vendor
from raidforge.core.utils import CmdResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeShell:
    """Replaces ``run_cmd``: answer from a ``argv-string -> stdout`` table."""

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


def install(monkeypatch, table, available):
    """Patch the process layer: only ``available`` binaries exist."""

    shell = FakeShell(table)
    monkeypatch.setattr(detect_mod, "run_cmd", shell)
    monkeypatch.setattr(detect_mod, "which", lambda name: f"/usr/local/bin/{name}" if name in available else None)
    monkeypatch.setattr(enum_mod, "run_cmd", shell)
    return shell


STORCLI_TABLE = {
    "storcli show J": text("storcli_show_J.json"),
    "storcli /c0 show J": text("storcli_c0_show_J.json"),
    "storcli /c0/eall/sall show J": text("storcli_sall_show_J.json"),
}

ARCCONF_TABLE = {
    "arcconf list": text("arcconf_list.txt"),
    "arcconf getconfigjson 1": text("arcconf_getconfigjson.json"),
}

SSACLI_TABLE = {
    "ssacli ctrl all show": text("ssacli_ctrl_all_show.txt"),
    "ssacli ctrl slot=0 pd all show detail": text("ssacli_pd_all_show_detail.txt"),
}

GENERIC_TABLE = {
    "lsscsi -g": text("lsscsi.txt"),
    "smartctl -i /dev/sda": text("smartctl_ssd.txt"),
    "smartctl -i /dev/sdb": text("smartctl_hdd.txt"),
    "nvme list -o json": '{"Devices": []}',
}


def test_storcli_ladder_and_inventory(monkeypatch):
    """storcli wins, three disks are discovered, one is refused."""

    cfg = default_config()
    table = {**STORCLI_TABLE, **ARCCONF_TABLE, **SSACLI_TABLE}
    install(monkeypatch, table, {"storcli", "arcconf", "ssacli"})

    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "storcli"
    assert detection.ok is True
    assert [c.index for c in detection.controllers] == [0]
    assert detection.controllers[0].vendor is Vendor.LSI
    assert detection.controllers[0].model == "MegaRAID Tri-Mode SAS3108"
    # Every rung is recorded for the evidence log.
    assert [a.tool for a in detection.attempts][0] == "storcli"

    disks, raw = enum_mod.enumerate_disks(cfg, detection)
    assert len(disks) == 3
    assert [name for name, _ in raw] == ["lsi_c0_sall_show_J.json"]
    assert sum(1 for d in disks if d.selectable) == 2
    assert disks[0].medium is Medium.HDD and disks[1].medium is Medium.SSD
    assert disks[0].firmware == "SN02"
    assert disks[0].firmware_or_raid == "RAID Onln / FW SN02"


def test_ladder_falls_back_to_arcconf(monkeypatch):
    cfg = default_config()
    install(monkeypatch, ARCCONF_TABLE, {"arcconf"})

    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "arcconf"

    disks, _ = enum_mod.enumerate_disks(cfg, detection)
    assert [d.disk_id for d in disks] == ["c1-ch0-dev0", "c1-ch0-dev1"]
    assert disks[0].protocol is Protocol.SAS
    assert disks[1].medium is Medium.SSD


def test_ladder_falls_back_to_ssacli(monkeypatch):
    cfg = default_config()
    install(monkeypatch, SSACLI_TABLE, {"ssacli"})

    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "ssacli"
    assert detection.controllers[0].index == 0

    disks, raw = enum_mod.enumerate_disks(cfg, detection)
    assert [d.controller_ref for d in disks] == ["1I:1:1", "1I:1:2", "2I:1:5"]
    assert disks[2].medium is Medium.UNKNOWN


def test_generic_path_uses_sg3_utils(monkeypatch):
    cfg = default_config()
    install(monkeypatch, GENERIC_TABLE, {"sg_scan"})

    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "sg3_utils"
    assert detection.controllers[0].vendor is Vendor.GENERIC

    disks, raw = enum_mod.enumerate_disks(cfg, detection)
    assert [d.device_node for d in disks] == ["/dev/sda", "/dev/sdb"]
    assert disks[0].medium is Medium.SSD and disks[0].protocol is Protocol.SATA
    assert disks[1].medium is Medium.HDD and disks[1].protocol is Protocol.SAS
    assert disks[0].firmware == "D0MU0205"
    assert disks[1].firmware == "SN02"
    assert disks[0].raid_state == "" and disks[1].raid_state == ""
    # Two smartctl dumps plus the lsscsi listing plus the nvme listing.
    assert len(raw) == 4


def test_forced_tool_skips_the_ladder(monkeypatch):
    cfg = default_config()
    cfg.tools.forced_tool = "ssacli"
    shell = install(monkeypatch, {**SSACLI_TABLE, **STORCLI_TABLE}, {"storcli", "ssacli"})

    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "ssacli"
    # storcli exists but was never invoked because it is not the forced tool.
    assert not [call for call in shell.calls if call.startswith("storcli")]


def test_no_controller_graceful_degradation(monkeypatch):
    cfg = default_config()
    install(monkeypatch, {}, {})

    detection = detect_mod.detect_controllers(cfg)
    assert detection.tool == "none"
    assert detection.ok is False
    assert detection.attempts and all(not a.ok or "mismatch" in (a.detail or "") for a in detection.attempts)

    disks, raw = enum_mod.enumerate_disks(cfg, detection)
    assert disks == []
    assert raw == []
