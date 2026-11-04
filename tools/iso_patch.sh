#!/usr/bin/env bash
#
# iso_patch.sh - app-only ISO refresh on the Debian build host.
#
# Reuses the cached live-build chroot, replaces /opt/raidforge from an app
# tarball, repacks filesystem.squashfs and replays the exact xorriso command
# recorded in binary/.disk/mkisofs.  No lb bootstrap/chroot stages run.
#
# Usage (from /root/raidforge):
#   bash tools/iso_patch.sh --mode release --app-tar .tmp/iso-app.tar.gz \
#       --name raidforge-1.2.0-20261105.iso --json
#
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

MODE=release
APP_TAR=""
NAME=""
JSON=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) MODE="${2:-}"; shift 2 ;;
        --app-tar) APP_TAR="${2:-}"; shift 2 ;;
        --name) NAME="${2:-}"; shift 2 ;;
        --json) JSON=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

SQUASH="binary/live/filesystem.squashfs"
APP_ROOT="chroot/opt/raidforge"
APP_SRC=".tmp/app-src"
START=$(date +%s)

die() { echo "iso_patch: $*" >&2; exit 1; }
log() { echo "[iso_patch] $*"; }

[[ "$MODE" == "release" || "$MODE" == "test" ]] || die "--mode must be release or test"
[[ -n "$APP_TAR" && -f "$APP_TAR" ]] || die "--app-tar missing: $APP_TAR"
[[ -n "$NAME" && "$NAME" != */* ]] || die "--name must be a bare file name"
[[ -f "$SQUASH" ]] || die "missing $SQUASH"
[[ -x "$APP_ROOT/venv/bin/python" ]] || die "missing $APP_ROOT/venv"
[[ -f "binary/isolinux/isolinux.cfg" && -f "binary/boot/grub/grub.cfg" ]] || die "binary boot tree missing"
[[ -f "binary/.disk/mkisofs" ]] || die "binary/.disk/mkisofs missing"
[[ -f "chroot/etc/raidforge/config.yaml" ]] || die "chroot site config missing (build the ISO first)"

# One patch/build at a time.  A concurrent live-build run would corrupt chroot/.
exec 9>.iso_patch.lock
flock -n 9 || die "another ISO patch/build is running"
if pgrep -af '[l]b (build|binary)' >/dev/null 2>&1; then
    die "live-build is running; wait for it"
fi
if mount | grep -q "$REPO/chroot"; then
    die "chroot filesystems are still mounted; unmount them before patching"
fi

SQUASH_BYTES=$(stat -c%s "$SQUASH")
AVAIL=$(df -B1 --output=avail "$REPO" | tail -1 | tr -d ' ')
(( AVAIL >= SQUASH_BYTES * 3 )) || die "not enough free space (need 3x squashfs)"

log "extracting app tarball"
rm -rf "$APP_SRC"
mkdir -p "$APP_SRC"
tar -xzf "$APP_TAR" -C "$APP_SRC"
[[ -f "$APP_SRC/raidforge/__init__.py" ]] || die "app tarball has no raidforge/"

log "replacing $APP_ROOT (venv and site config preserved)"
for entry in "$APP_ROOT"/* "$APP_ROOT"/.[!.]*; do
    [[ -e "$entry" ]] || continue
    case "$(basename "$entry")" in
        venv|config.yaml|config.d) continue ;;
    esac
    rm -rf "$entry"
done
cp -a "$APP_SRC/." "$APP_ROOT/"
find "$APP_ROOT" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$APP_ROOT" -name '*.pyc' -delete 2>/dev/null || true

log "refreshing BUILD_ID"
BUILD_HASH=$(cd "$REPO" && find app -name '*.py' -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')
printf '%s\n%s\n' "$(date -u +%Y%m%dT%H%M%S)" "$BUILD_HASH" > "$APP_ROOT/BUILD_ID"
VERSION=$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$APP_ROOT/raidforge/__init__.py" | head -1)
[[ -n "$VERSION" ]] || VERSION=unknown
cat > chroot/etc/raidforge-build-info <<INFO
image:      raidforge
built:      $(date -u +%Y-%m-%dT%H:%M:%SZ)
distribution: Debian 12 (bookworm)
app_version: $VERSION
INFO

log "cleaning build residue (900-cleanup equivalent)"
rm -rf chroot/usr/src/raidforge chroot/usr/src/raidforge-vendor
rm -rf chroot/var/lib/apt/lists/* chroot/var/cache/apt/*.bin chroot/var/cache/apt/archives/*.deb
rm -rf chroot/usr/share/man/* chroot/usr/share/doc/* chroot/usr/share/info/* 2>/dev/null || true
rm -rf chroot/var/log/apt/* chroot/var/log/dpkg.log* chroot/root/.cache 2>/dev/null || true
rm -f chroot/run/raidforge.done chroot/etc/resolv.conf

if grep -rIlE 'rf_live_|RAIDFORGE_AGENT_TOKEN|BEGIN (OPENSSH|RSA|EC|PRIVATE) PRIVATE KEY' \
    "$APP_ROOT/raidforge" "$APP_ROOT/run.py" >/dev/null 2>&1; then
    die "secret-like pattern found in the application sources"
fi

log "repacking squashfs ($MODE)"
if [[ "$MODE" == "test" ]]; then
    COMP=(-comp zstd -Xcompression-level 1)
else
    COMP=(-comp xz)
fi
mksquashfs chroot "$SQUASH.new" -no-progress "${COMP[@]}"
unsquashfs -cat "$SQUASH.new" opt/raidforge/BUILD_ID 2>/dev/null \
    | sed -n '2p' | grep -qx "$BUILD_HASH" || die "BUILD_ID canary mismatch"
chmod 0644 "$SQUASH.new"
mv -f "$SQUASH.new" "$SQUASH"

MODDATE=$(date -u +%Y%m%d%H%M%S00)
OUT="$REPO/$NAME"
if [[ -f "$OUT" ]]; then
    cp -p "$OUT" "$OUT.prev"
fi
if [[ -f "$OUT.sha256" ]]; then
    cp -p "$OUT.sha256" "$OUT.sha256.prev"
fi

ORIG_CMD=$(cat binary/.disk/mkisofs)
ISO_CMD=$(printf '%s' "$ORIG_CMD" | sed -E "s/--modification-date=[0-9]+/--modification-date=$MODDATE/")
printf '%s\n' "$ISO_CMD" > binary/.disk/mkisofs
RUN_CMD=$(printf '%s' "$ISO_CMD" | sed -E "s# -o [^ ]+ # -o $OUT #")
[[ "$RUN_CMD" == *"$OUT"* ]] || die "could not rewrite xorriso output path"

log "regenerating binary/sha256sum.txt"
(
    cd binary
    find . -type f \
        \! -path './isolinux/isolinux.bin' \
        \! -path './boot/boot.bin' \
        \! -path './boot/grub/stage2_eltorito' \
        \! -path './*SUMS' \
        \! -path './*sum.txt' \
        \! -path './*sum.README' \
        -print0 | sort -z | xargs -0 sha256sum > sha256sum.txt
)

log "rebuilding ISO"
bash -c "$RUN_CMD"
(cd "$REPO" && sha256sum "$NAME" > "$NAME.sha256")

log "verifying boot records"
xorriso -indev "$OUT" -report_el_torito plain 2>/dev/null | grep -q '/isolinux/isolinux.bin' \
    || die "BIOS El Torito entry missing"
xorriso -indev "$OUT" -report_el_torito plain 2>/dev/null | grep -q '/boot/grub/efi.img' \
    || die "UEFI El Torito entry missing"
xorriso -indev "$OUT" -pvd_info 2>/dev/null | grep -q 'RAIDFORGE' \
    || die "ISO volume id is not RAIDFORGE"

END=$(date +%s)
if [[ "$JSON" == "1" ]]; then
    python3 - "$NAME" "$MODE" "$BUILD_HASH" "$START" "$END" "$SQUASH" "$OUT" <<'PY'
import hashlib, json, os, sys

name, mode, build_hash, start, end, squash, out = sys.argv[1:]

def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

print("@@ISO_PATCH_JSON@@" + json.dumps({
    "ok": True,
    "iso": name,
    "path": out,
    "mode": mode,
    "build_id": build_hash,
    "sha256": sha256(out),
    "size": os.stat(out).st_size,
    "squashfs_sha256": sha256(squash),
    "squashfs_size": os.stat(squash).st_size,
    "seconds": int(end) - int(start),
}))
PY
else
    log "done: $OUT ($((END - START))s)"
fi
