"""Evidence upload: selected transport first, the other second, pending last.

Constraint 10 summary:

* wait up to ``report.wait_for_route_seconds`` (default 30) for a usable IPv4
  network (default route *or* any global address) before attempting anything;
* try the operator-selected transport first (``report.transport``: SSH or HTTP);
* on any failure, try the other transport;
* if both fail, copy the whole session directory into ``pending/``.  On the
  next boot ``retry_pending()`` scans that directory and retries every entry.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import Config, normalize_transport
from .utils import RC_NOT_FOUND, ensure_dir, run_cmd

log = logging.getLogger(__name__)


@dataclass
class UploadResult:
    """Uniform outcome description for every upload attempt."""

    channel: str
    ok: bool
    message: str
    detail: str = ""
    queued: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the evidence log."""

        return {
            "channel": self.channel,
            "ok": self.ok,
            "message": self.message,
            "detail": self.detail[-2000:],
            "queued": self.queued,
        }


# --------------------------------------------------------------------------
# network readiness
# --------------------------------------------------------------------------
def _remaining(deadline: Optional[float]) -> Optional[float]:
    """Seconds left before ``deadline`` (``None`` means "no deadline")."""

    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


# --------------------------------------------------------------------------
# endpoint selection
# --------------------------------------------------------------------------
def http_url_for(host: str, port: int, scheme: str = "http", path: str = "") -> str:
    """Build an HTTP(S) base URL from the editable receiver fields.

    Args:
        host: Receiver IPv4/hostname; empty disables HTTP.
        port: TCP port (already normalised by :class:`ReportConfig`).
        scheme: ``http`` or ``https``; anything else falls back to ``http``.
        path: Optional path prefix preserved from an explicit base URL.

    Returns:
        URL without a trailing slash, or ``""`` when no host is configured.
    """

    host = str(host or "").strip()
    if not host:
        return ""
    scheme = str(scheme or "http").strip().lower()
    if scheme not in ("http", "https"):
        scheme = "http"
    suffix = str(path or "").strip()
    if suffix and not suffix.startswith("/"):
        suffix = "/" + suffix
    return f"{scheme}://{host}:{int(port)}{suffix}".rstrip("/")


def _normalise_base(value: str) -> str:
    base = str(value or "").strip()
    if base and "://" not in base:
        base = "http://" + base
    return base.rstrip("/")


def _legacy_derived_base(report) -> str:
    """The value the old Settings page wrote on every save."""

    return http_url_for(report.ssh_host, report.http_port)


def webplane_url(cfg: Config) -> str:
    """Resolve the control-plane base URL (scheme://host:port, no api prefix)."""

    webplane = getattr(cfg, "webplane", None)
    if webplane is not None and str(getattr(webplane, "host", "") or "").strip():
        scheme = str(getattr(webplane, "scheme", "http") or "http")
        return http_url_for(webplane.host, webplane.port, scheme)
    try:
        from ..agent.config import DEFAULT_SERVER_URL, AgentConfig

        return _normalise_base(AgentConfig.load().server_url or DEFAULT_SERVER_URL)
    except Exception:  # pragma: no cover - agent config is always importable
        return ""


def http_endpoint_for(report, webplane: str = "") -> str:
    """Pure endpoint resolver used by the settings preview and tests."""

    base = _normalise_base(getattr(report, "http_base_url", ""))
    if base and base != _legacy_derived_base(report):
        return base
    host = str(getattr(report, "http_host", "") or "").strip()
    if host:
        return http_url_for(host, report.http_port)
    if normalize_transport(getattr(report, "transport", "ssh")) == "http":
        return _normalise_base(webplane)
    return _legacy_derived_base(report)


def http_endpoint(cfg: Config) -> str:
    """Return the effective HTTP evidence base URL.

    Priority: an explicit ``http_base_url`` (unless it is the legacy value the
    old Settings page derived from ``ssh_host``), then ``http_host``, then the
    webplane endpoint when HTTP is the preferred channel, then the historical
    ``ssh_host:http_port`` fallback.
    """

    report = cfg.report
    base = _normalise_base(report.http_base_url)
    if base and base != _legacy_derived_base(report):
        return base
    host = str(report.http_host or "").strip()
    if host:
        return http_url_for(host, report.http_port)
    if normalize_transport(report.transport) == "http":
        return _normalise_base(webplane_url(cfg))
    return _legacy_derived_base(report)


