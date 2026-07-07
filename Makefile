.DEFAULT_GOAL := help

PYTHON ?= $(shell for candidate in "$(VENV)/bin/python" python3.11 python3 python; do if $$candidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then command -v $$candidate || printf '%s\n' "$$candidate"; break; fi; done)
ARGS ?=
HUB ?=
VENV ?= .venv
LOCAL_BIN ?= $(HOME)/.local/bin
NPM ?= npm
UV ?= uv
CODEGRAPH ?= codegraph
IDE_DIR ?= ide
IDE_API_URL ?=
IDE_AUTH ?= auto
IDE_HANDOFF_FILE ?=
IDE_PROFILE ?=
IDE_FLEET ?=
IDE_HOST ?= 127.0.0.1
IDE_OPEN ?= 0
IDE_PORT ?= 5273
IDE_PACKAGE ?= dist/mac-ide-web.tar.gz
IDE_NODE_MODULES_STAMP := $(IDE_DIR)/node_modules/.package-lock.json
DESKTOP_NODE_MODULES_STAMP := desktop/node_modules/.package-lock.json

# Console scripts declared in pyproject.toml [project.scripts]; keep in sync.
CONSOLE_SCRIPTS = mac mac-hermes mac-agent mac-firecrawl-gateway mac-k8s-orchestrator mac-k8s-bootstrap mac-task-runner mac-webdav-server mac-evidence mac-hermes-gateway

.PHONY: help require-python require-npm require-uv codegraph-sync \
	install install-cli install-gui uninstall uninstall-cli \
	build build-cli build-gui package package-cli package-gui publish \
	clean clean-cli clean-gui distclean run-gui \
	install-hooks setup deploy test coverage test-api test-cli test-ui cli-coverage \
	test-portfolio fault-replay sanity-test compatibility-test \
	ide-install ide-run ide-dev ide-check ide-build ide-preview ide-package \
	desktop-install desktop-check desktop-package desktop-dist link-cli

help: ## Show the supported local build, install, run, test, and cleanup commands.
	@printf '%s\n' \
		'MAC local development and client commands' \
		'Install prerequisites: Python 3.11+, git, gh, npm, and CodeGraph.' \
		'Build and test targets also require uv.' \
		'' \
		'  make install       Install/link the CLI and prepare the canonical Fleet IDE' \
		'  make build         Build the CLI wheel and canonical Fleet IDE' \
		'  make run-gui       Run the Fleet IDE using the active mac login profile' \
		'  make test          Run the hermetic contract test suite' \
		'  make clean         Remove generated build artifacts (keep installed dependencies)' \
		'  make distclean     Also remove .venv and JavaScript dependencies' \
		'' \
		'Use make <target> with any target below:'
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

require-python:
	@if [ -z "$(PYTHON)" ]; then \
		echo "Python 3.11+ is required ($(VENV)/bin/python, python3.11, python3, or python)" >&2; \
		exit 127; \
	fi

require-npm:
	@command -v "$(NPM)" >/dev/null 2>&1 || { \
		echo "npm is required to build or run the Fleet IDE" >&2; \
		exit 127; \
	}

require-uv:
	@command -v "$(UV)" >/dev/null 2>&1 || { \
		echo "uv is required to build the Python wheel: https://docs.astral.sh/uv/" >&2; \
		exit 127; \
	}

codegraph-sync: ## Initialize or incrementally refresh the CodeGraph index.
	@MAC_CODEGRAPH_BIN="$(CODEGRAPH)" scripts/sync-codegraph.sh

# ---------------------------------------------------------------------------
# Common lifecycle: these are the targets a new contributor/client should use.
# ---------------------------------------------------------------------------

install: install-cli install-gui ## Install the CLI and canonical Fleet IDE from this checkout.
	@printf '%s\n' \
		'Installed MAC CLI + Fleet IDE.' \
		'  CLI:  mac --help' \
		'  GUI:  mac login && make run-gui'

install-cli: require-python codegraph-sync link-cli ## Create the Python environment and link MAC commands into ~/.local/bin.
	@echo "CLI installed from $(abspath $(VENV))"

install-gui: codegraph-sync build-gui ## Install locked GUI dependencies and build the canonical Fleet IDE.
	@echo "Fleet IDE installed in $(IDE_DIR)/dist (run with: make run-gui)"

build: build-cli build-gui ## Build both the CLI wheel and canonical Fleet IDE.
	@echo "built CLI wheel + Fleet IDE"

build-cli: require-python require-uv codegraph-sync ## Build the Python wheel into dist/.
	@mkdir -p dist
	rm -f dist/mac-*.whl
	$(UV) build --wheel
	@ls -1 dist/mac-*.whl

build-gui: require-npm codegraph-sync $(IDE_NODE_MODULES_STAMP) ## Type-check and build the canonical Fleet IDE.
	cd $(IDE_DIR) && $(NPM) run build

package: package-cli package-gui ## Produce verified CLI and Fleet IDE distribution artifacts.
	@echo "packaged CLI wheel + Fleet IDE web bundle"

