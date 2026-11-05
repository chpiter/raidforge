#!/usr/bin/env bash
#
# sync.sh - push this project tree to the debug/build host.
#
# Usage:
#   ./tools/sync.sh                  # one shot: upload what changed (incl. evidence key)
#   ./tools/sync.sh --all            # ignore mtimes, re-upload every file
#   ./tools/sync.sh --dry-run        # list what would be uploaded, upload nothing
#   ./tools/sync.sh --no-keys        # do NOT upload config/includes.chroot/etc/raidforge/keys
#   ./tools/sync.sh --with-wheels    # also copy vendor/wheels (off by default)
#   ./tools/sync.sh --with-drivers   # also copy vendor/cli + vendor/drivers
#   ./tools/sync.sh --arch arm64     # push to /srv/raidforge-arm64 (arm64 VM)
#   ./tools/sync.sh --with-keys      # compatibility alias: keys already upload by default
#   ./tools/sync.sh --watch [secs]   # keep syncing on change (default 3s)
#   ./tools/sync.sh --run "cmd"      # run a command on the host (no upload)
#   ./tools/sync.sh --print-excludes # print the transfer exclusion list and exit
#   REMOTE_DIR=/srv/eraser ./tools/sync.sh
#
# With --arch arm64 the tree is pushed to /srv/raidforge-arm64 and --run is
# executed inside the arm64 VM through tools/arm64_vm.sh.
#
# Build artefacts are never uploaded: cache/, chroot/, binary/, .build and
# *.iso are produced on the build host, and copying them back and forth would
# be both slow and destructive.
#
# Credentials come from tools/.remote.env (git-ignored) or the environment.
#
set -Eeuo pipefail

# Git Bash rewrites anything that looks like a posix path (/root/... becomes
# C:/Program Files/Git/root/...) before it reaches python, which silently
# creates a literal "C:" directory on the remote host.  Kill that behaviour
# for every child process of this script.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# With MSYS_NO_PATHCONV exported, bash hands /c/Users/... to the native python,
# which does not know that layout and os.walk() finds nothing.  The local side
# therefore needs a real Windows path; the remote side must stay posix.
if command -v cygpath >/dev/null 2>&1; then
    LOCAL_ARG="$(cygpath -w "$ROOT_DIR")"
else
    LOCAL_ARG="$ROOT_DIR"
fi

REMOTE_DIR="${REMOTE_DIR:-}"
ARCH_TARGET="${ARCH_TARGET:-amd64}"
INTERVAL=3
MODE="once"
NO_SKIP=""
ADD_WHEELS=0
ADD_DRIVERS=0
SKIP_KEYS=0
PRINT_EXCLUDES=0
RUN_CMD=""

log()  { printf '\033[1;34m[sync]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m  %s\n' "$*" >&2; exit 1; }

# Print the leading comment block (everything between the shebang and the first
# non-comment line) so the header can grow without renumbering a sed range.
usage() { awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)            NO_SKIP="--no-skip" ;;
        --dry-run)        MODE="dry" ;;
        --with-wheels)    ADD_WHEELS=1 ;;
        --with-drivers)   ADD_DRIVERS=1 ;;
        --arch)           [[ -n "${2:-}" ]] || die "--arch needs a value"
                          ARCH_TARGET="$2"; shift ;;
        --no-keys)        SKIP_KEYS=1 ;;
        --with-keys)      SKIP_KEYS=0 ;;   # compatibility alias
        --print-excludes) PRINT_EXCLUDES=1 ;;
        --watch)          MODE="watch"
                          if [[ "${2:-}" =~ ^[0-9]+$ ]]; then INTERVAL="$2"; shift; fi ;;
        --run)            [[ -n "${2:-}" ]] || die "--run needs a command"
                          RUN_CMD="$2"; shift ;;
        -h|--help)        usage; exit 0 ;;
        *)                die "unknown argument: $1" ;;
    esac
    shift
done

