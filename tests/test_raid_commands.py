"""Exact argv for every vendor/level/action (the dry-run contract)."""

from __future__ import annotations

import pytest

from raidforge.core.models import (
    ControllerInfo,
    DiskInfo,
    Medium,
    Protocol,
    Raid0Mode,
    RaidLevel,
    Vendor,
    VolumeInfo,
)
from raidforge.core.raid.commands import clear_plans, create_plans, delete_plans
from raidforge.core.raid.layout import build_layout


def lsi_controller() -> ControllerInfo:
    return ControllerInfo(index=0, vendor=Vendor.LSI, binary="storcli")


def pmc_controller() -> ControllerInfo:
    return ControllerInfo(index=1, vendor=Vendor.PMC, binary="arcconf")


def hpe_controller() -> ControllerInfo:
    return ControllerInfo(index=0, vendor=Vendor.HPE, binary="ssacli")


def lsi_disk(index: int) -> DiskInfo:
    return DiskInfo(
        disk_id=f"c0e32s{index}",
        controller_index=0,
        vendor=Vendor.LSI,
        medium=Medium.HDD,
        protocol=Protocol.SAS,
        enclosure=32,
        slot=index,
        size_bytes=1000,
    )


def pmc_disk(index: int) -> DiskInfo:
    return DiskInfo(
        disk_id=f"c1-ch0-dev{index}",
        controller_index=1,
        vendor=Vendor.PMC,
        medium=Medium.HDD,
        protocol=Protocol.SAS,
        channel=0,
        device_number=index,
        size_bytes=1000,
    )


def hpe_disk(index: int) -> DiskInfo:
    ref = f"1I:1:{index + 1}"
    return DiskInfo(
        disk_id=f"c0-{ref}",
        controller_index=0,
        vendor=Vendor.HPE,
        medium=Medium.HDD,
        protocol=Protocol.SAS,
        controller_ref=ref,
        size_bytes=1000,
    )


def huawei_controller() -> ControllerInfo:
    return ControllerInfo(index=0, vendor=Vendor.HUAWEI, binary="hiraidadm")


def huawei_disk(index: int) -> DiskInfo:
    return DiskInfo(
        disk_id=f"c0e0s{index}",
        controller_index=0,
        vendor=Vendor.HUAWEI,
        medium=Medium.HDD,
        protocol=Protocol.SAS,
        enclosure=0,
        slot=index,
        size_bytes=1000,
    )


# ---------------------------------------------------------------------------
# LSI / Broadcom
# ---------------------------------------------------------------------------
def test_lsi_raid0_single_and_per_disk():
    controller = lsi_controller()
    disks = [lsi_disk(0), lsi_disk(1)]
    single = create_plans(controller, build_layout(RaidLevel.RAID0, disks, name="RG0"))
    assert [plan.argv for plan in single] == [
        ["storcli", "/c0", "add", "vd", "r0", "drives=32:0,32:1", "size=all", "name=RG0"]
    ]

    per_disk = create_plans(
        controller, build_layout(RaidLevel.RAID0, disks, mode=Raid0Mode.PER_DISK, name="RG0")
    )
    assert len(per_disk) == 2
    assert per_disk[0].argv[-1] == "name=RG0-d01"
    assert per_disk[1].argv[-1] == "name=RG0-d02"
    assert all("drives=32:%d" % index in plan.argv for index, plan in enumerate(per_disk))


def test_lsi_raid1_raid5_raid10():
    controller = lsi_controller()
    raid1 = create_plans(controller, build_layout(RaidLevel.RAID1, [lsi_disk(0), lsi_disk(1)]))
    assert raid1[0].argv[4] == "r1"

    raid5 = create_plans(
        controller, build_layout(RaidLevel.RAID5, [lsi_disk(0), lsi_disk(1), lsi_disk(2)])
    )
    assert raid5[0].argv[4] == "r5"

    raid10 = create_plans(
        controller,
        build_layout(RaidLevel.RAID10, [lsi_disk(0), lsi_disk(1), lsi_disk(2), lsi_disk(3)], name="RG10"),
    )
    assert raid10[0].argv == [
        "storcli",
        "/c0",
        "add",
        "vd",
        "r10",
        "drives=32:0,32:1,32:2,32:3",
        "size=all",
        "pdperarray=2",
        "name=RG10",
    ]


