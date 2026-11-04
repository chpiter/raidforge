"""UTC helpers shared by the control plane and its tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime] = None) -> str:
    moment = value or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_in(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=float(seconds)))


def parse_iso(text: str) -> Optional[datetime]:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def seconds_since(text: str, now: Optional[datetime] = None) -> Optional[float]:
    moment = parse_iso(text)
    if moment is None:
        return None
    reference = now or utcnow()
    return max(0.0, (reference - moment).total_seconds())


__all__ = ["utcnow", "iso", "iso_in", "parse_iso", "seconds_since"]
