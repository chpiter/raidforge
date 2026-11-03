"""Pure RAID layout rules: levels, RAID0 grouping, spans, capacity, names."""

from __future__ import annotations

import pytest

from raidforge.core.models import DiskInfo, Medium, Protocol, Raid0Mode, RaidLevel, Vendor
from raidforge.core.raid.layout import build_layout, normalize_name, validate_layout


def disk(index: int, size: int = 1000, vendor: Vendor = Vendor.LSI, medium: Medium = Medium.HDD) -> DiskInfo:
    if vendor is Vendor.LSI:
        return DiskInfo(
            disk_id=f"c0e32s{index}",
            controller_index=0,
            vendor=vendor,
            medium=medium,
            protocol=Protocol.SAS,
            enclosure=32,
            slot=index,
            size_bytes=size,
        )
    if vendor is Vendor.PMC:
        return DiskInfo(
            disk_id=f"c1-ch0-dev{index}",
            controller_index=1,
            vendor=vendor,
            medium=medium,
            protocol=Protocol.SAS,
            channel=0,
            device_number=index,
            size_bytes=size,
        )
    return DiskInfo(
        disk_id=f"c0-1I:1:{index}",
        controller_index=0,
        vendor=vendor,
        medium=medium,
        protocol=Protocol.SAS,
        controller_ref=f"1I:1:{index}",
        size_bytes=size,
    )


def test_raid0_single_and_per_disk():
    disks = [disk(index) for index in range(4)]
    single = build_layout(RaidLevel.RAID0, disks, name="R0")
    assert validate_layout(single) == []
    assert single.spans == [["c0e32s0", "c0e32s1", "c0e32s2", "c0e32s3"]]
    assert single.volume_names == ["R0"]
    assert single.capacity_bytes == 4000

    per_disk = build_layout(RaidLevel.RAID0, disks, mode=Raid0Mode.PER_DISK, name="R0")
    assert validate_layout(per_disk) == []
    assert len(per_disk.spans) == 4
    assert per_disk.volume_names == ["R0-d01", "R0-d02", "R0-d03", "R0-d04"]
    assert per_disk.capacity_bytes == 4000


def test_raid1_requires_exactly_two():
    two = build_layout(RaidLevel.RAID1, [disk(0), disk(1)])
    assert validate_layout(two) == []
    assert two.capacity_bytes == 1000

    for count in (1, 3):
        layout = build_layout(RaidLevel.RAID1, [disk(index) for index in range(count)])
        assert validate_layout(layout)


def test_raid5_requires_three():
    ok = build_layout(RaidLevel.RAID5, [disk(index) for index in range(3)])
    assert validate_layout(ok) == []
    assert ok.capacity_bytes == 2000
    assert validate_layout(build_layout(RaidLevel.RAID5, [disk(0), disk(1)]))


def test_raid10_requires_even_and_four():
    four = build_layout(RaidLevel.RAID10, [disk(index) for index in range(4)])
    assert validate_layout(four) == []
    assert four.spans == [["c0e32s0", "c0e32s1"], ["c0e32s2", "c0e32s3"]]
    assert four.capacity_bytes == 2000

    six = build_layout(RaidLevel.RAID10, [disk(index) for index in range(6)])
    assert validate_layout(six) == []
    assert len(six.spans) == 3

    for count in (3, 5):
        assert validate_layout(build_layout(RaidLevel.RAID10, [disk(index) for index in range(count)]))


def test_raid10_pairing_is_deterministic():
    disks = [disk(index) for index in range(6)]
    first = build_layout(RaidLevel.RAID10, disks)
    second = build_layout(RaidLevel.RAID10, list(reversed(disks)))
    assert first.spans == second.spans
    assert first.spans == [
        ["c0e32s0", "c0e32s1"],
        ["c0e32s2", "c0e32s3"],
        ["c0e32s4", "c0e32s5"],
    ]


def test_cross_controller_and_duplicates_rejected():
    lsi = disk(0)
    pmc = disk(0, vendor=Vendor.PMC)
    assert validate_layout(build_layout(RaidLevel.RAID0, [lsi, pmc]))
    assert validate_layout(build_layout(RaidLevel.RAID0, [lsi, lsi]))


def test_unknown_media_and_unaddressable_rejected():
    unknown = disk(0, medium=Medium.UNKNOWN)
    assert validate_layout(build_layout(RaidLevel.RAID0, [unknown]))
    no_address = DiskInfo(
        disk_id="c0e32s9",
        controller_index=0,
        vendor=Vendor.LSI,
        medium=Medium.HDD,
        protocol=Protocol.SAS,
        size_bytes=1000,
    )
    assert validate_layout(build_layout(RaidLevel.RAID0, [no_address]))


def test_name_sanitising_and_warning():
    assert normalize_name("  OS/RAID 1  ") == "OS-RAID-1"
    assert normalize_name("x" * 40) == "x" * 15
    layout = build_layout(RaidLevel.RAID0, [disk(0)], name="OS / RAID")
    assert layout.name == "OS-RAID"
    assert any("normalised" in warning for warning in layout.warnings)


def test_mixed_media_warns_but_does_not_block():
    layout = build_layout(
        RaidLevel.RAID1,
        [disk(0, medium=Medium.HDD), disk(1, medium=Medium.SSD)],
    )
    assert validate_layout(layout) == []
    assert any("mixes HDD and SSD" in warning for warning in layout.warnings)


def test_empty_selection_is_invalid():
    layout = build_layout(RaidLevel.RAID0, [])
    assert validate_layout(layout)


def test_per_disk_only_for_raid0():
    layout = build_layout(RaidLevel.RAID1, [disk(0), disk(1)], mode=Raid0Mode.PER_DISK)
    assert layout.mode is Raid0Mode.SINGLE
    assert validate_layout(layout) == []


def test_pmc_and_hpe_addressability():
    pmc = build_layout(RaidLevel.RAID1, [disk(0, vendor=Vendor.PMC), disk(1, vendor=Vendor.PMC)])
    assert validate_layout(pmc) == []
    hpe = build_layout(RaidLevel.RAID1, [disk(0, vendor=Vendor.HPE), disk(1, vendor=Vendor.HPE)])
    assert validate_layout(hpe) == []