def test_lsi_explicit_stripe_is_emitted():
    controller = lsi_controller()
    layout = build_layout(
        RaidLevel.RAID10,
        [lsi_disk(0), lsi_disk(1), lsi_disk(2), lsi_disk(3)],
        stripe_kb=128,
    )
    assert create_plans(controller, layout)[0].argv == [
        "storcli",
        "/c0",
        "add",
        "vd",
        "r10",
        "drives=32:0,32:1,32:2,32:3",
        "size=all",
        "strip=128",
        "pdperarray=2",
    ]


def test_lsi_delete_and_clear():
    controller = lsi_controller()
    volume = VolumeInfo(volume_id="v2", controller_index=0, vendor=Vendor.LSI, level="RAID1")
    assert delete_plans(controller, [volume])[0].argv == [
        "storcli",
        "/c0/v2",
        "delete",
        "force",
    ]
    plans = clear_plans(controller)
    assert [plan.argv for plan in plans] == [
        ["storcli", "/c0/vall", "delete", "force"],
        ["storcli", "/c0/fall", "delete"],
    ]


def test_lsi_missing_address_is_rejected():
    bad = DiskInfo(
        disk_id="c0e32s9",
        controller_index=0,
        vendor=Vendor.LSI,
        medium=Medium.HDD,
        protocol=Protocol.SAS,
        size_bytes=1000,
    )
    with pytest.raises(ValueError):
        create_plans(lsi_controller(), build_layout(RaidLevel.RAID0, [bad]))


# ---------------------------------------------------------------------------
# Adaptec / Microchip
# ---------------------------------------------------------------------------
def test_adaptec_create_levels():
    controller = pmc_controller()
    raid10 = create_plans(
        controller,
        build_layout(
            RaidLevel.RAID10,
            [pmc_disk(0), pmc_disk(1), pmc_disk(2), pmc_disk(3)],
            name="RG10",
        ),
    )
    assert raid10[0].argv == [
        "arcconf",
        "create",
        "1",
        "logicaldrive",
        "name",
        "RG10",
        "method",
        "quick",
        "MAX",
        "10",
        "0",
        "0",
        "0",
        "1",
        "0",
        "2",
        "0",
        "3",
        "noprompt",
    ]


def test_adaptec_explicit_stripe_and_no_init():
    controller = pmc_controller()
    disks = [pmc_disk(0), pmc_disk(1)]
    explicit = create_plans(
        controller,
        build_layout(RaidLevel.RAID1, disks, name="RG1", stripe_kb=512),
    )[0]
    assert explicit.argv == [
        "arcconf",
        "create",
        "1",
        "logicaldrive",
        "stripesize",
        "512",
        "name",
        "RG1",
        "method",
        "quick",
        "MAX",
        "1",
        "0",
        "0",
        "0",
        "1",
        "noprompt",
    ]

    no_init = create_plans(
        controller,
        build_layout(RaidLevel.RAID1, disks, name="RG1", init="none"),
    )[0]
    assert "method" not in no_init.argv
    assert "quick" not in no_init.argv


def test_adaptec_delete_and_clear():
    controller = pmc_controller()
    volume = VolumeInfo(volume_id="2", controller_index=1, vendor=Vendor.PMC, level="RAID5")
    assert delete_plans(controller, [volume])[0].argv == [
        "arcconf",
        "delete",
        "1",
        "logicaldrive",
        "2",
        "noprompt",
    ]
    assert [plan.argv for plan in clear_plans(controller)] == [
        ["arcconf", "delete", "1", "array", "ALL", "noprompt"],
        ["arcconf", "uninit", "1", "all"],
    ]


# ---------------------------------------------------------------------------
# HPE Smart Array
# ---------------------------------------------------------------------------
def test_hpe_create_raid1_and_raid10():
    controller = hpe_controller()
    raid1 = create_plans(
        controller,
        build_layout(RaidLevel.RAID1, [hpe_disk(0), hpe_disk(1)], name="RG1"),
    )
    assert raid1[0].argv == [
        "ssacli",
        "ctrl",
        "slot=0",
        "create",
        "type=ld",
        "drives=1I:1:1,1I:1:2",
        "raid=1",
        "label=RG1",
        "forced",
    ]
    raid10 = create_plans(
        controller,
        build_layout(RaidLevel.RAID10, [hpe_disk(index) for index in range(4)]),
    )
    assert "raid=1+0" in raid10[0].argv
    assert "drives=1I:1:1,1I:1:2,1I:1:3,1I:1:4" in raid10[0].argv
    assert "parityinitializationmethod=rapid" not in raid10[0].argv


