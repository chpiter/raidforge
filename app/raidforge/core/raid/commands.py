"""Pure RAID command construction for every supported vendor grammar.

Every function returns :class:`RaidCommandPlan` objects and never runs anything.
The dry-run text shown to the operator is therefore byte-identical to what the
executor later submits.

Rows marked ``VERIFY`` are the ones whose option order/availability varies
between firmware generations; validate them on the target controller before
relying on them and then fix the one template here.
"""

from __future__ import annotations

from typing import Dict, List

from ..models import (
    ControllerInfo,
    DiskInfo,
    RaidAction,
    Raid0Mode,
    RaidCommandPlan,
    RaidLayout,
    RaidLevel,
    Vendor,
    VolumeInfo,
)
from .layout import ensure_valid, level_number

_DEFAULT_BINARY = {
    Vendor.LSI: "storcli",
    Vendor.PMC: "arcconf",
    Vendor.HPE: "ssacli",
}


def _binary(controller: ControllerInfo) -> str:
    return controller.binary or _DEFAULT_BINARY.get(controller.vendor, "")


def _ordered_disks(layout: RaidLayout) -> List[DiskInfo]:
    by_id: Dict[str, DiskInfo] = {disk.disk_id: disk for disk in layout.disks}
    ordered: List[DiskInfo] = []
    for span in layout.spans:
        for disk_id in span:
            disk = by_id.get(disk_id)
            if disk is not None:
                ordered.append(disk)
    return ordered or list(layout.disks)


def _lsi_ref(disk: DiskInfo) -> str:
    if disk.enclosure is None or disk.slot is None:
        raise ValueError(f"{disk.disk_id}: missing enclosure/slot address")
    return f"{disk.enclosure}:{disk.slot}"


def _pmc_coords(disk: DiskInfo) -> List[str]:
    if disk.channel is None or disk.device_number is None:
        raise ValueError(f"{disk.disk_id}: missing channel/device address")
    return [str(disk.channel), str(disk.device_number)]


def _hpe_ref(disk: DiskInfo) -> str:
    if not disk.controller_ref:
        raise ValueError(f"{disk.disk_id}: missing HPE controller reference")
    return disk.controller_ref


def _level_token(level: RaidLevel, vendor: Vendor) -> str:
    if vendor is Vendor.LSI:
        return f"r{level_number(level)}"
    if vendor is Vendor.PMC:
        return str(level_number(level))
    if vendor is Vendor.HPE:
        return "1+0" if level is RaidLevel.RAID10 else str(level_number(level))
    raise ValueError(f"unsupported vendor for RAID commands: {vendor.value}")


def _lsi_volume_token(volume_id: str) -> str:
    text = str(volume_id or "").strip()
    if not text:
        raise ValueError("empty volume id")
    return text if text.startswith("v") else f"v{text}"


def _pmc_volume_token(volume_id: str) -> str:
    text = str(volume_id or "").strip()
    return text[2:] if text.lower().startswith("ld") else text


def _hpe_volume_token(volume_id: str) -> str:
    text = str(volume_id or "").strip()
    return text[2:] if text.lower().startswith("ld") else text


def _create_plan(
    controller: ControllerInfo,
    argv: List[str],
    label: str,
    disks: List[DiskInfo],
    level: RaidLevel,
    span: List[str],
    notes: str = "",
) -> RaidCommandPlan:
    return RaidCommandPlan(
        action=RaidAction.CREATE,
        controller_index=controller.index,
        vendor=controller.vendor,
        label=label,
        argv=argv,
        level=level.value,
        disk_ids=[disk.disk_id for disk in disks],
        span_groups=[list(span)],
        notes=notes,
    )


def _lsi_create(controller: ControllerInfo, layout: RaidLayout, disks: List[DiskInfo], name: str) -> RaidCommandPlan:
    refs = ",".join(_lsi_ref(disk) for disk in disks)
    argv = [
        _binary(controller),
        f"/c{controller.index}",
        "add",
        "vd",
        _level_token(layout.level, Vendor.LSI),
        f"drives={refs}",
        "size=all",
        f"strip={layout.stripe_kb}",
    ]
    if layout.level is RaidLevel.RAID10:
        argv.append("pdperarray=2")
    if name:
        argv.append(f"name={name}")
    span = [disk.disk_id for disk in disks]
    return _create_plan(
        controller,
        argv,
        f"create {layout.level.value} on c{controller.index}",
        disks,
        layout.level,
        span,
        "Controller default initialisation applies; creation may run asynchronously",
    )


def _pmc_create(controller: ControllerInfo, layout: RaidLayout, disks: List[DiskInfo], name: str) -> RaidCommandPlan:
    argv = [_binary(controller), "create", str(controller.index), "logicaldrive"]
    # VERIFY: option order (stripesize/name) differs between arcconf builds.
    if layout.stripe_kb:
        argv += ["stripesize", str(layout.stripe_kb)]
    if name:
        argv += ["name", name]
    argv += ["MAX", _level_token(layout.level, Vendor.PMC)]
    for disk in disks:
        argv += _pmc_coords(disk)
    argv.append("noprompt")
    span = [disk.disk_id for disk in disks]
    return _create_plan(
        controller,
        argv,
        f"create {layout.level.value} on controller {controller.index}",
        disks,
        layout.level,
        span,
        "arcconf may initialise the new logical drive immediately",
    )


