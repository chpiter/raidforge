"""RAIDFORGE Agent 客户端。"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name in {"AgentRuntime", "build_agent"}:
        from .runtime import AgentRuntime, build_agent

        return {"AgentRuntime": AgentRuntime, "build_agent": build_agent}[name]
    raise AttributeError(name)


__all__ = ["AgentRuntime", "build_agent"]
