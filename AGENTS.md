# AGENTS.md

This file provides guidance to AI agents when working with code in this repository.

## Project Overview

This is a Python library called `aws` (package name: `aws`, module name: `pdum.aws`). AWS utils

The project uses a modern Python toolchain with UV for dependency management.

## Important Rules

### Versions and releases
`ship` (habemus-papadum-ship) releases this package. The manifest carries
`<last release>+dev` between releases; never edit a version field by hand.
`__version__` reads the installed metadata.

To release, ask the operator. When they agree, run the release workflow:
`gh workflow run release.yml -f bump=minor -f dry_run=false` (run with
`dry_run=true` first to see the plan). Do not publish from this machine.

Do not update repositories that consume this package; their scheduled refresh
does that. The one exception: if a change here needs a newer release of another
`habemus-papadum-*` package, raise that package's floor in this repository.

Do not commit a `path` or `editable` entry in `[tool.uv.sources]`. Do not pin
another `habemus-papadum-*` package to an exact version without a reason on the
same line or in `[tool.ship.pins]`. Run `uv run ship deps check` before you
finish.

## Development Commands

### Environment Setup
```bash
# Bootstrap the full toolchain (uv sync, pnpm install, widget build, hooks)
./scripts/setup.sh
```

**Important for Development**:
- Plain `uv sync` and `uv run` keep the locked versions; the lockfile changes only when a command is asked to change it (`uv add`, `uv remove`, `ship deps refresh`). CI uses `--locked`.
- Re-run `./scripts/setup.sh` whenever dependencies change

### Testing
```bash
# Run all tests
uv run pytest

# Run a specific test file
uv run pytest tests/test_example.py

# Run a specific test function
uv run pytest tests/test_example.py::test_version

# Run tests with coverage
uv run pytest --cov=src/pdum/aws --cov-report=xml --cov-report=term
```

### Code Quality
```bash
# Check code with ruff
uv run ruff check .

# Format code with ruff
uv run ruff format .

# Fix auto-fixable issues
uv run ruff check --fix .
```


### Documentation
```bash
# Serve documentation locally (auto-reloads on changes)
uv run mkdocs serve

# Build documentation
uv run mkdocs build

# Test demo notebooks (REQUIRED after any notebook changes)
./scripts/test_notebooks.sh
```

**Important**: After making any changes to demo notebooks (files in `docs/demos/*.ipynb`), you MUST run `./scripts/test_notebooks.sh` to verify the notebook executes without errors. Do not consider notebook changes complete until this test passes.

### Releasing
```bash
# Plan a release (operators run the real one through the release workflow)
uv run ship release habemus-papadum-aws --bump minor --dry-run

# Check the release policy: development links, lockfile, pins, version marker
uv run ship deps check
```


## Architecture

### Project Structure
- **src/pdum/aws/**: Main package source code (src-layout)
  - `__init__.py`: Package initialization and version
- **tests/**: Test suite using pytest
  - `test_example.py`: Example tests
- **docs/**: MkDocs documentation with mkdocstrings for API reference
  - `index.md`: Home page
  - `reference.md`: API reference
  - `demos/`: Demo notebooks (if included)

### Key Constraints
- **Python Version**: Requires Python 3.14+
- **Dependency Management**: Uses UV exclusively; uv.lock is committed
- **Build System**: Hatchling builds distributions; `ship` runs the build at release time
- **Documentation Style**: NumPy docstring style (see mkdocs.yml)

### Code Standards
- **Ruff Configuration**:
  - Target: Python 3.14
  - Line length: 120 characters
  - Linting rules: E (pycodestyle errors), F (pyflakes), W (warnings), I (isort)
- **Type Hints**: Use type hints where appropriate
- **Docstrings**: NumPy style, include Parameters, Returns, Raises sections

### Testing Strategy
- Test files must start with `test_` prefix
- Test classes must start with `Test` prefix
- Test functions must start with `test_` prefix
- Tests run with `-s` flag (no capture) by default
- Coverage reporting: use `--cov=src/pdum/aws --cov-report=xml --cov-report=term`

### Testing Configuration
The pytest configuration is in `pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-s"
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
```

Coverage configuration is also in `pyproject.toml`:
```toml
[tool.coverage.run]
source = ["src/pdum/aws"]
relative_files = true
omit = [
    "*/tests/*",
    "*/testing.py",
]
```

## CI/CD

### Continuous Integration
The project uses GitHub Actions for CI (`.github/workflows/ci.yml`):
- Runs on every push to main and pull requests- Executes linting with ruff
- Runs unit tests with coverage reporting
- Builds documentation to verify it compiles
- Posts coverage report as a PR comment


### Documentation Deployment
Documentation is deployed to GitHub Pages (`.github/workflows/docs.yml`):
- Triggered on every push to `main`
- Can also be triggered manually via workflow_dispatch
- Uses `./scripts/setup.sh` before running `mkdocs build`

### Release Process
Releases run from the `release` workflow (`.github/workflows/release.yml`),
which calls the reusable workflow in `habemus-papadum/pdum_ship`. It waits for
`ci.yml` to pass on the same commit, then runs `ship release habemus-papadum-aws`:
1. Reads the last tag `habemus-papadum-aws-v<version>` and applies the requested bump
2. Writes the release version into `pyproject.toml` and re-locks
3. Builds, runs the tests, publishes to PyPI with the `PYPI_API_TOKEN` secret
4. Commits, tags `habemus-papadum-aws-v<version>`, pushes commit and tag together
5. Creates the GitHub release
6. Writes `<version>+dev` back and pushes

Versions follow `X.Y.Z+dev` → `X.Y.Z` (tagged) → `X.Y.Z+dev`.