EXCLUDES=(
    # vcs / editor noise
    --exclude .git
    --exclude .idea
    --exclude .vscode
    --exclude .DS_Store
    # live-build working tree + outputs (produced on the build host)
    --exclude .build
    --exclude bootstrap
    --exclude cache
    --exclude .cache
    --exclude chroot
    --exclude binary
    --exclude .tmp
    --exclude '*.iso'
    --exclude '*.iso.sha256'
    --exclude '*.contents'
    --exclude '*.packages'
    --exclude '*.buildinfo'
    # staged copies recreated by build.sh
    --exclude 'config/includes.chroot/usr/src'
    # debug helper credentials stay local
    --exclude .remote.env
    # local evidence
    --exclude evidence-local
    # local virtualenvs are never part of the ISO
    --exclude .venv
    --exclude venv
    # webplane frontend deps / build output and local control-plane data
    --exclude node_modules
    --exclude dist
    --exclude webplane-data
    --exclude '*.sqlite3*'
)
# The evidence upload private key IS synced by default: an ISO built without it
# cannot upload a single session (try_rsync bails with "ssh key missing"), so a
# keyless sync silently produces a keyless, useless image.  On a shared build
# host pass `--no-keys` and place the key there by hand.
if [[ "$SKIP_KEYS" == "1" ]]; then
    EXCLUDES+=(--exclude keys)
fi
if [[ "$ADD_WHEELS" == "0" ]]; then
    # the build host keeps its own wheel cache; re-uploading it every sync is
    # tens of megabytes for no benefit
    EXCLUDES+=(--exclude 'vendor/wheels')
fi
if [[ "$ADD_DRIVERS" == "0" ]]; then
    # vendor binaries and out-of-tree drivers are large and change rarely
    EXCLUDES+=(--exclude 'vendor/cli' --exclude 'vendor/drivers/amd64' --exclude 'vendor/drivers/arm64')
fi

# Used by tests/sync-flags checks: dump the decision without needing ssh.
if [[ "$PRINT_EXCLUDES" == "1" ]]; then
    printf '%s\n' "${EXCLUDES[@]}"
    exit 0
fi

# --------------------------------------------------------------------------
# pick an interpreter that actually has paramiko
# --------------------------------------------------------------------------
# Note: the `py` launcher is deliberately not used - it honours the script's
# `#!/usr/bin/env python3` shebang and can land on a different interpreter than
# `py -c`, which is exactly how "paramiko is installed but import fails" happens.
PYTHON_BIN=""
pick_python() {
    local candidate
    for candidate in \
        "${PYTHON:-}" \
        python3 \
        python \
        C:/Python312/python.exe \
        /c/Python312/python.exe \
        C:/Python311/python.exe \
        /c/Python311/python.exe \
        /usr/bin/python3
    do
        [[ -n "$candidate" ]] || continue
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import paramiko' >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            return 0
        fi
    done
    return 1
}

pick_python || die "no python with paramiko found - pip install paramiko, or set PYTHON=/path/to/python"

[[ -f tools/.remote.env ]] || die "tools/.remote.env missing (REMOTE_HOST / REMOTE_USER / REMOTE_PASS)"
REMOTE_HOST="$(sed -n 's/^REMOTE_HOST=//p' tools/.remote.env | head -1)"

if [[ -z "$REMOTE_DIR" ]]; then
    if [[ "$ARCH_TARGET" == "amd64" ]]; then
        REMOTE_DIR="/root/raidforge"
    else
        # Arm64 builds run inside an emulated VM; this host directory is shared
        # into the VM with virtio-9p and is also where the ISO is copied back.
        REMOTE_DIR="/srv/raidforge-${ARCH_TARGET}"
    fi
fi

log "host ${REMOTE_HOST:-<unset>} -> ${REMOTE_DIR} (arch ${ARCH_TARGET}, python: ${PYTHON_BIN})"

if [[ -n "$RUN_CMD" ]]; then
    local_cmd="$RUN_CMD"
    if [[ "$ARCH_TARGET" != "amd64" ]]; then
        local_cmd="./tools/arm64_vm.sh run \"${local_cmd//\"/\\\"}\""
    fi
    log "remote: ${RUN_CMD}"
    set +e
    "$PYTHON_BIN" tools/rssh.py exec "cd '${REMOTE_DIR}' && ${local_cmd}"
    rc=$?
    set -e
    exit "$rc"
