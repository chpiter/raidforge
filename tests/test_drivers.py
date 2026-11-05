"""Driver resolver / verifier tests (stdlib only, no hardware required)."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.drivers as drivers  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_catalog() -> dict:
    return json.loads((ROOT / "vendor" / "drivers" / "catalog.json").read_text(encoding="utf-8"))


def device(vendor: str, product: str, klass: str, driver: str = "") -> dict:
    return {
        "slot": "0000:00:00.0",
        "vendor": vendor,
        "device": product,
        "class": klass,
        "driver": driver,
        "name": "test device",
    }


def test_catalog_exact_match() -> None:
    catalog = load_catalog()
    assert drivers.match_catalog(device("1000", "005d", "0104"), catalog) == "megaraid_sas"


def test_catalog_vendor_class_match() -> None:
    catalog = load_catalog()
    assert drivers.match_catalog(device("103c", "3239", "0104"), catalog) == "hpsa"


def test_bound_driver_wins_for_unknown_ids() -> None:
    catalog = load_catalog()
    unknown = device("9005", "ffff", "0104", driver="smartpqi")
    assert drivers.match_catalog(unknown, catalog) == "smartpqi"


def test_unknown_vendor_is_unresolved() -> None:
    catalog = load_catalog()
    assert drivers.match_catalog(device("dead", "beef", "0104"), catalog) is None


def test_alias_index_matches_modalias(tmp_path: Path) -> None:
    chroot = tmp_path / "chroot"
    module_dir = chroot / "lib" / "modules" / "6.1.0-test"
    module_dir.mkdir(parents=True)
    (module_dir / "modules.alias").write_text(
        "alias pci:v00001000d0000005Dsv*sd*bc01sc04i* megaraid_sas\n",
        encoding="utf-8",
    )
    index = drivers.AliasIndex.from_chroot(chroot)
    assert index.lookup(device("1000", "005d", "0104")) == "megaraid_sas"
    assert index.lookup(device("1000", "00ff", "0104")) is None


def test_firmware_map_prefix() -> None:
    mapping = json.loads(
        (ROOT / "vendor" / "drivers" / "firmware-map.json").read_text(encoding="utf-8")
    )
    assert drivers.firmware_package("qlogic/ql2300_fw.bin", mapping) == "firmware-qlogic"
    assert drivers.firmware_package("intel/ice/ddp/ice.pkg", mapping) == "firmware-misc-nonfree"
    assert drivers.firmware_package("nope/unknown.bin", mapping) is None


def test_probe_merge_dedupes_and_filters_arch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    probes = root / "vendor" / "drivers" / "probes"
    probes.mkdir(parents=True)
    amd64 = {
        "system": {"arch": "amd64", "hostname": "a"},
        "pci": [device("1000", "005d", "0104"), device("1000", "005d", "0104")],
        "firmware_missing": ["qlogic/a.bin"],
    }
    arm64 = {"system": {"arch": "arm64", "hostname": "b"}, "pci": [], "firmware_missing": []}
    (probes / "a.json").write_text(json.dumps(amd64), encoding="utf-8")
    (probes / "b.json").write_text(json.dumps(arm64), encoding="utf-8")
    monkeypatch.setattr(drivers, "ROOT", root)

    reports, used = drivers.load_probes([], "amd64")
    assert len(used) == 1
    merged = drivers.merge_probes(reports)
    assert merged is not None
    assert len(merged["pci"]) == 1
    assert merged["firmware_missing"] == ["qlogic/a.bin"]


@pytest.fixture()
def resolver_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    (root / "vendor" / "drivers").mkdir(parents=True)
    (root / "config" / "package-lists").mkdir(parents=True)
    (root / "config" / "includes.chroot" / "usr" / "src").mkdir(parents=True)
    (root / "vendor" / "drivers" / "catalog.json").write_bytes(
        (ROOT / "vendor" / "drivers" / "catalog.json").read_bytes()
    )
    (root / "vendor" / "drivers" / "firmware-map.json").write_bytes(
        (ROOT / "vendor" / "drivers" / "firmware-map.json").read_bytes()
    )
    monkeypatch.setattr(drivers, "ROOT", root)
    monkeypatch.setattr(drivers, "CATALOG_PATH", root / "vendor" / "drivers" / "catalog.json")
    monkeypatch.setattr(
        drivers, "FIRMWARE_MAP_PATH", root / "vendor" / "drivers" / "firmware-map.json"
    )
    monkeypatch.setattr(drivers, "SELECTION_PATH", root / "vendor" / "drivers" / "selection.json")
    monkeypatch.setattr(
        drivers, "GENERATED_LIST", root / "config" / "package-lists" / "26-drivers.generated.list.chroot"
    )
    monkeypatch.setattr(
        drivers, "STAGE_ROOT", root / "config" / "includes.chroot" / "usr" / "src" / "raidforge-drivers"
    )
    return root


def resolve_args(probe: Path, *, allow_unknown: bool = False, arch: str = "amd64") -> Namespace:
    return Namespace(
        arch=arch,
        probe=str(probe),
        profile="minimal",
        chroot="",
        allow_unknown=allow_unknown,
        dry_run=False,
    )


def test_resolve_with_probe(resolver_env: Path) -> None:
    probe = FIXTURES / "hwprobe_amd64.json"
    assert drivers.resolve(resolve_args(probe)) == 3

    manifest = json.loads(
        (resolver_env / "config" / "includes.chroot" / "usr" / "src" / "raidforge-drivers"
         / "amd64" / "manifest.json").read_text(encoding="utf-8")
    )
    modules = {item["module"]: item for item in manifest["modules"]}
    assert set(modules) == {"megaraid_sas", "hpsa", "i40e"}
    assert modules["megaraid_sas"]["required"] is True
    assert manifest["strict"] is True
    assert "firmware-qlogic" in manifest["packages"]
    assert "firmware-misc-nonfree" not in manifest["packages"]
    assert {item["file"] for item in manifest["firmware"]} == {
        "qlogic/ql2300_fw.bin",
        "intel/ice/ddp/ice-1.3.30.0.pkg",
    }
    assert len(manifest["unsupported"]) == 1

    generated = (
        resolver_env / "config" / "package-lists" / "26-drivers.generated.list.chroot"
    ).read_text(encoding="utf-8")
    assert "firmware-qlogic" in generated


def test_resolve_allow_unknown(resolver_env: Path) -> None:
    probe = FIXTURES / "hwprobe_amd64.json"
    assert drivers.resolve(resolve_args(probe, allow_unknown=True)) == 0


def test_resolve_arm64_probe(resolver_env: Path) -> None:
    probe = FIXTURES / "hwprobe_arm64.json"
    assert drivers.resolve(resolve_args(probe, arch="arm64")) == 0
    manifest = json.loads(
        (resolver_env / "config" / "includes.chroot" / "usr" / "src" / "raidforge-drivers"
         / "arm64" / "manifest.json").read_text(encoding="utf-8")
    )
    modules = {item["module"]: item for item in manifest["modules"]}
    assert set(modules) == {"megaraid_sas", "hns3"}
    assert manifest["unsupported"] == []


def test_resolve_without_probe_installs_no_extra_firmware(resolver_env: Path) -> None:
    args = Namespace(
        arch="amd64", probe="", profile="minimal", chroot="", allow_unknown=False, dry_run=False
    )
    assert drivers.resolve(args) == 0
    manifest = json.loads(
        (resolver_env / "config" / "includes.chroot" / "usr" / "src" / "raidforge-drivers"
         / "amd64" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["packages"] == []
    assert manifest["strict"] is False


def test_verify_reports_missing_required_module(tmp_path: Path) -> None:
    chroot = tmp_path / "chroot"
    (chroot / "etc" / "raidforge").mkdir(parents=True)
    (chroot / "lib" / "modules" / "6.1.0-test").mkdir(parents=True)
    installed = {
        "arch": "amd64",
        "kernel": "6.1.0-test",
        "modules": [{"module": "megaraid_sas", "required": True}],
        "firmware": [],
    }
    (chroot / "etc" / "raidforge" / "drivers.installed.json").write_text(
        json.dumps(installed), encoding="utf-8"
    )
    args = Namespace(arch="amd64", chroot=str(chroot), iso="")
    assert drivers.verify(args) == 1


def test_verify_accepts_present_module(tmp_path: Path) -> None:
    chroot = tmp_path / "chroot"
    (chroot / "etc" / "raidforge").mkdir(parents=True)
    module_dir = chroot / "lib" / "modules" / "6.1.0-test" / "kernel" / "drivers" / "scsi"
    module_dir.mkdir(parents=True)
    (module_dir / "megaraid_sas.ko").write_bytes(b"")
    installed = {
        "arch": "amd64",
        "kernel": "6.1.0-test",
        "modules": [{"module": "megaraid_sas", "required": True}],
        "firmware": [],
    }
    (chroot / "etc" / "raidforge" / "drivers.installed.json").write_text(
        json.dumps(installed), encoding="utf-8"
    )
    args = Namespace(arch="amd64", chroot=str(chroot), iso="")
    assert drivers.verify(args) == 0


def test_hiraid_module_is_arm64_only(resolver_env: Path) -> None:
    arm = Namespace(
        arch="arm64", probe="", profile="minimal", chroot="", allow_unknown=False, dry_run=False
    )
    assert drivers.resolve(arm) == 0
    manifest = json.loads(
        (resolver_env / "config" / "includes.chroot" / "usr" / "src" / "raidforge-drivers"
         / "arm64" / "manifest.json").read_text(encoding="utf-8")
    )
    modules = {item["module"]: item for item in manifest["modules"]}
    assert "hiraid" in modules
    assert modules["hiraid"]["load"] is True

    amd = Namespace(
        arch="amd64", probe="", profile="minimal", chroot="", allow_unknown=False, dry_run=False
    )
    assert drivers.resolve(amd) == 0
    manifest = json.loads(
        (resolver_env / "config" / "includes.chroot" / "usr" / "src" / "raidforge-drivers"
         / "amd64" / "manifest.json").read_text(encoding="utf-8")
    )
    assert "hiraid" not in {item["module"] for item in manifest["modules"]}