def ordered_transports(cfg: Config) -> List[str]:
    """Return ``[preferred, fallback]`` upload channel names."""

    primary = normalize_transport(cfg.report.transport)
    return [primary, "http" if primary == "ssh" else "ssh"]


def global_ipv4_addresses() -> List[Tuple[str, str]]:
    """Return ``[(interface, address/prefix)]`` for every non-loopback lease.

    "The network is up" is defined by the operator's requirement: as long as
    *one* NIC obtained an IPv4 address over DHCP, the appliance is reachable.
    ``scope global`` already hides loopback and 169.254/16 link-local addresses;
    the 127. check is belt and braces for odd iproute2 builds.

    All subprocesses go through :func:`run_cmd` (constraint 13).
    """

    result = run_cmd(["ip", "-o", "-4", "addr", "show", "scope", "global"], timeout=5)
    if not result.ok:
        return []

    found: List[Tuple[str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or "inet" not in fields:
            continue
        # "2: eno1    inet 192.168.40.50/24 brd ... scope global eno1"
        iface = fields[1].rstrip(":")
        address = fields[fields.index("inet") + 1]
        if address.startswith("127."):
            continue
        found.append((iface, address))
    return found


def network_summary() -> str:
    """Short ``"iface address"`` for the status bar / dashboard ("" if down)."""

    leases = global_ipv4_addresses()
    if not leases:
        return ""
    iface, address = leases[0]
    return f"{iface} {address.split('/', 1)[0]}"


def wait_for_default_route(timeout: int = 30, interval: float = 1.0) -> Tuple[bool, float]:
    """Wait until the appliance has a usable IPv4 network.

    Readiness is either a default route *or* a global IPv4 address on any NIC.
    The second condition matters on isolated benches where the collector sits
    in the same L2 segment and DHCP hands out an address but no gateway.

    Args:
        timeout: Maximum seconds to wait (constraint 10: 30).
        interval: Poll period.

    Returns:
        ``(ready, seconds_waited)``.
    """

    deadline = time.monotonic() + max(0, timeout)
    started = time.monotonic()
    while True:
        result = run_cmd(["ip", "-o", "-4", "route", "show", "default"], timeout=5)
        if result.ok and "default" in result.stdout:
            return True, round(time.monotonic() - started, 2)
        if global_ipv4_addresses():
            return True, round(time.monotonic() - started, 2)
        if time.monotonic() >= deadline:
            return False, round(time.monotonic() - started, 2)
        time.sleep(interval)


# --------------------------------------------------------------------------
# primary channel: rsync over SSH
# --------------------------------------------------------------------------
def _ssh_command(cfg: Config) -> str:
    parts = ["ssh", "-p", str(cfg.report.ssh_port), "-i", cfg.report.ssh_key_path]
    parts.extend(cfg.report.ssh_extra_opts or [])
    return " ".join(parts)


def _ssh_argv(cfg: Config) -> List[str]:
    """Same parameters as :func:`_ssh_command` but as an argv list.

    ``run_cmd`` is called without a shell, so the settings probe must not reuse
    the quoted string form (paths with spaces would be split).
    """

    argv = ["ssh", "-p", str(cfg.report.ssh_port), "-i", cfg.report.ssh_key_path]
    argv.extend(cfg.report.ssh_extra_opts or [])
    return argv


@dataclass
class ProbeCheck:
    """Outcome of a receiver reachability probe shown on the settings page."""

    ok: bool
    message: str
    detail: str = ""


#: Historical name kept so existing call sites and tests keep working.
SshCheck = ProbeCheck


def check_ssh(cfg: Config, timeout: float = 5.0) -> SshCheck:
    """Probe the receiver: key login plus remote root writability.

    Deliberately *not* an rsync dry-run - this runs every time the operator
    saves an IP and must return in seconds.  It proves the three things the
    operator can fix from the TUI (IP reachable, key accepted, remote root
    writable) and nothing more; the collector still needs ``rsync`` installed,
    which is documented as a deployment requirement rather than probed here.

    The short ``ConnectTimeout`` is added only here, never to the rsync
    ``-e`` command: a slow link must not truncate a real evidence upload.
    """

    report = cfg.report
    if not report.ssh_host:
        return SshCheck(False, "no receiver IP configured")
    if report.ssh_key_path and not Path(report.ssh_key_path).exists():
        return SshCheck(False, f"ssh key missing: {report.ssh_key_path}")
    if not shutil.which("rsync"):
        return SshCheck(False, "rsync binary not available")

    # shlex.quote keeps a path with spaces or metacharacters from becoming a
    # second shell command on the receiver.
    remote_root = shlex.quote(report.ssh_remote_root)
    remote_cmd = f"test -d {remote_root} && test -w {remote_root}"
    argv = _ssh_argv(cfg) + [
        "-o", f"ConnectTimeout={int(timeout)}",
        "-o", "ConnectionAttempts=1",
        f"{report.ssh_user}@{report.ssh_host}",
        remote_cmd,
    ]
    outcome = run_cmd(argv, timeout=timeout + 5)
    detail = (outcome.stderr or outcome.stdout or "").strip()[-2000:]

    if outcome.ok:
        return SshCheck(
            True,
            f"{report.ssh_user}@{report.ssh_host}:{report.ssh_port} reachable, remote root writable",
            detail,
        )
    if outcome.return_code == 255:
        first = next(
            (line.strip() for line in (outcome.stderr or "").splitlines() if line.strip()), ""
        )
        return SshCheck(False, f"ssh failed: {first[:120] or 'connection error'}", detail)
    if outcome.return_code == 1:
        return SshCheck(
            False, f"remote root missing or not writable: {report.ssh_remote_root}", detail
        )
    if outcome.return_code == RC_NOT_FOUND:
        return SshCheck(False, "ssh client not available", detail)
    return SshCheck(False, f"ssh check exit {outcome.return_code}", detail)


def check_http(cfg: Config, timeout: float = 5.0, endpoint: str = "") -> ProbeCheck:
    """Probe the HTTP receiver with a read-only ``GET /healthz``.

    Deliberately never POSTs a session: saving an IP must not create an empty
    upload on the collector.  Any failure is returned, never raised.
    """

    endpoint = _normalise_base(endpoint) or http_endpoint(cfg)
    if not endpoint:
        return ProbeCheck(False, "no http receiver configured (set receiver IP or http_base_url)")

    try:
        limit = max(1.0, min(float(timeout), 30.0))
    except (TypeError, ValueError):
        limit = 5.0
    url = endpoint + "/healthz"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=limit) as response:
            status = getattr(response, "status", 200)
            body = response.read().decode("utf-8", "replace")[:500]
            return ProbeCheck(True, f"{endpoint} reachable (HTTP {status})", body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:2000]
        return ProbeCheck(False, f"HTTP {exc.code} from {endpoint}", body)
    except (urllib.error.URLError, OSError) as exc:
        return ProbeCheck(False, f"http failed: {exc}", str(exc)[:2000])
    except ValueError as exc:  # pragma: no cover - malformed explicit base URL
        return ProbeCheck(False, f"invalid http endpoint: {exc}", str(exc)[:2000])


def try_rsync(cfg: Config, session_dir: Path, deadline: Optional[float] = None) -> UploadResult:
    """Copy the session directory to the collector with ``rsync``.

    Args:
        cfg: Loaded configuration (host, user, port, key path).
        session_dir: Local evidence directory to ship.

    Returns:
        :class:`UploadResult`.  Misconfiguration (empty host, missing key) is
        reported as a failure rather than raising.
    """

    report = cfg.report
    if not report.ssh_host:
        return UploadResult("rsync", False, "no ssh_host configured", "")
    if report.ssh_key_path and not Path(report.ssh_key_path).exists():
        return UploadResult("rsync", False, f"ssh key missing: {report.ssh_key_path}", "")
    if not shutil.which("rsync"):
        return UploadResult("rsync", False, "rsync binary not available", "")

    target = f"{report.ssh_user}@{report.ssh_host}:{report.ssh_remote_root.rstrip('/')}/"
    argv = [
        "rsync",
        "-a",
        "--timeout=30",
        "-e",
        _ssh_command(cfg),
        str(session_dir),
        target,
    ]
    budget = _remaining(deadline)
    if budget is not None and budget < 2:
        return UploadResult("rsync", False, "upload budget exhausted", "")
    timeout = max(60.0, float(report.http_timeout) * 3)
    if budget is not None:
        timeout = max(2.0, min(timeout, budget))
    outcome = run_cmd(argv, timeout=timeout)
    if outcome.ok:
        return UploadResult("rsync", True, f"uploaded to {target}", outcome.stdout)
    return UploadResult("rsync", False, f"rsync exit {outcome.return_code}", outcome.stderr)


# --------------------------------------------------------------------------
# secondary channel: HTTP POST
# --------------------------------------------------------------------------
def _pack_session(session_dir: Path, destination: Path) -> Path:
    """Tar+gzip a session directory, preserving relative paths."""

    with tarfile.open(destination, "w:gz") as archive:
        archive.add(session_dir, arcname=session_dir.name)
    return destination


def _multipart_encode(fields: Dict[str, str], filename: str, payload: bytes) -> Tuple[bytes, str]:
    """Build a minimal ``multipart/form-data`` body using only the stdlib."""

    boundary = f"----eraseapp{uuid.uuid4().hex}"
    lines: List[bytes] = []
    for key, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        lines.append(f"{value}\r\n".encode())
    lines.append(f"--{boundary}\r\n".encode())
    lines.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    )
    lines.append(b"Content-Type: application/gzip\r\n\r\n")
    lines.append(payload)
    lines.append(b"\r\n")
    lines.append(f"--{boundary}--\r\n".encode())
    return b"".join(lines), boundary


