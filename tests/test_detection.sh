#!/usr/bin/env bash
#
# tests/test_detection.sh - verify the detection chain, the enumeration output
# and the dry-run command construction without touching a real disk.
#
# Two modes:
#
#   tests/test_detection.sh          --mock (default): fake CLIs built from
#                                     tests/fixtures are put on PATH
#   tests/test_detection.sh --real   run against whatever this machine has
#                                     (NEVER executes anything: we only print plans)
#
# The mock CLIs are plain POSIX shell scripts without a file extension, so this
# integration test needs a POSIX host: Debian/Ubuntu, macOS, or WSL.  On Windows
# use `pytest tests` instead - tests/test_pipeline.py covers the same decision
# tree with the process layer faked out.
#
# Exit code 0 = every check passed.
#
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FIXTURES="$ROOT_DIR/tests/fixtures"
# The mock CLIs are separate processes: any mock written with a quoted heredoc
# (<<'EOF') can only see $FIXTURES if it is exported.  Without this the smartctl
# and lsscsi stubs silently cat "/smartctl_ssd.txt" and every disk ends up
# UNKNOWN - which looks like a parser bug but is really a test-harness bug.
export FIXTURES
APP_DIR="$ROOT_DIR/app"
PYTHON="${PYTHON:-python3}"
# Resolve to an absolute path: plan_output() runs with a scrubbed PATH, and the
# shell would fail to find a bare "python3" inside that assignment.
PYTHON="$(command -v "$PYTHON" 2>/dev/null || printf '%s' "$PYTHON")"

MODE="mock"
[[ "${1:-}" == "--real" ]] && MODE="real"
[[ "${1:-}" == "--mock" ]] && MODE="mock"

PASS=0
FAIL=0
MOCK_BIN=""

log()  { printf '\033[0;36m[test]\033[0m %s\n' "$*"; }
ok()   { printf '  \033[0;32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[0;31mFAIL\033[0m %s\n' "$*" >&2; FAIL=$((FAIL+1)); }

expect_contains() {
    # expect_contains <file> <needle> <description>
    if grep -Fq -- "$2" "$1"; then
        ok "$3"
    else
        bad "$3"
        printf '       expected to find: %s\n' "$2" >&2
        printf '       in output:\n' >&2
        sed 's/^/         /' "$1" >&2
    fi
}

expect_missing() {
    if ! grep -Fq -- "$2" "$1"; then
        ok "$3"
    else
        bad "$3"
        printf '       did NOT expect: %s\n' "$2" >&2
    fi
}

cleanup() {
    [[ -n "$MOCK_BIN" && -d "$MOCK_BIN" ]] && rm -rf "$MOCK_BIN"
    return 0
}
trap cleanup EXIT

# --------------------------------------------------------------------------
# mock CLIs
# --------------------------------------------------------------------------
build_mock_bin() {
    # Use a POSIX style location: some hosts (MSYS/Cygwin, hardened shells)
    # refuse recursive deletes inside the Windows profile temp hierarchy.
    MOCK_BIN="/tmp/erase-mock-$$-${RANDOM}"
    mkdir -p "$MOCK_BIN"
    local f

    cat > "$MOCK_BIN/storcli" <<'EOF'
#!/bin/sh
F="$FIXTURES"
if [ "${MOCK_RAID:-0}" = "1" ]; then
  case "$*" in
    "show J")                cat "$F/storcli_show_J.json" ;;
    "/c0 show J")            cat "$F/storcli_c0_show_J.json" ;;
    "/c0/eall/sall show J")  cat "$F/storcli_sall_show_J_raid.json" ;;
    "/c0/vall show all J")   cat "$F/storcli_vall_show_all_J.json" ;;
    *) echo "Status = Failure" ; exit 1 ;;
  esac
  exit 0
fi
case "$*" in
  "show J")                cat "$F/storcli_show_J.json" ;;
  "/c0 show J")            cat "$F/storcli_c0_show_J.json" ;;
  "/c0/eall/sall show J")  cat "$F/storcli_sall_show_J.json" ;;
  *) echo "Status = Failure" ; exit 1 ;;
esac
EOF
    cp "$MOCK_BIN/storcli" "$MOCK_BIN/storcli64"
    cp "$MOCK_BIN/storcli" "$MOCK_BIN/perccli64"

    cat > "$MOCK_BIN/arcconf" <<'EOF'
#!/bin/sh
F="$FIXTURES"
if [ "${MOCK_RAID:-0}" = "1" ]; then
  case "$*" in
    "list")                  cat "$F/arcconf_list.txt" ;;
    "getconfigjson 1")       cat "$F/arcconf_getconfigjson_raid.json" ;;
    "getconfig 1 ld")        cat "$F/arcconf_getconfig_ld.txt" ;;
    *) echo "Command failed" ; exit 1 ;;
  esac
  exit 0