package-cli: build-cli ## Verify the wheel's console-script entry points.
	@whl=$$(ls dist/mac-*.whl); \
		echo "verifying entry points in $$whl ..."; \
		entries=$$(unzip -p "$$whl" 'mac-*.dist-info/entry_points.txt' 2>/dev/null); \
		for s in $(CONSOLE_SCRIPTS); do \
			if printf '%s\n' "$$entries" | grep -q "^$$s = "; then \
				echo "  ok: $$s console script present"; \
			else \
				echo "  ERROR: $$s console script missing" >&2; exit 1; \
			fi; \
		done; \
		echo "packaged $$whl"

package-gui: build-gui ## Package the Fleet IDE static bundle into dist/.
	@mkdir -p "$$(dirname "$(IDE_PACKAGE)")"
	tar -czf "$(IDE_PACKAGE)" -C "$(IDE_DIR)/dist" .
	@echo "packaged Fleet IDE web bundle: $(IDE_PACKAGE)"

# Backward-compatible name. This target packages locally; it does not upload.
publish: package-cli ## Backward-compatible alias for package-cli (does not upload).

run-gui: ide-run ## Run the canonical Fleet IDE development server.

clean: clean-cli clean-gui ## Remove generated artifacts while preserving installed dependencies and CodeGraph.
	@rmdir dist 2>/dev/null || true
	@echo "removed generated build artifacts"

clean-cli: ## Remove Python build, test, and wheel artifacts.
	rm -rf build htmlcov .pytest_cache .ruff_cache
	rm -rf __pycache__
	rm -f .coverage .coverage.* dist/mac-*.whl
	@find src tests scripts plugin -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@find src tests scripts plugin -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete 2>/dev/null || true

clean-gui: ## Remove canonical and legacy GUI build artifacts, but keep node_modules.
	rm -rf $(IDE_DIR)/dist desktop/dist
	rm -f "$(IDE_PACKAGE)"

distclean: clean uninstall-cli ## Remove generated artifacts and all locally installed dependencies.
	rm -rf "$(VENV)" "$(IDE_DIR)/node_modules" desktop/node_modules
	@echo "removed local Python and JavaScript dependency environments"

uninstall: uninstall-cli clean-gui ## Remove checkout-linked CLI commands and generated GUI files.