def try_http(cfg: Config, session_dir: Path, deadline: Optional[float] = None) -> UploadResult:
    """POST the compressed session to ``/sessions``.

    Args:
        cfg: Loaded configuration (``report.http_base_url`` or receiver IP).
        session_dir: Local evidence directory to ship.

    Returns:
        :class:`UploadResult`.
    """

    report = cfg.report
    endpoint = http_endpoint(cfg)
    if not endpoint:
        return UploadResult(
            "http", False, "no http receiver configured (set receiver IP or http_base_url)", ""
        )

    budget = _remaining(deadline)
    if budget is not None and budget < 2:
        return UploadResult("http", False, "upload budget exhausted", "")

    url = endpoint + "/sessions"
    with tempfile.TemporaryDirectory(prefix="erase-upload-") as tmp:
        archive = Path(tmp) / f"{session_dir.name}.tar.gz"
        try:
            _pack_session(session_dir, archive)
        except OSError as exc:
            return UploadResult("http", False, f"packing failed: {exc}", "")
        body, boundary = _multipart_encode(
            {"server_sn": session_dir.name.split("_")[0], "session_id": session_dir.name},
            archive.name,
            archive.read_bytes(),
        )
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        timeout = float(report.http_timeout)
        if budget is not None:
            timeout = max(2.0, min(timeout, budget))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", "replace")[:500]
                return UploadResult("http", True, f"HTTP {response.status}", payload)
        except urllib.error.HTTPError as exc:
            return UploadResult("http", False, f"HTTP {exc.code}", exc.read().decode("utf-8", "replace")[:500])
        except (urllib.error.URLError, OSError) as exc:
            return UploadResult("http", False, f"network error: {exc}", "")


