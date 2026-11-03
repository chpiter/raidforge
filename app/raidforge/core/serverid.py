"""Server serial number resolution (constraint 11).

The serial number is the top level identity of a session: it appears in the
evidence directory name, in every JSON payload and inside the confirm token
that guards execution.  Real hardware frequently returns garbage here, so a
deterministic fallback ladder is required.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .models import utc_now_iso
from .utils import CmdResult, run_cmd

# Values that indicate dmidecode returned a placeholder rather than a serial.
PLACEHOLDER_SERIALS = {
    "",
    "0",
    "0000000000",
    "000000",
    "1234567890",
    "default string",
    "system serial number",
    "not specified",
    "to be filled by o.e.m.",
    "to be filled by oem",
    "none",
    "n/a",
    "na",
    "0123456789",
    "invalid",
    "unknown",
}

_SANITISE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")

# Same list with whitespace removed: dmidecode replies are matched *after*
# sanitising, so "System Serial Number" must also be recognised in that shape.
_NORMALISED_PLACEHOLDERS = {item.replace(" ", "") for item in PLACEHOLDER_SERIALS if not item.isdigit()}


@dataclass
class ServerId:
    """Resolved server identity together with where the value came from."""

    serial: str
    source: str
    raw_dmi: str = ""
    fallback_inputs: List[str] = field(default_factory=list)

    @property
    def confirm_token(self) -> str:
        """Audit identifier recorded in the evidence manifest.

        This is *not* the interactive gate: the operator types the short word
        ``yes`` (see ``ui/screens/confirm.py``).  The token is kept because it
        binds a session to one chassis in the manifest.
        """

        return f"CONFIRM-{self.serial}"

    @property
    def is_fallback(self) -> bool:
        """True when the DMI serial was unusable and a derived ID is in use."""

        return self.source.startswith("derived")

    def to_json_obj(self) -> dict:
        """Serialise for the evidence log."""

        return {
            "serial": self.serial,
            "source": self.source,
            "raw_dmi": self.raw_dmi,
            "is_fallback": self.is_fallback,
            "confirm_token": self.confirm_token,
            "fallback_inputs": self.fallback_inputs or [],
            "resolved_at": utc_now_iso(),
        }


def _clean(value: str) -> str:
    return _SANITISE_RE.sub("", value.strip())


def _usable(value: str) -> bool:
    cleaned = _clean(value)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if lowered in PLACEHOLDER_SERIALS or lowered in _NORMALISED_PLACEHOLDERS:
        return False
    if lowered.replace(" ", "").startswith("tobefilled"):
        return False
    return len(cleaned) >= 4


def read_mac_addresses() -> List[str]:
    """Return stable, non-virtual MAC addresses from sysfs."""

    macs: List[str] = []
    net_dir = "/sys/class/net"
    if not os.path.isdir(net_dir):
        return macs
    for name in sorted(os.listdir(net_dir)):
        if name == "lo":
            continue
        try:
            with open(os.path.join(net_dir, name, "address"), encoding="utf-8") as handle:
                mac = handle.read().strip()
        except OSError:
            continue
        if mac and mac.lower() != "00:00:00:00:00:00":
            macs.append(mac.lower())
    return macs


def _dmi(field_name: str) -> CmdResult:
    return run_cmd(["dmidecode", "-s", field_name], timeout=15)


def _derived_id(candidates: List[str]) -> str:
    import hashlib

    joined = "|".join(c for c in candidates if c)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest().upper()
    if not joined:
        return "UNKNOWN-NOINPUT"
    return f"UNK{digest[:12]}"


def resolve_server_id(force_values: Optional[dict] = None) -> ServerId:
    """Resolve the machine serial using the DMI -> fallback ladder.

    Ladder:

    1. ``dmidecode -s system-serial-number`` when it carries real content.
    2. ``dmidecode -s baseboard-serial-number``.
    3. ``dmidecode -s system-uuid``.
    4. Deterministic ``UNK<hash>`` derived from the first on-board MAC plus the
       UUID, so the same chassis always yields the same fallback token.

    Args:
        force_values: Optional dict overriding individual inputs; used by tests
            and by the ``RAIDFORGE_APP_FAKE_SN`` debugging escape hatch.

    Returns:
        A :class:`ServerId` whose ``source`` explains how it was obtained.
    """

    force_values = force_values or {}
    raw_system = force_values.get("system_serial") or _dmi("system-serial-number").stdout.strip()
    if _usable(raw_system):
        return ServerId(serial=_clean(raw_system), source="dmi.system-serial-number", raw_dmi=raw_system)

    fallback_inputs: List[str] = [f"system={raw_system}"]

    raw_baseboard = force_values.get("baseboard_serial") or _dmi("baseboard-serial-number").stdout.strip()
    fallback_inputs.append(f"baseboard={raw_baseboard}")
    if _usable(raw_baseboard):
        return ServerId(
            serial=_clean(raw_baseboard),
            source="dmi.baseboard-serial-number",
            raw_dmi=raw_system,
            fallback_inputs=fallback_inputs,
        )

    raw_uuid = force_values.get("system_uuid") or _dmi("system-uuid").stdout.strip()
    fallback_inputs.append(f"uuid={raw_uuid}")
    if _usable(raw_uuid) and _UUID_RE.match(_clean(raw_uuid)):
        return ServerId(
            serial=f"UUID-{_clean(raw_uuid)}",
            source="dmi.system-uuid",
            raw_dmi=raw_system,
            fallback_inputs=fallback_inputs,
        )

    macs: List[str] = force_values.get("macs") or read_mac_addresses()
    fallback_inputs.append(f"mac={macs[0] if macs else 'none'}")
    serial = _derived_id([raw_uuid, ", ".join(macs)])
    return ServerId(
        serial=serial,
        source="derived:uuid+mac",
        raw_dmi=raw_system,
        fallback_inputs=fallback_inputs,
    )