uninstall-cli: ## Remove only ~/.local/bin links that point into this checkout's venv.
	@for s in $(CONSOLE_SCRIPTS); do \
		path="$(LOCAL_BIN)/$$s"; \
		if [ -L "$$path" ]; then \
			target=$$(readlink "$$path" 2>/dev/null || true); \
			case "$$target" in "$(abspath $(VENV))"/*) rm -f "$$path"; echo "  removed $$path";; esac; \
		fi; \
	done

# ---------------------------------------------------------------------------
# Tool/bootstrap helpers.
# ---------------------------------------------------------------------------

$(VENV)/bin/mac: pyproject.toml scripts/bootstrap-project.py
	MAC_VENV="$(abspath $(VENV))" $(PYTHON) scripts/bootstrap-project.py

$(IDE_NODE_MODULES_STAMP): $(IDE_DIR)/package-lock.json
	cd $(IDE_DIR) && $(NPM) ci

$(DESKTOP_NODE_MODULES_STAMP): desktop/package-lock.json
	cd desktop && $(NPM) ci

install-hooks: ## Install the repository pre-push test + CodeGraph gate.
	@mkdir -p .git/hooks
	@if [ -f .git/hooks/pre-push ] && ! cmp -s scripts/pre-push .git/hooks/pre-push; then \
		if grep -q '^# Pre-push regression gate\. Install via: make install-hooks$$' .git/hooks/pre-push; then \
			echo "updating the existing MAC-managed pre-push hook"; \
		else \
			echo "refusing to overwrite an existing custom .git/hooks/pre-push" >&2; \
			echo "merge scripts/pre-push into it manually, or remove it and rerun make install-hooks" >&2; \
			exit 1; \
		fi; \
	fi
	cp -f scripts/pre-push .git/hooks/pre-push
	chmod +x .git/hooks/pre-push
	@echo "pre-push hook installed"

link-cli: $(VENV)/bin/mac ## Link this checkout's console scripts into ~/.local/bin.
	@mkdir -p "$(LOCAL_BIN)"
	@linked=0; for s in $(CONSOLE_SCRIPTS); do \
		if [ -x "$(VENV)/bin/$$s" ]; then \
			ln -sf "$(abspath $(VENV))/bin/$$s" "$(LOCAL_BIN)/$$s" && echo "  linked $(LOCAL_BIN)/$$s"; \
			linked=$$((linked+1)); \
		fi; \
	done; \
	if [ "$$linked" = 0 ]; then \
		echo "  no console scripts in $(VENV)/bin; run 'make install-cli'" >&2; exit 1; \
	fi
	@case ":$$PATH:" in *":$(LOCAL_BIN):"*) ;; *) echo "  NOTE: add $(LOCAL_BIN) to PATH (for example in ~/.bashrc)";; esac

# Fleet setup/deploy are intentionally separate from local installation.
setup: require-python codegraph-sync ## Configure a fleet and deploy it (not a local CLI install).
	$(PYTHON) setup.py $(ARGS)

deploy: require-python codegraph-sync ## Deploy to an already configured fleet hub.
	@if [ -z "$(HUB)" ] && ! printf '%s\n' "$(ARGS)" | grep -Eq -- '(^|[[:space:]])--(hub|new-hub)(=|[[:space:]]|$$)'; then \
		echo "usage: make deploy HUB=<hub-node> [ARGS='agent-a ...']"; \
		echo "   or: make deploy ARGS='--new-hub <hub-node> --target user@host[:port]'"; \
		exit 2; \
	fi
	$(PYTHON) setup.py $(if $(HUB),--hub $(HUB),) $(ARGS)

# ---------------------------------------------------------------------------
# Tests and quality gates.
# ---------------------------------------------------------------------------

test: codegraph-sync ## Run the mandatory hermetic contract test suite.
	scripts/run-contract-tests.sh

coverage: codegraph-sync ## Run the canonical statement/branch/subprocess coverage gate.
	scripts/run-contract-tests.sh

test-portfolio: codegraph-sync ## Measure per-test timings and unique line/arc contribution.
	MAC_TEST_PORTFOLIO=1 scripts/run-contract-tests.sh

fault-replay: ## Prove historical probes pass now and fail before their fixes.
	uv run --extra dev python scripts/fault-replay.py

sanity-test: codegraph-sync ## Run affected tests + public/process canaries, fail closed to full.
	scripts/run-sanity-tests.sh $(ARGS)

compatibility-test: codegraph-sync ## Run the secondary-version public/process compatibility slice.
	scripts/run-compatibility-tests.sh

test-api: codegraph-sync ## Run API-marked tests.
	uv run --extra dev pytest -q -m api tests/

test-cli: codegraph-sync ## Run CLI-marked tests.
	uv run --extra dev pytest -q -m cli tests/

test-ui: require-npm codegraph-sync $(IDE_NODE_MODULES_STAMP) ## Run API UI contracts and Fleet IDE browser tests.
	uv run --extra dev pytest -q -m ui tests/
	cd $(IDE_DIR) && $(NPM) run test:ui

cli-coverage: codegraph-sync ## Print CLI subcommand coverage.
	@$(VENV)/bin/python scripts/cli-coverage.py

# ---------------------------------------------------------------------------
# Canonical Fleet IDE compatibility targets.
# ---------------------------------------------------------------------------

ide-install: require-npm codegraph-sync $(IDE_NODE_MODULES_STAMP) ## Install locked Fleet IDE dependencies.

ide-run ide-dev: require-python require-npm codegraph-sync $(IDE_NODE_MODULES_STAMP) ## Run the Fleet IDE development server.
	@set -a; \
	if [ -f "$$HOME/.mac/.env" ]; then set -a; . "$$HOME/.mac/.env"; set +a; fi; \
	PYTHONPATH="$(abspath src)" \
	IDE_DIR="$(abspath $(IDE_DIR))" \
	IDE_API_URL="$(IDE_API_URL)" \
	IDE_AUTH="$(IDE_AUTH)" \
	IDE_HANDOFF_FILE="$(IDE_HANDOFF_FILE)" \
	IDE_PROFILE="$(IDE_PROFILE)" \
	IDE_FLEET="$(IDE_FLEET)" \
	IDE_HOST="$(IDE_HOST)" \
	IDE_OPEN="$(IDE_OPEN)" \
	IDE_PORT="$(IDE_PORT)" \
	NPM="$(NPM)" \
	"$(PYTHON)" -m mac.ide_launcher

ide-check: require-npm codegraph-sync $(IDE_NODE_MODULES_STAMP) ## Type-check the Fleet IDE.
	cd $(IDE_DIR) && $(NPM) run typecheck

ide-build: build-gui ## Backward-compatible alias for build-gui.

ide-preview: build-gui ## Preview the production Fleet IDE bundle.
	cd $(IDE_DIR) && $(NPM) run preview -- --host $(IDE_HOST) --port $(IDE_PORT)

ide-package: package-gui ## Backward-compatible alias for package-gui.

# Legacy Electron dashboard wrapper. It is not part of install/build because the
# canonical GUI is ide/. Keep these explicit until Fleet IDE desktop packaging
# replaces the maintenance-only renderer.
desktop-install: require-npm codegraph-sync $(DESKTOP_NODE_MODULES_STAMP) ## Install legacy desktop-wrapper dependencies.

desktop-check: require-npm codegraph-sync $(DESKTOP_NODE_MODULES_STAMP) ## Syntax-check the legacy desktop wrapper.
	cd desktop && $(NPM) run check

desktop-package: require-npm codegraph-sync $(DESKTOP_NODE_MODULES_STAMP) ## Build an unpacked legacy desktop wrapper.
	cd desktop && $(NPM) run package

desktop-dist: require-npm codegraph-sync $(DESKTOP_NODE_MODULES_STAMP) ## Build legacy desktop installer artifacts.
	cd desktop && $(NPM) run dist
