#!/usr/bin/env python3
"""Resolve server hardware to kernel modules / firmware and stage the extras.

Two entry points:

    tools/drivers.py resolve --arch amd64 [--probe hw.json] [--profile full]
    tools/drivers.py verify  --arch amd64 [--chroot chroot] [--iso image.iso]

`resolve` is called by build.sh before `lb build`.  It writes:

    config/package-lists/26-drivers.generated.list.chroot   extra firmware packages
    config/includes.chroot/usr/src/raidforge-drivers/<arch>/ manifest + payload
    vendor/drivers/selection.json                           human-readable summary

`verify` is called after `lb build` and asserts the modules/firmware really made
it into the image.

The tool is deliberately stdlib-only: it runs on the Debian build host with the
system python3, not inside the app venv.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "vendor" / "drivers" / "catalog.json"
FIRMWARE_MAP_PATH = ROOT / "vendor" / "drivers" / "firmware-map.json"
SELECTION_PATH = ROOT / "vendor" / "drivers" / "selection.json"
GENERATED_LIST = ROOT / "config" / "package-lists" / "26-drivers.generated.list.chroot"
STAGE_ROOT = ROOT / "config" / "includes.chroot" / "usr" / "src" / "raidforge-drivers"

# Already installed unconditionally by config/package-lists/25-firmware.list.chroot.
STATIC_FIRMWARE_PACKAGES = {"firmware-linux-free", "firmware-misc-nonfree"}

STORAGE_CLASSES = ("01",)
NETWORK_CLASSES = ("02",)


def die(message: str) -> "None":
    print(f"[drivers] {message}", file=sys.stderr)
    raise SystemExit(2)


def log(message: str) -> None:
    print(f"[drivers] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[drivers] warning: {message}", file=sys.stderr)


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        die(f"missing {display_path(path)}")
    except ValueError as error:
        die(f"invalid JSON in {display_path(path)}: {error}")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def normalize_arch(value: str) -> str:
    value = (value or "").lower()
    if value in ("x86_64", "amd64"):
        return "amd64"
    if value in ("aarch64", "arm64"):
        return "arm64"
    die(f"unsupported architecture: {value}")


def device_key(device: dict) -> str:
    return f"{device.get('vendor', '')}:{device.get('device', '')}".lower()


def device_class(device: dict) -> str:
    return (device.get("class") or "").lower()[:4]


def modalias(device: dict) -> str:
    vendor = int(device.get("vendor", "0") or "0", 16)
    product = int(device.get("device", "0") or "0", 16)
    klass = (device_class(device) or "0000").ljust(4, "0")
    return f"pci:v{vendor:08X}d{product:08X}sv*sd*bc{klass[:2]}sc{klass[2:4]}i*"


def match_catalog(device: dict, catalog: dict) -> Optional[str]:
    """Return a module name for a device, or None when the catalog has no answer."""
    key = device_key(device)
    vendor = device.get("vendor", "").lower()
    klass = device_class(device)
    driver = (device.get("driver") or device.get("module") or "").lower()
    modules = catalog.get("modules", {})

    # 1. exact vendor:device wins
    for entry in catalog.get("devices", []):
        ids = [item.lower() for item in entry.get("match", {}).get("pci_ids", [])]
        if key and key in ids:
            return entry.get("module")

    # 2. the driver already bound on the target is ground truth
    if driver and driver in modules:
        return driver

    # 3. vendor + class, only when unambiguous
    candidates = {
        entry.get("module")
        for entry in catalog.get("devices", [])
        if entry.get("match", {}).get("vendor", "").lower() == vendor
        and entry.get("match", {}).get("class", "").lower() == klass
    }
    candidates.discard(None)
    if len(candidates) == 1:
        return candidates.pop()
    return None


class AliasIndex:
    """Minimal modalias -> module matcher built from modules.alias."""

    def __init__(self) -> None:
        self.entries: List[Tuple[str, str]] = []

    @classmethod
    def from_chroot(cls, chroot: Path) -> "AliasIndex":
        index = cls()
        candidates = sorted(glob.glob(str(chroot / "lib" / "modules" / "*" / "modules.alias")))
        if not candidates:
            return index
        path = candidates[-1]
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    fields = line.split()
                    if len(fields) == 3 and fields[0] == "alias":
                        index.entries.append((fields[1].lower(), fields[2]))
        except OSError:
            return index
        log(f"alias index: {len(index.entries)} entries from {path}")
        return index

    def lookup(self, device: dict) -> Optional[str]:
        pattern = modalias(device).lower()
        for alias, module in self.entries:
            if fnmatch.fnmatchcase(pattern, alias):
                return module
        return None


def firmware_package(path: str, firmware_map: dict) -> Optional[str]:
    for rule in firmware_map.get("rules", []):
        prefix = rule.get("prefix", "")
        if prefix and not path.startswith(prefix):
            continue
        if not prefix:
            continue
        return rule.get("package") or firmware_map.get("default_package")
    return firmware_map.get("default_package")


def discover_payload(arch: str) -> Dict[str, List[str]]:
    base = ROOT / "vendor" / "drivers" / arch
    payload: Dict[str, List[str]] = {"debs": [], "ko": [], "dkms": [], "firmware": []}
    if not base.is_dir():
        return payload

    payload["debs"] = sorted(str(path.relative_to(base)) for path in (base / "debs").glob("*.deb"))
    payload["ko"] = sorted(
        str(path.relative_to(base))
        for path in (base / "ko").rglob("*")
        if path.is_file() and ".ko" in path.name
    )
    payload["dkms"] = sorted(
        str(path.relative_to(base))
        for path in (base / "dkms").iterdir()
        if path.is_dir() and (path / "dkms.conf").is_file()
    ) if (base / "dkms").is_dir() else []
    if (base / "firmware").is_dir():
        payload["firmware"] = sorted(
            str(path.relative_to(base / "firmware"))
            for path in (base / "firmware").rglob("*")
            if path.is_file()
        )
    return payload


def stage_payload(arch: str, payload: Dict[str, List[str]]) -> None:
    base = ROOT / "vendor" / "drivers" / arch
    target = STAGE_ROOT / arch
    if target.exists():
        shutil.rmtree(target)
    (target / "payload").mkdir(parents=True, exist_ok=True)

    for relative in payload["debs"]:
        source = base / relative
        destination = target / "payload" / "debs" / Path(relative).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for relative in payload["ko"]:
        source = base / relative
        destination = target / "payload" / "ko" / Path(relative).relative_to("ko")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for relative in payload["dkms"]:
        source = base / relative
        destination = target / "payload" / "dkms" / Path(relative).name
        shutil.copytree(source, destination, dirs_exist_ok=True)

    for relative in payload["firmware"]:
        source = base / "firmware" / relative
        destination = target / "payload" / "firmware" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def write_generated_list(packages: Iterable[str]) -> None:
    lines = [
        "# GENERATED by tools/drivers.py on every build - do not edit, do not commit.",
        "# Extra firmware packages resolved from the hardware catalog / probe report.",
    ]
    lines.extend(sorted(packages))
    GENERATED_LIST.parent.mkdir(parents=True, exist_ok=True)
    GENERATED_LIST.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_probes(explicit: Iterable[str], arch: str) -> Tuple[List[dict], List[str]]:
    """Merge explicit probe reports with every JSON dropped in probes/."""
    paths = [path for path in (explicit or []) if path]
    probe_dir = ROOT / "vendor" / "drivers" / "probes"
    if probe_dir.is_dir():
        paths.extend(sorted(str(path) for path in probe_dir.glob("*.json")))
    reports: List[dict] = []
    used: List[str] = []
    for path in paths:
        report = load_json(Path(path))
        report_arch = (report.get("system", {}).get("arch") or "").lower()
        if report_arch and report_arch != arch:
            log(f"skipping {Path(path).name}: report arch {report_arch}, target {arch}")
            continue
        reports.append(report)
        used.append(str(path))
    return reports, used


def merge_probes(reports: List[dict]) -> Optional[dict]:
    if not reports:
        return None
    merged = {
        "schema": 1,
        "system": reports[0].get("system", {}),
        "pci": [],
        "nics": [],
        "firmware_missing": [],
        "sources": [],
    }
    seen_devices: Set[Tuple[str, str, str]] = set()
    firmware: Set[str] = set()
    for report in reports:
        merged["sources"].append(report.get("system", {}).get("hostname", ""))
        for device in report.get("pci", []):
            key = (
                device.get("slot", ""),
                device.get("vendor", ""),
                device.get("device", ""),
            )
            if key in seen_devices:
                continue
            seen_devices.add(key)
            merged["pci"].append(device)
        merged["nics"].extend(report.get("nics", []))
        firmware.update(report.get("firmware_missing", []))
    merged["firmware_missing"] = sorted(firmware)
    return merged


def resolve(args) -> int:
    arch = normalize_arch(args.arch)
    catalog = load_json(CATALOG_PATH)
    firmware_map = load_json(FIRMWARE_MAP_PATH)
    modules_meta = catalog.get("modules", {})
    cli_arches = catalog.get("cli_arches", {})
    profile = args.profile

    explicit = getattr(args, "probe", "") or []
    if isinstance(explicit, str):
        explicit = [explicit] if explicit else []
    reports, report_paths = load_probes(explicit, arch)
    probe = merge_probes(reports)
    strict = probe is not None
    if report_paths:
        log(f"probe reports: {len(report_paths)} ({', '.join(Path(p).name for p in report_paths)})")
    alias = AliasIndex.from_chroot(Path(args.chroot)) if args.chroot and Path(args.chroot).is_dir() else AliasIndex()

    packages: Set[str] = set(catalog.get("profiles", {}).get(profile, []))
    modules: Dict[str, dict] = {}
    firmware: List[dict] = []
    unsupported: List[dict] = []
    warnings: List[str] = []

    def add_module(module: str, device: Optional[dict] = None) -> None:
        record = modules.setdefault(
            module, {"module": module, "required": strict, "devices": []}
        )
        if device:
            key = device_key(device)
            if key and key not in record["devices"]:
                record["devices"].append(key)
        meta = modules_meta.get(module, {})
        # Without a probe the build must stay generic: only the profile decides
        # which firmware packages are installed.  With a probe we know which
        # families are present and can add exactly their packages.
        if probe:
            for package in meta.get("packages", []):
                packages.add(package)
        for extra in meta.get("modules_extra", []):
            modules.setdefault(extra, {"module": extra, "required": strict, "devices": []})
        cli = meta.get("cli")
        if cli and arch not in cli_arches.get(cli, ["amd64"]):
            warnings.append(f"{cli} has no {arch} build; {module} falls back to generic mode")

    if probe:
        for device in probe.get("pci", []):
            module = match_catalog(device, catalog) or alias.lookup(device)
            if module:
                add_module(module, device)
                continue
            klass = device_class(device)
            relevant = klass.startswith(STORAGE_CLASSES) or klass.startswith(NETWORK_CLASSES)
            if relevant:
                unsupported.append(
                    {
                        "slot": device.get("slot", ""),
                        "device": device_key(device),
                        "class": klass,
                        "name": device.get("name", ""),
                        "reason": "no catalog entry and no matching modules.alias rule",
                    }
                )
        for path in probe.get("firmware_missing", []):
            package = firmware_package(path, firmware_map)
            firmware.append(
                {
                    "file": path,
                    "package": package or "",
                    "required": bool(package),
                }
            )
            if package:
                packages.add(package)
            else:
                warnings.append(f"no Debian package mapped for firmware file {path}")
    else:
        for module in sorted(modules_meta):
            add_module(module)

    payload = discover_payload(arch)
    if unsupported and not args.allow_unknown:
        for item in unsupported:
            warn(f"unsupported device {item['slot']} {item['device']} ({item['name']})")
        warn(
            "add a catalog entry, drop a .deb/.ko into vendor/drivers/"
            f"{arch}/, or use --allow-unknown to build anyway"
        )

    # Payloads the resolver cannot verify up-front are surfaced as warnings so
    # the build record explains why a module came from an external artefact.
    if payload["debs"] or payload["ko"] or payload["dkms"]:
        warnings.append(
            "out-of-tree payload staged: "
            + ", ".join(
                path
                for group in ("debs", "ko", "dkms")
                for path in payload[group]
            )
        )

    unique_firmware = {item["file"]: item for item in firmware if item.get("file")}
    firmware = [unique_firmware[name] for name in sorted(unique_firmware)]

    manifest = {
        "schema": 1,
        "arch": arch,
        "profile": profile,
        "catalog_version": catalog.get("version", "unknown"),
        "strict": strict,
        "modules": [modules[name] for name in sorted(modules)],
        "packages": sorted(packages - STATIC_FIRMWARE_PACKAGES),
        "firmware": firmware,
        "payload": payload,
        "warnings": sorted(set(warnings)),
        "unsupported": unsupported,
    }

    selection = {
        "arch": arch,
        "profile": profile,
        "strict": strict,
        "modules": {name: sorted(modules[name]["devices"]) for name in sorted(modules)},
        "packages": manifest["packages"],
        "firmware": manifest["firmware"],
        "unsupported": unsupported,
        "warnings": manifest["warnings"],
    }

    log(
        f"arch={arch} profile={profile} modules={len(modules)} "
        f"packages={len(manifest['packages'])} unsupported={len(unsupported)}"
    )

    if args.dry_run:
        print(json.dumps(selection, indent=2, sort_keys=True))
        return 1 if unsupported and not args.allow_unknown else 0

    write_generated_list(manifest["packages"])
    stage_payload(arch, payload)
    write_json(STAGE_ROOT / arch / "manifest.json", manifest)
    write_json(SELECTION_PATH, selection)

    if unsupported and not args.allow_unknown:
        return 3
    return 0


def module_present(chroot: Path, kernel: str, module: str) -> bool:
    root = chroot / "lib" / "modules" / kernel
    if list(root.glob(f"**/{module}.ko*")):
        return True
    builtin = root / "modules.builtin"
    if builtin.is_file():
        try:
            text = builtin.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return any(line.strip().endswith(f"/{module}.ko") for line in text.splitlines())
    return False


def verify(args) -> int:
    arch = normalize_arch(args.arch)
    chroot = Path(args.chroot) if args.chroot else None
    problems: List[str] = []

    def report(message: str, required: bool) -> None:
        if required:
            problems.append(message)
        else:
            log(message)

    if args.iso:
        squashfs = extract_squashfs(Path(args.iso))
        try:
            installed = read_installed_from_squashfs(squashfs)
            if installed is None:
                die("could not read /etc/raidforge/drivers.installed.json from the ISO")
            print(f"verifying ISO {args.iso}")
            listing = subprocess.run(
                ["unsquashfs", "-ll", str(squashfs)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            for record in installed.get("modules", []):
                module = record.get("module", "")
                if f"/{module}.ko" not in listing:
                    report(f"{module} not found in the ISO module tree", bool(record.get("required")))
            for record in installed.get("firmware", []):
                path = record.get("file", "")
                if path and f"/{path}" not in listing:
                    report(f"firmware {path} not found in the ISO", bool(record.get("required")))
        finally:
            shutil.rmtree(squashfs.parent, ignore_errors=True)
    else:
        if chroot is None or not (chroot / "etc" / "raidforge" / "drivers.installed.json").is_file():
            die("verify needs --chroot <dir> with etc/raidforge/drivers.installed.json or --iso <file>")
        installed = load_json(chroot / "etc" / "raidforge" / "drivers.installed.json")
        kernel = installed.get("kernel", "")
        print(f"verifying chroot {chroot} (kernel {kernel}, arch {installed.get('arch')})")
        for record in installed.get("modules", []):
            module = record.get("module", "")
            if not module_present(chroot, kernel, module):
                report(
                    f"{module} not found under {chroot}/lib/modules/{kernel}",
                    bool(record.get("required")),
                )
        for record in installed.get("firmware", []):
            path = record.get("file", "")
            if not (chroot / "lib" / "firmware" / path).is_file():
                report(
                    f"firmware {path} missing from {chroot}/lib/firmware",
                    bool(record.get("required")),
                )

        host_arch = normalize_arch(platform.machine())
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        if host_arch == arch and is_root and (chroot / "sbin" / "modprobe").exists():
            for record in installed.get("modules", []):
                module = record.get("module", "")
                result = subprocess.run(
                    ["chroot", str(chroot), "modprobe", "-n", "-q", "-S", kernel, module],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    report(
                        f"modprobe -n {module} failed: {result.stderr.strip()}",
                        bool(record.get("required")),
                    )

    if problems:
        for problem in problems:
            print(f"[drivers] FAIL: {problem}", file=sys.stderr)
        return 1
    log("driver verification ok")
    return 0


def extract_squashfs(iso: Path) -> Path:
    """Pull live/filesystem.squashfs out of the ISO so unsquashfs can read it."""
    workdir = Path(tempfile.mkdtemp(prefix="raidforge-iso-"))
    target = workdir / "filesystem.squashfs"
    result = subprocess.run(
        [
            "xorriso", "-osirrox", "on", "-indev", str(iso),
            "-extract", "/live/filesystem.squashfs", str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not target.exists():
        shutil.rmtree(workdir, ignore_errors=True)
        die(f"could not extract live/filesystem.squashfs from {iso}: {result.stderr.strip()}")
    return target


def read_installed_from_squashfs(path: Path) -> Optional[dict]:
    result = subprocess.run(
        ["unsquashfs", "-cat", str(path), "etc/raidforge/drivers.installed.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="RAIDFORGE driver resolver")
    sub = parser.add_subparsers(dest="command", required=True)

    resolve_parser = sub.add_parser("resolve", help="resolve hardware and stage drivers")
    resolve_parser.add_argument("--arch", default="amd64")
    resolve_parser.add_argument(
        "--probe",
        action="append",
        default=[],
        help="raidforge-probe JSON report (repeatable; vendor/drivers/probes/*.json "
             "are merged automatically)",
    )
    resolve_parser.add_argument("--profile", choices=("minimal", "full"), default="minimal")
    resolve_parser.add_argument("--chroot", default="", help="build chroot for modules.alias")
    resolve_parser.add_argument("--allow-unknown", action="store_true")
    resolve_parser.add_argument("--dry-run", action="store_true")
    resolve_parser.set_defaults(func=resolve)

    verify_parser = sub.add_parser("verify", help="verify a built image")
    verify_parser.add_argument("--arch", default="amd64")
    verify_parser.add_argument("--chroot", default="")
    verify_parser.add_argument("--iso", default="")
    verify_parser.set_defaults(func=verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
