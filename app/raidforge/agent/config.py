"""Agent 配置：进程环境 > /run/raidforge/agent.env > agent.yaml > 默认值。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ENV_PREFIX = "RF_AGENT_"
DEFAULT_YAML = Path("/etc/raidforge/agent.yaml")
DEFAULT_RUN_ENV = Path("/run/raidforge/agent.env")
DEFAULT_CMD_LINE = Path("/proc/cmdline")

_FIELD_ENV = {
    "SERVER_URL": "server_url",
    "API_PREFIX": "api_prefix",
    "CA_BUNDLE": "ca_bundle",
    "POLL_MODE": "poll_mode",
    "HEARTBEAT_INTERVAL": "heartbeat_interval",
    "POLL_INTERVAL_SHORT": "poll_interval_short",
    "POLL_JITTER_MS": "poll_jitter_ms",
    "CONFIRM_TIMEOUT_SECONDS": "confirm_timeout_seconds",
    "CONFIRM_POLICY": "confirm_policy",
    "EVIDENCE_DIR": "evidence_dir",
    "STATE_DIR": "state_dir",
    "LOG_DIR": "log_dir",
    "OPERATOR_NAME": "operator_name",
    "SERVER_SN_OVERRIDE": "server_sn_override",
    "REPORT_LOCAL_SUMMARY": "report_local_summary",
    "ALLOW_INSECURE_HTTP": "allow_insecure_http",
    "VERIFY_TLS": "verify_tls",
    "TOKEN": "token",
}

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}
_INT_FIELDS = {
    "heartbeat_interval",
    "poll_interval_short",
    "poll_jitter_ms",
    "confirm_timeout_seconds",
}
_BOOL_FIELDS = {"report_local_summary", "allow_insecure_http", "verify_tls"}
_PATH_FIELDS = {"evidence_dir", "state_dir", "log_dir"}


def _coerce(field_name: str, value: Any) -> Any:
    if value is None:
        return None
    if field_name in _INT_FIELDS:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None
    if field_name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        token = str(value).strip().lower()
        if token in _BOOL_TRUE:
            return True
        if token in _BOOL_FALSE:
            return False
        return None
    if field_name in _PATH_FIELDS:
        return Path(str(value).strip())
    return str(value).strip()


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith(ENV_PREFIX):
            key = key[len(ENV_PREFIX) :]
        values[key.lower()] = value.strip().strip("'\"")
    return values


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _cmdline_token(path: Path) -> str:
    try:
        parts = path.read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return ""
    for part in parts:
        if part.startswith("rf.token="):
            return part.split("=", 1)[1].strip()
    return ""


def _usb_token() -> str:
    """卷标 ``RAIDFORGE-CFG`` 的 U 盘：在常见挂载点下查找 agent.env。"""

    candidates = [Path("/run/raidforge/cfg/agent.env")]
    for parent in (Path("/media"), Path("/run/media"), Path("/mnt")):
        try:
            for entry in parent.glob("**/agent.env"):
                candidates.append(entry)
        except OSError:
            continue
    for candidate in candidates:
        if not candidate.is_file():
            continue
        values = _parse_env_file(candidate)
        token = values.get("token", "")
        if token:
            return token
    return ""


def _is_loopback(host: str) -> bool:
    name = (host or "").strip().lower()
    return name in {"localhost", "127.0.0.1", "::1", "[::1]"}


@dataclass
class AgentConfig:
    server_url: str = ""
    api_prefix: str = "/api/raidforge"
    ca_bundle: str = ""
    poll_mode: str = "short"
    heartbeat_interval: int = 10
    poll_interval_short: int = 3
    poll_jitter_ms: int = 500
    confirm_timeout_seconds: int = 300
    confirm_policy: str = "server"
    evidence_dir: Path = Path("/var/lib/raidforge/evidence")
    state_dir: Path = Path("/var/lib/raidforge/agent")
    log_dir: Path = Path("/var/log/raidforge-agent")
    operator_name: str = ""
    server_sn_override: str = ""
    report_local_summary: bool = False
    allow_insecure_http: bool = False
    verify_tls: bool = True
    token: str = field(default="", repr=False, compare=False)
    #: 由 server_sn 确定性推导；可由 runtime/selftest 填充。
    agent_id: str = ""

    # -- derived ------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.server_url and self.token)

    @property
    def base_url(self) -> str:
        return f"{self.server_url.rstrip('/')}{self.api_prefix}"

    @property
    def max_remote_queued(self) -> int:
        return 3

    @property
    def queue_max(self) -> int:
        return 4

    def redacted(self) -> Dict[str, Any]:
        payload = {item.name: getattr(self, item.name) for item in fields(self)}
        payload["token"] = "***" if self.token else ""
        for key in ("evidence_dir", "state_dir", "log_dir"):
            payload[key] = str(payload[key])
        return payload

    # -- loading ------------------------------------------------------------
    @classmethod
    def load(
        cls,
        yaml_path: Optional[Path] = None,
        run_env: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
        overrides: Optional[Dict[str, Any]] = None,
        cmdline: Optional[Path] = None,
    ) -> "AgentConfig":
        yaml_path = Path(yaml_path) if yaml_path else DEFAULT_YAML
        run_env = Path(run_env) if run_env else DEFAULT_RUN_ENV
        cmdline = Path(cmdline) if cmdline else DEFAULT_CMD_LINE
        environ = dict(os.environ if env is None else env)

        config = cls()

        # 1. YAML（agent.yaml 中的 operator.name 映射到 operator_name）
        yaml_values = _read_yaml(yaml_path)
        operator = yaml_values.pop("operator", None)
        if isinstance(operator, dict) and operator.get("name") is not None:
            yaml_values["operator_name"] = operator.get("name")
        config._apply(yaml_values, source="agent.yaml")

        # 2. /run 下的运行时 env 文件
        config._apply(_parse_env_file(run_env), source="agent.env")

        # 3. 进程环境变量
        env_values: Dict[str, Any] = {}
        for suffix, field_name in _FIELD_ENV.items():
            key = ENV_PREFIX + suffix
            if key in environ:
                env_values[field_name] = environ[key]
        config._apply(env_values, source="process env")

        # 4. 显式覆盖（测试/--set）
        if overrides:
            config._apply(overrides, source="overrides")

        # token 不在 yaml 中；只允许运行时注入
        if not config.token:
            config.token = environ.get("RF_AGENT_TOKEN", "") or _parse_env_file(run_env).get("token", "")
        if not config.token:
            config.token = _usb_token()
        if not config.token:
            config.token = _cmdline_token(cmdline)
        if yaml_path.is_file():
            try:
                if "token" in yaml_path.read_text(encoding="utf-8", errors="replace"):
                    log.warning("agent.yaml 中出现 token 字段；已忽略，token 仅允许运行时注入")
            except OSError:
                pass

        config._normalise()
        return config

    def _apply(self, values: Mapping[str, Any], source: str) -> None:
        for key, value in values.items():
            name = str(key).strip()
            if name == "operator":
                if isinstance(value, dict) and value.get("name") is not None:
                    self.operator_name = str(value.get("name") or "")[:64]
                continue
            if name == "token":
                continue
            if name not in {item.name for item in fields(self)}:
                log.warning("忽略未知 agent 配置键 %s（来源 %s）", name, source)
                continue
            coerced = _coerce(name, value)
            if coerced is None:
                log.warning("agent 配置 %s=%r 非法，使用默认值（来源 %s）", name, value, source)
                continue
            setattr(self, name, coerced)

    def _normalise(self) -> None:
        prefix = str(self.api_prefix or "/api/raidforge").strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        self.api_prefix = "/" + prefix.strip("/")

        if self.poll_mode != "short":
            log.warning("v1 只支持 poll_mode=short，收到 %r；回退 short", self.poll_mode)
            self.poll_mode = "short"

        self.heartbeat_interval = min(600, max(1, int(self.heartbeat_interval)))
        self.poll_interval_short = min(300, max(1, int(self.poll_interval_short)))
        self.poll_jitter_ms = min(5000, max(0, int(self.poll_jitter_ms)))
        self.confirm_timeout_seconds = min(600, max(60, int(self.confirm_timeout_seconds)))

        if self.confirm_policy not in {"server", "always"}:
            log.warning("confirm_policy=%r 非法；使用 server", self.confirm_policy)
            self.confirm_policy = "server"

        if not self.verify_tls:
            log.warning("verify_tls=false 不被允许；强制 True")
            self.verify_tls = True

        if self.server_url:
            parsed = urlparse(self.server_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                log.warning("server_url=%r 非法；Agent 进入未配置状态", self.server_url)
                self.server_url = ""
            elif parsed.scheme == "http" and not (
                self.allow_insecure_http and _is_loopback(parsed.hostname or "")
            ):
                log.warning("非回环地址不允许 http://；Agent 进入未配置状态")
                self.server_url = ""

        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            log.warning("ca_bundle=%s 不存在；仍按配置使用", self.ca_bundle)


__all__ = ["AgentConfig", "DEFAULT_RUN_ENV", "DEFAULT_YAML", "ENV_PREFIX"]
