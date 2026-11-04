"""``python -m raidforge.agent --selftest``：只读自检，不执行写操作。"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any, Dict, Optional

from ..config import load_config
from ..core.serverid import resolve_server_id
from .bootmedia import probe as probe_boot_media
from .client import AgentClient
from .clock import SystemClock, rfc3339
from .config import AgentConfig
from .models import ALL_TASK_TYPES, AgentLimits
from .snapshot import SnapshotProvider
from .version import AGENT_VERSION, PROTOCOL_VERSION


def _agent_id(server_sn: str) -> str:
    import re

    return ("rf-" + re.sub(r"[^A-Za-z0-9._-]", "", server_sn))[:64]


def _payload(cfg: AgentConfig, server_sn: str, snapshot: Any) -> Dict[str, Any]:
    boot = snapshot.boot_media if snapshot is not None else probe_boot_media()
    return {
        "schema_version": 1,
        "agent_id": cfg.agent_id,
        "agent_type": "raidforge",
        "agent_version": AGENT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "server_sn": server_sn,
        "hostname": socket.gethostname(),
        "status": "idle",
        "current_task": None,
        "queue_depth": 0,
        "queue_max": 4,
        "capabilities": snapshot.heartbeat_capabilities() if snapshot is not None else {
            "task_types": ALL_TASK_TYPES,
            "dry_run": True,
            "evidence_kinds": [],
            "controllers": [],
        },
        "hardware": snapshot.hardware if snapshot is not None else {},
        "boot_media": boot.heartbeat_payload(),
        "storage": snapshot.storage if snapshot is not None else {},
        "last_error": None,
        "server_time_delta_s": 0,
    }


def selftest(dry: bool = False, yaml_path: Optional[str] = None) -> int:
    clock = SystemClock()
    cfg = load_config()
    agent_cfg = AgentConfig.load(yaml_path=yaml_path)
    server_id = resolve_server_id()
    server_sn = str(agent_cfg.server_sn_override or server_id.serial or "UNKNOWN")
    agent_cfg.agent_id = _agent_id(server_sn)

    print("== RAIDFORGE Agent selftest ==")
    print(json.dumps(agent_cfg.redacted(), ensure_ascii=False, indent=2, default=str))
    print(f"server_sn: {server_sn} (source={server_id.source})")
    print(f"agent_id: {agent_cfg.agent_id}")
    print(f"configured: {agent_cfg.configured}")

    provider = SnapshotProvider(
        cfg,
        server_sn,
        agent_cfg.evidence_dir,
        clock=clock,
        cache_seconds=0.0,
    )
    snapshot = provider.capture(fresh=True)
    print(f"topology_hash: {snapshot.topology_hash}")
    print("controllers:")
    for controller in snapshot.controllers:
        tool = controller.get("tool") or {}
        print(
            "  - {cid} {vendor} {model} serial={serial} fw={firmware} tool={name} {version}".format(
                cid=controller.get("controller_id"),
                vendor=controller.get("vendor"),
                model=controller.get("model"),
                serial=controller.get("serial"),
                firmware=controller.get("firmware"),
                name=tool.get("name"),
                version=tool.get("version"),
            )
        )
    print(f"pds: {len(snapshot.pds)}  vds: {len(snapshot.vds)}")
    print(
        "boot_media: determined={} protected={} error={}".format(
            snapshot.boot_media.determined,
            snapshot.boot_media.protected_count,
            snapshot.boot_media.error,
        )
    )
    print(f"storage_free_bytes: {snapshot.storage.get('free_bytes')}")

    payload = _payload(agent_cfg, server_sn, snapshot)
    if dry or not agent_cfg.configured:
        print("heartbeat payload (dry):")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    client = AgentClient(agent_cfg, clock=clock)
    response = client.heartbeat(payload)
    print(f"heartbeat: status={response.status} error_code={response.error_code or '-'}")
    return 0 if response.ok else 1


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m raidforge.agent")
    parser.add_argument("--selftest", action="store_true", help="只读自检")
    parser.add_argument("--dry", action="store_true", help="只打印心跳请求，不发送")
    parser.add_argument("--config", default=None, help="agent.yaml 路径")
    args = parser.parse_args(argv)
    if args.selftest or args.config is not None:
        return selftest(dry=args.dry, yaml_path=args.config)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
