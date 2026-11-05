#!/usr/bin/env bash
#
# arm64_vm.sh - build the RAIDFORGE live ISO for Kunpeng/aarch64 without ARM
# hardware.
#
# Runs ON THE x86 BUILD HOST (192.168.40.132).  It creates a Debian 12 arm64
# VM under qemu-system-aarch64 (TCG), shares a host directory into it over
# virtio-9p, and exposes SSH on 127.0.0.1:2222.  tools/sync.sh --arch arm64
# pushes into the shared directory; `run` rsyncs it into the VM and executes
# the command there.
#
#   ./tools/arm64_vm.sh init          # one-time: image + key + cloud-init seed
#   ./tools/arm64_vm.sh start|stop|status
#   ./tools/arm64_vm.sh sync          # 9p share -> /root/raidforge in the VM
#   ./tools/arm64_vm.sh run [--no-sync] "cd /root/raidforge && ./build.sh"
#   ./tools/arm64_vm.sh pull          # copy *.iso from the VM into $SHARE/out
#
# Tunables: VM_DIR, SHARE, SSH_PORT, SMP, MEM, IMAGE_URL, DISK_SIZE.
#
set -Eeuo pipefail

VM_DIR="${VM_DIR:-/var/lib/raidforge-arm64}"
SHARE="${SHARE:-/srv/raidforge-arm64}"
SSH_PORT="${SSH_PORT:-2222}"
SMP="${SMP:-2}"
MEM="${MEM:-2048}"
DISK_SIZE="${DISK_SIZE:-24G}"
IMAGE_URL="${IMAGE_URL:-https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-arm64.qcow2}"

BASE_IMAGE="${VM_DIR}/debian-12-genericcloud-arm64.qcow2"
DISK="${VM_DIR}/disk.qcow2"
SEED="${VM_DIR}/seed.iso"
KEY="${VM_DIR}/id_ed25519"
PIDFILE="${VM_DIR}/qemu.pid"
SERIAL="${VM_DIR}/serial.log"

log()  { printf '\033[1;34m[arm64-vm]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

ssh_args=(-i "$KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no
          -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR)

require() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || die "missing tool: $tool"
    done
}

vm_pid() {
    [[ -f "$PIDFILE" ]] || return 1
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && printf '%s\n' "$pid"
}

wait_ssh() {
    local timeout="${1:-900}"
    local waited=0
    log "waiting for the VM to accept SSH (up to ${timeout}s)"
    while (( waited < timeout )); do
        if ssh "${ssh_args[@]}" root@127.0.0.1 true >/dev/null 2>&1; then
            log "SSH is up"
            return 0
        fi
        sleep 10
        waited=$((waited + 10))
    done
    die "VM did not come up; check ${SERIAL}"
}

write_cloud_init() {
    local pubkey
    pubkey="$(cat "${KEY}.pub")"
    cat > "${VM_DIR}/user-data" <<EOF
#cloud-config
hostname: raidforge-arm64
manage_etc_hosts: true
disable_root: false
ssh_pwauth: false
users:
  - default
  - name: root
    ssh_authorized_keys:
      - ${pubkey}
package_update: true
packages:
  - live-build
  - debootstrap
  - xorriso
  - grub-efi-arm64-bin
  - mtools
  - squashfs-tools
  - rsync
  - python3
  - python3-pip
  - openssh-server
  - file
  - curl
write_files:
  - path: /etc/ssh/sshd_config.d/99-raidforge.conf
    permissions: '0644'
    content: |
      PermitRootLogin prohibit-password
runcmd:
  - [ bash, -c, "mkdir -p /mnt/host && grep -q ' /mnt/host ' /etc/fstab || echo 'host /mnt/host 9p trans=virtio,version=9p2000.L,_netdev,nofail 0 0' >> /etc/fstab" ]
  - [ bash, -c, "mount -a || true" ]
  # live-build + mksquashfs need more than 2 GB; a guest swapfile keeps them
  # from being OOM-killed on the small build host.
  - [ bash, -c, "if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab; fi; swapon --show | grep -q /swapfile || swapon /swapfile" ]
  - [ systemctl, restart, ssh ]
EOF
    cat > "${VM_DIR}/meta-data" <<EOF
instance-id: raidforge-arm64
local-hostname: raidforge-arm64
EOF
}

cmd_host_prep() {
    command -v apt-get >/dev/null 2>&1 || die "this command is for the Debian build host"
    log "installing arm64 emulation / VM tooling"
    apt-get update
    apt-get install -y qemu-system-arm qemu-efi-aarch64 cloud-image-utils rsync
    log "done; now run ./tools/arm64_vm.sh init"
}

