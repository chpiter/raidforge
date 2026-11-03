"""Small parsing helpers shared by the RAID providers."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Iterator, Optional, Sequence

_LEVEL_RE = re.compile(r"RAID\s*([0-9]+(?:\+[0-9]+)?)", re.IGNORECASE)


def normalize_level(value: Any) -> str:
    """Normalise a vendor RAID-level string to ``RAID0``/``RAID1``/``RAID5``/``RAID10``."""

    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return ""
    if "1+0" in text or "10" in text:
        return "RAID10"
    match = _LEVEL_RE.search(text)
    token = match.group(1) if match else text
    if token in ("5", "50", "6", "60"):
        return "RAID5" if token == "5" else ""
    if token == "1":
        return "RAID1"
    if token == "0":
        return "RAID0"
    return ""


def first_value(record: Dict[str, Any], names: Sequence[str]) -> str:
    """Return the first non-empty value among candidate keys (case tolerant)."""

    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for name in names:
        value = lowered.get(name.strip().lower())
        if value not in (None, "", "-", "N/A"):
            return str(value).strip()
    return ""


def walk_dicts(node: Any) -> Iterator[Dict[str, Any]]:
    """Yield every dict in a nested JSON document."""

    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_dicts(item)


def as_int(value: Any) -> Optional[int]:
    """Best-effort integer conversion."""

    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def unique(items: Iterable[str]) -> list:
    """Stable de-duplication preserving order."""

    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


__all__ = ["as_int", "first_value", "normalize_level", "unique", "walk_dicts"]
