.PHONY: install test lint fmt build clean check
install:
	pip install -e ".[all,dev]" && playwright install chromium
test:
	python -m pytest -q
lint:
	python -m ruff check src tests
fmt:
	python -m ruff check --fix src tests
check: lint test
	python -m pubkit.cli validate examples/inside-ai-infra
build: clean check
	python -m build && python -m twine check --strict dist/*
clean:
	rm -rf dist build .pytest_cache .ruff_cache src/*.egg-info
