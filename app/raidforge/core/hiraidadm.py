"""Text parsers for Huawei ``hiraidadm`` (HiRAID / SP686C).

Every command prints a banner and a status block::

    ==========================================
        Hiraidadm   Information
    ==========================================
        Name: hiraidadm
        Version :1.2.0.2
        Build Time: ...

        Status Code = 0
        Status = Success
        Description = None
    ==========================================

    Controllers Number = 1
    ...

followed by ``Key | Value`` lines or fixed-width tables.  These helpers strip
the noise and turn the output into dicts so detection, disk enumeration and the
RAID provider all share one parser.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

_STATUS_RE = re.compile(r"Status\s+Code\s*=\s*(-?\d+)", re.IGNORECASE)
_SECTION_RE = re.compile(r"Hiraidadm\s+Information", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^\s*[-=]{3,}\s*$")
_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /_.()\-]*?)\s*\|\s*(.*?)\s*$")
_EQ_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /_.()\-]*?)\s*=\s*(.*?)\s*$")
_INT_ROW_RE = re.compile(r"^\s*(\d+)\s+")

#: Header columns of ``hiraidadm cN show pdlist`` (first line, right side
#: continues on a second header line).  Kept as an ordered token list so the
#: offset parser can slice multi-word headers such as ``Media Type``.
PDLIST_HEADER = (
    "Index",
    "Enc",
    "Slot",
    "DID",
    "Intf",
    "Media Type",
    "Status",
    "PreFail",
    "SlowDrive",
    "LinkSpd",
)
PDLIST_CONTINUATION = ("Capacity", "SecType", "FUA", "SED", "Spin", "FW", "Model")

VDLIST_HEADERS = (
    ("ID", "Name", "RAID Level", "Status", "Type", "Capacity", "SUSz", "SecSz",
     "RCache", "WCache", "RealWCache"),
    ("ID", "Name", "RAID Level", "Status", "VD Type", "Capacity"),
)

PDARRAY_HEADER = ("Enc", "Slot", "DID", "Intf", "Media", "Status", "Capacity")


def status_code(text: str) -> Optional[int]:
    """Return the tool exit status embedded in the output, or ``None``."""

    match = _STATUS_RE.search(text or "")
    return int(match.group(1)) if match else None


def is_success(text: str) -> bool:
    """True when the status block exists and reports success (or is absent)."""

    code = status_code(text)
    return code is None or code == 0


def _is_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _SEPARATOR_RE.match(line):
        return True
    if _SECTION_RE.search(stripped):
        return True
    low = stripped.lower()
    if low.startswith("name: hiraidadm") or low.startswith("build time"):
        return True
    if low.startswith("version :") or low.startswith("version:"):
        return True
    if re.match(r"^status\s+code\s*=", low):
        return True
    if re.match(r"^status\s*=\s*(success|fail)", low):
        return True
    if re.match(r"^description\s*=", low):
        return True
    return False


def body_lines(text: str) -> List[str]:
    """Return the useful lines (banner, separators and status block removed)."""

    return [line for line in (text or "").splitlines() if not _is_noise(line)]


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).lower()


def parse_kv(text: str) -> Dict[str, str]:
    """Parse ``Key | Value`` and ``Key = Value`` lines into a lower-case dict."""

    fields: Dict[str, str] = {}
    for line in body_lines(text):
        match = _KV_RE.match(line)
        if not match:
            match = _EQ_RE.match(line)
        if not match:
            continue
        fields[_key(match.group(1))] = match.group(2).strip()
    return fields


def _header_positions(line: str, tokens: Sequence[str]) -> Optional[List[int]]:
    starts: List[int] = []
    cursor = 0
    for token in tokens:
        index = line.find(token, cursor)
        if index < 0:
            return None
        starts.append(index)
        cursor = index + len(token)
    return starts


def _slice_row(line: str, starts: Sequence[int], tokens: Sequence[str]) -> Dict[str, str]:
    row: Dict[str, str] = {}
    for position, token in enumerate(tokens):
        start = starts[position]
        end = starts[position + 1] if position + 1 < len(starts) else None
        value = line[start:end] if end is not None else line[start:]
        row[_key(token)] = value.strip()
    return row


def _parse_two_line_table(
    text: str, header: Sequence[str], continuation: Sequence[str]
) -> List[Dict[str, str]]:
    lines = body_lines(text)
    header_index = -1
    starts: Optional[List[int]] = None
    for index, line in enumerate(lines):
        starts = _header_positions(line, header)
        if starts is not None:
            header_index = index
            break
    if starts is None or header_index < 0:
        return []

    cont_starts: Optional[List[int]] = None
    next_index = header_index + 1
    if next_index < len(lines):
        candidate = _header_positions(lines[next_index], continuation)
        if candidate is not None:
            cont_starts = candidate
            next_index += 1

    records: List[Dict[str, str]] = []
    for line in lines[next_index:]:
        if _header_positions(line, header) is not None:
            break
        if _INT_ROW_RE.match(line):
            records.append(_slice_row(line, starts, header))
        elif records and cont_starts is not None and line.strip():
            records[-1].update(_slice_row(line, cont_starts, continuation))
    return [record for record in records if record.get("index", "").isdigit()]


def _parse_one_line_table(
    text: str, candidates: Sequence[Sequence[str]]
) -> List[Dict[str, str]]:
    lines = body_lines(text)
    for header in candidates:
        for index, line in enumerate(lines):
            starts = _header_positions(line, header)
            if starts is None:
                continue
            records: List[Dict[str, str]] = []
            for row_line in lines[index + 1 :]:
                if _header_positions(row_line, header) is not None:
                    break
                if _INT_ROW_RE.match(row_line):
                    records.append(_slice_row(row_line, starts, header))
                elif records and not row_line.strip():
                    continue
            if records:
                return records
    return []


def parse_pdlist(text: str) -> List[Dict[str, str]]:
    """Parse ``hiraidadm cN show pdlist`` into one dict per physical drive."""

    records = _parse_two_line_table(text, PDLIST_HEADER, PDLIST_CONTINUATION)
    return [record for record in records if record.get("enc", "").isdigit()]


def parse_vdlist(text: str) -> List[Dict[str, str]]:
    """Parse ``hiraidadm cN show vdlist`` (best effort; IDs are authoritative)."""

    records = _parse_one_line_table(text, VDLIST_HEADERS)
    if not records:
        # Fallback: the only stable thing is the leading VD id.
        for line in body_lines(text):
            match = _INT_ROW_RE.match(line)
            if not match:
                continue
            value = int(match.group(1))
            records.append({"id": str(value)})
    return records


def vd_ids(text: str) -> List[str]:
    """Return the virtual-drive IDs from a ``show vdlist`` response."""

    ids: List[str] = []
    for record in parse_vdlist(text):
        value = str(record.get("id", "")).strip()
        if value.isdigit() and value not in ids:
            ids.append(value)
    return ids


def parse_pdarray(text: str) -> List[Dict[str, str]]:
    """Parse ``hiraidadm cN:vdM show pdarray`` (member physical drives)."""

    records = _parse_one_line_table(text, (PDARRAY_HEADER,))
    if records:
        return records
    # Header variants or missing header: fall back to leading enc/slot/did.
    fallback: List[Dict[str, str]] = []
    for line in body_lines(text):
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+", line)
        if match:
            fallback.append({"enc": match.group(1), "slot": match.group(2), "did": match.group(3)})
    return fallback


def controller_entries(text: str) -> List[Dict[str, str]]:
    """Parse ``hiraidadm show allctrl`` into one dict per controller."""

    if not is_success(text):
        return []
    count = 0
    count_match = re.search(r"Controllers\s+Number\s*=\s*(\d+)", text, re.IGNORECASE)
    if count_match:
        count = int(count_match.group(1))

    entries: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for line in body_lines(text):
        match = _KV_RE.match(line)
        if not match:
            continue
        key, value = _key(match.group(1)), match.group(2).strip()
        if key == "controller id":
            if current is not None:
                entries.append(current)
            current = {"controller id": value}
        elif current is not None:
            current[key] = value
    if current is not None:
        entries.append(current)

    entries = [entry for entry in entries if entry.get("controller id", "").isdigit()]
    if count:
        entries = entries[:count]
    return entries


def controller_fields(text: str) -> Dict[str, str]:
    """Parse ``hiraidadm cN show`` into model/serial/firmware fields."""

    fields = parse_kv(text)
    model = (
        fields.get("model")
        or fields.get("product name")
        or fields.get("board model")
        or ""
    )
    serial = fields.get("serial number") or fields.get("serial") or ""
    firmware = fields.get("firmware version") or fields.get("fw version") or ""
    sas = fields.get("sas address") or fields.get("sasaddress") or ""
    return {
        "model": model.strip(),
        "serial": serial.strip(),
        "firmware": firmware.strip(),
        "sas_address": sas.strip(),
    }


def erase_progress(text: str) -> Dict[str, str]:
    """Parse ``... show erase`` / ``show sanitize`` progress."""

    return parse_kv(text)


__all__ = [
    "body_lines",
    "controller_entries",
    "controller_fields",
    "erase_progress",
    "is_success",
    "parse_kv",
    "parse_pdarray",
    "parse_pdlist",
    "parse_vdlist",
    "status_code",
    "vd_ids",
]
