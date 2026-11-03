"""Guard against stale project/branding names after the raidforge rename."""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    ".pytest_cache",
    "__pycache__",
    "artifacts",
    "vendor",
    "binary",
    "chroot",
    "cache",
    ".build",
    ".git",
}
SKIP_FILES = {
    Path("tools") / ".remote.env",
    Path("config") / "includes.chroot" / "etc" / "raidforge" / "keys" / "evidence_ed25519",
    Path("tests") / "test_rename_guard.py",
}
TEXT_EXT = {
    ".py",
    ".sh",
    ".md",
    ".toml",
    ".txt",
    ".example",
    ".yaml",
    ".yml",
    ".cfg",
    ".network",
    ".service",
    ".conf",
    ".list",
    ".chroot",
    ".binary",
    ".hook",
    ".json",
}
NAMES = {"Makefile", ".gitignore", "Dockerfile", "env", "README.vendor-tools"}
OLD_TOKENS = (
    "erase_app",
    "erase-app",
    "erase-tui",
    "ERASE_",
    "debian-live-eraser",
    "erase-mono",
    "erase-evidence",
    "erase-report",
    "erase-build-info",
    "erase-console",
    "erase-dhcp",
    "EraseApp",
    "ERASE-TUI",
)


def text_files():
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in names:
            path = Path(root) / name
            rel = path.relative_to(REPO)
            if rel in SKIP_FILES:
                continue
            if path.suffix.lower() in {".iso", ".png", ".jpg", ".jpeg", ".svg", ".cast", ".pyc", ".whl"}:
                continue
            if rel.parts and rel.parts[0] == "auto":
                pass
            elif path.suffix.lower() not in TEXT_EXT and name not in NAMES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            yield rel, text


def test_no_stale_branding_tokens():
    offenders = []
    for rel, text in text_files():
        in_migration = False
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "LEGACY MIGRATION BEGIN" in line:
                in_migration = True
                continue
            if "LEGACY MIGRATION END" in line:
                in_migration = False
                continue
            if in_migration or "LEGACY MIGRATION" in line:
                continue
            for token in OLD_TOKENS:
                if token in line:
                    offenders.append(f"{rel}:{line_number}: {line.strip()[:140]}")
                    break
    assert not offenders, "stale raidforge rename tokens:\n" + "\n".join(offenders)
