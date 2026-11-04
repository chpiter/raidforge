#!/usr/bin/env bash
#
# build.sh - build the "server disk erase" Debian 12 live ISO.
#
# Usage:
#   ./build.sh                 # full rebuild (clean chroot), ~6m45s
#   ./build.sh --refresh       # reuse the chroot, re-run only the app stages (~4m)
#   ./build.sh --fast          # --refresh + zstd squashfs + no boot countdown (~2m)
#   ./build.sh --no-vendor     # reuse what is already in vendor/wheels
#   ./build.sh --vendor-only   # only download python wheels, then exit
#   ./build.sh --preflight     # only run the environment checks
#   ./build.sh --full-clean    # drop caches, rebuild everything from scratch
#   ./build.sh --clean         # lb clean --all and remove staging artefacts
#   ./build.sh --server-url https://cp.internal --api-prefix /api/raidforge
#                              # 生成 /etc/raidforge/agent.yaml（不含 token）
#   ./build.sh --poll-mode short --ca-bundle /etc/raidforge/ca.pem
#
# Output: raidforge-<version>-<date>.iso (+ .sha256)
#         --fast appends -test to the name, so a test image can never ship.
#
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

DISTRO="bookworm"
ARCH="amd64"
APP_VERSION="$(python3 - <<'PY' 2>/dev/null || echo 1.0.0
import re, os
init = os.path.join("app", "raidforge", "__init__.py")
print(re.search(r'__version__\s*=\s*"([^"]+)"', open(init).read()).group(1))
PY
)"
STAMP="$(date +%Y%m%d)"

STAGE_SRC="config/includes.chroot/usr/src/raidforge"
STAGE_WHEELS="config/includes.chroot/usr/src/raidforge-vendor"
VENDOR_DIR="vendor/wheels"
SQUASHFS="binary/live/filesystem.squashfs"

DO_VENDOR=1
DO_BUILD=1
# Cache kept by default: dropping the bootstrap tarball + .deb cache makes every
# rebuild re-download ~700 MB of packages. Use --full-clean for a pristine build.
KEEP_CACHE=1
PREFLIGHT_ONLY=0
VENDOR_ONLY=0
CLEAN_ONLY=0
ALLOW_MISSING_TOOLS="${ALLOW_MISSING_TOOLS:-0}"

# full = lb clean + lb build (slow, always correct)
# refresh = keep the chroot, re-run only the stages that carry the app
REBUILD_MODE="full"
SQUASH_TYPE="xz"
SQUASH_LEVEL=""
ISO_SUFFIX=""
BUILD_ID_WANT=""
AGENT_SERVER_URL=""
AGENT_API_PREFIX="/api/raidforge"
AGENT_POLL_MODE="short"
AGENT_CA_BUNDLE=""