def _hpe_create(controller: ControllerInfo, layout: RaidLayout, disks: List[DiskInfo], name: str) -> RaidCommandPlan:
    refs = ",".join(_hpe_ref(disk) for disk in disks)
    argv = [
        _binary(controller),
        "ctrl",
        f"slot={controller.index}",
        "create",
        "type=ld",
        f"drives={refs}",
        f"raid={_level_token(layout.level, Vendor.HPE)}",
        f"ss={layout.stripe_kb}",
    ]
    if name:
        argv.append(f"label={name}")
    argv.append("forced")
    span = [disk.disk_id for disk in disks]
    return _create_plan(
        controller,
        argv,
        f"create {layout.level.value} on slot {controller.index}",
        disks,
        layout.level,
        span,
        "ssacli `forced` suppresses the interactive data-loss prompt only",
    )


def create_plans(controller: ControllerInfo, layout: RaidLayout) -> List[RaidCommandPlan]:
    """Build create commands for ``layout`` on ``controller``.

    RAID0 in ``per-disk`` mode yields one command per disk; every other layout
    yields a single virtual drive.
    """

    ensure_valid(layout)
    if controller.vendor is Vendor.GENERIC:
        raise ValueError("generic (sg3_utils) devices cannot be managed as hardware RAID")

    ordered = _ordered_disks(layout)
    builder = {
        Vendor.LSI: _lsi_create,
        Vendor.PMC: _pmc_create,
        Vendor.HPE: _hpe_create,
    }[controller.vendor]

    if layout.level is RaidLevel.RAID0 and layout.mode is Raid0Mode.PER_DISK:
        plans: List[RaidCommandPlan] = []
        for index, disk in enumerate(ordered):
            name = layout.volume_names[index] if index < len(layout.volume_names) else ""
            plans.append(builder(controller, layout, [disk], name))
        return plans

    name = layout.volume_names[0] if layout.volume_names else ""
    return [builder(controller, layout, ordered, name)]


def delete_plans(controller: ControllerInfo, volumes: List[VolumeInfo]) -> List[RaidCommandPlan]:
    """Build one delete command per selected virtual drive."""

    if controller.vendor is Vendor.GENERIC:
        raise ValueError("generic (sg3_utils) devices cannot be managed as hardware RAID")
    plans: List[RaidCommandPlan] = []
    for volume in volumes:
        if controller.vendor is Vendor.LSI:
            vd = _lsi_volume_token(volume.volume_id)
            argv = [_binary(controller), f"/c{controller.index}/{vd}", "delete", "force"]
        elif controller.vendor is Vendor.PMC:
            argv = [
                _binary(controller),
                "delete",
                str(controller.index),
                "logicaldrive",
                _pmc_volume_token(volume.volume_id),
                "noprompt",
            ]
        elif controller.vendor is Vendor.HPE:
            argv = [
                _binary(controller),
                "ctrl",
                f"slot={controller.index}",
                "ld",
                _hpe_volume_token(volume.volume_id),
                "delete",
                "forced",
            ]
        else:  # pragma: no cover - enum is closed
            raise ValueError(f"unsupported vendor: {controller.vendor.value}")
        plans.append(
            RaidCommandPlan(
                action=RaidAction.DELETE_VD,
                controller_index=controller.index,
                vendor=controller.vendor,
                label=f"delete {volume.volume_id} ({volume.level_label}) on c{controller.index}",
                argv=argv,
                level=volume.level,
                disk_ids=list(volume.disk_ids),
                volume_id=volume.volume_id,
                span_groups=[list(span) for span in volume.spans],
                notes="IRREVERSIBLE: the virtual drive and its data are deleted",
            )
        )
    return plans


def clear_plans(controller: ControllerInfo) -> List[RaidCommandPlan]:
    """Build the multi-step "clear controller configuration" sequence.

    The sequence deletes every virtual drive and then removes foreign / stale
    RAID metadata.  Each step is shown separately in the confirmation dialog.
    """

    if controller.vendor is Vendor.GENERIC:
        raise ValueError("generic (sg3_utils) devices cannot be managed as hardware RAID")
    binary = _binary(controller)
    index = controller.index

    if controller.vendor is Vendor.LSI:
        steps = [
            (
                [binary, f"/c{index}/vall", "delete", "force"],
                "delete every virtual drive",
                "VERIFY: some firmware rejects `force` on vall delete",
            ),
            (
                [binary, f"/c{index}/fall", "delete"],
                "delete foreign configuration metadata",
                "Destroys foreign DDF metadata on every drive attached to this controller",
            ),
        ]
    elif controller.vendor is Vendor.PMC:
        steps = [
            (
                [binary, "delete", str(index), "array", "ALL", "noprompt"],
                "delete every array and logical drive",
                "All data on every logical drive is lost",
            ),
            (
                [binary, "uninit", str(index), "all"],
                "uninitialise every physical drive",
                "VERIFY: some arcconf builds want `noprompt` on uninit",
            ),
        ]
    elif controller.vendor is Vendor.HPE:
        steps = [
            (
                [binary, "ctrl", f"slot={index}", "ld", "all", "delete", "forced"],
                "delete every logical drive",
                "All data on every logical drive is lost",
            ),
            (
                [binary, "ctrl", f"slot={index}", "modify", "clearconfig", "forced"],
                "clear controller configuration",
                "VERIFY: firmware variants differ on whether clearconfig also deletes LDs",
            ),
        ]
    else:  # pragma: no cover - enum is closed
        raise ValueError(f"unsupported vendor: {controller.vendor.value}")

    return [
        RaidCommandPlan(
            action=RaidAction.CLEAR_CONFIG,
            controller_index=index,
            vendor=controller.vendor,
            label=f"clear config step {position}: {label}",
            argv=argv,
            notes=notes,
        )
        for position, (argv, label, notes) in enumerate(steps, start=1)
    ]


__all__ = ["clear_plans", "create_plans", "delete_plans"]
