.PHONY: audit build check format install test

install:
	uv sync --locked --all-groups

format:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest --cov --cov-report=term-missing

audit:
	uv export --locked --no-emit-project --all-groups --format requirements-txt | uv run pip-audit --strict -r /dev/stdin

build:
	uv build --clear

check:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy
	uv run pytest --cov --cov-report=term-missing
	uv build --clear
	uv run twine check --strict dist/*
	uv export --locked --no-emit-project --all-groups --format requirements-txt | uv run pip-audit --strict -r /dev/stdin
