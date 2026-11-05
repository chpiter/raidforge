"""Pure-function tests for the layered incremental ISO update."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("iso_update_tiers", ROOT / "tools" / "iso_update.py")
iso = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = iso
SPEC.loader.exec_module(iso)


@pytest.mark.parametrize(
    ("path", "layer"),
    [
        ("app/raidforge/core/detect.py", iso.APP_ONLY),
        ("app/run.py", iso.APP_ONLY),
        ("app/config.yaml.example", iso.APP_ONLY),
        ("app/requirements.txt", iso.REFRESH_VENV),
        ("app/pyproject.toml", iso.REFRESH_VENV),
        ("vendor/wheels/arm64/textual-7.5.0-py3-none-any.whl", iso.REFRESH_VENV),
        ("config/includes.chroot/etc/raidforge/keys/evidence_ed25519", iso.REFRESH_VENV),
        ("config/includes.chroot/usr/local/sbin/raidforge-tui", iso.REFRESH),
        ("config/hooks/live/010-install-app.hook.chroot", iso.REFRESH),
        ("tools/drivers.py", iso.REFRESH),
        ("vendor/drivers/catalog.json", iso.REFRESH),
        ("auto/config", iso.FULL),
        ("config/package-lists/00-base.list.chroot", iso.FULL),
        ("build.sh", iso.FULL),
        ("webplane/api/control_api.py", iso.NO_ISO),
        ("tests/test_core.py", iso.NO_ISO),
        ("tools/sync.sh", iso.NO_ISO),
        ("README.md", iso.NO_ISO),
        ("Makefile", iso.NO_ISO),
        (".gitignore", iso.NO_ISO),
        ("config/package-lists/26-drivers.generated.list.chroot", iso.NO_ISO),
    ],
)
def test_layer_for(path, layer):
    assert iso.layer_for(path) == layer


def test_classify_empty_is_up_to_date():
    assert iso.classify([]) == iso.UP_TO_DATE


def test_classify_takes_the_highest_layer():
    assert iso.classify(["webplane/api/control_api.py", "app/raidforge/x.py"]) == iso.APP_ONLY
    assert (
        iso.classify(
            ["app/raidforge/x.py", "config/hooks/live/070-drivers.hook.chroot"]
        )
        == iso.REFRESH
    )
    assert (
        iso.classify(["config/includes.chroot/etc/raidforge/env", "build.sh"])
        == iso.FULL
    )
    assert iso.classify(["tests/test_core.py", "README.md"]) == iso.NO_ISO


def test_archs_from():
    assert iso.archs_from("both") == ["amd64", "arm64"]
    assert iso.archs_from("arm64") == ["arm64"]
    with pytest.raises(SystemExit):
        iso.archs_from("riscv64")


def test_effective_layer_patch_only_on_amd64():
    caps = iso.Caps(patch=True, refresh=True)
    assert iso.effective_layer(iso.APP_ONLY, "amd64", None, "release", caps)[0] == iso.APP_ONLY
    kind, reasons = iso.effective_layer(iso.APP_ONLY, "arm64", None, "release", caps)
    assert kind == iso.REFRESH
    assert reasons


def test_effective_layer_degrades_on_missing_prerequisites():
    no_patch = iso.Caps(patch=False, refresh=True)
    assert (
        iso.effective_layer(iso.APP_ONLY, "amd64", None, "release", no_patch)[0]
        == iso.REFRESH
    )
    no_refresh = iso.Caps(patch=False, refresh=False)
    assert (
        iso.effective_layer(iso.REFRESH, "amd64", None, "release", no_refresh)[0]
        == iso.FULL
    )
    assert (
        iso.effective_layer(
            iso.REFRESH_VENV, "amd64", None, "release", no_refresh
        )[0]
        == iso.FULL
    )


def test_effective_layer_mode_switch_forces_full():
    state = {"iso": {"mode": "test"}}
    caps = iso.Caps(patch=True, refresh=True)
    for layer in (iso.APP_ONLY, iso.REFRESH, iso.REFRESH_VENV):
        kind, reasons = iso.effective_layer(layer, "amd64", state, "release", caps)
        assert kind == iso.FULL
        assert "mode" in reasons[0]


def test_collect_name_marks_architecture():
    assert iso.collect_name("raidforge-1.2.0-20261105.iso", "amd64") == (
        "raidforge-1.2.0-20261105-X86.iso"
    )
    assert iso.collect_name("raidforge-1.2.0-20261105-arm64.iso", "arm64") == (
        "raidforge-1.2.0-20261105-ARM.iso"
    )
    assert iso.collect_name("raidforge-1.2.0-20261105-arm64-test.iso", "arm64") == (
        "raidforge-1.2.0-20261105-ARM.iso"
    )


def test_build_command_per_layer():
    assert iso.build_command(iso.FULL, "release", "") == "./build.sh"
    assert iso.build_command(iso.REFRESH, "release", "") == "./build.sh --refresh"
    assert (
        iso.build_command(iso.REFRESH_VENV, "release", "")
        == "./build.sh --refresh --venv-reset"
    )
    assert iso.build_command(iso.REFRESH, "test", "") == "./build.sh --fast"
    assert (
        iso.build_command(iso.FULL, "test", "full")
        == "./build.sh --fast --profile full"
    )


def test_state_files_are_per_architecture():
    amd64 = iso.state_path_for("amd64")
    arm64 = iso.state_path_for("arm64")
    assert amd64.name == "iso-state.json"
    assert arm64.name == "iso-state-arm64.json"
    assert amd64 != arm64


def test_fingerprint_groups_cover_gitignored_payloads():
    groups = iso.fingerprint_groups()
    assert set(groups) == {"drivers", "cli", "wheels", "keys"}
    assert all(len(value) == 64 for value in groups.values())


def test_patch_engine_sha_is_tracked():
    assert len(iso.patch_engine_sha()) == 64
