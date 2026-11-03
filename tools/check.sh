#!/usr/bin/env bash
#
# check.sh - L0: everything that must be true before an ISO build (~15s).
#
#   ./tools/check.sh          # unit tests + detection walk + UI smoke + palette
#   ./tools/check.sh --fast   # skip the UI parts (no Textual needed)
#
# This exists because a full ISO build is 6m45s, of which the application is
# about 5s.  Iterating on code through the ISO is 80x more expensive than it
# needs to be, and most mistakes show up here.
#
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

FAILED=0
log()  { printf '\033[1;34m[check]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m   ok\033[0m  %s\n' "$*"; }
bad()  { printf '\033[1;31m  FAIL\033[0m %s\n' "$*" >&2; FAILED=1; }
warn() { printf '\033[1;33m[warn]\033[0m  %s\n' "$*" >&2; }

# --- pick interpreters -----------------------------------------------------
# Anything with pytest runs the unit tests; only an interpreter with the
# *pinned* Textual can run the UI smoke test and the live palette guard.  The
# shipped ISO uses app/requirements.txt; a dev box with Textual 8.x renders
# differently, and "fixing" the app for it would be fixing the wrong target.
PINNED_TEXTUAL="$(sed -n 's/^textual==\([0-9][0-9.]*\).*/\1/p' app/requirements.txt | head -1)"
PY_ANY=""
PY_UI=""
for candidate in "${PYTHON:-}" python3 python C:/Python312/python.exe /usr/bin/python3; do
    [[ -n "$candidate" ]] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    "$candidate" -c 'import pytest' >/dev/null 2>&1 || continue
    [[ -z "$PY_ANY" ]] && PY_ANY="$candidate"
    if "$candidate" -c 'import textual' >/dev/null 2>&1; then
        version="$("$candidate" -c 'import textual; print(getattr(textual, "__version__", ""))' 2>/dev/null)"
        if [[ -z "$PINNED_TEXTUAL" || "$version" == "$PINNED_TEXTUAL" ]]; then
            PY_UI="$candidate"
            break
        fi
        warn "$candidate has textual ${version:-?}, need ${PINNED_TEXTUAL} - not using it for UI checks"
    fi
done

if [[ -z "$PY_ANY" ]]; then
    bad "no python with pytest found (pip install pytest, or set PYTHON=...)"
    exit 1
fi

log "interpreter: $PY_ANY${PY_UI:+  (textual: $PY_UI)}"

# --- 0. shell syntax -------------------------------------------------------
# A syntax error in sync.sh would otherwise only appear on the operator's
# machine, after the edit meant to be verified.
syntax_ok=1
for script in build.sh tools/*.sh auto/* config/hooks/live/*.hook.* tests/*.sh; do
    [[ -f "$script" ]] || continue
    if ! bash -n "$script" 2>/tmp/erase-check-bash.err; then
        bad "bash -n $script"
        tail -3 /tmp/erase-check-bash.err >&2
        syntax_ok=0
    fi
done
[[ "$syntax_ok" == "1" ]] && ok "bash -n"

# --- 1. unit tests ---------------------------------------------------------
if "$PY_ANY" -m pytest tests -q; then
    ok "pytest"
else
    bad "pytest"
fi

# --- 2. detection ladder (mock CLIs, no hardware needed) --------------------
if [[ -x tests/test_detection.sh ]]; then
    if PYTHON="${PY_UI:-$PY_ANY}" bash tests/test_detection.sh --mock >/tmp/erase-check-detection.log 2>&1; then
        ok "detection walk (mock)"
    else
        bad "detection walk (mock) - see /tmp/erase-check-detection.log"
        tail -20 /tmp/erase-check-detection.log >&2
    fi
fi

if [[ "$FAST" == "1" ]]; then
    [[ "$FAILED" == "0" ]] || exit 1
    log "fast mode: skipped the UI checks"
    exit 0
fi

# --- 3. UI smoke at the shipped console size -------------------------------
if [[ -z "$PY_UI" ]]; then
    warn "no interpreter with the pinned Textual (${PINNED_TEXTUAL:-see app/requirements.txt}) - UI smoke and live palette check SKIPPED."
    warn "That means the thing that broke on the appliance is NOT being verified."
    warn "Run this inside the build chroot, or install the pinned wheel into a venv."
else
    if UI_SMOKE_SIZE=80x25 RAIDFORGE_APP_ROOT="$ROOT_DIR/app" "$PY_UI" tests/ui_smoke.py >/tmp/erase-check-ui.log 2>&1; then
        ok "UI smoke (80x25)"
    else
        bad "UI smoke (80x25) - see /tmp/erase-check-ui.log"
        tail -25 /tmp/erase-check-ui.log >&2
    fi
fi

exit "$FAILED"
