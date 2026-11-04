"""Erase eligibility must be separate from RAID-creation eligibility."""

from __future__ import annotations

from raidforge.config import load_config
from raidforge.core.evidence import EvidenceSession
from raidforge.core.models import (
    DiskInfo,
    Medium,
    Protocol,
    RaidBundle,
    RaidCapabilities,
    RaidTopology,
    Vendor,
    VolumeInfo,
)
from raidforge.core.serverid import resolve_server_id
from raidforge.ui.state import AppState


def _state(tmp_path) -> AppState:
    cfg = load_config(
        overrides={
            "evidence": {"root": str(tmp_path / "evidence")},
            "report": {"enabled": False},
        }
    )
    sid = resolve_server_id(force_values={"system_serial": "SN-ERASE-POLICY"})
    return AppState(cfg=cfg, evidence=EvidenceSession(cfg, sid.serial), server_id=sid)


def _generic_disk(disk_id: str, medium: Medium = Medium.SSD) -> DiskInfo:
    return DiskInfo(
        disk_id=disk_id,
        controller_index=0,
        vendor=Vendor.GENERIC,
        medium=medium,
        protocol=Protocol.UNKNOWN,
        device_node=f"/dev/{disk_id}",
        size_bytes=20_000_000_000,
    )


def _generic_state(tmp_path) -> AppState:
    state = _state(tmp_path)
    disks = [_generic_disk("sda"), _generic_disk("sdb", Medium.HDD), _generic_disk("sdc")]
    state.physical_disks = list(disks)
    topology = RaidTopology(
        controller_index=0,
        vendor=Vendor.GENERIC,
        tool="sg3_utils",
        binary="sg_scan",
        volumes=[],
        membership={},
        free_disk_ids=[],
        blocked={
            disk.disk_id: "RAID management not supported on the generic (sg3_utils) path"
            for disk in disks
        },
        disk_states={disk.disk_id: "" for disk in disks},
    )
    state.raid = RaidBundle(
        tool="sg3_utils",
        binary="sg_scan",
        topologies=[topology],
        capabilities={0: RaidCapabilities(create=False, delete_vd=False, clear_config=False)},
    )
    state.refresh_virtual_views()
    return state


def _lsi_state(tmp_path) -> AppState:
    state = _state(tmp_path)
    disks = [
        DiskInfo(
            disk_id="c0e32s0",
            controller_index=0,
            vendor=Vendor.LSI,
            medium=Medium.HDD,
            protocol=Protocol.SAS,
            enclosure=32,
            slot=0,
            size_bytes=2_000_000_000_000,
            raid_state="UGood",
        ),
        DiskInfo(
            disk_id="c0e32s1",
            controller_index=0,
            vendor=Vendor.LSI,
            medium=Medium.HDD,
            protocol=Protocol.SAS,
            enclosure=32,
            slot=1,
            size_bytes=2_000_000_000_000,
            raid_state="Onln",
        ),
        DiskInfo(
            disk_id="c0e32s2",
            controller_index=0,
            vendor=Vendor.LSI,
            medium=Medium.HDD,
            protocol=Protocol.SAS,
            enclosure=32,
            slot=2,
            size_bytes=2_000_000_000_000,
            raid_state="Onln",
        ),
    ]
    volume = VolumeInfo(
        volume_id="v0",
        controller_index=0,
        vendor=Vendor.LSI,
        level="RAID1",
        raw_level="1",
        name="data",
        state="Optl",
        size_bytes=2_000_000_000_000,
        disk_ids=["c0e32s1"],
        spans=[["c0e32s1"]],
    )
    state.physical_disks = list(disks)
    topology = RaidTopology(
        controller_index=0,
        vendor=Vendor.LSI,
        tool="storcli",
        binary="storcli",
        volumes=[volume],
        membership={"c0e32s1": "v0"},
        free_disk_ids=["c0e32s0"],
        blocked={"c0e32s2": "state 'Onln' is not free (need UGood)"},
        disk_states={disk.disk_id: disk.raid_state for disk in disks},
    )
    state.raid = RaidBundle(
        tool="storcli",
        binary="storcli",
        topologies=[topology],
        capabilities={0: RaidCapabilities(create=True, delete_vd=True, clear_config=True)},
    )
    state.refresh_virtual_views()
    return state


def test_generic_topology_blocks_raid_creation_but_not_erase(tmp_path):
    state = _generic_state(tmp_path)
    disk = state.disk_by_id("sda")

    assert state.blocked_reason(disk)
    assert state.erase_blocked_reason(disk) == ""
    assert state.raid_capabilities(0).create is False


def test_erase_refuses_unknown_medium(tmp_path):
    state = _generic_state(tmp_path)
    disk = DiskInfo(
        disk_id="sdd",
        controller_index=0,
        vendor=Vendor.GENERIC,
        medium=Medium.UNKNOWN,
        protocol=Protocol.UNKNOWN,
        device_node="/dev/sdd",
    )
    reason = state.erase_blocked_reason(disk)
    assert "media type UNKNOWN" in reason


def test_erase_refuses_vd_member(tmp_path):
    state = _lsi_state(tmp_path)
    reason = state.erase_blocked_reason(state.disk_by_id("c0e32s1"))
    assert "member of v0" in reason


def test_erase_refuses_real_controller_blocked_disk(tmp_path):
    state = _lsi_state(tmp_path)
    reason = state.erase_blocked_reason(state.disk_by_id("c0e32s2"))
    assert "not free" in reason


def test_erase_allows_real_controller_free_disk(tmp_path):
    state = _lsi_state(tmp_path)
    assert state.erase_blocked_reason(state.disk_by_id("c0e32s0")) == ""


def test_prune_selection_keeps_generic_and_drops_raid_blocked(tmp_path):
    generic = _generic_state(tmp_path)
    generic.physical_selection = {"sda", "sdc"}
    generic.prune_selection()
    assert generic.physical_selection == {"sda", "sdc"}

    lsi = _lsi_state(tmp_path)
    lsi.physical_selection = {"c0e32s0", "c0e32s1", "c0e32s2"}
    lsi.prune_selection()
    assert lsi.physical_selection == {"c0e32s0"}


def test_raid_wizard_still_uses_blocked_reason(tmp_path):
    state = _generic_state(tmp_path)
    assert state.blocked_reason(state.disk_by_id("sda"))
