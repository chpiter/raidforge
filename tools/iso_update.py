#!/usr/bin/env python3
"""Classify local changes and run the cheapest safe ISO update.

Layered incremental build:

    NO_ISO       changes that never reach the image (webplane, tests, docs)
    APP_ONLY     app/ sources -> patch the cached chroot + replay xorriso (amd64)
    REFRESH_VENV dependency changes -> build.sh --refresh --venv-reset
    REFRESH      includes / hooks / drivers -> build.sh --refresh
    FULL         auto/, package lists, build.sh -> ./build.sh

The remote build host has no git, and the live-build chroot is the source of
the last squashfs.  This tool keeps a local file manifest as the baseline and
escalates a layer whenever its prerequisites are missing.  Release artefacts
are collected into /root/raidforge/out as -X86.iso / -ARM.iso with .sha256.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
ENV = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}

# Set from --arch for the duration of one target.
ARCH = "amd64"

UP_TO_DATE = "UP_TO_DATE"
NO_ISO = "NO_ISO"
APP_ONLY = "APP_ONLY"
REFRESH_VENV = "REFRESH_VENV"
REFRESH = "REFRESH"
FULL = "FULL"

LAYER_ORDER = {
    UP_TO_DATE: 0,
    NO_ISO: 1,
    APP_ONLY: 2,
    REFRESH_VENV: 3,
    REFRESH: 4,
    FULL: 5,
}
ARCHS = ("amd64", "arm64")
ARCH_MARK = {"amd64": "X86", "arm64": "ARM"}

NO_ISO_PREFIXES = (
    "webplane/",
    "tests/",
    "tools/",
    "artifacts/",
    ".tmp/",
    "doc/",
    ".qoder/",
    ".workbuddy/",
)
NO_ISO_EXACT = {"README.md", "Makefile", ".gitignore"}
IGNORE_PREFIXES = ("UNKNOWN-NOINPUT_",)

KEYS_DIR = "config/includes.chroot/etc/raidforge/keys"

_PENDING_WEBPLANE = False


@dataclass(frozen=True)
class Caps:
    patch: bool
    refresh: bool
    source: str = "remote"


def state_path_for(arch: str) -> Path:
    if arch == "amd64":
        return ROOT / ".tmp" / "iso-state.json"
    return ROOT / ".tmp" / f"iso-state-{arch}.json"


def remote_dir(arch: str) -> str:
    return "/root/raidforge" if arch == "amd64" else f"/srv/raidforge-{arch}"


def is_ignored(path: str) -> bool:
    return path.startswith(IGNORE_PREFIXES)


def die(message: str) -> "None":
    print(f"[iso_update] {message}", file=sys.stderr)
    raise SystemExit(2)


def warn(message: str) -> None:
    print(f"[iso_update] warn: {message}", file=sys.stderr, flush=True)


def log(message: str) -> None:
    print(f"[iso_update] {message}", flush=True)


def run(
    command: List[str],
    *,
    text: bool = True,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=str(ROOT),
        env=ENV,
        capture_output=capture,
        text=text,
        check=check,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        die(f"unsafe ISO name: {name!r}")
    return name


# --------------------------------------------------------------------------
# manifests
# --------------------------------------------------------------------------
def git_paths(args: List[str]) -> List[str]:
    result = run(["git", *args], text=False, check=False)
    if result.returncode != 0:
        die("git failed: " + result.stderr.decode("utf-8", "replace").strip())
    return [item for item in result.stdout.decode("utf-8", "replace").split("\0") if item]


def current_manifest() -> Dict[str, str]:
    manifest: Dict[str, str] = {}
    for relative in git_paths(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    ):
        if is_ignored(relative):
            continue
        path = ROOT / relative
        if path.is_file():
            manifest[relative] = sha256_file(path)
    return manifest


def head_manifest() -> Dict[str, str]:
    result = run(["git", "archive", "HEAD"], text=False)
    manifest: Dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            digest = hashlib.sha256(handle.read()).hexdigest()
            name = member.name[2:] if member.name.startswith("./") else member.name
            if not is_ignored(name):
                manifest[name] = digest
    return manifest


def dirty_paths() -> set:
    paths = set(git_paths(["diff", "--name-only", "-z", "HEAD"]))
    paths.update(git_paths(["ls-files", "--others", "--exclude-standard", "-z"]))
    return paths


def synthetic_baseline() -> Dict[str, str]:
    """Build the tree that produced the current ISO.

    Clean paths use the working tree byte-for-byte (the synced build host saw
    the same bytes); dirty paths use the HEAD object as a sentinel so they are
    reported as changed on the next run.
    """

    current = current_manifest()
    head = head_manifest()
    dirty = dirty_paths()
    baseline = {path: digest for path, digest in current.items() if path not in dirty}
    for path in dirty:
        if path in head:
            baseline[path] = head[path]
    return baseline


def changed_paths(baseline: Dict[str, str], current: Dict[str, str]) -> List[str]:
    paths = set(baseline) | set(current)
    return sorted(path for path in paths if baseline.get(path) != current.get(path))


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------
def is_no_iso(path: str) -> bool:
    return path in NO_ISO_EXACT or path.startswith(NO_ISO_PREFIXES)


def is_app_only(path: str) -> bool:
    if path == "app/run.py":
        return True
    if path.startswith("app/raidforge/"):
        return True
    if path.startswith("app/") and path.endswith(".example"):
        return True
    return False


def is_venv_path(path: str) -> bool:
    if path in ("app/requirements.txt", "app/pyproject.toml"):
        return True
    if path.startswith("vendor/wheels/"):
        return True
    return path.startswith(KEYS_DIR + "/")


def is_refresh_path(path: str) -> bool:
    if path.startswith("config/includes.chroot/"):
        return True
    if path.startswith("config/hooks/"):
        return True
    if path in ("vendor/drivers/catalog.json", "vendor/drivers/firmware-map.json"):
        return True
    if path.startswith(("vendor/drivers/", "vendor/cli/")):
        return True
    return path == "tools/drivers.py"


def layer_for(path: str) -> str:
    if is_venv_path(path):
        return REFRESH_VENV
    if is_refresh_path(path):
        return REFRESH
    if is_app_only(path):
        return APP_ONLY
    # Generated on the build host from the driver catalog; never a source input.
    if path.endswith("/26-drivers.generated.list.chroot"):
        return NO_ISO
    if is_no_iso(path):
        return NO_ISO
    return FULL


def classify(changed: Iterable[str]) -> str:
    paths = list(changed)
    if not paths:
        return UP_TO_DATE
    kind = NO_ISO
    for path in paths:
        layer = layer_for(path)
        if LAYER_ORDER[layer] > LAYER_ORDER[kind]:
            kind = layer
    return kind


def archs_from(value: str) -> List[str]:
    if value == "both":
        return list(ARCHS)
    if value not in ARCHS:
        die(f"unsupported arch: {value}")
    return [value]


def patch_engine_sha() -> str:
    path = ROOT / "tools" / "iso_patch.sh"
    return sha256_file(path) if path.is_file() else ""


def build_command(kind: str, mode: str, profile: str) -> str:
    if mode == "test":
        # --fast is the refresh path with zstd + no boot countdown + -test name.
        command = "./build.sh --fast"
    elif kind == FULL:
        command = "./build.sh"
    else:
        command = "./build.sh --refresh"
    if kind == REFRESH_VENV:
        command += " --venv-reset"
    if profile:
        command += f" --profile {profile}"
    return command


def eta_for(kind: str, arch: str) -> str:
    if kind == APP_ONLY:
        return "~1-2m"
    if arch == "arm64":
        return {"REFRESH_VENV": "~25-45m", REFRESH: "~20-40m", FULL: "~1-2h"}.get(
            kind, "?"
        )
    return {"REFRESH_VENV": "~5m", REFRESH: "~4m", FULL: "~7m"}.get(kind, "?")


def effective_layer(
    kind: str, arch: str, state: Optional[dict], want_mode: str, caps: Caps
) -> Tuple[str, List[str]]:
    """Apply the safety and fallback rules for the requested layer."""
    reasons: List[str] = []
    if kind in (APP_ONLY, REFRESH_VENV, REFRESH):
        prev_mode = ""
        if state:
            prev_mode = (state.get("iso") or {}).get("mode", "") or ""
        if prev_mode and prev_mode != want_mode:
            return FULL, [f"build mode {prev_mode} -> {want_mode}"]
    if kind == APP_ONLY:
        if arch != "amd64":
            kind = REFRESH
            reasons.append("arm64 has no ISO patch path")
        elif not caps.patch:
            kind = REFRESH
            reasons.append("patch prerequisites missing")
    if kind in (REFRESH, REFRESH_VENV) and not caps.refresh:
        return FULL, reasons + ["refresh prerequisites missing"]
    return kind, reasons


def collect_name(src_name: str, arch: str) -> str:
    base = src_name[:-4] if src_name.endswith(".iso") else src_name
    for _ in range(3):
        stripped = False
        for suffix in ("-test", "-arm64", "-X86", "-ARM"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                stripped = True
        if not stripped:
            break
    return f"{base}-{ARCH_MARK[arch]}.iso"


# --------------------------------------------------------------------------
# fingerprints (gitignored payloads the manifest cannot see)
# --------------------------------------------------------------------------
def _hash_group(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(ROOT)).replace("\\", "/").encode("utf-8"))
        digest.update(sha256_file(path).encode("ascii"))
    return digest.hexdigest()


def fingerprint_groups() -> Dict[str, str]:
    groups: Dict[str, List[Path]] = {"drivers": [], "cli": [], "wheels": [], "keys": []}
    for name in ("catalog.json", "firmware-map.json"):
        path = ROOT / "vendor" / "drivers" / name
        if path.is_file():
            groups["drivers"].append(path)
    drivers_dir = ROOT / "vendor" / "drivers" / ARCH
    if drivers_dir.is_dir():
        groups["drivers"].extend(p for p in drivers_dir.rglob("*") if p.is_file())
    cli_dir = ROOT / "vendor" / "cli" / ARCH
    if cli_dir.is_dir():
        groups["cli"].extend(p for p in cli_dir.rglob("*") if p.is_file())
    wheels_dir = ROOT / "vendor" / "wheels" / ARCH
    if wheels_dir.is_dir():
        groups["wheels"].extend(p for p in wheels_dir.rglob("*") if p.is_file())
    elif ARCH == "amd64":
        legacy = ROOT / "vendor" / "wheels"
        if legacy.is_dir():
            groups["wheels"].extend(p for p in legacy.glob("*.whl") if p.is_file())
    keys_dir = ROOT / KEYS_DIR
    if keys_dir.is_dir():
        groups["keys"].extend(p for p in keys_dir.rglob("*") if p.is_file())
    return {name: _hash_group(paths) for name, paths in groups.items()}


def combined_fingerprint(groups: Dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(groups):
        digest.update(name.encode("ascii"))
        digest.update(groups[name].encode("ascii"))
    return digest.hexdigest()


def fingerprint_layer(state: Optional[dict]) -> Optional[str]:
    """Return the layer implied by gitignored payload changes, if any."""
    if not state:
        return None
    current = fingerprint_groups()
    stored = state.get("fingerprints")
    if not stored:
        previous = state.get("fingerprint")
        if previous and previous != combined_fingerprint(current):
            return REFRESH
        return None
    kind: Optional[str] = None
    if stored.get("wheels") != current["wheels"]:
        kind = REFRESH_VENV
    if any(stored.get(name) != current[name] for name in ("drivers", "cli", "keys")):
        if not kind or LAYER_ORDER[REFRESH] > LAYER_ORDER[kind]:
            kind = REFRESH
    return kind


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
def load_state(arch: str) -> Optional[dict]:
    path = state_path_for(arch)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"cannot read {path}: {exc}")
    state["manifest"] = {
        path: digest
        for path, digest in (state.get("manifest") or {}).items()
        if not is_ignored(path)
    }
    return state


def save_state(state: dict) -> None:
    path = state_path_for(ARCH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"state saved: {path}")


def state_from_info(
    manifest: Dict[str, str],
    info: dict,
    *,
    layer: str,
    mode: str = "",
    caps: Optional[Caps] = None,
    profile: str = "",
) -> dict:
    name = info.get("ISO", "")
    groups = fingerprint_groups()
    return {
        "version": 2,
        "arch": ARCH,
        "fingerprints": groups,
        "fingerprint": combined_fingerprint(groups),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "git_head": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "manifest": manifest,
        "layer": layer,
        "iso": {
            "name": name,
            "sha256": info.get("SHA", ""),
            "size": int(info.get("SIZE", "0") or 0),
            "mode": mode or ("test" if name.endswith("-test.iso") else "release"),
        },
        "squashfs_sha256": info.get("SQUASH", ""),
        "build_id": info.get("BUILD_ID", ""),
        "caps": {
            "patch": bool(caps.patch) if caps else False,
            "refresh": bool(caps.refresh) if caps else False,
        },
        "options": {"profile": profile},
        "patch_engine_sha": patch_engine_sha(),
    }


def caps_from_state(state: Optional[dict]) -> Caps:
    stored = (state or {}).get("caps") or {}
    # A recorded successful build implies both caches existed at that moment.
    present = state is not None
    return Caps(
        patch=bool(stored.get("patch", present and ARCH == "amd64")),
        refresh=bool(stored.get("refresh", present)),
        source="state",
    )


# --------------------------------------------------------------------------
# remote plumbing
# --------------------------------------------------------------------------
def rssh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run([sys.executable, str(ROOT / "tools" / "rssh.py"), *args], check=check)


def remote_exec(script: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell script on the build host (amd64) or inside the arm64 VM."""
    if ARCH == "amd64":
        return rssh("exec", script, check=check)
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    inner = f"echo {encoded} | base64 -d | bash"
    return rssh(
        "exec",
        f"cd {remote_dir(ARCH)} && ./tools/arm64_vm.sh run --no-sync \"{inner}\"",
        check=check,
    )


