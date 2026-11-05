"""Central configuration (constraint 13).

Every tunable lives here.  Defaults are baked in so the application runs with
zero configuration; ``/opt/raidforge/config.yaml`` (or ``./config.yaml`` when
running from a checkout) can override any leaf key, then every
``/etc/raidforge/config.d/*.yaml`` fragment (sorted) is merged on top, and
environment variables of the form ``RAIDFORGE_<SECTION>_<KEY>`` win over everything
except explicit in-process overrides.

Precedence, lowest to highest:

1. dataclass defaults in this module
2. ``/opt/raidforge/config.yaml`` (factory template, symlink to /etc)
3. ``./config.yaml`` (developer checkout; the same file in the image)
4. ``/etc/raidforge/config.d/*.yaml`` (site overrides written by the TUI)
5. an explicit ``--config`` file
6. ``RAIDFORGE_*`` environment variables (debug / automation)
7. ``overrides`` passed to :func:`load_config` (tests, ``--set``)
"""

from __future__ import annotations

import contextlib
import os
import logging
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

try:  # PyYAML ships in the ISO; degrade gracefully when absent (unit tests).
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

SYSTEM_CONFIG_PATH = "/opt/raidforge/config.yaml"
LOCAL_CONFIG_PATH = "config.yaml"
CONFIG_DIR = "/etc/raidforge/config.d"
SITE_OVERRIDE_PATH = CONFIG_DIR + "/90-site.yaml"
ENV_PREFIX = "RAIDFORGE_"

YAML_KEY = "config"


@dataclass
class UiConfig:
    """Visual settings for the Textual application.

    The default is the 7.2 deep-blue palette.  The monochrome palette remains
    available for 16-colour VGA/fbcon consoles where a dark background is
    quantised to bright black, but it is only used when explicitly selected.
    """

    # DEPRECATED, accepted and ignored: the palette is defined by ui/palette.py.
    # The fields stay so that an existing /etc/raidforge/config.yaml (which may
    # still carry the old navy/cyan values) keeps loading instead of raising
    # TypeError at startup.
    background: str = "#002B49"
    foreground: str = "#E6F4FA"
    accent: str = "#4FC3F7"
    danger: str = "#FF5252"
    ok: str = "#66BB6A"
    refresh_interval: float = 1.0
    #: auto | raidforge | raidforge-mono.  "auto" is an alias for deep blue;
    #: monochrome must be selected explicitly.
    theme: str = "raidforge"


@dataclass
class ToolConfig:
    """Timeouts and discovery policy for the vendor tools."""

    detect_timeout: float = 20.0
    enum_timeout: float = 30.0
    command_timeout: float = 60.0
    detection_order: List[str] = field(
        default_factory=lambda: ["hiraidadm", "storcli", "perccli", "arcconf", "ssacli", "sg3_utils"]
    )
    forced_tool: Optional[str] = None
    enable_nvme: bool = True
    vendor_bin_dir: str = "/usr/local/bin"
    default_hdd_method: str = "thorough"
    default_ssd_method: Optional[str] = None


@dataclass
class RaidConfig:
    """RAID management defaults (creation and destructive write limits)."""

    #: ``None`` means "controller default": no stripe option is emitted.
    stripe_kb: Optional[int] = None
    #: ``fast`` requests fast initialisation where the vendor exposes it.
    init: str = "fast"
    #: RAID commands can take longer than erase submissions (controller work).
    command_timeout: float = 120.0
    allow_clear_config: bool = True
    name_max_len: int = 15

    def __post_init__(self) -> None:
        if not self.stripe_kb:
            self.stripe_kb = None
        else:
            self.stripe_kb = int(self.stripe_kb)
        self.init = "none" if str(self.init or "").strip().lower() == "none" else "fast"


