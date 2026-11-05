.DEFAULT_GOAL := help
SHELL := /bin/bash

# ---------------- images ----------------
build:            ## build the ISO (preflight + wheels + lb build)
	./build.sh

build-no-vendor:  ## build using whatever is already in vendor/wheels
	./build.sh --no-vendor

vendor:           ## only download python wheels
	./build.sh --vendor-only

preflight:        ## only run environment checks
	./build.sh --preflight

clean:            ## lb clean --all + remove staging artefacts
	./build.sh --clean

clean-all: clean
	rm -rf vendor/wheels

# ---------------- local development loop ----------------
plan:             ## print the dry-run command plan for this machine
	cd app && python3 run.py --dry-run --print-plan --no-upload --no-record

run:              ## run the TUI locally (dry-run, nothing is submitted)
	cd app && python3 run.py --dry-run --no-upload --no-record

test:             ## detection / enumeration / dry-run tests (mock mode)
	tests/test_detection.sh --mock

check:            ## L0: pytest + detection walk + UI smoke (~15s)
	./tools/check.sh

pytest:           ## unit tests for the parsing and command layers
	cd app && python3 -m pytest ../tests -q

compile:          ## syntax check everything
	python3 -m compileall -q app/raidforge webplane tests

# ---------------- remote debug host ----------------
sync:             ## push the tree to the build host (192.168.40.132)
	./tools/sync.sh

sync-all:         ## push every file, ignoring mtimes
	./tools/sync.sh --all

sync-watch:       ## keep pushing on every change (Ctrl+C to stop)
	./tools/sync.sh --watch

remote-build: sync ## sync, then build the ISO on the build host
	./tools/sync.sh --run "./build.sh"

# ---------------- arm64 (emulated VM on the build host) ----------------
vm-host-prep: ## install qemu arm64 / EFI / cloud-init tooling on the build host
	./tools/sync.sh --run "bash /root/raidforge/tools/arm64_vm.sh host-prep"

vm-init: ## one-time: create the Debian 12 arm64 build VM (on 192.168.40.132)
	./tools/sync.sh --run "bash /root/raidforge/tools/arm64_vm.sh init"

vm-start: ## start the arm64 build VM
	./tools/sync.sh --run "bash /root/raidforge/tools/arm64_vm.sh start"

vm-stop: ## stop the arm64 build VM
	./tools/sync.sh --run "bash /root/raidforge/tools/arm64_vm.sh stop"

vm-status: ## show arm64 VM status
	./tools/sync.sh --run "bash /root/raidforge/tools/arm64_vm.sh status"

sync-arm64: ## push the tree + wheels + drivers into the arm64 VM share
	./tools/sync.sh --arch arm64 --with-wheels --with-drivers

remote-build-arm64: sync-arm64 ## build the arm64 ISO inside the VM
	./tools/sync.sh --arch arm64 --run "./build.sh"

iso-update-arm64: ## classify changes and rebuild the arm64 ISO
	./tools/iso_update.sh --arch arm64

# ---------------- drivers ----------------
drivers: ## resolve drivers/firmware from the catalog (no probe report)
	python3 tools/drivers.py resolve --arch amd64

drivers-probe: ## resolve from a probe report: make drivers-probe PROBE=hw.json [PROFILE=full]
	python3 tools/drivers.py resolve --arch amd64 --probe "$(PROBE)" --profile "$(or $(PROFILE),minimal)"

drivers-verify: ## verify the built chroot against the driver manifest
	python3 tools/drivers.py verify --arch amd64 --chroot chroot

remote-sh:        ## open a shell command on the build host, e.g. make remote-sh CMD=uptime
	./tools/sync.sh --run "$(CMD)"

iso-adopt:        ## record the current ISO as the incremental baseline
	./tools/iso_update.sh --adopt

iso-update:       ## classify changes and patch/build the ISO (release xz)
	./tools/iso_update.sh

iso-update-test:  ## app-only zstd test ISO
	./tools/iso_update.sh --test

webplane:         ## run the control plane locally (Flask, port 8080)
	python3 -m webplane

webplane-test:    ## run control-plane unit tests
	python3 -m pytest webplane/tests -q

webplane-web:     ## build the Vue frontend into webplane/web/dist
	cd webplane/web && npm run build

webplane-deploy:  ## build, sync and restart webplane on 192.168.40.132:8080
	./tools/deploy_webplane.sh

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: build build-no-vendor vendor preflight clean clean-all plan run test check pytest compile \
        webplane webplane-test webplane-web webplane-deploy help \
        sync sync-all sync-watch remote-build remote-sh iso-adopt iso-update iso-update-test \
        vm-init vm-start vm-stop vm-status sync-arm64 remote-build-arm64 iso-update-arm64 \
        vm-host-prep \
        drivers drivers-probe drivers-verify