def run_sync() -> None:
    args = ["bash", "tools/sync.sh"]
    if ARCH != "amd64":
        args += ["--arch", ARCH]
    run(args, capture=False)


def ensure_vm() -> None:
    if ARCH != "arm64":
        return
    log("ensuring the arm64 build VM is running")
    rssh(
        "exec",
        "bash /root/raidforge/tools/arm64_vm.sh status "
        "|| bash /root/raidforge/tools/arm64_vm.sh start",
        check=False,
    )


def sync_vm() -> None:
    """rsync the 9p share into the arm64 guest (needed before adopt/inspection)."""
    if ARCH != "arm64":
        return
    rssh("exec", "bash /root/raidforge/tools/arm64_vm.sh sync", check=False)


def probe_caps() -> Caps:
    script = "\n".join(
        [
            "cd /root/raidforge",
            'check() { if [ -e "$1" ]; then echo "OK $1"; else echo "NO $1"; fi; }',
            "check .build/binary_iso",
            "check binary/live/filesystem.squashfs",
            "check binary/boot/grub/grub.cfg",
            "check binary/.disk/mkisofs",
            "check chroot/opt/raidforge/venv/bin/python",
        ]
    )
    result = remote_exec(script, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        print(output, file=sys.stderr)
        return Caps(False, False, "probe-failed")
    found = {
        line.split(" ", 1)[1]
        for line in output.splitlines()
        if line.startswith("OK ")
    }
    refresh = {
        ".build/binary_iso",
        "binary/live/filesystem.squashfs",
        "binary/boot/grub/grub.cfg",
        "chroot/opt/raidforge/venv/bin/python",
    } <= found
    patch = refresh and "binary/.disk/mkisofs" in found
    return Caps(patch=patch, refresh=refresh, source="remote")


def remote_iso_info(name: str = "", *, adopt: bool = False) -> dict:
    select = f"ISO={safe_name(name)}" if name else (
        "ISO=$(ls -t raidforge-*.iso 2>/dev/null | grep -v '\\.prev$' | head -1)"
    )
    script = "\n".join(
        [
            "set -e",
            "cd /root/raidforge",
            select,
            '[ -n "$ISO" ] || { echo "NO_ISO"; exit 3; }',
            '[ -f "$ISO" ] || { echo "MISSING_ISO=$ISO"; exit 3; }',
            '[ -f "$ISO.sha256" ] || { echo "MISSING_SHA=$ISO.sha256"; exit 4; }',
            'sha256sum -c "$ISO.sha256" >/dev/null || { echo "BAD_SHA=$ISO"; exit 5; }',
            'APP_HASH=$(find app -name "*.py" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk "{print \\$1}")',
            'ISO_HASH=$(unsquashfs -cat binary/live/filesystem.squashfs opt/raidforge/BUILD_ID 2>/dev/null | sed -n "2p" | awk "{print \\$1}")',
            'echo "APP_HASH=$APP_HASH"',
            'echo "ISO_HASH=$ISO_HASH"',
            'echo "ISO=$ISO"',
            'echo "SHA=$(sha256sum "$ISO" | awk "{print \\$1}")"',
            'echo "SIZE=$(stat -c%s "$ISO")"',
            'echo "SQUASH=$(sha256sum binary/live/filesystem.squashfs | awk "{print \\$1}")"',
            'echo "BUILD_ID=$ISO_HASH"',
        ]
    )
    result = remote_exec(script, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        print(output, file=sys.stderr)
        die("remote ISO inspection failed")
    info: Dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            info[key.strip()] = value.strip()
    if adopt and info.get("APP_HASH") != info.get("ISO_HASH"):
        print(output, file=sys.stderr)
        die(
            "current ISO does not match the remote app tree; "
            "run --baseline or rebuild before adopting"
        )
    return info


def remote_iso_matches(name: str, sha256: str) -> bool:
    if not name or not sha256:
        return False
    script = "\n".join(
        [
            "cd /root/raidforge",
            f'[ -f "{safe_name(name)}" ] || exit 3',
            f'echo "SHA=$(sha256sum "{safe_name(name)}" | awk "{{print \\$1}}")"',
        ]
    )
    result = rssh("exec", script, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return False
    for line in output.splitlines():
        if line.startswith("SHA="):
            return line.split("=", 1)[1].strip() == sha256
    return False


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
def deploy_webplane() -> None:
    log("deploying webplane")
    run(["bash", "tools/deploy_webplane.sh"], capture=False)


def app_version() -> str:
    text = (APP_DIR / "raidforge" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def output_name(mode: str, override: str) -> str:
    if override:
        name = override.strip()
        if "/" in name or "\\" in name:
            die("--iso must be a bare file name")
        if mode == "test" and name.endswith(".iso") and not name.endswith("-test.iso"):
            name = name[:-4] + "-test.iso"
        return name
    suffix = ""
    if ARCH != "amd64":
        suffix += f"-{ARCH}"
    if mode == "test":
        suffix += "-test"
    return f"raidforge-{app_version()}-{datetime.now().strftime('%Y%m%d')}{suffix}.iso"


def create_app_tar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for item in sorted(APP_DIR.rglob("*")):
            if not item.is_file():
                continue
            if "__pycache__" in item.parts or item.suffix == ".pyc":
                continue
            archive.add(item, arcname=str(item.relative_to(APP_DIR)))


def patch_app(mode: str, name: str, changed: List[str]) -> dict:
    if ARCH != "amd64":
        die("app-only ISO patching is amd64-only; run a refresh build for arm64")
    run_sync()
    tar_path = ROOT / ".tmp" / "iso-app.tar.gz"
    create_app_tar(tar_path)
    log(f"uploading app tarball ({tar_path.stat().st_size} bytes)")
    rssh("exec", "mkdir -p /root/raidforge/.tmp")
    rssh("put", str(ROOT / "tools" / "iso_patch.sh"), "/root/raidforge/tools/iso_patch.sh")
    rssh("put", str(tar_path), "/root/raidforge/.tmp/iso-app.tar.gz")
    command = (
        "cd /root/raidforge && bash tools/iso_patch.sh "
        f"--mode {mode} --app-tar .tmp/iso-app.tar.gz --name {safe_name(name)} --json"
    )
    log(f"patching ISO as {name} ({mode})")
    result = rssh("exec", command, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    marker = output.rfind("@@ISO_PATCH_JSON@@")
    if result.returncode != 0 or marker < 0:
        print(output, file=sys.stderr)
        die("remote ISO patch failed")
    payload = json.loads(output[marker + len("@@ISO_PATCH_JSON@@") :].splitlines()[0])
    print(output[:marker], end="")
    return payload


def clear_stale_mounts() -> None:
    """Drop leftover chroot pseudo-filesystems from an interrupted lb run."""
    script = "\n".join(
        [
            "cd /root/raidforge",
            "pgrep -af '[l]b (build|binary)' >/dev/null && { echo BUSY; exit 1; }",
            "for m in $(mount | awk '/\\/chroot\\/(dev|proc|sys)/ {print $3}'); do",
            '  echo "umount $m"; umount -l "$m" 2>/dev/null || true',
            "done",
            "exit 0",
        ]
    )
    result = remote_exec(script, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    if "BUSY" in output:
        die("another live-build run is active on the build host")
    for line in output.splitlines():
        if line.startswith("umount "):
            log(f"cleared stale mount: {line.split(' ', 1)[1]}")


def run_layer(kind: str, args) -> None:
    command = build_command(kind, args.mode, args.profile)
    log(f"running {kind}: {command}")
    run_sync()
    clear_stale_mounts()
    sync_args = ["bash", "tools/sync.sh"]
    if ARCH != "amd64":
        sync_args += ["--arch", ARCH]
    run(sync_args + ["--run", f"cd /root/raidforge && {command}"], capture=False)


def collect_iso(iso_name: str, *, pull: bool) -> None:
    """Copy the release ISO into /root/raidforge/out under its X86/ARM name."""
    if not iso_name:
        warn("no ISO name recorded - out/ was not updated")
        return
    if ARCH == "arm64" and pull:
        log("copying the arm64 ISO out of the VM")
        result = rssh("exec", "bash /root/raidforge/tools/arm64_vm.sh pull", check=False)
        if result.returncode != 0:
            warn("arm64 ISO pull failed - out/ was not updated")
            return
    src = (
        f"/root/raidforge/{iso_name}"
        if ARCH == "amd64"
        else f"/srv/raidforge-{ARCH}/out/{iso_name}"
    )
    marked = collect_name(iso_name, ARCH)
    script = "\n".join(
        [
            "set -e",
            "OUT=/root/raidforge/out",
            f'SRC="{src}"',
            f'DST="$OUT/{marked}"',
            'mkdir -p "$OUT"',
            '[ -f "$SRC" ] || { echo "MISSING $SRC"; exit 3; }',
            'if [ -f "$DST" ]; then',
            '  A=$(sha256sum "$SRC" | cut -d" " -f1)',
            '  B=$(sha256sum "$DST" | cut -d" " -f1)',
            '  if [ "$A" = "$B" ]; then echo "UNCHANGED $DST"; exit 0; fi',
            '  echo "REPLACING $DST ($B -> $A)"',
            "fi",
            'cp -f "$SRC" "$DST"',
            f'cd "$OUT" && sha256sum "{marked}" > "{marked}.sha256"',
            'echo "COLLECTED $DST"',
        ]
    )
    result = rssh("exec", script, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    for line in output.splitlines():
        if line.startswith(("UNCHANGED", "COLLECTED", "REPLACING", "MISSING")):
            log(line)
    if result.returncode != 0:
        print(output, file=sys.stderr)
        warn(f"could not collect {iso_name} into out/")
    else:
        log(f"out/: {marked}")


def ensure_collected(iso_name: str, mode: str) -> None:
    if mode == "test" or not iso_name:
        return
    collect_iso(iso_name, pull=False)


# --------------------------------------------------------------------------
# flows
# --------------------------------------------------------------------------
def adopt(args) -> None:
    info = remote_iso_info(args.iso, adopt=True)
    log("adopting the current ISO as baseline")
    manifest = synthetic_baseline()
    run_sync()
    sync_vm()
    save_state(
        state_from_info(
            manifest,
            info,
            layer="ADOPT",
            caps=Caps(patch=ARCH == "amd64", refresh=True),
            profile=args.profile,
        )
    )
    log("baseline established; future runs can use the incremental layers")


def try_adopt(args) -> bool:
    try:
        adopt(args)
        return True
    except SystemExit as exc:
        if exc.code == 0:
            raise
        warn("could not adopt the current ISO as a baseline")
        return False


def baseline(args) -> dict:
    run_sync()
    clear_stale_mounts()
    kind = FULL
    command = "./build.sh --fast" if (args.fast or args.mode == "test") else "./build.sh"
    if args.profile:
        command += f" --profile {args.profile}"
    log(f"running a full build: {command}")
    sync_args = ["bash", "tools/sync.sh"]
    if ARCH != "amd64":
        sync_args += ["--arch", ARCH]
    run(sync_args + ["--run", f"cd /root/raidforge && {command}"], capture=False)
    info = remote_iso_info()
    save_state(
        state_from_info(
            current_manifest(),
            info,
            layer=kind,
            caps=Caps(patch=ARCH == "amd64", refresh=True),
            profile=args.profile,
        )
    )
    return info


def plan_layer(state: dict, current: Dict[str, str], args) -> Tuple[str, List[str], List[str]]:
    changed = changed_paths(state.get("manifest", {}), current)
    kind = classify(changed)
    reasons: List[str] = []
    fp_layer = fingerprint_layer(state)
    if fp_layer and LAYER_ORDER[fp_layer] > LAYER_ORDER[kind]:
        kind = fp_layer
        reasons.append("gitignored payload (wheels/keys/drivers) changed")
    if (
        kind == APP_ONLY
        and state.get("patch_engine_sha")
        and state["patch_engine_sha"] != patch_engine_sha()
    ):
        kind = REFRESH
        reasons.append("tools/iso_patch.sh changed")
    previous_profile = (state.get("options") or {}).get("profile", "")
    if previous_profile != args.profile and LAYER_ORDER[kind] < LAYER_ORDER[REFRESH]:
        kind = REFRESH
        reasons.append(
            f"build profile {previous_profile or 'default'} -> {args.profile or 'default'}"
        )
    return kind, changed, reasons


def print_plan(kind: str, changed: List[str], reasons: List[str], args) -> None:
    if kind == UP_TO_DATE:
        command = "nothing to do"
    elif kind == NO_ISO:
        command = "no ISO rebuild"
    elif kind == APP_ONLY:
        command = "tools/iso_patch.sh"
    else:
        command = build_command(kind, args.mode, args.profile)
    collect = "no (-test)" if args.mode == "test" or args.no_collect else "-X86/-ARM"
    log(
        f"[{ARCH}] layer={kind} eta={eta_for(kind, ARCH)} "
        f"command={command} collect={collect}"
    )
    for reason in reasons:
        log(f"[{ARCH}]   reason: {reason}")
    for path in changed[:20]:
        log(f"[{ARCH}]   changed: {path}")
    if len(changed) > 20:
        log(f"[{ARCH}]   ... {len(changed) - 20} more")


def run_one(arch: str, args) -> None:
    global ARCH, _PENDING_WEBPLANE
    ARCH = arch
    state = load_state(arch)
    current = current_manifest()

    def want_collect(mode: str) -> bool:
        return mode != "test" and not args.no_collect

    if args.adopt:
        if args.dry_run:
            log(f"[{arch}] layer=ADOPT eta=? command=adopt current ISO")
            return
        run_sync()
        ensure_vm()
        adopt(args)
        adopted = load_state(arch) or {}
        adopted_name = (adopted.get("iso") or {}).get("name", "")
        adopted_mode = (adopted.get("iso") or {}).get("mode", "release")
        if want_collect(adopted_mode) and adopted_name:
            if arch == "arm64":
                collect_iso(adopted_name, pull=True)
            else:
                ensure_collected(adopted_name, adopted_mode)
        return

    if state is None and args.dry_run:
        log(f"[{arch}] layer=FULL eta={eta_for(FULL, arch)} command=./build.sh (no baseline state)")
        return

    if args.baseline or (state is None and not args.dry_run):
        if args.dry_run:
            log(f"[{arch}] layer={FULL} eta={eta_for(FULL, arch)} command=./build.sh (--baseline)")
            return
        if state is None:
            run_sync()
            ensure_vm()
            if try_adopt(args):
                state = load_state(arch)
            else:
                log(f"[{arch}] no usable baseline; running a full build")
                info = baseline(args)
                if want_collect(args.mode) and info.get("ISO"):
                    collect_iso(info["ISO"], pull=arch == "arm64")
                return
        else:
            run_sync()
            ensure_vm()
            info = baseline(args)
            if want_collect(args.mode) and info.get("ISO"):
                collect_iso(info["ISO"], pull=arch == "arm64")
            return
    if state is None:
        die(f"[{arch}] no baseline state; run --adopt or --baseline first")

    kind, changed, reasons = plan_layer(state, current, args)
    if args.force or args.full or args.fast:
        kind = FULL
        reasons.append("forced full build")
    elif args.refresh:
        kind = REFRESH
        reasons.append("forced refresh build")
    if args.app:
        if arch != "amd64":
            die("--app is amd64-only; use --refresh for arm64")
        if kind != APP_ONLY:
            die(f"classification is {kind}, not APP_ONLY; use --refresh or --full")
        kind = APP_ONLY

    if any(path.startswith("webplane/") for path in changed):
        _PENDING_WEBPLANE = True

    caps = caps_from_state(state)
    if not args.dry_run and kind != NO_ISO:
        if arch == "arm64":
            run_sync()
            ensure_vm()
        caps = probe_caps()
    kind, fallbacks = effective_layer(kind, arch, state, args.mode, caps)
    reasons.extend(f"fallback: {item}" for item in fallbacks)

    if args.dry_run:
        print_plan(kind, changed, reasons, args)
        return

    if kind == UP_TO_DATE:
        iso = (state.get("iso") or {}).get("name", "")
        sha = (state.get("iso") or {}).get("sha256", "")
        if arch == "amd64" and iso and not remote_iso_matches(iso, sha):
            log("remote ISO no longer matches the recorded state - rebuilding")
            kind = REFRESH if caps.refresh else FULL
        else:
            log("working tree matches the last ISO baseline; nothing to do")
            if want_collect((state.get("iso") or {}).get("mode", args.mode)):
                ensure_collected(iso, (state.get("iso") or {}).get("mode", args.mode))
            return

    if kind == NO_ISO:
        log("changes do not affect the ISO")
        info = {
            "ISO": (state.get("iso") or {}).get("name", ""),
            "SHA": (state.get("iso") or {}).get("sha256", ""),
            "SIZE": str((state.get("iso") or {}).get("size", 0)),
            "SQUASH": state.get("squashfs_sha256", ""),
            "BUILD_ID": state.get("build_id", ""),
        }
        save_state(
            state_from_info(
                current,
                info,
                layer=kind,
                mode=(state.get("iso") or {}).get("mode", args.mode),
                caps=caps,
                profile=args.profile,
            )
        )
        if want_collect((state.get("iso") or {}).get("mode", args.mode)):
            ensure_collected(info["ISO"], (state.get("iso") or {}).get("mode", args.mode))
        return

    if kind == APP_ONLY:
        name = output_name(args.mode, args.iso)
        try:
            payload = patch_app(args.mode, name, changed)
        except SystemExit:
            if not caps.refresh:
                raise
            warn("ISO patch failed; escalating to a refresh build")
            kind = REFRESH
            payload = None
        if payload is None:
            run_layer(kind, args)
            info = remote_iso_info()
            save_state(
                state_from_info(
                    current_manifest(),
                    info,
                    layer=kind,
                    mode=args.mode,
                    caps=Caps(patch=ARCH == "amd64", refresh=True),
                    profile=args.profile,
                )
            )
            if args.mode != "test" and not args.no_collect:
                collect_iso(info.get("ISO", ""), pull=False)
            return        log(
            "ISO patched: {iso} ({seconds}s, {size} bytes)".format(
                iso=payload.get("iso", name),
                seconds=payload.get("seconds", "?"),
                size=payload.get("size", "?"),
            )
        )
        info = {
            "ISO": payload.get("iso", name),
            "SHA": payload.get("sha256", ""),
            "SIZE": str(payload.get("size", 0)),
            "SQUASH": payload.get("squashfs_sha256", ""),
            "BUILD_ID": payload.get("build_id", ""),
        }
        save_state(
            state_from_info(
                current_manifest(),
                info,
                layer=kind,
                mode=args.mode,
                caps=Caps(patch=ARCH == "amd64", refresh=True),
                profile=args.profile,
            )
        )
        if want_collect(args.mode):
            ensure_collected(info["ISO"], args.mode)
        return

    try:
        run_layer(kind, args)
    except subprocess.CalledProcessError:
        if kind not in (REFRESH, REFRESH_VENV):
            raise
        warn(f"{kind} build failed; escalating to a full build")
        kind = FULL
        run_layer(kind, args)
    info = remote_iso_info()
    save_state(
        state_from_info(
            current_manifest(),
            info,
            layer=kind,
            mode=args.mode,
            caps=Caps(patch=ARCH == "amd64", refresh=True),
            profile=args.profile,
        )
    )
    if args.mode != "test" and not args.no_collect:
        collect_iso(info.get("ISO", ""), pull=arch == "arm64")
    log(f"[{arch}] {kind} complete: {info.get('ISO', '?')}")


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Layered incremental RAIDFORGE ISO update (default: amd64 + arm64)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--release", action="store_const", const="release", dest="mode")
    mode.add_argument("--test", action="store_const", const="test", dest="mode")
    parser.set_defaults(mode="release")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--app", action="store_true", help="force the amd64 patch layer")
    parser.add_argument("--refresh", action="store_true", help="force the refresh layer")
    parser.add_argument("--force", action="store_true", help="force a full build")
    parser.add_argument("--full", action="store_true", help="force a full build")
    parser.add_argument("--fast", action="store_true", help="full build with --fast")
    parser.add_argument("--iso", default="", help="ISO file name (adopt/patch)")
    parser.add_argument("--arch", choices=(*ARCHS, "both"), default="both")
    parser.add_argument("--profile", choices=("", "minimal", "full"), default="")
    parser.add_argument("--no-collect", action="store_true", help="do not copy into out/")
    parser.add_argument("--no-deploy-webplane", action="store_true")
    return parser.parse_args(argv)


def acquire_lock() -> Path:
    path = ROOT / ".tmp" / "iso-update.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        die(f"another iso_update is running (remove {path} if stale)")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(f"pid={os.getpid()} started={datetime.now().isoformat()}\n")
    return path


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.adopt and args.baseline:
        die("choose either --adopt or --baseline")
    if args.fast:
        args.mode = "test"
    archs = archs_from(args.arch)
    lock = None if args.dry_run else acquire_lock()
    try:
        for arch in archs:
            run_one(arch, args)
    finally:
        if lock and lock.exists():
            lock.unlink()
    if (
        _PENDING_WEBPLANE
        and "amd64" in archs
        and not args.no_deploy_webplane
        and not args.dry_run
    ):
        deploy_webplane()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