def test_hpe_raid5_uses_rapid_parity_init_and_explicit_stripe():
    controller = hpe_controller()
    disks = [hpe_disk(index) for index in range(3)]
    default = create_plans(controller, build_layout(RaidLevel.RAID5, disks))[0]
    assert default.argv == [
        "ssacli",
        "ctrl",
        "slot=0",
        "create",
        "type=ld",
        "drives=1I:1:1,1I:1:2,1I:1:3",
        "raid=5",
        "parityinitializationmethod=rapid",
        "forced",
    ]

    explicit = create_plans(
        controller,
        build_layout(RaidLevel.RAID5, disks, stripe_kb=512),
    )[0]
    assert "ss=512" in explicit.argv

    no_init = create_plans(
        controller,
        build_layout(RaidLevel.RAID5, disks, init="none"),
    )[0]
    assert "parityinitializationmethod=rapid" not in no_init.argv


def test_hpe_per_disk_raid0():
    controller = hpe_controller()
    plans = create_plans(
        controller,
        build_layout(RaidLevel.RAID0, [hpe_disk(0), hpe_disk(1)], mode=Raid0Mode.PER_DISK),
    )
    assert len(plans) == 2
    assert all("raid=0" in plan.argv for plan in plans)
    assert all("parityinitializationmethod=rapid" not in plan.argv for plan in plans)
    assert plans[0].argv[5] == "drives=1I:1:1"
    assert plans[1].argv[5] == "drives=1I:1:2"


def test_hpe_delete_and_clear():
    controller = hpe_controller()
    volume = VolumeInfo(volume_id="1", controller_index=0, vendor=Vendor.HPE, level="RAID1")
    assert delete_plans(controller, [volume])[0].argv == [
        "ssacli",
        "ctrl",
        "slot=0",
        "ld",
        "1",
        "delete",
        "forced",
    ]
    assert [plan.argv for plan in clear_plans(controller)] == [
        ["ssacli", "ctrl", "slot=0", "ld", "all", "delete", "forced"],
        ["ssacli", "ctrl", "slot=0", "modify", "clearconfig", "forced"],
    ]


# ---------------------------------------------------------------------------
# Huawei HiRAID
# ---------------------------------------------------------------------------
def test_huawei_create_levels_and_per_disk():
    controller = huawei_controller()
    disks = [huawei_disk(0), huawei_disk(1)]
    raid1 = create_plans(controller, build_layout(RaidLevel.RAID1, disks, name="vd0"))
    assert [plan.argv for plan in raid1] == [
        [
            "hiraidadm",
            "c0",
            "create",
            "vd",
            "raid1",
            "name=vd0",
            "size=max_left",
            "drives=0:0,0:1",
        ]
    ]

    raid10 = create_plans(
        controller, build_layout(RaidLevel.RAID10, disks + [huawei_disk(2), huawei_disk(3)])
    )[0]
    assert "raid10" in raid10.argv
    assert raid10.argv[-1] == "drives=0:0,0:1,0:2,0:3"

    per_disk = create_plans(
        controller,
        build_layout(RaidLevel.RAID0, disks, mode=Raid0Mode.PER_DISK, name="RG0"),
    )
    assert len(per_disk) == 2
    assert per_disk[0].argv[-1] == "drives=0:0"
    assert per_disk[1].argv[-1] == "drives=0:1"


def test_huawei_delete_and_clear():
    controller = huawei_controller()
    volume = VolumeInfo(volume_id="vd1", controller_index=0, vendor=Vendor.HUAWEI, level="RAID1")
    assert delete_plans(controller, [volume])[0].argv == [
        "hiraidadm",
        "c0:vd1",
        "delete",
        "noprompt",
    ]
    assert [plan.argv for plan in clear_plans(controller)] == [
        ["hiraidadm", "c0", "reset", "defaultconfig", "noprompt"],
        ["hiraidadm", "c0", "clear", "fncfg"],
    ]
