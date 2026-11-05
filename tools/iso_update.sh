#!/usr/bin/env bash
#
# iso_update.sh - classify local changes and choose the cheapest ISO path.
#
#   ./tools/iso_update.sh --adopt                 # record the current ISO baseline
#   ./tools/iso_update.sh --dry-run               # print classification only
#   ./tools/iso_update.sh                         # app-only patch or full fallback
#   ./tools/iso_update.sh --test                  # zstd + -test image
#   ./tools/iso_update.sh --baseline [--fast]     # force a normal build
#   ./tools/iso_update.sh --arch arm64            # arm64 state/baseline (VM build)
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
