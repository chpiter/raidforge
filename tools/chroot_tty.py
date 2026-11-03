#!/usr/bin/env python3
"""L1: run the real TUI inside the build chroot, in a pty, in ~15 seconds.

Why this exists
---------------
Building an ISO takes 6m45s, of which the application contributes about 5
seconds.  For UI work that is unusable, and a headless Textual test cannot see
the two things that actually break on the appliance: the 16-colour console
palette and the shell wrapper around the app.

This tool mirrors ``app/`` into ``chroot/opt/raidforge`` (keeping the venv and
the site config), then runs ``/usr/local/sbin/raidforge-tui`` inside the chroot on
an 80x25 pty with ``TERM=linux`` - i.e. the same runtime, the same wrapper and
the same colour depth as the real console - and reports what came out.

Usage (on the build host, as root):
    python3 tools/chroot_tty.py                 # mirror + run, prints a report
    python3 tools/chroot_tty.py --no-mirror     # run what is already there
    python3 tools/chroot_tty.py --keys "\\r\\x1bq"  # custom key script
    python3 tools/chroot_tty.py --chroot /other/chroot

Artifacts land in ``artifacts/l1/``: raw bytes, de-escaped text, SGR histogram
and a small JSON summary.  Exit code 0 = the app started, exited on Q, and only
monochrome colours were emitted.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import termios
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "artifacts" / "l1"

#: Colours allowed on the wire; kept in sync with raidforge.ui.theme.
#: Note what is *absent*: 90/100 (bright black, the pair that made the table
#: header invisible on fbcon) and 38/48 (extended colour, i.e. any truecolour
#: escape that slipped through).
ALLOWED_SGR = {"", "0", "1", "22", "7", "27", "39", "49", "30", "37", "40", "47", "97", "107"}

KEEP_IN_APP_ROOT = {"venv", "config.yaml", "config.d"}

CSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def log(message: str) -> None:
    print(f"[l1] {message}", flush=True)


def die(message: str) -> "None":
    print(f"[l1] FAIL {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# mirror the working tree into the chroot
# ---------------------------------------------------------------------------
def mirror(chroot: Path) -> None:
    """Copy app/ into the chroot, replacing everything but the venv + config."""

    target = chroot / "opt" / "raidforge"
    if not (target / "venv" / "bin" / "python").exists():
        die(f"{target}/venv missing - run ./build.sh once to create the chroot")

    for entry in target.iterdir():
        if entry.name in KEEP_IN_APP_ROOT:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()

    shutil.copytree(REPO / "app", target, dirs_exist_ok=True)
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    wrapper = REPO / "config" / "includes.chroot" / "usr" / "local" / "sbin" / "raidforge-tui"
    if wrapper.exists():
        destination = chroot / "usr" / "local" / "sbin" / "raidforge-tui"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wrapper, destination)
        destination.chmod(0o755)

    log(f"mirrored app/ into {target}")


# ---------------------------------------------------------------------------
# run the TUI on a pty
# ---------------------------------------------------------------------------
def run_in_pty(chroot: Path, keys: str, cols: int, rows: int, timeout: float, exit_menu: bool):
    child_env = {
        "TERM": "linux",
        "TEXTUAL_COLOR_SYSTEM": "standard",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "RAIDFORGE_EVIDENCE_ROOT": "/tmp/l1-evidence",
        "RAIDFORGE_REPORT_ENABLED": "false",
        "RAIDFORGE_EXIT_MENU": "on" if exit_menu else "off",
    }
    argv = ["chroot", str(chroot), "/usr/bin/env", "-i"]
    argv += [f"{key}={value}" for key, value in child_env.items()]
    argv += ["/usr/local/sbin/raidforge-tui", "--no-record", "--no-upload"]

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    pid = os.fork()
    if pid == 0:  # pragma: no cover - child process
        os.close(master)
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        if slave > 2:
            os.close(slave)
        os.execvp(argv[0], argv)
        os._exit(127)

    os.close(slave)
    chunks: list = []
    started = time.monotonic()
    deadline = started + timeout

    # Give the TUI a moment to paint before touching the keyboard: keys sent at
    # the boot loader menu would stop its countdown instead of reaching the app.
    time.sleep(2.0)
    pending = bytearray(keys.encode("utf-8"))
    next_key = time.monotonic() + 0.6
    exited_at = None

    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if data:
                chunks.append(data)
        if pending and time.monotonic() >= next_key:
            try:
                os.write(master, bytes(pending[:1]))
            except OSError:
                pending.clear()
            else:
                del pending[:1]
                next_key = time.monotonic() + 0.35
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            exited_at = time.monotonic()
            break

    if exited_at is None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
        exit_code = None
    else:
        exit_code = os.waitstatus_to_exitcode(status)

    os.close(master)
    raw = b"".join(chunks)
    return raw, exit_code, (exited_at - started) if exited_at else None


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------
PURE = {(0, 0, 0), (255, 255, 255)}


def _walk_sgr(params: str):
    """Yield the "problems" in one SGR parameter list.

    Extended colours (``38;2;r;g;b`` / ``48;2;...``) are tolerated **only** when
    the payload is pure black or pure white: Rich emulates ``reverse`` by
    swapping the two colours, so a reversed cell legitimately carries an
    explicit rgb triple.  Anything else - a brand colour, bright black, an
    8-bit palette index - is a leak.
    """

    tokens = [int(token) if token.isdigit() else -1
              for token in params.split(";") if token != ""]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in (38, 48):
            form = tokens[index + 1] if index + 1 < len(tokens) else None
            if form == 2 and index + 4 < len(tokens):
                payload = tuple(tokens[index + 2:index + 5])
                if payload not in PURE:
                    yield f"extended colour {payload}"
                index += 5
                continue
            yield f"extended colour form {form}"
            index += 2
            continue
        if token not in {int(value) for value in ALLOWED_SGR if value}:
            yield str(token)
        index += 1


def analyse(raw: bytes):
    text = raw.decode("utf-8", "replace")
    plain = CSI_RE.sub("", text)

    histogram: Counter = Counter()
    offenders: Counter = Counter()
    for params in SGR_RE.findall(text):
        histogram[params or "0"] += 1
        for problem in _walk_sgr(params):
            offenders[problem] += 1
    return plain, histogram, offenders


def main() -> int:
    parser = argparse.ArgumentParser(description="run the TUI inside the build chroot")
    parser.add_argument("--chroot", default=str(REPO / "chroot"))
    parser.add_argument("--no-mirror", action="store_true", help="skip the file copy")
    parser.add_argument("--keys", default="\\r\\x1bq", help="keystrokes, Python escapes")
    parser.add_argument("--cols", type=int, default=80)
    parser.add_argument("--rows", type=int, default=25)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--with-menu", action="store_true", help="expect the post-exit menu")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    chroot = Path(args.chroot).resolve()
    if not (chroot / "opt" / "raidforge" / "venv" / "bin" / "python").exists():
        die(f"{chroot}/opt/raidforge/venv missing - run ./build.sh once")

    if not args.no_mirror:
        mirror(chroot)

    keys = args.keys.encode().decode("unicode_escape") if "\\" in args.keys else args.keys
    raw, exit_code, elapsed = run_in_pty(
        chroot, keys, args.cols, args.rows, args.timeout, args.with_menu
    )

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "raw.bin").write_bytes(raw)
    plain, histogram, offenders = analyse(raw)
    (ARTIFACTS / "screen.txt").write_text(plain, encoding="utf-8")
    (ARTIFACTS / "sgr.txt").write_text(
        "\n".join(f"{token:>5}  x{count}" for token, count in sorted(histogram.items())),
        encoding="utf-8",
    )

    expectations = ["raidforge", "Server SN"]
    if args.with_menu:
        expectations.append("raidforge session finished")
    missing = [needle for needle in expectations if needle not in plain]

    summary = {
        "exit_code": exit_code,
        "seconds_to_exit": None if elapsed is None else round(elapsed, 2),
        "bytes": len(raw),
        "offending_sgr": dict(offenders),
        "missing_markers": missing,
        "artifacts": str(ARTIFACTS),
    }
    (ARTIFACTS / "meta.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not args.quiet:
        log(f"{len(raw)} bytes, exit={exit_code}, exit after {summary['seconds_to_exit']}s")
        log(f"sgr histogram: {dict(sorted(histogram.items()))}")
        if missing:
            log(f"missing markers: {missing}")

    if exit_code is None:
        die("the TUI did not exit in time (this is the 'frozen console' bug)")

    if offenders:
        die(f"non-monochrome SGR reached the terminal: {dict(offenders)}")

    if exit_code != 0:
        die(f"unexpected exit code {exit_code}")

    if missing:
        die(f"expected text missing from the screen: {missing}")

    log(f"OK - artifacts in {ARTIFACTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
