.PHONY: install-hooks test test-api test-cli test-ui

install-hooks:
	cp scripts/pre-push .git/hooks/pre-push
	chmod +x .git/hooks/pre-push
	@echo "pre-push hook installed"

test:
	uv run --extra dev pytest -q

test-api:
	uv run --extra dev pytest -q -m api tests/

test-cli:
	uv run --extra dev pytest -q -m cli tests/

test-ui:
	uv run --extra dev pytest -q -m ui tests/
