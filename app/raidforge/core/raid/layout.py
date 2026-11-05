"""Pure RAID layout validation and planning helpers.

Nothing here executes a command or reads a vendor output; the functions map a
list of :class:`DiskInfo` onto spans, volume names, capacity and validation
errors.  Keeping this layer pure makes every RAID level rule unit-testable.
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..models import DiskInfo, Medium, Raid0Mode, RaidLayout, RaidLevel, Vendor

_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_LEVEL_NUMBERS = {
    RaidLevel.RAID0: 0,
    RaidLevel.RAID1: 1,
    RaidLevel.RAID5: 5,
    RaidLevel.RAID10: 10,
}


def level_number(level: RaidLevel) -> int:
    """Return ``0``/``1``/``5``/``10`` for a :class:`RaidLevel`."""

    return _LEVEL_NUMBERS[level]


def sort_disks(disks: List[DiskInfo]) -> List[DiskInfo]:
    """Deterministic order used for span pairing and command construction."""

    return sorted(
        disks,
        key=lambda disk: (
            disk.controller_index,
            disk.enclosure if disk.enclosure is not None else 0,
            disk.slot if disk.slot is not None else 0,
            disk.channel if disk.channel is not None else 0,
            disk.device_number if disk.device_number is not None else 0,
            disk.disk_id,
        ),
    )


def normalize_name(name: str, max_len: int = 15) -> str:
    """Sanitise a VD name to the vendor-safe alphabet and maximum length."""

    cleaned = _NAME_RE.sub("-", str(name or "").strip()).strip("-_")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[: max(0, int(max_len))]


def disk_address(disk: DiskInfo, vendor: Optional[Vendor] = None) -> Optional[str]:
    """Return the vendor addressing token for a disk, or ``None``.

    LSI uses ``enclosure:slot``, Adaptec uses ``channel:device`` and HPE uses
    the controller reference such as ``1I:1:1``.
    """

    vendor = vendor or disk.vendor
    if vendor is Vendor.LSI:
        if disk.enclosure is not None and disk.slot is not None:
            return f"{disk.enclosure}:{disk.slot}"
        return None
    if vendor is Vendor.PMC:
        if disk.channel is not None and disk.device_number is not None:
            return f"{disk.channel}:{disk.device_number}"
        return None
    if vendor is Vendor.HPE:
        return disk.controller_ref or None
    return None


def addressable(disk: DiskInfo, vendor: Optional[Vendor] = None) -> bool:
    """True when the provider knows how to name this disk in a command."""

    return disk_address(disk, vendor) is not None


def _capacity(level: RaidLevel, disks: List[DiskInfo]) -> int:
    sizes = [max(0, int(disk.size_bytes or 0)) for disk in disks]
    if not sizes:
        return 0
    if level is RaidLevel.RAID0:
        return sum(sizes)
    if level is RaidLevel.RAID1:
        return min(sizes)
    if level is RaidLevel.RAID5:
        return (len(sizes) - 1) * min(sizes) if len(sizes) >= 2 else 0
    if level is RaidLevel.RAID10:
        return (len(sizes) // 2) * min(sizes)
    return 0


def build_layout(
    level: RaidLevel,
    disks: List[DiskInfo],
    mode: Raid0Mode = Raid0Mode.SINGLE,
    name: str = "",
    stripe_kb: Optional[int] = None,
    name_max_len: int = 15,
    init: str = "fast",
) -> RaidLayout:
    """Build a layout (spans, names, capacity, warnings) from a disk list.

    The layout is *not* rejected here; call :func:`validate_layout` (or
    :func:`ensure_valid`) before constructing commands so the UI can show every
    problem at once.
    """

    ordered = sort_disks(list(disks))
    effective_mode = mode if level is RaidLevel.RAID0 else Raid0Mode.SINGLE
    stripe = None
    if stripe_kb not in (None, "", 0):
        stripe = int(stripe_kb)
        if stripe <= 0:
            stripe = None
    init_value = "none" if str(init or "").strip().lower() == "none" else "fast"

    if level is RaidLevel.RAID10:
        spans = [ordered[index : index + 2] for index in range(0, len(ordered), 2)]
        spans = [span for span in spans if span]
    elif effective_mode is Raid0Mode.PER_DISK:
        spans = [[disk] for disk in ordered]
    else:
        spans = [ordered] if ordered else []

    sanitized = normalize_name(name, name_max_len)
    if sanitized:
        if effective_mode is Raid0Mode.PER_DISK:
            volume_names = [f"{sanitized}-d{index:02d}" for index in range(1, len(ordered) + 1)]
        else:
            volume_names = [sanitized]
    else:
        volume_names = []

    warnings: List[str] = []
    if name and not sanitized:
        warnings.append(f"name {name!r} contains no usable characters; it will be omitted")
    elif name and sanitized != str(name).strip():
        warnings.append(f"name normalised to {sanitized!r}")

    media = {disk.medium for disk in ordered}
    protocols = {disk.protocol for disk in ordered}
    if Medium.UNKNOWN in media:
        warnings.append("selection contains a disk with UNKNOWN media")
    if len(media) > 1:
        warnings.append("selection mixes HDD and SSD media")
    if len(protocols) > 1:
        warnings.append("selection mixes SAS/SATA/NVMe protocols")
    if level in (RaidLevel.RAID1, RaidLevel.RAID5, RaidLevel.RAID10):
        sizes = {disk.size_bytes for disk in ordered if disk.size_bytes}
        if len(sizes) > 1:
            warnings.append("member disks have different capacities; usable size follows the smallest")

    return RaidLayout(
        level=level,
        disks=ordered,
        mode=effective_mode,
        stripe_kb=stripe,
        init=init_value,
        name=sanitized,
        spans=[[disk.disk_id for disk in span] for span in spans],
        volume_names=volume_names,
        capacity_bytes=_capacity(level, ordered),
        warnings=warnings,
    )


def validate_layout(layout: RaidLayout) -> List[str]:
    """Return every structural problem with ``layout`` (empty means valid)."""

    errors: List[str] = []
    disks = layout.disks
    if not disks:
        return ["select at least one disk"]

    ids = [disk.disk_id for disk in disks]
    if len(set(ids)) != len(ids):
        errors.append("selection contains duplicate disks")

    controllers = {disk.controller_index for disk in disks}
    if len(controllers) > 1:
        errors.append("all member disks must belong to the same controller")

    if layout.mode is Raid0Mode.PER_DISK and layout.level is not RaidLevel.RAID0:
        errors.append("one-VD-per-disk is only valid for RAID0")

    for disk in disks:
        if disk.medium is Medium.UNKNOWN:
            errors.append(f"{disk.disk_id}: media type is UNKNOWN")
        if not disk.size_bytes:
            errors.append(f"{disk.disk_id}: capacity is unknown (0 bytes)")
        if not addressable(disk, disk.vendor):
            errors.append(f"{disk.disk_id}: no vendor address (enclosure/slot, channel/device or ref)")

    count = len(disks)
    if layout.level is RaidLevel.RAID0:
        if count < 1:
            errors.append("RAID0 needs at least 1 disk")
    elif layout.level is RaidLevel.RAID1:
        if count != 2:
            errors.append(f"RAID1 needs exactly 2 disks (selected {count})")
    elif layout.level is RaidLevel.RAID5:
        if count < 3:
            errors.append(f"RAID5 needs at least 3 disks (selected {count})")
    elif layout.level is RaidLevel.RAID10:
        if count < 4:
            errors.append(f"RAID10 needs at least 4 disks (selected {count})")
        elif count % 2:
            errors.append(f"RAID10 needs an even number of disks (selected {count})")
    else:  # pragma: no cover - enum is closed
        errors.append(f"unsupported RAID level {layout.level}")

    return errors


def ensure_valid(layout: RaidLayout) -> None:
    """Raise :class:`ValueError` when the layout cannot be created."""

    errors = validate_layout(layout)
    if errors:
        raise ValueError("; ".join(errors))


__all__ = [
    "addressable",
    "build_layout",
    "disk_address",
    "ensure_valid",
    "level_number",
    "normalize_name",
    "sort_disks",
    "validate_layout",
]