cmd_init() {
    require qemu-system-aarch64 qemu-img cloud-localds ssh-keygen curl rsync ssh
    mkdir -p "$VM_DIR" "$SHARE/out"
    local free_gb
    free_gb="$(df -Pk "$VM_DIR" | awk 'NR==2 {print int($4/1024/1024)}')"
    if [[ "${free_gb:-0}" -lt 20 ]]; then
        warn "${VM_DIR} has ${free_gb} GB free; an arm64 build needs about 20 GB"
    fi
    [[ -f /usr/share/qemu-efi-aarch64/QEMU_EFI.fd ]] \
        || die "install qemu-efi-aarch64 (provides QEMU_EFI.fd)"
    if [[ ! -f "$BASE_IMAGE" ]]; then
        log "downloading ${IMAGE_URL}"
        curl -fL --retry 3 -o "${BASE_IMAGE}.part" "$IMAGE_URL"
        mv "${BASE_IMAGE}.part" "$BASE_IMAGE"
    fi
    if [[ ! -f "$DISK" ]]; then
        log "creating a ${DISK_SIZE} build disk"
        qemu-img convert -O qcow2 "$BASE_IMAGE" "$DISK"
        qemu-img resize "$DISK" "$DISK_SIZE"
    fi
    if [[ ! -f "$KEY" ]]; then
        log "generating SSH key ${KEY}"
        ssh-keygen -t ed25519 -N "" -f "$KEY" >/dev/null
    fi
    chmod 0600 "$KEY"
    write_cloud_init
    cloud-localds "$SEED" "${VM_DIR}/user-data" "${VM_DIR}/meta-data"
    log "VM image ready in ${VM_DIR}"
    log "next: ./tools/arm64_vm.sh start   (first boot installs packages; expect 20-40 min under TCG)"
}

cmd_start() {
    require qemu-system-aarch64
    [[ -f "$DISK" && -f "$SEED" ]] || die "run init first"
    if pid="$(vm_pid)"; then
        log "already running (pid ${pid})"
        return 0
    fi
    [[ -f /usr/share/qemu-efi-aarch64/QEMU_EFI.fd ]] \
        || die "install qemu-efi-aarch64"
    mkdir -p "$SHARE"
    log "starting arm64 VM (${SMP} vCPU, ${MEM} MB, TCG)"
    qemu-system-aarch64 \
        -M virt -cpu max -smp "$SMP" -m "$MEM" \
        -accel tcg,thread=multi \
        -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
        -drive if=virtio,file="$DISK",format=qcow2,cache=writeback \
        -drive if=virtio,file="$SEED",format=raw,readonly=on \
        -virtfs local,path="$SHARE",mount_tag=host,security_model=none,multidevs=remap \
        -device virtio-rng-pci \
        -netdev user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22 \
        -device virtio-net-pci,netdev=net0 \
        -display none -serial "file:${SERIAL}" \
        -pidfile "$PIDFILE" -daemonize
    wait_ssh
}

cmd_stop() {
    local pid
    if pid="$(vm_pid)"; then
        log "stopping VM (pid ${pid})"
        kill "$pid"
        for _ in $(seq 1 30); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PIDFILE"
    else
        log "VM is not running"
    fi
}

cmd_status() {
    local pid
    if pid="$(vm_pid)"; then
        log "running (pid ${pid}, ssh 127.0.0.1:${SSH_PORT})"
        if ssh "${ssh_args[@]}" root@127.0.0.1 \
               'echo "guest: $(uname -m) $(uname -r)"; df -h / | tail -1' 2>/dev/null; then
            return 0
        fi
        warn "SSH is not answering yet"
        return 1
    fi
    log "stopped"
    return 1
}

cmd_sync() {
    vm_pid >/dev/null || die "VM is not running (./tools/arm64_vm.sh start)"
    command -v rsync >/dev/null 2>&1 || die "host needs rsync"
    log "syncing ${SHARE} -> /root/raidforge"
    rsync -a --delete \
        --exclude .git --exclude chroot --exclude binary --exclude .build \
        --exclude cache --exclude bootstrap --exclude .tmp --exclude artifacts \
        --exclude .venv --exclude '*.iso' --exclude '*.iso.sha256' \
        --exclude vendor/wheels \
        -e "ssh ${ssh_args[*]}" \
        "${SHARE}/" root@127.0.0.1:/root/raidforge/
}

cmd_run() {
    local do_sync=1
    if [[ "${1:-}" == "--no-sync" ]]; then
        do_sync=0
        shift
    fi
    [[ $# -gt 0 ]] || die "run needs a command"
    vm_pid >/dev/null || die "VM is not running (./tools/arm64_vm.sh start)"
    (( do_sync )) && cmd_sync
    log "guest: $*"
    ssh "${ssh_args[@]}" root@127.0.0.1 "mkdir -p /root/raidforge && cd /root/raidforge && $*"
}

cmd_pull() {
    vm_pid >/dev/null || die "VM is not running"
    mkdir -p "$SHARE/out"
    log "copying ISO artefacts to ${SHARE}/out"
    rsync -a -e "ssh ${ssh_args[*]}" \
        root@127.0.0.1:'/root/raidforge/raidforge-*.iso*' "${SHARE}/out/" || true
    ls -lh "$SHARE/out" 2>/dev/null || true
}

usage() {
    awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"
}

case "${1:-}" in
    host-prep) shift; cmd_host_prep ;;
    init)     cmd_init ;;
    start)    cmd_start ;;
    stop)     cmd_stop ;;
    status)   cmd_status ;;
    sync)     cmd_sync ;;
    run)      shift; cmd_run "$@" ;;
    pull)     cmd_pull ;;
    -h|--help|"") usage ;;
    *)        die "unknown command: $1" ;;
esac
