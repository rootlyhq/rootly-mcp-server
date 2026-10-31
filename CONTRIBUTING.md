# Contributing to Rootly MCP Server

## Submitting Changes

1. Fork the repository and create a feature branch
2. Make your changes with clear, atomic commits
3. Open a Pull Request with a description that clearly explains:
   - What the change does
   - Why it's needed
   - Any breaking changes or migration steps

## Developer Setup

### Prerequisites
- Python 3.12 or higher
- [`uv`](https://github.com/astral-sh/uv) for dependency management

### 1. Set Up Virtual Environment

Create and activate a virtual environment:

```bash
uv venv .venv
source .venv/bin/activate  # Always activate before running scripts
```

### 2. Install Dependencies

Install all project dependencies:

```bash
uv pip install .
```

To add new dependencies during development:
```bash
uv pip install <package>
```

### 3. Set Up Git Hooks (Recommended)

Install pre-commit hooks to automatically run the quality gate before commits:

```bash
make hooks   # or: ./scripts/setup-hooks.sh
```

The hook runs `make check` (lint, format check, type checks, unit tests) —
the same gate CI enforces.

### 4. Verify Installation

The server should now be ready to use with your MCP-compatible editor.

Additional testing tools are available in the `tests/` directory.

## Common Tasks

The `Makefile` wraps the toolchain; run `make help` to list every target.
Common ones: `make check` (pre-push gate), `make test`, and `make fetch-spec`
to refresh the bundled OpenAPI spec from source.

## Releases

Releases are cut by [release-please](https://github.com/googleapis/release-please).
PR titles become the squash commit, so write them as
[conventional commits](https://www.conventionalcommits.org/): `fix:` makes a
patch release, `feat:` a minor one, and `feat!:` or a `BREAKING CHANGE:` footer
a major one. `chore:`, `ci:`, `docs:`, `test:` and dependency bumps don't
trigger a release.

On every push to `main`, release-please keeps one open release PR up to date.
It bumps the version in `pyproject.toml`, `uv.lock` and `server.json`, and adds
a `CHANGELOG.md` section. Merging that PR tags the release, creates the GitHub
Release and publishes the package to PyPI. Don't bump versions or edit released
`CHANGELOG.md` sections by hand.
