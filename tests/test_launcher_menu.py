"""Launcher menu regression: a shell must hand control back to the menu.

The live failure: choosing "4" in the post-run menu ended the launcher with
``exit 0``.  ``/root/.bash_profile`` had *exec*'d the launcher, so the whole
login session died, agetty autologged in again, saw ``/run/raidforge.done``
and dropped to a plain root shell - and ``exit`` there repeated that forever.
The four-item menu could never be reached again.

This test drives the real script on a pty with a stub ``venv/bin/python``:

* shell -> ``exit`` -> the menu is printed again;
* restart app -> the stub runs a second time;
* EOF at the prompt -> clean exit instead of a redraw spin.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

if os.name != "posix":
    # pty/termios do not exist on Windows; skip before importing them.
    pytest.skip("needs a POSIX pty", allow_module_level=True)

import pty  # noqa: E402
import select  # noqa: E402
import signal  # noqa: E402
import time  # noqa: E402

pytestmark = pytest.mark.skipif(
    not Path("/bin/bash").exists(),
    reason="needs /bin/bash for the menu shell",
)

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "config" / "includes.chroot" / "usr" / "local" / "sbin" / "raidforge-tui"
STUB_MARK = "STUB-TUI-RAN"
MENU_MARK = b"raidforge session finished"


def _sandbox(tmp_path: Path):
    """Create a fake app root whose python stub just exits 0."""

    app = tmp_path / "app"
    bindir = app / "venv" / "bin"
    bindir.mkdir(parents=True)
    python = bindir / "python"
    python.write_text(f"#!/bin/sh\necho {STUB_MARK}\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    return app, tmp_path / "raidforge.done"


def _spawn(app: Path, done: Path):
    env = {
        "TERM": "linux",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(app.parent),
        "LANG": "C.UTF-8",
        "RAIDFORGE_APP_ROOT": str(app),
        "RAIDFORGE_DONE_FLAG": str(done),
        "RAIDFORGE_EXIT_MENU": "on",
    }
    pid, master = pty.fork()
    if pid == 0:  # pragma: no cover - child
        try:
            os.execve("/bin/sh", ["/bin/sh", str(LAUNCHER)], env)
        finally:
            os._exit(127)
    return pid, master


def _drain(master: int, timeout: float, needle: bytes | None = None) -> bytes:
    """Read until ``needle`` shows up (or timeout); return what arrived."""

    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if needle is not None and needle in buf:
            return buf
        if needle is None:
            return buf
    return buf


def _wait_choice(master: int, timeout: float = 10.0) -> bytes:
    return _drain(master, timeout, b"choice:")


def _finish(pid: int, master: int, output: bytes) -> int:
    """Wait for the launcher to exit on its own; only then tear the pty down."""

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            try:
                os.close(master)
            except OSError:
                pass
            return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        time.sleep(0.05)
    try:
        os.close(master)
    except OSError:
        pass
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    return -1


def test_shell_exit_returns_to_menu(tmp_path):
    app, done = _sandbox(tmp_path)
    pid, master = _spawn(app, done)
    seen = b""
    try:
        seen += _wait_choice(master)
        assert MENU_MARK in seen, f"menu never appeared: {seen!r}"
        assert STUB_MARK.encode() in seen, f"stub app never ran: {seen!r}"

        # 4 -> interactive bash; "exit" must bring the menu back.
        os.write(master, b"4\n")
        time.sleep(0.6)
        seen += _drain(master, 1.0)
        os.write(master, b"exit\n")
        seen += _wait_choice(master)
        assert seen.count(MENU_MARK) >= 2, f"menu did not return after shell exit: {seen!r}"

        # 3 -> restart the app must run the stub a second time, then redraw.
        os.write(master, b"3\n")
        seen += _wait_choice(master)
        assert seen.count(STUB_MARK.encode()) >= 2, f"restart did not re-run app: {seen!r}"
        assert seen.count(MENU_MARK) >= 3, f"menu did not return after restart: {seen!r}"

        # Ctrl-D at the prompt is EOF: leave instead of spinning on a dead tty.
        os.write(master, b"\x04")
        code = _finish(pid, master, seen)
        assert code == 0, f"launcher exit code {code}, output {seen!r}"
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_eof_at_menu_exits_cleanly(tmp_path):
    """No tty input must end the launcher, not redraw the menu forever."""

    app, done = _sandbox(tmp_path)
    pid, master = _spawn(app, done)
    seen = b""
    try:
        seen += _wait_choice(master)
        os.write(master, b"\x04")
        code = _finish(pid, master, seen)
        assert code == 0, f"EOF did not exit cleanly: {seen!r}"
        assert seen.count(MENU_MARK) == 1, f"menu spun on EOF: {seen!r}"
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
