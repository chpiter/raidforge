"""Identifier generation matching the agent validation regexes."""

from __future__ import annotations

from uuid import uuid4


def new_task_id() -> str:
    return f"tsk_{uuid4().hex}"


def new_lease_id() -> str:
    return f"lse_{uuid4().hex}"


__all__ = ["new_task_id", "new_lease_id"]