fi
case "$*" in
  "list")                  cat "$F/arcconf_list.txt" ;;
  "getconfigjson 1")       cat "$F/arcconf_getconfigjson.json" ;;
  "getconfig 1 pd")        echo "no json support" ; exit 1 ;;
  *) echo "Command failed" ; exit 1 ;;
esac
EOF

    cat > "$MOCK_BIN/ssacli" <<'EOF'
#!/bin/sh
F="$FIXTURES"
if [ "${MOCK_RAID:-0}" = "1" ]; then
  case "$*" in
    "ctrl all show")                        cat "$F/ssacli_ctrl_all_show.txt" ;;
    "ctrl slot=0 pd all show detail")       cat "$F/ssacli_pd_all_show_detail.txt" ;;
    "ctrl slot=0 ld all show detail")       cat "$F/ssacli_ld_all_show_detail.txt" ;;
    "ctrl slot=0 pd all show")              cat "$F/ssacli_pd_all_show.txt" ;;
    "ctrl slot=0 show detail")              cat "$F/ssacli_ctrl_all_show.txt" ;;
    "ctrl slot=0 show config detail")       cat "$F/ssacli_ld_all_show_detail.txt" ;;
    *) echo "Error: no such controller" ; exit 1 ;;
  esac
  exit 0
fi
case "$*" in
  "ctrl all show")                      cat "$F/ssacli_ctrl_all_show.txt" ;;
  "ctrl slot=0 pd all show detail")     cat "$F/ssacli_pd_all_show_detail.txt" ;;
  *) echo "Error: no such controller" ; exit 1 ;;
esac
EOF
    cp "$MOCK_BIN/ssacli" "$MOCK_BIN/hpssacli"

    cat > "$MOCK_BIN/sg_scan" <<'EOF'
#!/bin/sh
echo "/dev/sg0: scsi0 ch 0 id 0 lun 0 [dt 0x0] disk"
echo "/dev/sg1: scsi0 ch 0 id 1 lun 0 [dt 0x0] disk"
EOF

    cat > "$MOCK_BIN/lsscsi" <<EOF
#!/bin/sh
case "\$1" in
  -g) cat "$FIXTURES/lsscsi.txt" ;;
  *)  cat "$FIXTURES/lsscsi.txt" ;;
esac
EOF

    cat > "$MOCK_BIN/smartctl" <<'EOF'
