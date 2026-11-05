"""sync.sh defaults: the evidence private key ships with every sync.

A keyless build host produced a keyless ISO, and every upload then failed with
"ssh key missing" - the actual field bug.  These tests pin the default and the
two escape hatches without needing ssh or paramiko (--print-excludes exits
before the host is contacted).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SYNC = REPO / "tools" / "sync.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="needs bash to evaluate sync.sh"
)


def _excludes(*args: str) -> str:
    completed = subprocess.run(
        ["bash", str(SYNC), "--print-excludes", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_key_is_synced_by_default():
    output = _excludes()
    assert "--exclude" in output
    assert "keys" not in output.split()
    # wheels and driver payloads stay opt-in (--with-wheels / --with-drivers)
    assert "vendor/wheels" in output
    assert "vendor/cli" in output


def test_with_drivers_includes_vendor_payloads():
    output = _excludes("--with-drivers")
    assert "vendor/cli" not in output
    assert "vendor/wheels" in output


def test_arm64_flag_is_accepted():
    completed = subprocess.run(
        ["bash", str(SYNC), "--arch", "arm64", "--print-excludes"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--exclude" in completed.stdout


def test_no_keys_excludes_the_key_directory():
    lines = _excludes("--no-keys").splitlines()
    assert any(
        line.strip() == "--exclude" and lines[index + 1].strip() == "keys"
        for index, line in enumerate(lines[:-1])
    )


def test_with_keys_is_a_compatibility_noop():
    assert _excludes("--with-keys") == _excludes()