@dataclass
class EvidenceConfig:
    """Where evidence lands and how it is recorded."""

    root: str = "/var/log/raidforge-evidence"
    record_session: bool = True
    recorder: str = "asciinema"
    recorder_options: List[str] = field(default_factory=lambda: ["--overwrite", "--quiet"])
    snapshot_on_screen_change: bool = True


def normalize_transport(value: Any) -> str:
    """Return the preferred upload channel: ``ssh`` or ``http``.

    Empty means "use the historical default"; unknown values warn and fall
    back rather than turning a config typo into a startup failure on tty1.
    """

    text = str(value or "").strip().lower()
    if not text:
        return "ssh"
    if text in ("ssh", "http"):
        return text
    log.warning("unknown report.transport %r; using ssh", value)
    return "ssh"


def _coerce_port(value: Any, default: int, field_name: str) -> int:
    """Normalise a TCP port coming from YAML, env, ``--set`` or a default."""

    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        log.warning("invalid report.%s %r; using %d", field_name, value, default)
        return default
    if 1 <= port <= 65535:
        return port
    log.warning("report.%s out of range (%r); using %d", field_name, value, default)
    return default


@dataclass
class ReportConfig:
    """Evidence upload: selected transport first, the other second, pending last."""

    enabled: bool = True
    wait_for_route_seconds: int = 30
    #: Preferred channel; the other one is still attempted before queueing.
    transport: str = "ssh"
    ssh_host: str = ""
    ssh_user: str = "evidence"
    ssh_port: int = 22
    ssh_key_path: str = "/etc/raidforge/keys/evidence_ed25519"
    ssh_remote_root: str = "/srv/evidence/incoming"
    ssh_extra_opts: List[str] = field(
        default_factory=lambda: ["-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
    )
    http_base_url: str = ""
    #: HTTP evidence receiver host.  Empty means "follow the webplane URL",
    #: which only applies when ``transport`` is ``http``.
    http_host: str = ""
    http_port: int = 8080
    http_timeout: float = 30.0
    pending_root: str = "/var/lib/raidforge/pending"
    uploaded_root: str = "/var/lib/raidforge/uploaded"
    #: Legacy cap for explicit ``finalize_session(publish=True)`` calls and
    #: tests.  The application exit path no longer uploads; erase tasks upload
    #: immediately with ``run_upload_seconds``.
    exit_upload_seconds: float = 30.0
    #: Best-effort upload budget after each erase command sequence, while the
    #: TUI stays live.  A failure is recorded in ReportStatus/events; the exit
    #: path no longer builds a report or queues another upload.
    run_upload_seconds: float = 30.0

    def __post_init__(self) -> None:
        """Normalise values from every configuration source in one place.

        ``--set`` injects raw strings, and hand-written YAML may contain an
        invalid transport or port.  Doing this here keeps upload code simple and
        makes the dataclass safe to construct directly in tests.
        """

        self.transport = normalize_transport(self.transport)
        self.ssh_port = _coerce_port(self.ssh_port, 22, "ssh_port")
        self.http_port = _coerce_port(self.http_port, 8080, "http_port")



@dataclass
class WebplaneConfig:
    """Control-plane endpoint; empty host inherits the Agent's server_url."""

    host: str = ""
    port: int = 8080
    scheme: str = "http"
    api_prefix: str = "/api/raidforge"

    def __post_init__(self) -> None:
        host = str(self.host or "").strip()
        if "://" in host:
            from urllib.parse import urlsplit

            parsed = urlsplit(host)
            host = parsed.hostname or ""
            if parsed.scheme in {"http", "https"}:
                self.scheme = parsed.scheme
            if parsed.port:
                self.port = parsed.port
        self.host = host.rstrip("/")
        self.scheme = "https" if str(self.scheme or "").lower() == "https" else "http"
        self.port = _coerce_port(self.port, 8080, "webplane.port")
        prefix = str(self.api_prefix or "/api/raidforge").strip()
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        self.api_prefix = "/" + prefix.strip("/")


@dataclass
class Config:
    """Root configuration object threaded through every module."""

    app_name: str = "raidforge"
    app_version: str = "1.2.0"
    dry_run: bool = False
    ui: UiConfig = field(default_factory=UiConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
    raid: RaidConfig = field(default_factory=RaidConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    webplane: WebplaneConfig = field(default_factory=WebplaneConfig)

    # --- convenience accessors -------------------------------------------------
    @property
    def detect_timeout(self) -> float:
        return self.tools.detect_timeout

    @property
    def enum_timeout(self) -> float:
        return self.tools.enum_timeout

    @property
    def command_timeout(self) -> float:
        return self.tools.command_timeout

    @property
    def detection_order(self) -> List[str]:
        return self.tools.detection_order

    @property
    def forced_tool(self) -> Optional[str]:
        return self.tools.forced_tool

    @property
    def enable_nvme(self) -> bool:
        return self.tools.enable_nvme

    @property
    def default_hdd_method(self) -> str:
        return self.tools.default_hdd_method

    @property
    def default_ssd_method(self) -> Optional[str]:
        return self.tools.default_ssd_method

    @property
    def raid_stripe_kb(self) -> Optional[int]:
        return self.raid.stripe_kb

    @property
    def raid_init(self) -> str:
        return self.raid.init

    @property
    def raid_command_timeout(self) -> float:
        return self.raid.command_timeout

    @property
    def evidence_root(self) -> str:
        return self.evidence.root

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the whole tree, including the YAML/ENV provenance."""

        return asdict(self)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into ``base`` (mutates ``base``)."""

    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_yaml(path: str) -> Dict[str, Any]:
    if yaml is None:
        return {}
    candidate = Path(path)
    if not candidate.is_file():
        return {}
    try:
        with candidate.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception:  # pragma: no cover - broken YAML must not brick the TUI
        return {}


def config_fragments(directory: Optional[str] = None) -> List[Path]:
    """Return the ``*.yaml`` overlay fragments, sorted by name.

    Only one level is read: the directory is a flat drop-in area owned by the
    appliance (``/etc/raidforge/config.d``), not a tree.
    """

    base = Path(directory or CONFIG_DIR)
    if not base.is_dir():
        return []
    return sorted(item for item in base.glob("*.yaml") if item.is_file())


def write_site_override(
    updates: Dict[str, Any],
    path: Optional[str] = None,
) -> Path:
    """Merge ``updates`` into the TUI-owned overlay file and write it atomically.

    The factory template keeps its comments; this file only holds the deltas
    the operator actually changed.  A same-directory temporary file plus
    ``os.replace`` means a reader never sees a half-written YAML.

    Raises:
        RuntimeError: PyYAML is missing (it is installed in the image).
        OSError: the directory is not writable (live /etc is a tmpfs overlay).
    """

    if yaml is None:  # pragma: no cover - the ISO always ships PyYAML
        raise RuntimeError("PyYAML is required to save settings")

    target = Path(path or SITE_OVERRIDE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    current = _load_yaml(str(target))
    if not isinstance(current, dict):  # pragma: no cover - defensive
        current = {}
    merged = _deep_merge(current, updates or {})

    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(target.parent),
        prefix=".90-site-",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write("# Written by the RAIDFORGE TUI; merged over config.yaml.\n")
            handle.write("# Safe to edit by hand. Last write wins at boot.\n")
            yaml.safe_dump(merged, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, 0o644)
        os.replace(handle.name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise
    return target


def _split_env_key(key: str, known_top: set) -> Optional[tuple]:
    """Map ``RAIDFORGE_<TOP>_<FIELD>`` onto ``(top, field)``.

    Accepts both ``RAIDFORGE_TOOLS_COMMAND_TIMEOUT`` and ``RAIDFORGE_tools__command_timeout``
    spellings.  ``Optional``-safe: returns ``None`` when the key is unknown.
    """

    lowered = key.lower().replace("__", "_")
    for section in sorted(known_top, key=len, reverse=True):
        prefix = f"{section}_"
        if lowered.startswith(prefix) and len(lowered) > len(prefix):
            return section, lowered[len(prefix) :]
    return None, None


def _env_overrides() -> Dict[str, Any]:
    """Map ``RAIDFORGE_SECTION_KEY`` style variables onto nested dicts."""

    result: Dict[str, Any] = {}
    known_top = {f.name for f in fields(Config)}
    for key, raw in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        lowered = key[len(ENV_PREFIX) :].lower()
        # Top level scalar, e.g. RAIDFORGE_DRY_RUN.
        if lowered in known_top:
            result[lowered] = _coerce(raw)
            continue
        # Nested scalar, e.g. RAIDFORGE_TOOLS_COMMAND_TIMEOUT.
        section, field_name = _split_env_key(lowered, known_top)
        if section and field_name:
            result.setdefault(section, {})[field_name] = _coerce(raw)
    return result


def _coerce(raw: str) -> Any:
    """Convert env strings into plausible Python values."""

    lowered = raw.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered == "":
        return ""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if "," in raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def _construct(cls, data: Dict[str, Any]):
    """Build a dataclass from ``data``, ignoring keys it does not know.

    Without this, removing a field (or a typo in config.yaml) turns into a hard
    ``TypeError`` at startup - on tty1, where there is no way to see the error.
    """

    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        log.warning("ignoring unknown %s keys: %s", cls.__name__, ", ".join(unknown))
    return cls(**{key: value for key, value in data.items() if key in known})


def _build(data: Dict[str, Any]) -> Config:
    cfg = Config()
    if "app_name" in data:
        cfg.app_name = str(data["app_name"])
    if "app_version" in data:
        cfg.app_version = str(data["app_version"])
    cfg.dry_run = bool(data.get("dry_run", cfg.dry_run))
    cfg.ui = _construct(UiConfig, {**asdict(UiConfig()), **(data.get("ui") or {})})
    cfg.tools = _construct(ToolConfig, {**asdict(ToolConfig()), **(data.get("tools") or {})})
    cfg.raid = _construct(RaidConfig, {**asdict(RaidConfig()), **(data.get("raid") or {})})
    cfg.evidence = _construct(EvidenceConfig, {**asdict(EvidenceConfig()), **(data.get("evidence") or {})})
    cfg.report = _construct(ReportConfig, {**asdict(ReportConfig()), **(data.get("report") or {})})
    cfg.webplane = _construct(
        WebplaneConfig, {**asdict(WebplaneConfig()), **(data.get("webplane") or {})}
    )
    return cfg


def load_config(
    extra_path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    config_dir: Optional[str] = None,
) -> Config:
    """Load configuration from files, environment and explicit overrides.

    Args:
        extra_path: Additional YAML file consulted after config.d but before
            environment variables (``--config``).
        overrides: Dict merged on top of everything else (used by tests and by
            the ``--set key=value`` style debugging flags).
        config_dir: Overlay directory (default :data:`CONFIG_DIR`); tests point
            this at a temporary directory.

    Returns:
        A fully populated :class:`Config`.
    """

    merged: Dict[str, Any] = {}
    merged = _deep_merge(merged, _load_yaml(SYSTEM_CONFIG_PATH))
    merged = _deep_merge(merged, _load_yaml(LOCAL_CONFIG_PATH))
    for fragment in config_fragments(config_dir):
        merged = _deep_merge(merged, _load_yaml(str(fragment)))
    if extra_path:
        merged = _deep_merge(merged, _load_yaml(extra_path))
    merged = _deep_merge(merged, _env_overrides())
    if overrides:
        merged = _deep_merge(merged, overrides)
    return _build(merged)


def default_config() -> Config:
    """Return the pristine default configuration."""

    return Config()