# --------------------------------------------------------------------------
# tertiary: pending queue
# --------------------------------------------------------------------------
def enqueue_pending(cfg: Config, session_dir: Path) -> Path:
    """Copy a session directory into ``pending/`` for a later retry."""

    target = Path(cfg.report.pending_root) / session_dir.name
    try:
        if target.resolve() == session_dir.resolve():
            # retry_pending() calls upload_session() on a directory that already
            # lives in pending/; copying it onto itself used to rmtree the source
            # and then fail, destroying the evidence it was meant to preserve.
            log.info("session already in the pending queue: %s", target)
            return target
    except OSError:  # pragma: no cover - resolve() can fail on odd mounts
        pass

    pending = ensure_dir(target)
    for item in session_dir.iterdir():
        destination = pending / item.name
        if item.is_dir():
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)
    (pending / ".queued").write_text(str(time.time()), encoding="utf-8")
    log.warning("session queued for retry: %s", pending)
    return pending


def upload_session(
    cfg: Config,
    session_dir: Path,
    deadline: Optional[float] = None,
    on_status: Optional[Callable[[str], None]] = None,
    queue_on_failure: bool = True,
    skip_route_wait: bool = False,
) -> UploadResult:
    """Run the full upload ladder for one session directory.

    Args:
        cfg: Loaded configuration.
        session_dir: Evidence directory to ship.
        deadline: ``time.monotonic()`` value after which no new channel is
            started.  ``None`` keeps the historical, unbounded behaviour.
        on_status: Called with a one-line human readable status after every
            stage, so the operator sees progress instead of a blank console.
        queue_on_failure: When False, a failed upload is *not* copied into the
            pending queue (used when retrying something that is already there).
        skip_route_wait: Assume the network is up (the dashboard already
            probed it, so waiting again just burns the exit budget).

    Returns:
        The first successful :class:`UploadResult`, or the ``pending`` result.
    """

    def status(message: str) -> None:
        if on_status is not None:
            try:
                on_status(message)
            except Exception:  # pragma: no cover - reporting must never raise
                pass

    if not cfg.report.enabled:
        status("upload disabled by configuration")
        return UploadResult("none", False, "reporting disabled by configuration", "")

    if not skip_route_wait:
        budget = _remaining(deadline)
        wait_for = cfg.report.wait_for_route_seconds
        if budget is not None:
            wait_for = int(max(0, min(wait_for, budget)))
        ready, waited = wait_for_default_route(wait_for)
        status(f"waiting for network ....... {'ok' if ready else 'not ready'} ({waited}s)")
        if not ready:
            if not queue_on_failure:
                return UploadResult("pending", False, f"network not ready within {waited}s", "", queued=True)
            pending = enqueue_pending(cfg, session_dir)
            status(f"queued for retry .......... {pending}")
            return UploadResult("pending", False, f"network not ready within {waited}s", str(pending), queued=True)

    order = ordered_transports(cfg)
    status(f"transport order ........... {order[0]} -> {order[1]}")
    for name in order:
        attempt, label = (
            (try_rsync, "rsync to collector")
            if name == "ssh"
            else (try_http, "http post")
        )
        if _remaining(deadline) is not None and _remaining(deadline) < 2:
            status(f"{label:<26} skipped (budget exhausted)")
            break
        try:
            result = attempt(cfg, session_dir, deadline=deadline)
        except Exception as exc:  # pragma: no cover - never let upload kill the app
            log.exception("upload channel %s crashed", attempt.__name__)
            result = UploadResult(attempt.__name__, False, f"exception: {exc}", "")
        status(f"{label:<26} {'ok' if result.ok else 'FAILED'} ({result.message})")
        if result.ok:
            return result

    if not queue_on_failure:
        return UploadResult("pending", False, "all channels failed (already queued)", str(session_dir), queued=True)

    pending = enqueue_pending(cfg, session_dir)
    status(f"queued for retry .......... {pending}")
    return UploadResult("pending", False, "all channels failed; queued", str(pending), queued=True)


def retry_pending(cfg: Config, max_items: int = 25) -> List[UploadResult]:
    """Retry every queued session, moving successes to ``uploaded/``.

    Args:
        cfg: Loaded configuration.
        max_items: Safety valve so a badly broken collector cannot stall boot.

    Returns:
        One result per attempted queue entry.
    """

    results: List[UploadResult] = []
    root = Path(cfg.report.pending_root)
    if not root.is_dir():
        return results

    for index, pending in enumerate(sorted(root.iterdir())):
        if not pending.is_dir() or index >= max_items:
            continue
        result = upload_session(cfg, pending, queue_on_failure=False)
        if result.ok:
            uploaded = ensure_dir(Path(cfg.report.uploaded_root)) / pending.name
            if uploaded.exists():
                shutil.rmtree(uploaded, ignore_errors=True)
            shutil.move(str(pending), str(uploaded))
        results.append(result)
    return results