#!/bin/sh
node=""
for a in "$@"; do case "$a" in /dev/*) node="$a";; esac; done
F="$FIXTURES"
case "$node" in
  /dev/sda)  cat "$F/smartctl_ssd.txt" ;;
  /dev/sdb)  cat "$F/smartctl_hdd.txt" ;;
  *)         echo "smartctl: unknown device"; exit 4 ;;
esac
EOF

    cat > "$MOCK_BIN/nvme" <<'EOF'
#!/bin/sh
echo "[]"
EOF

    chmod +x "$MOCK_BIN"/*
    log "mock CLIs created in $MOCK_BIN"
}

# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
plan_output() {
    # plan_output <available-bin-list> -> prints plan to stdout
    # ``available`` is a space separated list of tool names to expose.
    local available="$1" bin tool cmd_path
    bin="/tmp/erase-bin-$$-${RANDOM}"
    mkdir -p "$bin"
    for tool in $available; do
        [[ -f "$MOCK_BIN/$tool" ]] && cp "$MOCK_BIN/$tool" "$bin/$tool" || true
    done
    # PATH must contain the mock bin *only*: with "$bin:$PATH" the host's real
    # lsscsi/smartctl leak through, so "nothing available" (case 5) still
    # enumerated the host's own /dev/sda.  The stubs shell out to cat & friends,
    # so a minimal coreutils set is symlinked into the sandbox instead.
    for tool in cat head tail grep sed awk cut sort uniq tr wc mktemp sha256sum; do
        cmd_path="$(command -v "$tool" 2>/dev/null)" || continue
        ln -sf "$cmd_path" "$bin/$tool"
    done
    PATH="$bin" \
    RAIDFORGE_REPORT_ENABLED=false \
    RAIDFORGE_EVIDENCE_ROOT="$(mktemp -d)" \
    "$PYTHON" "$APP_DIR/run.py" --dry-run --no-record --no-upload --print-plan
    rm -rf "$bin"
}

raid_plan_output() {
    # Same sandbox, but the read-only RAID topology printer.  MOCK_RAID=1 makes
    # the vendor stubs answer the RAID subcommands with RAID fixtures.
    local available="$1" bin tool cmd_path
    bin="/tmp/erase-raid-bin-$$-${RANDOM}"
    mkdir -p "$bin"
    for tool in $available; do
        [[ -f "$MOCK_BIN/$tool" ]] && cp "$MOCK_BIN/$tool" "$bin/$tool" || true
    done
    for tool in cat head tail grep sed awk cut sort uniq tr wc mktemp sha256sum; do
        cmd_path="$(command -v "$tool" 2>/dev/null)" || continue
        ln -sf "$cmd_path" "$bin/$tool"
    done
    PATH="$bin" \
    MOCK_RAID=1 \
    RAIDFORGE_REPORT_ENABLED=false \
    RAIDFORGE_EVIDENCE_ROOT="$(mktemp -d)" \
    "$PYTHON" "$APP_DIR/run.py" --dry-run --no-record --no-upload --print-raid-plan
    rm -rf "$bin"
}

# --------------------------------------------------------------------------
# checks: mock mode
# --------------------------------------------------------------------------
if [[ "$MODE" == "mock" ]]; then
    build_mock_bin

    echo
    log "== case 1: storcli wins the ladder =="
    out_file="$(mktemp)"
    plan_output "storcli arcconf ssacli lsscsi smartctl nvme sg_scan" > "$out_file"
    expect_contains "$out_file" "tool=storcli" "detection picks storcli"
    expect_contains "$out_file" "storcli /c0/e32/s0 start erase thorough" "HDD -> thorough"
    expect_contains "$out_file" "storcli /c0/e32/s1 start erase secure" "SED SSD -> sanitize"
    expect_contains "$out_file" "c0e32s2                [UNKNOWN media - not erasable]" "unknown media is refused"
    expect_missing   "$out_file" "c0e32s2                storcli" "unknown media gets no command"

    echo
    log "== case 2: storcli absent, arcconf wins =="
    plan_output "arcconf ssacli lsscsi smartctl nvme sg_scan" > "$out_file"
    expect_contains "$out_file" "tool=arcconf" "detection picks arcconf"
    expect_contains "$out_file" "arcconf TASK START 1 DEVICE 0 0 erase NOPROMPT" "PMC HDD -> erase"
    expect_contains "$out_file" "arcconf TASK START 1 DEVICE 0 1 secureerase NOPROMPT" "PMC SSD -> block erase (secureerase)"

    echo
    log "== case 3: only ssacli present =="
    plan_output "ssacli lsscsi smartctl nvme sg_scan" > "$out_file"
    expect_contains "$out_file" "tool=ssacli" "detection picks ssacli"
    expect_contains "$out_file" "ssacli ctrl slot=0 pd 1I:1:1 modify erase erasepattern=overwrite forced" "HPE HDD -> overwrite pattern"
    expect_contains "$out_file" "ssacli ctrl slot=0 pd 1I:1:2 modify erase erasepattern=block forced" "HPE SSD -> block erase pattern"
    expect_contains "$out_file" "[UNKNOWN media - not erasable]" "HPE disk without rotational speed is refused"

    echo
    log "== case 4: no RAID CLI, generic sg3_utils path =="
    plan_output "lsscsi smartctl nvme sg_scan" > "$out_file"
    expect_contains "$out_file" "tool=sg3_utils" "detection falls back to sg3_utils"
    expect_contains "$out_file" "blkdiscard --force /dev/sda" "SATA SSD -> block erase"
    expect_contains "$out_file" "sg_format --format --size=512 --verify /dev/sdb" "SAS HDD -> format+verify"

    echo
    log "== case 5: nothing available =="
    plan_output "" > "$out_file"
    expect_contains "$out_file" "tool=none" "no tool -> graceful degradation"
    expect_contains "$out_file" "disks=0" "no inventory without tools"

    echo
    log "== case 6: storcli64 alias + read-only RAID topology =="
    raid_plan_output "storcli64 lsscsi smartctl nvme sg_scan" > "$out_file"
    expect_contains "$out_file" "tool=storcli binary=storcli64" "storcli64 is accepted"
    expect_contains "$out_file" "volumes: 2" "two VDs parsed"
    expect_contains "$out_file" "free disks: c0e32s6" "only UGood/unmapped disk is free"
    expect_contains "$out_file" "blocked c0e32s7:" "foreign disk is blocked"

    echo
    log "== case 7: perccli64 alias =="
    raid_plan_output "perccli64 lsscsi smartctl nvme sg_scan" > "$out_file"
    expect_contains "$out_file" "tool=perccli binary=perccli64" "perccli64 is accepted"
    expect_contains "$out_file" "volumes: 2" "perccli RAID topology parsed"

    echo
    log "== case 8: hpssacli alias + HPE RAID topology =="
    raid_plan_output "hpssacli lsscsi smartctl nvme sg_scan" > "$out_file"
    expect_contains "$out_file" "tool=ssacli binary=hpssacli" "hpssacli is accepted"
    expect_contains "$out_file" "volumes: 2" "HPE logical drives parsed"
else
    echo
    log "real mode: printing the plan for this machine (nothing is executed)"
    "$PYTHON" "$APP_DIR/run.py" --dry-run --no-record --no-upload --print-plan || {
        bad "the application exited non-zero"
    }
    ok "real probe completed"
fi

echo
if [[ "$FAIL" -eq 0 ]]; then
    printf '\033[0;32m=== all %s checks passed ===\033[0m\n' "$PASS"
    exit 0
fi
printf '\033[0;31m=== %s failed, %s passed ===\033[0m\n' "$FAIL" "$PASS"
exit 1