fi

# --------------------------------------------------------------------------
# transfer
# --------------------------------------------------------------------------
# Windows has no exec bit: path.stat() reports no S_IXUSR for build.sh, so the
# SFTP side would land everything as 0644 and `./build.sh` would then fail with
# "permission denied" on the build host.  Restore the bits once, remotely.
fix_modes() {
    "$PYTHON_BIN" tools/rssh.py exec "cd '${REMOTE_DIR}' && \
chmod 0755 build.sh tools/*.sh tools/*.py 2>/dev/null; \
chmod 0755 auto/* config/hooks/live/*.hook.* 2>/dev/null; \
chmod 0755 config/includes.chroot/usr/local/sbin/* 2>/dev/null; \
find vendor/cli -mindepth 2 -maxdepth 2 -type f -exec chmod 0755 {} + 2>/dev/null; \
find tests -name '*.sh' -exec chmod 0755 {} + 2>/dev/null; \
chmod 0755 config/includes.chroot/usr/local/bin/storcli config/includes.chroot/usr/local/bin/storcli64 config/includes.chroot/usr/local/bin/perccli config/includes.chroot/usr/local/bin/perccli64 config/includes.chroot/usr/local/bin/arcconf config/includes.chroot/usr/local/bin/ssacli config/includes.chroot/usr/local/bin/hpssacli config/includes.chroot/usr/local/bin/hiraidadm 2>/dev/null; \
if [ -d config/includes.chroot/etc/raidforge/keys ]; then chmod 0700 config/includes.chroot/etc/raidforge/keys; find config/includes.chroot/etc/raidforge/keys -type f -exec chmod 0600 {} +; fi; \
true" >/dev/null
}

do_sync() {
    local start elapsed extra=""
    [[ "$MODE" == "dry" ]] && extra="--dry-run"
    start=$SECONDS
    "$PYTHON_BIN" tools/rssh.py push "$LOCAL_ARG" "$REMOTE_DIR" "${EXCLUDES[@]}" ${NO_SKIP} ${extra}
    elapsed=$(( SECONDS - start ))
    if [[ "$MODE" == "dry" ]]; then
        log "dry-run only - nothing was uploaded"
    else
        fix_modes
        log "done in ${elapsed}s"
        if [[ "$SKIP_KEYS" == "1" ]] && compgen -G "config/includes.chroot/etc/raidforge/keys/*" >/dev/null; then
            warn "evidence upload key was skipped (--no-keys) - rsync uploads will fail"
        elif [[ "$SKIP_KEYS" == "0" ]] && ! compgen -G "config/includes.chroot/etc/raidforge/keys/*" >/dev/null; then
            warn "no evidence upload key in config/includes.chroot/etc/raidforge/keys - the ISO cannot rsync"
        fi
    fi
}

if [[ "$MODE" != "watch" ]]; then
    do_sync
    exit 0
fi

# --------------------------------------------------------------------------
# watch mode: poll mtimes, no extra dependencies
# --------------------------------------------------------------------------
STAMP="$(mktemp)"
cleanup() { rm -f "$STAMP"; }
trap cleanup EXIT INT TERM
touch "$STAMP"

log "watching for changes every ${INTERVAL}s - Ctrl+C to stop"
do_sync
touch "$STAMP"

while true; do
    sleep "$INTERVAL"
    changed="$(find . \
        \( -name .git -o -name cache -o -name chroot -o -name binary -o -name .build \
           -o -name bootstrap -o -name __pycache__ -o -name .pytest_cache -o -name .cache \
           -o -name .venv \) -prune \
        -o -type f -newer "$STAMP" -print 2>/dev/null | head -5)"
    if [[ -n "$changed" ]]; then
        log "change detected:"
        printf '  %s\n' $changed
        do_sync
        touch "$STAMP"
    fi
done
