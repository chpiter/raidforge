"""Process, filesystem and hashing helpers.

Every ``subprocess`` invocation in this project MUST go through
:func:`run_cmd`; that single choke point is what makes the evidence log
trustworthy and the unit tests easy to fake.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

Command = Union[Sequence[str], str]

# Return codes reserved by :func:`run_cmd` for conditions where the OS never
# produced a real exit status.
RC_NOT_FOUND = 127
RC_TIMEOUT = 124
RC_EXCEPTION = 126


@dataclass
class CmdResult:
    """Uniform result tuple for every external command execution.

    Returned instead of a bare tuple so call sites can use
    ``res.return_code`` / ``res.stdout`` without remembering an ordering.
    """

    return_code: int
    stdout: str
    stderr: str
    duration: float
    argv: List[str]
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """True when the process exited with status 0."""

        return self.return_code == 0

    def as_tuple(self) -> Tuple[int, str, str, float]:
        """Return the historical ``(rc, stdout, stderr, duration)`` tuple."""

        return self.return_code, self.stdout, self.stderr, self.duration


def run_cmd(
    cmd: Command,
    timeout: float = 60.0,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
    stdin_data: Optional[str] = None,
    shell: bool = False,
) -> CmdResult:
    """Execute a command and return code / stdout / stderr / elapsed seconds.

    Args:
        cmd: Either an argv sequence (preferred) or a shell string when
            ``shell=True``.
        timeout: Wall clock limit in seconds.  A timeout is reported, never
            raised, so callers keep control of the session.
        env: Extra environment merged on top of :data:`os.environ`.
        cwd: Optional working directory.
        stdin_data: Text piped to the child's stdin.
        shell: Run through ``/bin/sh`` (only for trusted, static strings).

    Returns:
        A :class:`CmdResult`.  Non-true return codes never raise: missing
        binaries map to :data:`RC_NOT_FOUND`, timeouts to :data:`RC_TIMEOUT`.
    """

    argv = cmd if isinstance(cmd, list) else list(cmd) if not isinstance(cmd, str) else [cmd]
    if isinstance(cmd, str):
        argv = cmd.split() if not shell else argv
    display = argv if isinstance(argv, list) else list(argv)

    child_env = os.environ.copy()
    if env:
        child_env.update(env)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            display,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env,
            cwd=cwd,
            shell=shell,
            input=stdin_data,
            check=False,
        )
        return CmdResult(
            return_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration=round(time.monotonic() - started, 3),
            argv=list(display),
        )
    except FileNotFoundError as exc:
        return CmdResult(
            return_code=RC_NOT_FOUND,
            stdout="",
            stderr=f"command not found: {exc}",
            duration=round(time.monotonic() - started, 3),
            argv=list(display),
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return CmdResult(
            return_code=RC_TIMEOUT,
            stdout=out,
            stderr=f"{err}\ntimeout after {timeout}s".strip(),
            duration=round(time.monotonic() - started, 3),
            argv=list(display),
            timed_out=True,
        )
    except OSError as exc:  # pragma: no cover - defensive
        return CmdResult(
            return_code=RC_EXCEPTION,
            stdout="",
            stderr=f"os error: {exc}",
            duration=round(time.monotonic() - started, 3),
            argv=list(display),
        )


def which(binary: str) -> Optional[str]:
    """Return the absolute path of ``binary`` or ``None``."""

    return shutil.which(binary)


def sha256_file(path: Union[str, Path], chunk_size: int = 1 << 20) -> str:
    """Compute the SHA-256 hex digest of a file."""

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dir(path: Union[str, Path]) -> Path:
    """Create ``path`` (parents included) and return it as a :class:`Path`."""

    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def tail_lines(text: str, limit: int = 40) -> str:
    """Keep only the last ``limit`` lines of a blob for log readability."""

    lines = (text or "").splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[-limit:])


def parse_decimal_size(value: str) -> int:
    """Parse a vendor size string such as ``1.818 TB`` into bytes.

    Handles both SI suffixes (``TB``/``GB``) and bare 512-byte block counts
    used by some ``ssacli`` output variants.

    Args:
        value: Raw size cell from a vendor tool.

    Returns:
        Size in bytes, or ``0`` when nothing sensible can be parsed.
    """

    if value is None:
        return 0
    text = str(value).strip().upper().replace(",", "")
    if not text:
        return 0

    multipliers = {
        "B": 1,
        "KB": 10**3,
        "MB": 10**6,
        "GB": 10**9,
        "TB": 10**12,
        "PB": 10**15,
        "BYTES": 1,
    }
    for suffix, factor in sorted(multipliers.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            try:
                return int(float(number) * factor)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def flatten_kv(obj: Any, prefix: str = "") -> Iterable[Tuple[str, Any]]:
    """Yield ``(dotted.path, value)`` pairs for every leaf of a nested dict."""

    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from flatten_kv(value, path)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from flatten_kv(value, f"{prefix}[{index}]")
    else:
        yield prefix, obj