log()  { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m  %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '5,20p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --refresh)     REBUILD_MODE="refresh" ;;
        --fast)        REBUILD_MODE="refresh"
                       SQUASH_TYPE="zstd"
                       SQUASH_LEVEL="1"
                       ISO_SUFFIX="-test"
                       export RAIDFORGE_BOOT_TIMEOUT="${RAIDFORGE_BOOT_TIMEOUT:-1}" ;;
        --no-vendor)   DO_VENDOR=0 ;;
        --vendor-only) VENDOR_ONLY=1; DO_BUILD=0 ;;
        --preflight)   PREFLIGHT_ONLY=1; DO_BUILD=0; DO_VENDOR=0 ;;
        --clean)       CLEAN_ONLY=1; DO_BUILD=0; DO_VENDOR=0 ;;
        --keep-cache)  KEEP_CACHE=1 ;;          # default, kept for compatibility
        --full-clean|--no-keep-cache) KEEP_CACHE=0 ;;
        --server-url)  [[ $# -ge 2 ]] || die "--server-url needs a value"; AGENT_SERVER_URL="$2"; shift ;;
        --api-prefix)  [[ $# -ge 2 ]] || die "--api-prefix needs a value"; AGENT_API_PREFIX="$2"; shift ;;
        --poll-mode)   [[ $# -ge 2 ]] || die "--poll-mode needs a value"; AGENT_POLL_MODE="$2"; shift ;;
        --ca-bundle)   [[ $# -ge 2 ]] || die "--ca-bundle needs a value"; AGENT_CA_BUNDLE="$2"; shift ;;
        -h|--help)     usage; exit 0 ;;
        *)             die "unknown argument: $1" ;;
    esac
    shift
done

ISO_OUTPUT="raidforge-${APP_VERSION}-${STAMP}${ISO_SUFFIX}.iso"

# --------------------------------------------------------------------------
# 1. preflight
# --------------------------------------------------------------------------
preflight() {
    log "preflight: environment"

    [[ "$(uname -m)" == "x86_64" ]] || warn "expected x86_64 host, got $(uname -m)"

    command -v lb >/dev/null 2>&1 || die "live-build not found; run: sudo apt install live-build debootstrap"
    local lbv
    lbv="$(lb --version 2>/dev/null || echo unknown)"
    log "live-build: ${lbv}"

    if [[ "$(id -u)" -ne 0 ]]; then
        command -v sudo >/dev/null 2>&1 || die "must run as root (or install sudo)"
        log "not root - will retry via sudo if needed"
    fi

    for pkg in debootstrap xorriso grub-pc-bin grub-efi-amd64-bin mtools syslinux-utils; do
        dpkg -s "$pkg" >/dev/null 2>&1 || warn "package '$pkg' is missing - the build may fail later"
    done

    # The squashfs type is a hard requirement of --fast; failing loudly beats
    # silently producing an xz image and wondering why the build is slow.
    if ! lb config --help 2>&1 | grep -q "chroot-squashfs-compression-type"; then
        die "this live-build has no --chroot-squashfs-compression-type (too old)"
    fi
    command -v unsquashfs >/dev/null 2>&1 || warn "unsquashfs missing - the freshness canary will be skipped"

    log "preflight: vendor tools in config/includes.chroot/usr/local/bin"
    local found=0
    for tool in storcli storcli64 perccli perccli64 arcconf ssacli hpssacli; do
        local bin="config/includes.chroot/usr/local/bin/${tool}"
        if [[ -x "$bin" ]]; then
            log "  found ${tool}"
            found=1
        elif [[ -f "$bin" ]]; then
            # Vendor zips unpack 0644, and includes.chroot preserves the mode -
            # without +x the tool is dead weight inside the image.
            if chmod +x "$bin" 2>/dev/null; then
                log "  found ${tool} (added missing +x)"
                found=1
            else
                warn "  ${tool} present but not executable and chmod failed"
            fi
        else
            warn "  missing ${tool} (drop the vendor archive here before building)"
        fi
    done
    if [[ "$found" -eq 0 ]]; then
        if [[ "$ALLOW_MISSING_TOOLS" == "1" ]]; then
            warn "no RAID CLI present - building anyway (only sg3_utils/generic mode will work)"
        else
            die "no RAID CLI present. See README section 'Vendor tools', or set ALLOW_MISSING_TOOLS=1"
        fi
    fi
    log "compat shims (storcli<-storcli64/perccli64, ssacli<-hpssacli) are created in the image when needed"

    # Not fatal: remove-audit sessions still run and the evidence still lands in
    # the pending queue, but no session will ever reach the control plane because
    # try_rsync() refuses to start without the key.  Say so before the 7 minute
    # build instead of after the first failed upload.
    if ! compgen -G "config/includes.chroot/etc/raidforge/keys/*" >/dev/null; then
        warn "no evidence upload key in config/includes.chroot/etc/raidforge/keys/"
        warn "  the ISO will have no private key, so rsync upload cannot work (evidence queues locally)"
    fi

    # CRLF breaks every shebang in the image; the failure surfaces 20 minutes
    # into the build as "/usr/bin/env: '/root/xxx.hook.chroot': No such file or
    # directory", which is impossible to debug from there.
    local crlf_files
    crlf_files="$(grep -rIl --exclude-dir=node_modules --exclude-dir=dist --exclude-dir=webplane-data --exclude-dir=__pycache__ $'\r' build.sh Makefile auto config tools tests app webplane 2>/dev/null | head -5 || true)"
    if [[ -n "$crlf_files" ]]; then
        printf '%s\n' "$crlf_files" >&2
        die "CRLF line endings found above - run dos2unix (or tools/lf.sh) and retry"
    fi

    [[ -f app/requirements.txt ]] || die "app/requirements.txt missing"
}

# --------------------------------------------------------------------------
# 2. stage sources + python wheels
# --------------------------------------------------------------------------
write_agent_config() {
    local target="config/includes.chroot/etc/raidforge/agent.yaml"
    mkdir -p "$(dirname "$target")"
    [[ "$AGENT_POLL_MODE" == "short" ]] || die "v1 only supports --poll-mode short"
    if [[ -n "$AGENT_SERVER_URL" && "$AGENT_SERVER_URL" != https://* ]]; then
        die "--server-url must use https:// (http is loopback-only at runtime)"
    fi
    cat > "$target" <<EOF
# 由 build.sh 生成；只包含非秘密键。token 只能运行时注入。
server_url: "${AGENT_SERVER_URL}"
api_prefix: "${AGENT_API_PREFIX}"
ca_bundle: "${AGENT_CA_BUNDLE}"
poll_mode: "${AGENT_POLL_MODE}"
heartbeat_interval: 10
poll_interval_short: 3
poll_jitter_ms: 500
confirm_timeout_seconds: 300
confirm_policy: "server"
evidence_dir: "/var/lib/raidforge/evidence"
state_dir: "/var/lib/raidforge/agent"
log_dir: "/var/log/raidforge-agent"
operator:
  name: ""
server_sn_override: ""
report_local_summary: false
allow_insecure_http: false
verify_tls: true
EOF
    log "agent config -> ${target}"
}

scan_agent_secrets() {
    local hits=""
    if [[ -d "$STAGE_SRC" ]]; then
        hits="$(grep -RIlE "rf_live_|RAIDFORGE_AGENT_TOKEN|BEGIN (OPENSSH|RSA|EC|PRIVATE) PRIVATE KEY" "$STAGE_SRC" 2>/dev/null | grep -v "/keys/" || true)"
    fi
    local generated="config/includes.chroot/etc/raidforge/agent.yaml"
    if [[ -f "$generated" ]] && grep -qE "^[[:space:]]*token[[:space:]]*:" "$generated"; then
        [[ -n "$hits" ]] && hits+=$'
'
        hits+="${generated}: token field present"
    fi
    if [[ -n "$hits" ]]; then
        printf '%s
' "$hits" >&2
        die "agent credentials found in staged ISO inputs"
    fi
    if command -v unsquashfs >/dev/null 2>&1 && [[ -f "$SQUASHFS" ]]; then
        local tmp
        tmp="$(mktemp -d)"
        unsquashfs -q -no-xattrs -d "$tmp" "$SQUASHFS" opt/raidforge etc/raidforge >/dev/null 2>&1 || true
        hits="$(grep -RIlE "rf_live_|RAIDFORGE_AGENT_TOKEN" "$tmp/opt/raidforge" 2>/dev/null || true)"
        if [[ -f "$tmp/etc/raidforge/agent.yaml" ]] && grep -qE "^[[:space:]]*token[[:space:]]*:" "$tmp/etc/raidforge/agent.yaml"; then
            [[ -n "$hits" ]] && hits+=$'
'
            hits+="squashfs etc/raidforge/agent.yaml: token field present"
        fi
        rm -rf "$tmp"
        [[ -z "$hits" ]] || { printf '%s
' "$hits" >&2; die "agent credentials found in ISO"; }
    fi
}

stage_sources() {
    write_agent_config
    log "staging application sources -> ${STAGE_SRC}"
    rm -rf "$STAGE_SRC"
    mkdir -p "$(dirname "$STAGE_SRC")"
    cp -a app/. "${STAGE_SRC}/"
    rm -rf "${STAGE_SRC}/__pycache__" "${STAGE_SRC:?}/"*"/__pycache__" 2>/dev/null || true

    # Content canary.  Every build artefact is stamped to SOURCE_DATE_EPOCH, so
    # mtimes cannot tell a fresh image from a stale one; this id is hashed into
    # the sources and read back out of the finished squashfs instead.
    {
        date -u +%Y%m%dT%H%M%S
        find app -name '*.py' -type f -print0 | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum
    } > "${STAGE_SRC}/BUILD_ID"
    BUILD_ID_WANT="$(head -1 "${STAGE_SRC}/BUILD_ID")"
    log "  build id: ${BUILD_ID_WANT}"
}

vendor_wheels() {
    mkdir -p "$VENDOR_DIR"
    local textual_pin
    textual_pin="$(awk -F'==' '/^textual==/{print $2; exit}' app/requirements.txt)"
    if [[ -z "$(ls -A "$VENDOR_DIR" 2>/dev/null)" ]]         || ! compgen -G "${VENDOR_DIR}/textual-${textual_pin}-*.whl" >/dev/null         || ! compgen -G "${VENDOR_DIR}/requests-*.whl" >/dev/null; then
        log "downloading python wheels for textual ${textual_pin} (Debian 12 ships Python 3.11)"
        rm -f "${VENDOR_DIR}"/textual-*.whl "${VENDOR_DIR}"/requests-*.whl
        python3 -m pip download \
            -r app/requirements.txt \
            -d "$VENDOR_DIR" \
            --only-binary=:all: \
            --platform manylinux_2_17_x86_64 \
            --platform manylinux_2_28_x86_64 \
            --python-version 3.11 \
            --implementation cp \
            --abi cp311 \
            || die "wheel download failed; check network access or run with --no-vendor"
    else
        log "reusing existing wheels in ${VENDOR_DIR} (textual ${textual_pin}, requests present)"
    fi
}

stage_wheels() {
    rm -rf "$STAGE_WHEELS"
    mkdir -p "$(dirname "$STAGE_WHEELS")"
    mkdir -p "$STAGE_WHEELS"
    cp -a "${VENDOR_DIR}/." "${STAGE_WHEELS}/" 2>/dev/null || true
}

unstage() {
    log "removing staging artefacts from the image tree"
    rm -rf "$STAGE_SRC" "$STAGE_WHEELS"
}

# --------------------------------------------------------------------------
# 3. live-build driver
# --------------------------------------------------------------------------
lb_config() {
    local maybe_sudo=""
    if [[ "$(id -u)" -ne 0 ]]; then maybe_sudo="sudo"; fi

    log "lb config (${DISTRO}/${ARCH}, squashfs ${SQUASH_TYPE}${SQUASH_LEVEL:+ level $SQUASH_LEVEL})"
    # The compression type is always passed explicitly, derived from the build
    # mode.  NB: `lb config` does NOT reset options that are absent from the
    # command line - a --fast run leaves LB_CHROOT_SQUASHFS_COMPRESSION_LEVEL=1
    # in config/chroot, and mksquashfs then rejects `-Xcompression-level` under
    # xz (zstd-only option).  So the level is forced below, after lb config.
    $maybe_sudo lb config \
        --chroot-squashfs-compression-type "${SQUASH_TYPE}" \
        ${SQUASH_LEVEL:+--chroot-squashfs-compression-level "${SQUASH_LEVEL}"}

    # Force the level to match this build mode: empty for xz (its default
    # preset applies), 1 for the --fast zstd build.
    sed -i "s|^LB_CHROOT_SQUASHFS_COMPRESSION_LEVEL=.*|LB_CHROOT_SQUASHFS_COMPRESSION_LEVEL=\"${SQUASH_LEVEL:-}\"|" config/chroot

    # `lb config` drops binary/ whenever the config hash changes (e.g. going
    # from xz back to zstd).  A full build regenerates it from scratch anyway,
    # but --refresh depends on it, so only the refresh path is fatal.
    if [[ -d binary ]]; then
        return 0
    fi
    if [[ "${REBUILD_MODE}" == "refresh" ]]; then
        die "lb config removed the binary tree - rerun ./build.sh (full)"
    fi
    warn "lb config removed the binary tree - a full build will recreate it"
}

finalize_image() {
    [[ -d "$(dirname "$SQUASHFS")" ]] || warn "no binary/live directory - the build stopped early"

    if [[ -f live-image-amd64.hybrid.iso ]]; then
        mv -f live-image-amd64.hybrid.iso "$ISO_OUTPUT"
        sha256sum "$ISO_OUTPUT" > "${ISO_OUTPUT}.sha256"
        log "image ready: ${ISO_OUTPUT}"
        cat "${ISO_OUTPUT}.sha256"
    else
        warn "expected live-image-amd64.hybrid.iso not found - check binaries/*.log"
        ls -la . 2>/dev/null | head -20
        return 1
    fi
}

do_build() {
    local maybe_sudo=""
    if [[ "$(id -u)" -ne 0 ]]; then maybe_sudo="sudo"; fi

    lb_config

    log "lb build - this takes a while"
    $maybe_sudo lb build

    scan_agent_secrets
    log "unstaging build inputs"
    unstage

    verify_freshness
    finalize_image
}

# --------------------------------------------------------------------------
# 4. refresh path: reuse the chroot, re-run only the stages that carry the app
# --------------------------------------------------------------------------
require_refresh_state() {
    local missing=()

    [[ -f .build/binary_iso ]] || missing+=(".build/binary_iso (no completed build yet)")
    [[ -x chroot/usr/bin/python3 ]] || missing+=("chroot/usr/bin/python3 (chroot missing)")
    [[ -x chroot/opt/raidforge/venv/bin/python ]] || missing+=("chroot/opt/raidforge/venv (app not installed)")
    [[ -f chroot/usr/local/sbin/raidforge-tui ]] || missing+=("chroot/usr/local/sbin/raidforge-tui")
    [[ -f "$SQUASHFS" ]] || missing+=("$SQUASHFS (binary stage missing)")
    [[ -f binary/isolinux/isolinux.cfg ]] || missing+=("binary/isolinux/isolinux.cfg")
    [[ -f binary/boot/grub/grub.cfg ]] || missing+=("binary/boot/grub/grub.cfg")
    compgen -G "${VENDOR_DIR}/*.whl" >/dev/null || missing+=("${VENDOR_DIR}/*.whl (wheels not downloaded)")
    command -v unsquashfs >/dev/null 2>&1 || missing+=("unsquashfs (squashfs-tools)")

    if [[ ${#missing[@]} -gt 0 ]]; then
        printf '  - %s\n' "${missing[@]}" >&2
        die "--refresh needs a completed build to work from: run ./build.sh once (full)"
    fi

    # mksquashfs writes a temporary copy next to the target.
    local free_kb need_kb
    free_kb="$(df -Pk . | awk 'NR==2 {print $4}')"
    need_kb=$(( $(stat -c %s "$SQUASHFS") / 1024 * 2 ))
    if [[ "$free_kb" -lt "$need_kb" ]]; then
        die "not enough free space for mksquashfs (${free_kb} kB free, ${need_kb} kB needed)"
    fi
}

# Re-run one live-build stage by removing its marker: that is exactly what
# `--force` means, but it works on every live-build version and lets us detect
# the dangerous case - a stage that silently answers "already done".
refresh_stage() {
    local stage="$1" out marker=".build/$1"
    local maybe_sudo=""
    if [[ "$(id -u)" -ne 0 ]]; then maybe_sudo="sudo"; fi

    rm -f "$marker"
    log "stage: ${stage}"
    if ! out="$($maybe_sudo lb "$stage" 2>&1)"; then
        printf '%s\n' "$out" >&2
        die "stage ${stage} failed"
    fi
    printf '%s\n' "$out" | grep -E "^(P|W|E):" | tail -5 || true

    if printf '%s\n' "$out" | grep -qE "Skipping .*already done"; then
        die "stage ${stage} reported 'already done' even though its marker was removed"
    fi
    [[ -f "$marker" ]] || warn "stage ${stage} left no marker - it may not have run"
}

verify_freshness() {
    [[ -n "${BUILD_ID_WANT}" ]] || { warn "no BUILD_ID staged - skipping the canary"; return 0; }
    command -v unsquashfs >/dev/null 2>&1 || { warn "unsquashfs missing - skipping the canary"; return 0; }
    [[ -f "$SQUASHFS" ]] || { warn "no squashfs to inspect - skipping the canary"; return 0; }

    local want got
    want="${BUILD_ID_WANT}"
    got="$(unsquashfs -cat "$SQUASHFS" opt/raidforge/BUILD_ID 2>/dev/null | head -1 || true)"
    if [[ "$want" != "$got" ]]; then
        die "squashfs contains a different app build (want '${want}', got '${got}') - a stage was skipped"
    fi
    log "freshness ok (${want})"
}

verify_boot_timeout() {
    [[ -n "$ISO_SUFFIX" ]] || return 0
    [[ -n "${RAIDFORGE_BOOT_TIMEOUT:-}" ]] || return 0
    local tenths=$(( RAIDFORGE_BOOT_TIMEOUT * 10 ))
    grep -q "^timeout ${tenths}$" binary/isolinux/isolinux.cfg \
        || die "isolinux timeout was not patched (RAIDFORGE_BOOT_TIMEOUT did not reach the hook)"
    grep -q "^set timeout=${RAIDFORGE_BOOT_TIMEOUT}$" binary/boot/grub/grub.cfg \
        || die "grub timeout was not patched (RAIDFORGE_BOOT_TIMEOUT did not reach the hook)"
    log "boot countdown: ${RAIDFORGE_BOOT_TIMEOUT}s"
}

refresh_build() {
    require_refresh_state
    lb_config
    stage_sources
    stage_wheels

    # Order matters: includes puts /usr/src/raidforge in the chroot, hooks turn
    # it into /opt/raidforge, binary_chroot copies the chroot for squashing.
    refresh_stage chroot_includes_after_packages
    refresh_stage chroot_hooks
    refresh_stage binary_chroot
    refresh_stage binary_rootfs
    refresh_stage binary_hooks
    refresh_stage binary_checksums
    refresh_stage binary_iso

    scan_agent_secrets
    unstage
    verify_boot_timeout
    verify_freshness
    finalize_image
}

# live-build keys "already done" off empty marker files in .build/, not off the
# stage directories themselves.  A leftover marker with a missing tree is the
# classic dead end:
#   W: Skipping bootstrap, already done
#   chroot: failed to run command '/usr/bin/env': No such file or directory
# Since keeping the cache is now the default, re-arm any stage whose marker
# outlived its directory before handing over to lb build.
rearm_stale_stages() {
    local stage dir
    for stage in bootstrap chroot; do
        [[ -f ".build/${stage}" ]] || continue
        dir="$stage"
        if [[ ! -d "$dir" ]] || [[ -z "$(ls -A "$dir" 2>/dev/null)" ]]; then
            warn "stale .build/${stage} marker but ${dir}/ is missing - re-arming"
            rm -f ".build/${stage}" .build/"${stage}"_* 2>/dev/null || true
        fi
    done
    # Re-arming bootstrap only helps when the cached tree is still there.
    if [[ ! -d bootstrap ]] && [[ -z "$(ls -A cache/bootstrap 2>/dev/null)" ]]; then
        warn "no bootstrap cache either - debootstrap will run again (network needed)"
    fi
}

lb_clean() {
    local maybe_sudo=""
    if [[ "$(id -u)" -ne 0 ]]; then maybe_sudo="sudo"; fi
    if [[ "$KEEP_CACHE" == "1" ]]; then
        # Default path: keep cache/bootstrap and cache/packages.* so the next
        # build starts at the chroot stage instead of re-downloading ~700 MB.
        log "lb clean --binary --chroot (cache kept; --full-clean to drop it)"
        $maybe_sudo lb clean --binary --chroot || true
        rearm_stale_stages
    else
        log "lb clean --all (cache dropped - full rebuild)"
        $maybe_sudo lb clean --all || true
    fi
}

do_clean() {
    local maybe_sudo=""
    if [[ "$(id -u)" -ne 0 ]]; then maybe_sudo="sudo"; fi
    $maybe_sudo lb clean --all || true
    rm -rf "$STAGE_SRC" "$STAGE_WHEELS"
    log "cleaned"
}

# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
trap 'echo; warn "interrupted - cleaning staging tree"; rm -rf "$STAGE_SRC" "$STAGE_WHEELS"' ERR

if [[ "$PREFLIGHT_ONLY" == "1" ]]; then
    preflight
    log "preflight ok"
    exit 0
fi

if [[ "$CLEAN_ONLY" == "1" ]]; then
    do_clean
    exit 0
fi

if [[ "$VENDOR_ONLY" == "1" ]]; then
    log "vendor-only: skipping build environment checks"
    vendor_wheels
    exit 0
fi

preflight

if [[ "$DO_VENDOR" == "1" ]]; then
    vendor_wheels
fi

if [[ "$DO_BUILD" == "1" ]]; then
    if [[ "$REBUILD_MODE" == "refresh" ]]; then
        log "refresh build: reusing the chroot, ${SQUASH_TYPE} squashfs"
        refresh_build
    else
        # Clean first: auto/clean deletes the staged source tree, so staging has
        # to happen afterwards or the hook would find /usr/src/raidforge missing.
        lb_clean
        stage_sources
        stage_wheels
        do_build
    fi
fi
