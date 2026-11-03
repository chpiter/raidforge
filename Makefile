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
	python3 -m compileall -q app/raidforge receiver tests

# ---------------- remote debug host ----------------
sync:             ## push the tree to the build host (192.168.40.132)
	./tools/sync.sh

sync-all:         ## push every file, ignoring mtimes
	./tools/sync.sh --all

sync-watch:       ## keep pushing on every change (Ctrl+C to stop)
	./tools/sync.sh --watch

remote-build: sync ## sync, then build the ISO on the build host
	./tools/sync.sh --run "./build.sh"

remote-sh:        ## open a shell command on the build host, e.g. make remote-sh CMD=uptime
	./tools/sync.sh --run "$(CMD)"

receiver:         ## run the collector stub locally
	python3 -m uvicorn receiver.app:app --host 0.0.0.0 --port 8080

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: build build-no-vendor vendor preflight clean clean-all plan run test check pytest compile receiver help \
        sync sync-all sync-watch remote-build remote-sh
