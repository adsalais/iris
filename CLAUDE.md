# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

Greenfield Python 3.14 package scaffolded with `uv` / hatchling. As of this writing the package contains only a placeholder `main()` in `src/iris/__init__.py`. README.md is empty. Pytest is wired up; no linter or formatter yet. Treat new conventions as up for negotiation rather than inferring them from the current skeleton.

## Commands

The project uses a `src/`-layout with hatchling as the build backend and a `.python-version` pinning 3.14. A `.venv` is already present.

- Run the CLI entry point: `uv run iris` (defined as `iris = "iris:main"` in `pyproject.toml`)
- Install/sync after editing `pyproject.toml`: `uv sync`
- Add a runtime dep: `uv add <pkg>` — and `uv add --dev <pkg>` for dev-only.

### Tests

Pytest is the test runner. Config lives under `[tool.pytest.ini_options]` in `pyproject.toml` (`testpaths = ["tests"]`, `--import-mode=importlib`).

- Run the full suite: `uv run pytest`
- Run a single file: `uv run pytest tests/test_smoke.py`
- Run a single test by node id: `uv run pytest tests/test_smoke.py::test_main_runs_and_greets`
- Filter by name: `uv run pytest -k <substring>`
- Stop at first failure with verbose tracebacks: `uv run pytest -x -vv`

Conventions for new tests:
- Tests live under `tests/` at the repo root (sibling to `src/`), not inside the package.
- **Do not add `__init__.py` under `tests/`** — `--import-mode=importlib` requires `tests/` to *not* be a package, but in exchange every test file must have a unique basename across the suite.
- Import the package as `from iris import …`. The src-layout means tests only resolve after `uv sync` (or any `uv run`) has installed the package in editable mode.

## Layout

- `src/iris/` — the package. Imports use `iris.<module>` (src-layout), so the package is only importable after `uv sync` / `pip install -e .` has placed it on the path.
- `tests/` — pytest test modules; not a package (see import-mode note above).
- `pyproject.toml` — single source of truth for metadata, deps, scripts, and tool config.
