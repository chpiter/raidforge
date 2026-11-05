#!/usr/bin/env bash
#
# iso_update.sh - layered incremental ISO update (patch -> refresh -> full).
#
#   ./tools/iso_update.sh --adopt                 # record the current X86/ARM ISO baseline
#   ./tools/iso_update.sh --dry-run               # print layer/command/eta only (no network)
#   ./tools/iso_update.sh                         # amd64 + arm64, cheapest safe layer
#   ./tools/iso_update.sh --arch amd64            # one architecture
#   ./tools/iso_update.sh --refresh               # force the refresh layer
#   ./tools/iso_update.sh --test                  # zstd + -test image (not collected)
#   ./tools/iso_update.sh --baseline [--fast]     # force a normal build
#   ./tools/iso_update.sh --no-collect            # do not copy into /root/raidforge/out
#
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in python python3 C:/Python312/python.exe; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import paramiko' >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi
[[ -n "$PYTHON_BIN" ]] || {
    echo "no python with paramiko found (set PYTHON=...)" >&2
    exit 2
}

exec "$PYTHON_BIN" tools/iso_update.py "$@"
