PYTHON ?= $(shell for candidate in python3 python; do if $$candidate -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then command -v $$candidate; break; fi; done)
ARGS ?=
HUB ?=

.PHONY: require-python install-hooks setup deploy test test-api test-cli test-ui

require-python:
	@if [ -z "$(PYTHON)" ]; then \
		echo "Python 3.9+ is required (python3 or python)"; \
		exit 127; \
	fi

install-hooks:
	cp -f scripts/pre-push .git/hooks/pre-push
	chmod +x .git/hooks/pre-push
	@echo "pre-push hook installed"

setup: require-python
	$(PYTHON) setup.py $(ARGS)

deploy: require-python
	@if [ -z "$(HUB)" ] && ! printf '%s\n' "$(ARGS)" | grep -Eq -- '(^|[[:space:]])--(hub|new-hub)(=|[[:space:]]|$$)'; then \
		echo "usage: make deploy HUB=<hub-node> [ARGS='agent-a ...']"; \
		echo "   or: make deploy ARGS='--new-hub <hub-node> --target user@host[:port]'"; \
		exit 2; \
	fi
	$(PYTHON) setup.py $(if $(HUB),--hub $(HUB),) $(ARGS)

test:
	uv run --extra dev pytest -q

test-api:
	uv run --extra dev pytest -q -m api tests/

test-cli:
	uv run --extra dev pytest -q -m cli tests/

test-ui:
	uv run --extra dev pytest -q -m ui tests/
