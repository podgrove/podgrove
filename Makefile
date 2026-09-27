.DEFAULT_GOAL := help
.PHONY: help install test lint e2e verify docs-check package
help:
	@echo "install     Install development dependencies"
	@echo "test        Run local unit/browser tests (no cluster or Docker daemon)"
	@echo "lint        Check Python source without rewriting it"
	@echo "verify      Run lint and local tests"
	@echo "e2e         Opt-in disposable LOCAL Docker integration matrix"
	@echo "docs-check  Check documentation into a new evidence file"
	@echo "package     Build source/wheel archives"
install:
	uv sync --locked --extra test --extra web-test
test:
	uv run --locked --extra test --extra web-test pytest -q -m 'not integration and not cluster'
lint:
	uv run --locked --extra test ruff check podgrove scripts tests
e2e:
	uv run --locked --extra test python scripts/e2e.py --mongo
verify: lint test
docs-check:
	uv run --locked --extra test python scripts/verify_docs.py --output artifacts/docs-new.json
package:
	uv build
