"""可注入时钟。

所有轮询、退避、确认超时都通过 :class:`Clock` 取时间与睡眠，测试可以推进
虚拟时间而不真正 sleep。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def utcnow(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """生产环境时钟。"""

    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


def rfc3339(value: datetime | None = None) -> str:
    """UTC RFC3339，带 ``Z``。"""

    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(text: str) -> datetime | None:
    """解析 RFC3339；失败返回 ``None``（调用方按任务过期处理）。"""

    if not text:
        return None
    raw = str(text).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def minutes_since(started_at: datetime | None, now: datetime) -> int:
    if started_at is None:
        return 0
    return max(0, int((now - started_at).total_seconds() // 60))


__all__ = ["Clock", "SystemClock", "rfc3339", "parse_rfc3339", "minutes_since", "timedelta"]
