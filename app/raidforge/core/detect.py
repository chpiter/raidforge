"""Controller detection ladder.

The ladder is fixed but every rung may be provided by more than one binary
name (``storcli``/``storcli64``, ``perccli``/``perccli64``,
``ssacli``/``hpssacli``).  The first rung that both exists **and** answers with
at least one controller wins; its resolved binary is carried on
:class:`~raidforge.core.models.DetectionResult` so enumeration and the RAID
providers talk to the same executable.

Every attempted rung is recorded so the evidence log can prove why a tool was
skipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..config import Config
from .models import ControllerInfo, DetectionAttempt, DetectionResult, Vendor
from .utils import run_cmd, which

_SLOT_RE = re.compile(r"(?:Smart Array|Smart HBA)\s+.+?Slot\s+(\d+)", re.IGNORECASE)
_ARCCONF_CTRL_RE = re.compile(r"^\s*Controller\s+(\d+)\s*:", re.MULTILINE)

#: Aliases accepted in ``tools.detection_order`` / ``tools.forced_tool``.
TOOL_ALIASES: Dict[str, str] = {
    "storcli64": "storcli",
    "perccli64": "perccli",
    "hpssacli": "ssacli",
    "sg_scan": "sg3_utils",
}


def normalize_tool(name: Optional[str]) -> str:
    """Map a binary/config alias onto the canonical ladder tool name."""

    text = str(name or "").strip().lower()
    return TOOL_ALIASES.get(text, text)


def _first_available(candidates: Sequence[str]) -> Optional[str]:
    """Return the first candidate that resolves through PATH."""

    for name in candidates:
        if which(name):
            return name
    return None


def _controllers_from_storcli(cfg: Config, binary: str = "storcli") -> List[ControllerInfo]:
    """Ask ``storcli``/``perccli`` how many controllers exist and describe each."""

    controllers: List[ControllerInfo] = []
    show = run_cmd([binary, "show", "J"], timeout=cfg.detect_timeout)
    count = 0
    try:
        payload = json.loads(show.stdout)
        for entry in payload.get("Controllers", []):
            data = entry.get("Response Data", {}) or {}
            raw = data.get("Number of Controllers") or data.get("Number of Controllers ")
            if isinstance(raw, int):
                count = raw
                break
    except (json.JSONDecodeError, AttributeError):
        count = 0

    if count <= 0:
        # Some builds report nothing useful in JSON; fall back to the text table.
        text = run_cmd([binary, "show"], timeout=cfg.detect_timeout)
        match = re.search(r"Number of Controllers\s*=\s*(\d+)", text.stdout)
        count = int(match.group(1)) if match else 0

    for index in range(count):
        detail = run_cmd([binary, f"/c{index}", "show", "J"], timeout=cfg.detect_timeout)
        model = serial = firmware = ""
        raw_json: Dict = {}
        if detail.ok:
            try:
                payload = json.loads(detail.stdout)
                raw_json = payload
                for entry in payload.get("Controllers", []):
                    data = entry.get("Response Data", {}) or {}
                    model = data.get("Product Name") or data.get("Product Name ") or ""
                    serial = data.get("Serial Number") or ""
                    firmware = data.get("FW Version") or data.get("Firmware Version") or ""
                    if not model:
                        for node in data.values():
                            if isinstance(node, dict):
                                model = node.get("Product Name") or model
                                serial = serial or node.get("Serial Number", "")
                                firmware = firmware or node.get("FW Version", "")
                    break
            except json.JSONDecodeError:
                match = re.search(r"Product Name\s*=\s*(.+)", detail.stdout)
                model = match.group(1).strip() if match else ""
        controllers.append(
            ControllerInfo(
                index=index,
                vendor=Vendor.LSI,
                model=model.strip(),
                firmware=str(firmware).strip(),
                serial=str(serial).strip(),
                binary=binary,
                raw_json=raw_json if isinstance(raw_json, dict) else {},
            )
        )
    return controllers


def _controllers_from_arcconf(cfg: Config, binary: str = "arcconf") -> List[ControllerInfo]:
    """Enumerate Adaptec/Microsemi controllers via ``arcconf list``."""

    listing = run_cmd([binary, "list"], timeout=cfg.detect_timeout)
    indices = [int(value) for value in _ARCCONF_CTRL_RE.findall(listing.stdout)]
    if not indices and listing.ok:
        indices = [int(value) for value in re.findall(r"^\s*Controller\s+(\d+)", listing.stdout, re.MULTILINE)]
    controllers: List[ControllerInfo] = []
    for index in indices:
        info = run_cmd([binary, "getconfigjson", str(index)], timeout=cfg.detect_timeout)
        model = serial = firmware = ""
        raw_json: Dict = {}
        if info.ok:
            try:
                raw_json = json.loads(info.stdout)
            except json.JSONDecodeError:
                raw_json = {}

        def walk(node: object) -> None:
            nonlocal model, serial, firmware
            if isinstance(node, dict):
                for key, value in node.items():
                    low = str(key).lower()
                    if low in ("model", "modelname", "controllermodel", "controllername") and isinstance(value, str) and not model:
                        model = value.strip()
                    elif low in ("serialnumber", "serial") and isinstance(value, str) and not serial:
                        serial = value.strip()
                    elif low in ("firmwareversion", "biosversion") and isinstance(value, str) and not firmware:
                        firmware = value.strip()
                    else:
                        walk(value)

        walk(raw_json or {"text": info.stdout})
        controllers.append(
            ControllerInfo(
                index=index,
                vendor=Vendor.PMC,
                model=model,
                firmware=firmware,
                serial=serial,
                binary=binary,
                raw_json=raw_json if isinstance(raw_json, dict) else {},
            )
        )
    return controllers


def _controllers_from_ssacli(cfg: Config, binary: str = "ssacli") -> List[ControllerInfo]:
    """Enumerate HPE Smart Array controllers via ``ssacli ctrl all show``."""

    listing = run_cmd([binary, "ctrl", "all", "show"], timeout=cfg.detect_timeout)
    slots = _SLOT_RE.findall(listing.stdout)
    controllers: List[ControllerInfo] = []
    for slot in slots:
        index = int(slot)
        line_match = re.search(rf"^(.*Slot\s+{index}.*)$", listing.stdout, re.MULTILINE | re.IGNORECASE)
        model = serial = firmware = ""
        if line_match:
            line = line_match.group(1)
            model = line.strip()
            firmware_match = re.search(r"\(FW:([^)]*)\)", line)
            firmware = firmware_match.group(1).strip() if firmware_match else ""
        controllers.append(
            ControllerInfo(
                index=index,
                vendor=Vendor.HPE,
                model=model,
                firmware=firmware,
                serial=serial,
                binary=binary,
                raw_json={"listing_line": line_match.group(1) if line_match else ""},
            )
        )
    return controllers


def _controllers_from_sg3_utils(cfg: Config, binary: str = "") -> List[ControllerInfo]:
    """Generic path: no controller, only direct attached devices."""

    return [
        ControllerInfo(
            index=0,
            vendor=Vendor.GENERIC,
            model="Direct attached (sg3_utils / smartctl)",
            binary=binary,
            raw_json={},
        )
    ]


@dataclass(frozen=True)
class _Rung:
    """One detection rung: canonical name, binaries and enumerator."""

    name: str
    candidates: Tuple[str, ...]
    vendor: Vendor
    provider: str
    enumerate: Callable[[Config, str], List[ControllerInfo]]


_LADDER: List[_Rung] = [
    _Rung("storcli", ("storcli", "storcli64"), Vendor.LSI, "lsi", _controllers_from_storcli),
    _Rung("perccli", ("perccli", "perccli64"), Vendor.LSI, "lsi", _controllers_from_storcli),
    _Rung("arcconf", ("arcconf",), Vendor.PMC, "adaptec", _controllers_from_arcconf),
    _Rung("ssacli", ("ssacli", "hpssacli"), Vendor.HPE, "hpe", _controllers_from_ssacli),
    _Rung("sg3_utils", ("sg_scan",), Vendor.GENERIC, "generic", _controllers_from_sg3_utils),
]

_DEFAULT_ORDER = ["storcli", "perccli", "arcconf", "ssacli", "sg3_utils"]


def detect_controllers(cfg: Config) -> DetectionResult:
    """Walk the detection ladder and return the winning tool plus controllers.

    Args:
        cfg: Loaded application configuration; honours
            :attr:`Config.detection_order` and :attr:`Config.forced_tool`.

    Returns:
        A :class:`DetectionResult`.  When no tool works the result carries an
        empty controller list and every failed attempt.
    """

    order = [normalize_tool(name) for name in (cfg.detection_order or _DEFAULT_ORDER)]
    forced = normalize_tool(cfg.forced_tool)
    attempts: List[DetectionAttempt] = []

    for rung in _LADDER:
        if rung.name not in order:
            continue
        if forced and forced != rung.name:
            attempts.append(
                DetectionAttempt(
                    tool=rung.name,
                    vendor=rung.vendor.value,
                    ok=False,
                    detail="skipped: forced_tool mismatch",
                )
            )
            continue

        binary = _first_available(rung.candidates)
        if not binary:
            attempts.append(
                DetectionAttempt(
                    tool=rung.name,
                    vendor=rung.vendor.value,
                    ok=False,
                    return_code=127,
                    stderr="binary not found in PATH",
                )
            )
            continue

        attempts.append(
            DetectionAttempt(
                tool=rung.name,
                vendor=rung.vendor.value,
                ok=True,
                binary=binary,
                detail=f"found at {which(binary)}",
            )
        )
        controllers = rung.enumerate(cfg, binary)
        for controller in controllers:
            controller.tool = rung.name
            controller.binary = controller.binary or binary
        if controllers:
            return DetectionResult(
                tool=rung.name,
                vendor=controllers[0].vendor,
                controllers=controllers,
                binary=binary,
                provider=rung.provider,
                attempts=attempts,
            )
        attempts.append(
            DetectionAttempt(
                tool=rung.name,
                vendor=rung.vendor.value,
                ok=False,
                binary=binary,
                detail="binary present but no controller answered",
            )
        )

    return DetectionResult(
        tool="none",
        vendor=Vendor.GENERIC,
        controllers=[],
        binary="",
        provider="generic",
        attempts=attempts,
    )
