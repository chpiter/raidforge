#!/usr/bin/env python3
"""L4: boot an ISO in qemu, drive the TUI, and prove the exit menu works.

There is no KVM on the build host, so this runs under TCG and takes a few
minutes - that is why it is a milestone check, not the inner loop (that is
``tools/check.sh`` and ``tools/chroot_tty.py``).

The pass criterion is deliberately physical: after the TUI exits and the menu
appears, the script presses ``1`` (power off) and waits for the **qemu process
to end**.  If the VM powers off, then the quit path, the evidence finalisation,
the console reset and the menu all worked.  No OCR, no pixel comparison of text.

    python3 tools/qemu_drive.py --iso raidforge-1.0.0-20261102-test.iso
    python3 tools/qemu_drive.py --iso ... --keep-running   # leave the VM up
    python3 tools/qemu_drive.py --iso ... --settings-ip 10.255.255.1
        # also prove that a failed SSH probe does not freeze the Settings screen
    python3 tools/qemu_drive.py --iso ... --erase-rounds 2
        # two erase runs in one boot; the session must contain one DOCX report

Every step writes ``NN-step.png`` into ``artifacts/qemu/`` so a human can review
what the machine actually showed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def log(message: str) -> None:
    print(f"[qemu] {message}", flush=True)


def die(message: str) -> "None":
    print(f"[qemu] FAIL {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# monitor socket
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        for _ in range(30):
            try:
                self.sock.connect(str(path))
                break
            except OSError:
                time.sleep(0.5)
        else:
            die(f"cannot connect to the qemu monitor at {path}")
        self._drain(0.3)

    def _drain(self, timeout: float) -> str:
        self.sock.settimeout(timeout)
        chunks = []
        while True:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data.decode("utf-8", "replace"))
        return "".join(chunks)

    def command(self, line: str, wait: float = 0.4) -> str:
        self.sock.sendall(line.encode() + b"\n")
        time.sleep(wait)
        return self._drain(wait)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# screenshots
# ---------------------------------------------------------------------------
def _read_ppm(path: Path):
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        die(f"{path} is not a P6 PPM")
    pos, fields = 2, []
    while len(fields) < 3:
        while data[pos : pos + 1].isspace():
            pos += 1
        start = pos
        while not data[pos : pos + 1].isspace():
            pos += 1
        fields.append(int(data[start:pos]))
    pos += 1
    width, height, _ = fields
    return width, height, data[pos : pos + width * height * 3]


def _write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    raw = b"".join(b"\x00" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def screendump(monitor: Monitor, out_dir: Path, name: str) -> tuple[str, int]:
    """Capture the screen, write a PNG, and return (digest, lit sample count).

    ``lit`` counts non-zero bytes in the sampled RGB payload.  An all-black
    frame is what the console shows while the kernel switches modes or while
    squashfs is still loading; treating it as "the UI has settled" makes the
    driver send keystrokes into a half-started console.
    """

    ppm = out_dir / f"{name}.ppm"
    monitor.command(f"screendump {ppm}", wait=0.8)
    for _ in range(20):
        if ppm.exists() and ppm.stat().st_size > 1024:
            break
        time.sleep(0.2)
    else:
        die(f"screendump produced nothing for {name}")

    width, height, rgb = _read_ppm(ppm)
    _write_png(out_dir / f"{name}.png", width, height, rgb)
    digest = hashlib.sha256(rgb).hexdigest()
    ppm.unlink(missing_ok=True)
    return digest, sum(1 for byte in rgb[::97] if byte)


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def wait_for_stable_screen(monitor: Monitor, out_dir: Path, timeout: float, interval: float,
                           prefix: str = "00-boot", blink: bool = False) -> str:
    """Wait until three consecutive frames are identical.

    Never send keys before this: any keystroke during the boot-menu countdown
    stops the countdown and leaves the VM sitting on the menu, which looks
    exactly like "the TUI never started".

    With ``blink=True`` the rule relaxes to "the last three frames span at
    most two distinct digests": the shell menu shows a blinking hardware
    cursor at the prompt, so consecutive captures alternate between the two
    cursor states forever and a strict equality test never settles.
    """

    deadline = time.monotonic() + timeout
    recent: list[str] = []
    index = 0
    while time.monotonic() < deadline:
        index += 1
        digest, lit = screendump(monitor, out_dir, f"{prefix}-{index:02d}")
        if lit < 50:
            # All-black frame: kernel mode switch or squashfs still loading.
            # Reset the streak so this cannot masquerade as a settled screen.
            recent = []
            time.sleep(interval)
            continue
        recent = (recent + [digest])[-3:]
        if blink:
            if len(recent) >= 3 and len(set(recent)) <= 2:
                log(f"screen settled (blink-aware) after {index} captures")
                return digest
        elif len(recent) >= 3 and recent[-1] == recent[-2] == recent[-3]:
            log(f"screen stable after {index} captures")
            return digest
        time.sleep(interval)
    die(f"the screen never settled within {timeout:.0f}s - the VM is stuck")


def press(monitor: Monitor, out_dir: Path, keys: str, wait: float, expect_change: bool = True) -> str:
    before, _ = screendump(monitor, out_dir, "_before")
    for key in keys.split():
        monitor.command(f"sendkey {key}", wait=wait)
    after, _ = screendump(monitor, out_dir, "_after")
    if expect_change and before == after:
        die(f"keys {keys!r} changed nothing on screen "
            f"(the disk may be unselectable - see {out_dir})")
    return after


def run(args) -> int:
    iso = Path(args.iso).resolve()
    if not iso.exists():
        die(f"ISO not found: {iso}")

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    disk = out_dir / "fake-disk.img"
    if not disk.exists() or disk.stat().st_size < 1 << 30:
        with open(disk, "wb") as handle:
            handle.truncate(2 << 30)  # sparse: no real 2GB used

    sock = out_dir / "monitor.sock"
    sock.unlink(missing_ok=True)
    serial = out_dir / "serial.log"

    cmd = [
        "qemu-system-x86_64", "-m", "2048", "-smp", "2",
        "-accel", "tcg,thread=multi",
        "-cdrom", str(iso), "-boot", "d",
        "-vga", "std", "-display", "none", "-no-reboot", "-snapshot",
        "-monitor", f"unix:{sock},server,nowait",
        "-serial", f"file:{serial}",
        # An NVMe namespace, not a SCSI disk: qemu's scsi-hd answers smartctl
        # with "SMART unsupported", which the app correctly classifies as
        # UNKNOWN medium and refuses to select - leaving the confirm dialog
        # unreachable.  nvme-cli does enumerate the emulated namespace, so the
        # NVMe code path (and its selection/dry-run) is exercised end to end.
        "-drive", f"file={disk},format=raw,if=none,id=d1",
        "-device", "nvme,serial=ERASE01,drive=d1",
        "-netdev", "user,id=n0", "-device", "virtio-net-pci,netdev=n0",
    ]
    log("starting qemu (TCG, no KVM)")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    try:
        monitor = Monitor(sock)
        log(f"waiting for the TUI (up to {args.wait_boot:.0f}s)")
        wait_for_stable_screen(monitor, out_dir, args.wait_boot, args.interval)
        screendump(monitor, out_dir, "01-dashboard")

        if args.settings_ip:
            # Exercise the one screen with a background network probe: an
            # unreachable IP must not freeze the console, and saving must show
            # a result (the screen digest changes after Enter).
            log(f"dashboard -> settings, save {args.settings_ip}")
            press(monitor, out_dir, "s", args.key_wait)
            # The IPv4 field has a blinking cursor, so strict frame equality
            # never settles; the blink-aware rule accepts the two cursor states.
            wait_for_stable_screen(monitor, out_dir, 30.0, 2.0,
                                   prefix="01a-settings", blink=True)
            for _ in range(24):
                monitor.command("sendkey backspace", wait=0.05)
            for char in args.settings_ip:
                monitor.command(f"sendkey {'dot' if char == '.' else char}", wait=0.05)
            time.sleep(1.0)
            before, _ = screendump(monitor, out_dir, "01b-settings-typed")
            monitor.command("sendkey ret", wait=0.5)
            time.sleep(args.settings_wait)
            after, _ = screendump(monitor, out_dir, "01c-settings-probe")
            if before == after:
                die("saving the receiver IP produced no visible result - the probe "
                    "may have blocked the UI")
            monitor.command("sendkey esc", wait=1.0)
            wait_for_stable_screen(monitor, out_dir, 20.0, 2.0, prefix="01d-back-to-dashboard")

        log("dashboard -> disk list")
        press(monitor, out_dir, "ret", args.key_wait)
        screendump(monitor, out_dir, "02-disk-list")

        rounds = max(1, int(args.erase_rounds))
        for round_index in range(1, rounds + 1):
            suffix = "" if round_index == 1 else f"-r{round_index}"
            if round_index == 1:
                log("select the first disk")
                press(monitor, out_dir, "spc", args.key_wait)
            else:
                # Back from the finished progress screen, clear and re-select:
                # this exercises the multi-run path the DOCX report summarises.
                log(f"round {round_index}/{rounds}: back to the disk list")
                press(monitor, out_dir, "esc", args.key_wait)
                wait_for_stable_screen(monitor, out_dir, 30.0, 2.0,
                                       prefix=f"05a-back-to-disks-r{round_index}")
                press(monitor, out_dir, "n", args.key_wait)
                press(monitor, out_dir, "spc", args.key_wait)
            screendump(monitor, out_dir, f"03-selected{suffix}")

            log(f"round {round_index}/{rounds}: open the confirmation dialog")
            press(monitor, out_dir, "ret", args.key_wait)
            screendump(monitor, out_dir, f"04-confirm{suffix}")

            log("type yes and submit")
            for key in ("y", "e", "s"):
                monitor.command(f"sendkey {key}", wait=args.key_slow)
            press(monitor, out_dir, "ret", args.key_wait)
            time.sleep(args.submit_wait)
            screendump(monitor, out_dir, f"05-progress{suffix}")

        log("quit the TUI (Q, twice: first may cancel a live run, second exits)")
        monitor.command("sendkey q", wait=args.key_wait)
        time.sleep(args.exit_wait / 2)
        screendump(monitor, out_dir, "06-after-quit")
        # Press Q again: if the run was still active the first Q only cancelled
        # it (by design), and if it had already finished the first Q exited and
        # this one just hits the menu's "unknown choice" - both are harmless.
        monitor.command("sendkey q", wait=args.key_wait)

        # The app may spend up to ~30s uploading evidence before the menu
        # appears; keystrokes sent meanwhile sit in the tty line buffer with
        # no Enter, so a later "1" would arrive glued to them ("q1") and be
        # rejected by the menu.  Wait for the menu to settle, then flush any
        # partial line with a bare Enter before making the real choice.
        log("waiting for the exit menu to settle")
        wait_for_stable_screen(monitor, out_dir, 120.0, args.interval,
                               prefix="06-menu", blink=True)
        screendump(monitor, out_dir, "06b-after-quit-2")
        monitor.command("sendkey ret", wait=1.0)   # flush "unknown choice" line
        time.sleep(1.0)
        screendump(monitor, out_dir, "06c-menu-flushed")

        log("choose [1] power off - the VM must actually stop")
        monitor.command("sendkey 1", wait=0.5)
        monitor.command("sendkey ret", wait=0.5)

        deadline = time.monotonic() + args.poweroff_wait
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log("VM powered off - exit path, evidence step and menu all worked")
                monitor.close()
                return 0
            time.sleep(1.0)

        screendump(monitor, out_dir, "07-still-running")
        die(f"the VM did not power off within {args.poweroff_wait:.0f}s - see {out_dir}")
    finally:
        if proc.poll() is None and not args.keep_running:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        sock.unlink(missing_ok=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="boot an ISO in qemu and drive the TUI")
    parser.add_argument("--iso", required=True)
    parser.add_argument("--out", default=str(REPO / "artifacts" / "qemu"))
    parser.add_argument("--wait-boot", type=float, default=300.0,
                        help="max seconds to wait for a stable screen (TCG is slow)")
    parser.add_argument("--interval", type=float, default=8.0)
    parser.add_argument("--key-wait", type=float, default=1.2)
    parser.add_argument("--key-slow", type=float, default=0.6)
    parser.add_argument("--submit-wait", type=float, default=12.0)
    parser.add_argument("--erase-rounds", type=int, default=1,
                        help="drive this many erase runs before quitting; round 2+ "
                             "returns to the disk list and clears/reselects the disk "
                             "(the DOCX report must accumulate all of them)")
    parser.add_argument("--exit-wait", type=float, default=120.0,
                        help="wait after Q before checking the exit menu; the "
                             "final upload may still be finishing")
    parser.add_argument("--poweroff-wait", type=float, default=90.0)
    parser.add_argument("--settings-ip", default="",
                        help="save this receiver IP in Settings before the disk flow "
                             "(use an unreachable address to prove a failed probe does "
                             "not freeze the console)")
    parser.add_argument("--settings-wait", type=float, default=8.0)
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()

    if shutil.which("qemu-system-x86_64") is None:
        die("qemu-system-x86_64 not installed (apt install qemu-system-x86)")

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
