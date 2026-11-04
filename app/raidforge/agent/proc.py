"""子进程与厂商工具版本探测。

硬件扫描仍走 core；这里只提供可注入的 ``run_cmd`` 包装和工具版本查询，
便于测试替换。
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
from typing import Dict, List, Optional, Sequence

from ..core.utils import CmdResult, run_cmd

log = logging.getLogger(__name__)

_VERSION_CACHE: Dict[str, str] = {}
_VERSION_LOCK = threading.Lock()


def resolve_binary(binary: str) -> str:
    if not binary:
        return ""
    return shutil.which(binary) or ""


class Runner:
    """薄包装，测试可替换为记录 argv 的假实现。"""

    def run(self, argv: Sequence[str], timeout: float = 10.0) -> CmdResult:
        return run_cmd(argv, timeout=timeout)


def _version_argv(binary: str) -> List[str]:
    name = (binary or "").lower()
    if "storcli" in name or "perccli" in name:
        return [binary, "-v"]
    if "ssacli" in name or "hpssacli" in name:
        return [binary, "version"]
    if "arcconf" in name:
        return [binary]
    if "sg_scan" in name or "smartctl" in name:
        return [binary, "--version"]
    return [binary, "--version"]


def tool_version(binary: str, runner: Optional[Runner] = None, timeout: float = 5.0) -> str:
    """返回厂商工具版本；失败返回空串，绝不抛异常。"""

    path = resolve_binary(binary) or binary
    if not path:
        return ""
    with _VERSION_LOCK:
        if path in _VERSION_CACHE:
            return _VERSION_CACHE[path]

    runner = runner or Runner()
    version = ""
    try:
        result = runner.run(_version_argv(path), timeout=timeout)
        text = (result.stdout or "") + "\n" + (result.stderr or "")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = re.search(r"(?:version|ver\.?)[ :=]*([0-9][0-9A-Za-z._-]{1,31})", line, re.I)
            version = match.group(1) if match else line[:80]
            break
    except Exception as exc:  # pragma: no cover - runner 已保证不抛
        log.warning("工具版本探测失败 %s: %s", path, exc)
        version = ""

    with _VERSION_LOCK:
        _VERSION_CACHE[path] = version
    return version


def clear_version_cache() -> None:
    with _VERSION_LOCK:
        _VERSION_CACHE.clear()


__all__ = ["Runner", "clear_version_cache", "resolve_binary", "tool_version"]
