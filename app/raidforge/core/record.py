"""Session recording via asciinema (constraint 9, second bullet).

Two modes are supported:

``wrapper`` (default)
    The parent process launches ``asciinema rec <cast> -c <inner argv>`` and
    simply waits.  This guarantees the recording covers the *entire* session,
    including startup and shutdown, and survives operator ``Ctrl-K`` aborts.
    Recursion is prevented with the ``RAIDFORGE_APP_INNER`` marker environment
    variable.

``inline``
    :class:`Recorder` starts asciinema as a background child of the running app
    and terminates it on exit.  Used when an outer wrapper is not available
    (e.g. when developing outside the ISO).
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from ..config import Config
from .utils import which

INNER_ENV_FLAG = "RAIDFORGE_APP_INNER"


def is_inner_process() -> bool:
    """True when we are running inside an asciinema wrapper already."""

    return os.environ.get(INNER_ENV_FLAG) == "1"


def recorder_available(cfg: Config) -> bool:
    """True when the configured recorder binary is on PATH."""

    return bool(which(cfg.evidence.recorder))


def build_wrapper_argv(cast_path: Path, inner_argv: Sequence[str], cfg: Config) -> List[str]:
    """Build the asciinema command that wraps the whole application.

    Args:
        cast_path: Destination ``session.cast``.
        inner_argv: The argv to run *inside* the recording.
        cfg: Loaded configuration (recorder name and extra options).

    Returns:
        argv ready to be handed to :func:`subprocess.Popen`.
    """

    command = " ".join(shlex.quote(part) for part in inner_argv)
    # Options go *before* the positional filename: asciinema 2.x is a clap
    # parser and older builds stopped option parsing at the first positional.
    # ``--overwrite`` keeps a retried session from dying on an existing cast.
    argv: List[str] = [cfg.evidence.recorder, "rec", "--overwrite"]
    argv.extend(cfg.evidence.recorder_options or [])
    argv.extend(["-t", f"erase-session {cast_path.parent.name}", "-c", command, str(cast_path)])
    return argv


def run_recorded(inner_argv: Sequence[str], cast_path: Path, cfg: Config, env_marker: bool = True) -> int:
    """Run ``inner_argv`` inside an asciinema recording and return its exit code.

    Args:
        inner_argv: Command to record, typically ``[python, "-m", "raidforge"]``.
        cast_path: Destination recording file.
        cfg: Loaded configuration.
        env_marker: Export ``RAIDFORGE_APP_INNER=1`` so the child does not recurse.

    Returns:
        The exit status of the recorder process.
    """

    env = os.environ.copy()
    if env_marker:
        env[INNER_ENV_FLAG] = "1"
    argv = build_wrapper_argv(cast_path, inner_argv, cfg)
    process = subprocess.Popen(argv, env=env)
    return process.wait()


class Recorder:
    """Start/stop an asciinema child process from inside the application."""

    def __init__(self, cast_path: Path, cfg: Config) -> None:
        self.cast_path = cast_path
        self.cfg = cfg
        self._proc: Optional[subprocess.Popen] = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, command: Sequence[str]) -> bool:
        """Launch the recorder around ``command``; returns success flag."""

        if not recorder_available(self.cfg):
            return False
        argv = build_wrapper_argv(self.cast_path, command, self.cfg)
        env = os.environ.copy()
        env[INNER_ENV_FLAG] = "1"
        self._proc = subprocess.Popen(argv, env=env, start_new_session=True)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Terminate the recording child, ignoring any exception."""

        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=timeout)
        except Exception:  # pragma: no cover - best effort cleanup
            try:
                self._proc.kill()
            except Exception:
                pass
        finally:
            self._proc = None
