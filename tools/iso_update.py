#!/usr/bin/env python3
"""Classify local changes and run the cheapest safe ISO update.

The remote build host has no git, and the live-build chroot is the source of
the last squashfs.  This tool keeps a local file manifest as the baseline:
app-only changes patch the cached chroot and replay xorriso; anything that
affects the ISO build itself falls back to build.sh.
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
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
ENV = {**os.environ, "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}

# Set from --arch; amd64 keeps the historical state file name.
ARCH = "amd64"


def state_path() -> Path:
    if ARCH == "amd64":
        return ROOT / ".tmp" / "iso-state.json"
    return ROOT / ".tmp" / f"iso-state-{ARCH}.json"


def remote_dir() -> str:
    return "/root/raidforge" if ARCH == "amd64" else f"/srv/raidforge-{ARCH}"

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
NO_ISO_EXACT = {"README.md", "Makefile"}
IGNORE_PREFIXES = ("UNKNOWN-NOINPUT_",)


def is_ignored(path: str) -> bool:
    """Generated test/evidence directories must never affect classification."""

    return path.startswith(IGNORE_PREFIXES)


def die(message: str) -> "None":
    print(f"[iso_update] {message}", file=sys.stderr)
    raise SystemExit(2)


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


def classify(changed: Iterable[str]) -> str:
    paths = list(changed)
    if not paths:
        return "UP_TO_DATE"
    if all(is_no_iso(path) for path in paths):
        return "NO_ISO"
    if all(is_no_iso(path) or is_app_only(path) for path in paths):
        return "APP_ONLY"
    return "NORMAL"


def needs_webplane(changed: Iterable[str]) -> bool:
    return any(path.startswith("webplane/") for path in changed)


def load_state() -> Optional[dict]:
    path = state_path()
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
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"state saved: {path}")


def catalog_fingerprint() -> str:
    """Fingerprint the gitignored driver payloads, which the manifest cannot see."""
    digest = hashlib.sha256()
    candidates: List[Path] = [
        ROOT / "vendor" / "drivers" / "catalog.json",
        ROOT / "vendor" / "drivers" / "firmware-map.json",
    ]
    for base in (ROOT / "vendor" / "drivers" / ARCH, ROOT / "vendor" / "cli" / ARCH):
        if base.is_dir():
            candidates.extend(sorted(path for path in base.rglob("*") if path.is_file()))
    for path in candidates:
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(sha256_file(path).encode("ascii"))
    return digest.hexdigest()


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
        f"cd {remote_dir()} && ./tools/arm64_vm.sh run --no-sync \"{inner}\"",
        check=check,
    )


def remote_iso_info(name: str = "", *, adopt: bool = False) -> dict:
    select = f"ISO={name}" if name else (
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


def state_from_info(manifest: Dict[str, str], info: dict, *, mode: str = "") -> dict:
    name = info.get("ISO", "")
    return {
        "version": 1,
        "arch": ARCH,
        "fingerprint": catalog_fingerprint(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "git_head": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "manifest": manifest,
        "iso": {
            "name": name,
            "sha256": info.get("SHA", ""),
            "size": int(info.get("SIZE", "0") or 0),
            "mode": mode or ("test" if name.endswith("-test.iso") else "release"),
        },
        "squashfs_sha256": info.get("SQUASH", ""),
        "build_id": info.get("BUILD_ID", ""),
    }


def run_sync() -> None:
    args = ["bash", "tools/sync.sh"]
    if ARCH != "amd64":
        args += ["--arch", ARCH]
    run(args, capture=False)


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
        die("app-only ISO patching is amd64-only; run a normal build for arm64")
    run_sync()
    tar_path = ROOT / ".tmp" / "iso-app.tar.gz"
    create_app_tar(tar_path)
    log(f"uploading app tarball ({tar_path.stat().st_size} bytes)")
    rssh("exec", "mkdir -p /root/raidforge/.tmp")
    rssh("put", str(ROOT / "tools" / "iso_patch.sh"), "/root/raidforge/tools/iso_patch.sh")
    rssh("put", str(tar_path), "/root/raidforge/.tmp/iso-app.tar.gz")
    command = (
        "cd /root/raidforge && bash tools/iso_patch.sh "
        f"--mode {mode} --app-tar .tmp/iso-app.tar.gz --name {name} --json"
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


def print_plan(kind: str, changed: List[str], *, webplane: bool) -> None:
    log(f"classification: {kind}")
    for path in changed:
        log(f"  changed: {path}")
    if webplane:
        log("webplane changes detected: deploy after the ISO step")


def adopt(args) -> None:
    info = remote_iso_info(args.iso, adopt=True)
    log("adopting the current ISO as baseline")
    manifest = synthetic_baseline()
    run_sync()
    save_state(state_from_info(manifest, info))
    log("baseline established; future app-only runs can use the patch path")


def baseline(args) -> None:
    run_sync()
    fast = args.fast or args.mode == "test"
    build = "./build.sh" + (" --fast" if fast else "")
    if getattr(args, "profile", ""):
        build += f" --profile {args.profile}"
    sync_args = ["bash", "tools/sync.sh"]
    if ARCH != "amd64":
        sync_args += ["--arch", ARCH]
    log(f"running normal build: {build}")
    run(sync_args + ["--run", f"cd /root/raidforge && {build}"], capture=False)
    info = remote_iso_info()
    save_state(state_from_info(current_manifest(), info))
    log("baseline build recorded")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Incremental RAIDFORGE ISO update")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--release", action="store_const", const="release", dest="mode")
    mode.add_argument("--test", action="store_const", const="test", dest="mode")
    parser.set_defaults(mode="release")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--app", action="store_true", help="force the app-only path")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--full", action="store_true", help="force a normal build")
    parser.add_argument("--fast", action="store_true", help="normal build with --fast")
    parser.add_argument("--iso", default="", help="ISO file name (adopt/patch)")
    parser.add_argument("--arch", choices=("amd64", "arm64"), default="amd64")
    parser.add_argument("--profile", choices=("", "minimal", "full"), default="")
    parser.add_argument("--no-deploy-webplane", action="store_true")
    args = parser.parse_args(argv)

    global ARCH
    ARCH = args.arch

    if args.adopt and args.baseline:
        die("choose either --adopt or --baseline")
    if args.full or args.fast:
        kind = "NORMAL"
    else:
        kind = ""

    state = load_state()
    if args.adopt:
        if args.dry_run:
            print_plan("ADOPT", [], webplane=False)
            return 0
        adopt(args)
        return 0

    if args.baseline or not state:
        if args.dry_run:
            print_plan("BASELINE" if state is None else "NORMAL", [], webplane=False)
            return 0
        if not (args.baseline or args.full):
            die("no baseline state; run --adopt or --baseline first")
        baseline(args)
        return 0

    current = current_manifest()
    changed = changed_paths(state.get("manifest", {}), current)
    kind = kind or classify(changed)
    if state.get("fingerprint") and state["fingerprint"] != catalog_fingerprint():
        log("driver catalog / CLI / payload changed")
        kind = "NORMAL"
    webplane = needs_webplane(changed)
    if args.app:
        if kind != "APP_ONLY" and not args.force:
            die(f"classification is {kind}, not APP_ONLY; use --force to override")
        kind = "APP_ONLY"

    if args.dry_run:
        print_plan(kind, changed, webplane=webplane)
        return 0

    if kind == "UP_TO_DATE":
        log("working tree matches the last ISO baseline; nothing to do")
        if webplane and not args.no_deploy_webplane:
            deploy_webplane()
        return 0

    if kind == "NO_ISO":
        log("changes do not affect the ISO")
        if webplane and not args.no_deploy_webplane:
            deploy_webplane()
        save_state(state_from_info(current, {
            "ISO": state["iso"]["name"],
            "SHA": state["iso"]["sha256"],
            "SIZE": str(state["iso"]["size"]),
            "SQUASH": state.get("squashfs_sha256", ""),
            "BUILD_ID": state.get("build_id", ""),
        }, mode=state["iso"].get("mode", "")))
        return 0

    if kind == "NORMAL":
        log("changes affect the ISO build itself; falling back to build.sh")
        baseline(args)
        if webplane and not args.no_deploy_webplane:
            deploy_webplane()
        return 0

    if ARCH != "amd64":
        # Repacking a squashfs + replaying xorriso inside the emulated VM is
        # fragile and barely faster than build.sh --fast; use the normal path.
        log("arm64: app-only ISO patching is disabled; running a normal build")
        baseline(args)
        if webplane and not args.no_deploy_webplane:
            deploy_webplane()
        return 0

    # APP_ONLY
    name = output_name(args.mode, args.iso)
    payload = patch_app(args.mode, name, changed)
    log(
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
    save_state(state_from_info(current_manifest(), info, mode=args.mode))
    if webplane and not args.no_deploy_webplane:
        deploy_webplane()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
