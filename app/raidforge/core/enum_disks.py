"""Physical disk enumeration for all supported tool families (constraint 6).

Everything here is a pure ``text -> List[DiskInfo]`` function plus a thin
:func:`enumerate_disks` dispatcher, so each parser can be unit tested against
the fixtures in ``tests/fixtures`` without touching real hardware.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config
from .hiraidadm import parse_kv as parse_hiraidadm_kv, parse_pdlist as parse_hiraidadm_pdlist
from .models import ControllerInfo, DetectionResult, DiskInfo, Medium, Protocol, Vendor
from .utils import parse_decimal_size, run_cmd

_DRIVE_KEY_RE = re.compile(r"Drive\s*/c(\d+)/e(\d+)/s(\d+)", re.IGNORECASE)
_LSSCSI_RE = re.compile(
    r"^\[(?P<host>\d+):(?P<bus>\d+):(?P<target>\d+):(?P<lun>\d+)\]\s+"
    r"(?P<type>\S+)\s+(?P<vendor>\S+)\s+(?P<model>.+?)\s+(?P<rev>\S+)\s+"
    r"(?P<node>/dev/\S+)(?:\s+(?P<sg>/dev/sg\d+))?\s*$"
)
_ROTATION_RE = re.compile(r"Rotation Rate:\s*(.+)", re.IGNORECASE)
_SMART_FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 /\-_.]*?):\s+(.*?)\s*$")

# Column names that may carry useful metadata even when ``Med`` is absent.
_SIZE_KEYS = ("Size", "Size (MB)", "Capacity", "Total Size")


def _medium_from_text(value: Optional[str]) -> Medium:
    """Map a free-form vendor media column onto :class:`Medium`."""

    if not value:
        return Medium.UNKNOWN
    text = str(value).strip().lower()
    if text.startswith("ssd") or "solid" in text or "flash" in text:
        return Medium.SSD
    if text.startswith("hdd") or text.startswith("hdd:") or "rotational" in text:
        return Medium.HDD
    return Medium.UNKNOWN


def _medium_from_rotation(value: Optional[str]) -> Medium:
    """Map a rotational-speed column onto :class:`Medium`.

    ``7200`` / ``7200 rpm`` -> HDD; ``Solid State Device`` -> SSD; anything else
    stays UNKNOWN so the operator has to look at it instead of guessing.
    """

    if not value:
        return Medium.UNKNOWN
    text = str(value).strip()
    if re.search(r"\d", text):
        return Medium.HDD
    return _medium_from_text(text)


def _protocol_from_text(value: Optional[str]) -> Protocol:
    """Map a free-form interface column onto :class:`Protocol`."""

    text = (value or "").strip().lower()
    if "nvme" in text:
        return Protocol.NVME
    if text.startswith("sas"):
        return Protocol.SAS
    if text.startswith("sata"):
        return Protocol.SATA
    return Protocol.UNKNOWN


def _first_value(record: Dict[str, Any], names: Tuple[str, ...]) -> str:
    """Return the first non-empty value among candidate keys (case tolerant)."""

    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, "", "-"):
            return str(value).strip()
    return ""


# --------------------------------------------------------------------------
# LSI / Broadcom (storcli)
# --------------------------------------------------------------------------
def parse_storcli_json(text: str, controller: ControllerInfo) -> List[DiskInfo]:
    """Parse ``storcli /c{c}/eall/sall show J`` output into normalised disks.

    Args:
        text: Raw stdout of the ``show J`` command.
        controller: The controller these disks hang off.

    Returns:
        One :class:`DiskInfo` per discovered physical drive.  Drives whose media
        type cannot be resolved get :attr:`Medium.UNKNOWN`, which makes them
        non-selectable by design.
    """

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []

    records: List[Tuple[int, int, int, Dict[str, Any]]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                match = _DRIVE_KEY_RE.search(str(key))
                if match and isinstance(value, dict):
                    records.append((int(match.group(1)), int(match.group(2)), int(match.group(3)), value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)

    disks: List[DiskInfo] = []
    for ctrl_idx, enclosure, slot, record in records:
        medium = _medium_from_text(_first_value(record, ("Med", "Media", "Media Type")))
        protocol = _protocol_from_text(_first_value(record, ("Intf", "Interface", "Intf ")))
        if medium is Medium.UNKNOWN:
            # Secondary hint: presence of a spin rate implies spinning rust.
            spin = _first_value(record, ("Sp", "Sp:", "Spin"))
            if spin and re.search(r"\d", spin) and "ssd" not in spin.lower():
                medium = Medium.HDD
        disks.append(
            DiskInfo(
                disk_id=f"c{ctrl_idx}e{enclosure}s{slot}",
                controller_index=controller.index if ctrl_idx == controller.index else ctrl_idx,
                vendor=Vendor.LSI,
                medium=medium,
                protocol=protocol,
                enclosure=enclosure,
                slot=slot,
                model=_first_value(record, ("Model", "Model Number", "Model ")),
                serial=_first_value(record, ("SN", "Serial Number", "Serial No")),
                firmware=_first_value(
                    record,
                    ("Frw", "FW", "Firmware Revision", "Firmware Rev", "Firmware Version", "Revision"),
                ),
                wwn=_first_value(record, ("WWN", "World Wide ID", "WorldWideId")),
                size_bytes=parse_decimal_size(_first_value(record, _SIZE_KEYS)),
                raid_state=_first_value(record, ("State", "DG State", "Shield")),
                raw=dict(record),
            )
        )
    disks.sort(key=lambda d: (d.controller_index, d.enclosure or 0, d.slot or 0))
    return disks


# --------------------------------------------------------------------------
# PMC / Adaptec / Microsemi (arcconf)
# --------------------------------------------------------------------------
def parse_arcconf_json(text: str, controller: ControllerInfo) -> List[DiskInfo]:
    """Parse ``arcconf getconfigjson {ctrl}`` output.

    The vendor JSON shape varies between firmware generations, so we walk the
    tree collecting any object that looks like a physical device instead of
    relying on a fixed path.
    """

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []

    records: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            lowered = {str(k).lower(): k for k in node.keys()}
            looks_like_device = any(k in lowered for k in ("serialnumber", "serialno", "serial")) and any(
                k in lowered for k in ("channel", "device", "deviceid", "bay", "slot", "physicaldriveid")
            )
            if looks_like_device:
                records.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)

    disks: List[DiskInfo] = []
    for record in records:
        medium = _medium_from_text(
            _first_value(record, ("MediaType", "DriveType", "DeviceType", "Type", "Medium"))
        )
        if medium is Medium.UNKNOWN:
            rotation = _first_value(record, ("RotationRate", "RotationalSpeed", "SpindleSpeed"))
            if rotation:
                medium = _medium_from_rotation(rotation)
        protocol = _protocol_from_text(_first_value(record, ("InterfaceType", "BusType", "Interface")))
        channel = _to_int(_first_value(record, ("Channel", "Ch")))
        device = _to_int(_first_value(record, ("Device", "DeviceID", "Device Number")))
        disks.append(
            DiskInfo(
                disk_id=f"c{controller.index}-ch{channel}-dev{device}",
                controller_index=controller.index,
                vendor=Vendor.PMC,
                medium=medium,
                protocol=protocol,
                channel=channel,
                device_number=device,
                model=_first_value(record, ("Model", "ModelName", "VendorId")),
                serial=_first_value(record, ("SerialNumber", "SerialNumber ", "Serial")),
                firmware=_first_value(
                    record, ("Firmware", "FirmwareVersion", "FirmwareRev", "Revision")
                ),
                wwn=_first_value(record, ("SASAddress", "WorldWideId", "WWN", "WWID")),
                size_bytes=parse_decimal_size(_first_value(record, _SIZE_KEYS + ("Size(MB)",))),
                raid_state=_first_value(record, ("State", "Status", "DeviceState")),
                raw=dict(record),
            )
        )
    disks.sort(key=lambda d: (d.channel or 0, d.device_number or 0))
    return disks


def parse_arcconf_text(text: str, controller: ControllerInfo) -> List[DiskInfo]:
    """Fallback parser for ``arcconf getconfig {ctrl} pd`` plain output."""

    disks: List[DiskInfo] = []
    current: Optional[DiskInfo] = None
    for line in text.splitlines():
        header = re.match(
            r"\s*Device #?(\d+).*channel\s*(\d+),?\s*device\s*(\d+)", line, re.IGNORECASE
        )
        if header:
            device_no, channel = int(header.group(1)), int(header.group(2))
            device_index = int(header.group(3))
            if current:
                disks.append(current)
            current = DiskInfo(
                disk_id=f"c{controller.index}-ch{channel}-dev{device_index}",
                controller_index=controller.index,
                vendor=Vendor.PMC,
                channel=channel,
                device_number=device_index,
            )
            continue
        if current and ":" in line:
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key.startswith("device is"):
                continue
            if key in ("model", "model number"):
                current.model = value
            elif key.startswith("serial"):
                current.serial = value
            elif key in ("firmware", "fw", "revision") or key.startswith("firmware "):
                current.firmware = value
            elif key.startswith("size"):
                current.size_bytes = parse_decimal_size(value)
            elif key in ("state", "status"):
                current.raid_state = value
            elif key.startswith("interface"):
                current.protocol = _protocol_from_text(value)
            elif "solid state" in value.lower() or "ssd" in value.lower():
                current.medium = Medium.SSD
            elif key in ("rotational speed", "rotation rate", "spindle"):
                current.medium = _medium_from_text(value)
    if current:
        disks.append(current)
    return disks


# --------------------------------------------------------------------------
# HPE Smart Array (ssacli)
# --------------------------------------------------------------------------
def parse_ssacli_detail(text: str, controller: ControllerInfo) -> List[DiskInfo]:
    """Parse ``ssacli ctrl slot={n} pd all show detail``.

    Output is a sequence of blank-line separated blocks, one per physical
    drive; each block opens with ``physicaldrive <port:box:bay>``.
    """

    disks: List[DiskInfo] = []
    for block in re.split(r"\n\s*\n", text):
        header = re.search(r"^\s*physicaldrive\s+(\S+)", block, re.IGNORECASE | re.MULTILINE)
        if not header:
            continue
        ref = header.group(1).rstrip(",")
        fields: Dict[str, str] = {}
        for line in block.splitlines():
            match = _SMART_FIELD_RE.match(line.strip())
            if match:
                fields[match.group(1).strip().lower()] = match.group(2).strip()

        medium = _medium_from_text(
            fields.get("drive technology") or fields.get("media type") or fields.get("media technology")
        )
        if medium is Medium.UNKNOWN:
            medium = _medium_from_rotation(fields.get("rotational speed") or fields.get("rotational speed rpm"))
        protocol = _protocol_from_text(fields.get("interface type") or fields.get("interface"))
        box = _to_int(fields.get("box"))
        bay = _to_int(fields.get("bay"))
        disks.append(
            DiskInfo(
                disk_id=f"c{controller.index}-{ref}",
                controller_index=controller.index,
                vendor=Vendor.HPE,
                medium=medium,
                protocol=protocol,
                enclosure=box,
                slot=bay,
                model=fields.get("model", ""),
                serial=fields.get("serial number", ""),
                firmware=fields.get("firmware revision", "")
                or fields.get("firmware version", ""),
                wwn=fields.get("world wide id", fields.get("wwid", "")),
                size_bytes=parse_decimal_size(fields.get("size") or fields.get("capacity", "")),
                raid_state=fields.get("status", ""),
                controller_ref=ref,
                raw=fields,
            )
        )
    disks.sort(key=lambda d: d.controller_ref)
    return disks


# --------------------------------------------------------------------------
# Generic (sg3_utils + smartctl + nvme-cli)
# --------------------------------------------------------------------------
def parse_lsscsi(text: str) -> List[Dict[str, str]]:
    """Parse ``lsscsi -g`` into a list of small dicts."""

    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        match = _LSSCSI_RE.match(line.strip())
        if not match:
            continue
        row = match.groupdict()
        row["sg"] = row.get("sg") or ""
        rows.append(row)
    return rows


def parse_smartctl_identity(text: str) -> Dict[str, str]:
    """Extract the interesting key/value pairs from ``smartctl -i`` output."""

    fields: Dict[str, str] = {}
    for line in text.splitlines():
        match = _SMART_FIELD_RE.match(line.strip())
        if match:
            fields[match.group(1).strip().lower()] = match.group(2).strip()
    return fields


def medium_from_smartctl(fields: Dict[str, str]) -> Tuple[Medium, Protocol]:
    """Decide media type from smartctl identity output (constraint 6).

    Returns:
        ``(medium, protocol)``.  Anything indeterminate becomes
        :attr:`Medium.UNKNOWN`, which blocks selection by design.
    """

    rotation = fields.get("rotation rate", "")
    medium = _medium_from_rotation(rotation)
    if medium is Medium.UNKNOWN and rotation and re.search(r"\d", rotation):
        medium = Medium.HDD
    protocol = _protocol_from_text(fields.get("transport protocol") or fields.get("protocol") or "")
    if protocol is Protocol.UNKNOWN:
        # SATA / NVMe drives expose their transport through a version line
        # rather than a protocol field.
        for key in fields:
            lowered = key.lower()
            if lowered.startswith("sata version"):
                protocol = Protocol.SATA
                break
            if lowered.startswith("nvme version"):
                protocol = Protocol.NVME
                break
    return medium, protocol


def parse_nvme_list(text: str) -> List[DiskInfo]:
    """Parse ``nvme list -o json`` output into NVMe disks.

    ``nvme-cli`` is inconsistent here: with no NVMe devices some builds print
    ``{"Devices": []}`` while others print a bare ``[]``.  Treat both as "no
    devices" instead of assuming a mapping (that mistake used to crash the whole
    enumeration with ``'list' object has no attribute 'get'``).
    """

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []

    if isinstance(payload, list):
        devices = payload
    elif isinstance(payload, dict):
        devices = payload.get("Devices") or []
    else:
        return []

    disks: List[DiskInfo] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        path = device.get("DevicePath") or device.get("Path") or ""
        size = device.get("PhysicalSize")
        try:
            size_bytes = int(size)
        except (TypeError, ValueError):
            size_bytes = parse_decimal_size(str(size or ""))
        disks.append(
            DiskInfo(
                disk_id=path.rsplit("/", 1)[-1] or path,
                controller_index=0,
                vendor=Vendor.GENERIC,
                medium=Medium.SSD,
                protocol=Protocol.NVME,
                model=str(device.get("ModelNumber") or device.get("Model") or "").strip(),
                serial=str(device.get("SerialNumber") or "").strip(),
                firmware=str(device.get("Firmware") or "").strip(),
                size_bytes=size_bytes,
                device_node=path,
                raw=device,
            )
        )
    return disks


# --------------------------------------------------------------------------
# Huawei HiRAID (hiraidadm)
# --------------------------------------------------------------------------
def parse_hiraidadm_pd(text: str, controller: ControllerInfo) -> List[DiskInfo]:
    """Parse ``hiraidadm cN show pdlist`` into :class:`DiskInfo` objects."""

    disks: List[DiskInfo] = []
    for row in parse_hiraidadm_pdlist(text):
        enclosure = _to_int(row.get("enc"))
        slot = _to_int(row.get("slot"))
        if enclosure is None or slot is None:
            continue
        medium = _medium_from_text(row.get("media type") or row.get("media"))
        if medium is Medium.UNKNOWN:
            rotation = _to_int(row.get("spin"))
            if rotation == 0:
                medium = Medium.SSD
            elif rotation is not None:
                medium = Medium.HDD
        protocol = _protocol_from_text(row.get("intf"))
        disks.append(
            DiskInfo(
                disk_id=f"c{controller.index}e{enclosure}s{slot}",
                controller_index=controller.index,
                vendor=Vendor.HUAWEI,
                medium=medium,
                protocol=protocol,
                enclosure=enclosure,
                slot=slot,
                device_number=_to_int(row.get("did")),
                model=row.get("model", ""),
                firmware=row.get("fw", ""),
                size_bytes=parse_decimal_size(row.get("capacity", "")),
                raid_state=row.get("status", ""),
                raw=dict(row),
            )
        )
    disks.sort(key=lambda disk: (disk.enclosure or 0, disk.slot or 0))
    return disks


def augment_hiraidadm_details(
    cfg: Config, binary: str, disks: List[DiskInfo], raw_outputs: List[Tuple[str, str]]
) -> None:
    """Fill serial/firmware from ``hiraidadm cN:eM:sK show`` (bounded calls)."""

    for disk in disks[:64]:
        if disk.enclosure is None or disk.slot is None:
            continue
        address = f"c{disk.controller_index}:e{disk.enclosure}:s{disk.slot}"
        detail = run_cmd([binary, address, "show"], timeout=cfg.enum_timeout)
        if not detail.ok:
            continue
        raw_outputs.append((f"huawei_{address.replace(':', '_')}_show.txt", detail.stdout))
        fields = parse_hiraidadm_kv(detail.stdout)
        disk.serial = fields.get("serial number", "") or disk.serial
        disk.firmware = disk.firmware or fields.get("firmware version", "")
        disk.raid_state = disk.raid_state or fields.get("status", "")
        disk.raw.update(fields)


# --------------------------------------------------------------------------
# dispatcher
# --------------------------------------------------------------------------
def enumerate_disks(cfg: Config, detection: DetectionResult) -> Tuple[List[DiskInfo], List[Tuple[str, str]]]:
    """Enumerate every physical disk behind the detected controller(s).

    Args:
        cfg: Loaded configuration (timeouts, NVMe toggle).
        detection: Result of the detection ladder.

    Returns:
        ``(disks, raw_outputs)`` where ``raw_outputs`` is a list of
        ``(filename, text)`` pairs to be archived under
        ``controller_raw/`` in the evidence directory.
    """

    disks: List[DiskInfo] = []
    raw_outputs: List[Tuple[str, str]] = []

    binary = detection.binary or {
        "lsi": "storcli",
        "pmc": "arcconf",
        "hpe": "ssacli",
        "huawei": "hiraidadm",
    }.get(detection.vendor.value, "")

    for controller in detection.controllers:
        if controller.vendor is Vendor.HUAWEI:
            tool = binary or "hiraidadm"
            result = run_cmd(
                [tool, f"c{controller.index}", "show", "pdlist"], timeout=cfg.enum_timeout
            )
            raw_outputs.append((f"huawei_c{controller.index}_pdlist.txt", result.stdout))
            found = parse_hiraidadm_pd(result.stdout, controller)
            augment_hiraidadm_details(cfg, tool, found, raw_outputs)
            disks.extend(found)
        elif controller.vendor is Vendor.LSI:
            result = run_cmd(
                [binary or "storcli", f"/c{controller.index}/eall/sall", "show", "J"], timeout=cfg.enum_timeout
            )
            raw_outputs.append((f"lsi_c{controller.index}_sall_show_J.json", result.stdout))
            disks.extend(parse_storcli_json(result.stdout, controller))
        elif controller.vendor is Vendor.PMC:
            tool = binary or "arcconf"
            result = run_cmd([tool, "getconfigjson", str(controller.index)], timeout=cfg.enum_timeout)
            if result.ok:
                raw_outputs.append((f"pmc_c{controller.index}_getconfigjson.json", result.stdout))
            found = parse_arcconf_json(result.stdout, controller)
            if not found:
                legacy = run_cmd([tool, "getconfig", str(controller.index), "pd"], timeout=cfg.enum_timeout)
                raw_outputs.append((f"pmc_c{controller.index}_getconfig_pd.txt", legacy.stdout))
                found = parse_arcconf_text(legacy.stdout, controller)
            disks.extend(found)
        elif controller.vendor is Vendor.HPE:
            result = run_cmd(
                [binary or "ssacli", "ctrl", f"slot={controller.index}", "pd", "all", "show", "detail"],
                timeout=cfg.enum_timeout,
            )
            raw_outputs.append((f"hpe_c{controller.index}_pd_all_show_detail.txt", result.stdout))
            disks.extend(parse_ssacli_detail(result.stdout, controller))
        else:
            disks.extend(_enumerate_generic(cfg, raw_outputs))

    return disks, raw_outputs


def _enumerate_generic(cfg: Config, raw_outputs: List[Tuple[str, str]]) -> List[DiskInfo]:
    """Generic enumeration via ``lsscsi -g`` + ``smartctl -i`` (+ ``nvme``)."""

    disks: List[DiskInfo] = []
    listing = run_cmd(["lsscsi", "-g"], timeout=cfg.enum_timeout)
    raw_outputs.append(("generic_lsscsi.txt", listing.stdout))

    for row in parse_lsscsi(listing.stdout):
        if row["type"] != "disk":
            continue
        node = row["node"]
        identity = run_cmd(["smartctl", "-i", node], timeout=cfg.enum_timeout)
        raw_outputs.append((f"generic_ident_{node.replace('/', '_')}.txt", identity.stdout))
        fields = parse_smartctl_identity(identity.stdout)
        medium, protocol = medium_from_smartctl(fields)
        disks.append(
            DiskInfo(
                disk_id=node.rsplit("/", 1)[-1],
                controller_index=0,
                vendor=Vendor.GENERIC,
                medium=medium,
                protocol=protocol,
                model=fields.get("model number") or fields.get("model") or row["model"],
                serial=fields.get("serial number", ""),
                firmware=fields.get("firmware version") or fields.get("revision", ""),
                wwn=fields.get("lu wwn device id", ""),
                size_bytes=parse_decimal_size(fields.get("user capacity", "").split("[")[0]),
                device_node=node,
                scsi_generic_node=row.get("sg", "") or "",
                raw=fields,
            )
        )

    if cfg.enable_nvme:
        nvme = run_cmd(["nvme", "list", "-o", "json"], timeout=cfg.enum_timeout)
        if nvme.ok:
            raw_outputs.append(("generic_nvme_list.json", nvme.stdout))
            for disk in parse_nvme_list(nvme.stdout):
                if disk.device_node not in {d.device_node for d in disks}:
                    disks.append(disk)

    disks.sort(key=lambda d: d.device_node or d.disk_id)
    return disks


def _to_int(value: Optional[str]) -> Optional[int]:
    """Best effort int conversion used by the vendor parsers."""

    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
