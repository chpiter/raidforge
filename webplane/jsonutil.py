"""Small JSON helpers for SQLite text columns."""

from __future__ import annotations

import json
from typing import Any


def loads(text: Any, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = ["loads", "dumps"]
