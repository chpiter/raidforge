#!/usr/bin/env bash
#
# deploy_webplane.sh - build the Vue UI, sync the tree, and restart webplane
# on the remote control-plane host (default 192.168.40.132:/root/raidforge).
#
# Credentials are read from tools/.remote.env (git-ignored).  The service runs
# on port 8080 with the default agent token "raidforge".  A pid file is used so
# this script never has to pattern-kill unrelated processes (e.g. ISO builds).
#
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Git Bash rewrites /root/... arguments before they reach python.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

REMOTE_DIR="${REMOTE_DIR:-/root/raidforge}"
REMOTE_HOST="${REMOTE_HOST:-192.168.40.132}"
WEBPLANE_PORT="${WEBPLANE_PORT:-8080}"
WEBPLANE_TOKEN="${WEBPLANE_AGENT_TOKEN:-raidforge}"

log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m  %s\n' "$*" >&2; exit 1; }

[[ -f tools/.remote.env ]] || die "tools/.remote.env missing (REMOTE_HOST/REMOTE_USER/REMOTE_PASS)"

PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in python3 python C:/Python312/python.exe; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import paramiko' >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi
[[ -n "$PYTHON_BIN" ]] || die "no python with paramiko found (set PYTHON=...)"

log "1/5 build frontend"
if command -v npm >/dev/null 2>&1; then
    (cd webplane/web && npm ci --no-audit --no-fund && npm run build)
else
    die "npm not found; cannot build webplane/web/dist"
fi

log "2/5 sync source to ${REMOTE_HOST}:${REMOTE_DIR}"
./tools/sync.sh

log "3/5 upload frontend dist (excluded from sync)"
"$PYTHON_BIN" tools/rssh.py push webplane/web/dist "$REMOTE_DIR/webplane/web/dist"

log "4/5 ensure remote venv and restart webplane on port ${WEBPLANE_PORT}"
REMOTE_CMD=$(cat <<EOF
set -e
cd "$REMOTE_DIR"
if [ ! -x webplane/.venv/bin/python ]; then
    python3 -m venv webplane/.venv
fi
webplane/.venv/bin/pip install -q -r webplane/requirements.txt
if [ -f webplane.pid ]; then
    kill "\$(cat webplane.pid)" 2>/dev/null || true
    rm -f webplane.pid
fi
WEBPLANE_DATA_DIR="$REMOTE_DIR/webplane-data" \\
WEBPLANE_AGENT_TOKEN="$WEBPLANE_TOKEN" \\
WEBPLANE_HOST=0.0.0.0 \\
WEBPLANE_PORT=$WEBPLANE_PORT \\
nohup webplane/.venv/bin/python -m webplane > webplane.log 2>&1 < /dev/null &
echo \$! > webplane.pid
sleep 2
curl -fsS "http://127.0.0.1:$WEBPLANE_PORT/api/cp/health"
echo
EOF
)
"$PYTHON_BIN" tools/rssh.py exec "$REMOTE_CMD"

log "5/5 external health check"
if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://${REMOTE_HOST}:${WEBPLANE_PORT}/api/cp/health" && echo
else
    warn "curl not found; skipped external health check"
fi

log "done: http://${REMOTE_HOST}:${WEBPLANE_PORT}/servers"
