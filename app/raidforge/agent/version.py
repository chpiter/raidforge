"""Agent 版本与协议版本。

版本号只从 ``raidforge.__version__`` 派生，避免出现第四份版本来源。
"""

from __future__ import annotations

from .. import __version__

AGENT_VERSION = __version__
PROTOCOL_VERSION = 1

__all__ = ["AGENT_VERSION", "PROTOCOL_VERSION", "__version__"]
