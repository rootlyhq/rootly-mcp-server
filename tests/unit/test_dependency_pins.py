"""Guards against the shipped image drifting from what the project locks and tests.

A dependency bump edits `pyproject.toml`; `uv.lock` must follow it, and the
Docker image must install exactly the lock. The image used to `pip install -e .`
(which re-resolves at build time, so production ran different versions than CI)
and then re-pinned pydantic by hand. It now runs `uv sync --locked`, so these
tests pin that mechanism in place rather than any single version.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
UV_LOCK = REPO_ROOT / "uv.lock"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _pyproject_pin(package: str) -> str:
    """The exact version pinned for a package in pyproject's dependencies."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    for entry in data["project"]["dependencies"]:
        match = re.fullmatch(rf"{re.escape(package)}==([0-9][^,\s]*)", entry.strip())
        if match:
            return match.group(1)
    raise AssertionError(f"{package} is not pinned with == in pyproject.toml")


def _locked_version(package: str) -> str:
    """The version uv.lock resolves a package to."""
    text = UV_LOCK.read_text(encoding="utf-8")
    match = re.search(rf'^name = "{re.escape(package)}"\nversion = "([^"]+)"', text, re.MULTILINE)
    assert match, f"{package} not found in uv.lock"
    return match.group(1)


def _dockerfile_text() -> str:
    """The Dockerfile with comments and line continuations removed."""
    lines = [
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    return re.sub(r"\\\n\s*", " ", "\n".join(lines))


def _dockerfile_pins(package: str) -> list[str]:
    """Every explicit `package==version` the Dockerfile installs."""
    return re.findall(rf"{re.escape(package)}==([0-9][^\s\"']*)", _dockerfile_text())


def _dockerfile_run_commands() -> list[str]:
    return re.findall(r"^RUN (.+)$", _dockerfile_text(), re.MULTILINE)


def _dockerfile_python_versions() -> set[str]:
    return set(re.findall(r"^FROM python:(\d+\.\d+)", _dockerfile_text(), re.MULTILINE))


def _ci_test_matrix_pythons() -> list[str]:
    match = re.search(r"python-version:\s*\[([^\]]+)\]", CI_WORKFLOW.read_text(encoding="utf-8"))
    assert match, "CI test matrix python-version list not found"
    return re.findall(r'"([0-9.]+)"', match.group(1))


@pytest.mark.unit
class TestPydanticPinsAgree:
    """pyproject and uv.lock must agree on pydantic (the image installs the lock)."""

    def test_lockfile_matches_pyproject(self):
        # uv sync re-resolves rather than failing, so a stale lock is invisible
        # in local runs. CI also enforces this with `uv lock --check`; this test
        # gives the same signal without needing the uv binary.
        assert _locked_version("pydantic") == _pyproject_pin("pydantic")

    def test_dockerfile_does_not_override_the_lock(self):
        # A hand-written `pydantic==X` in the Dockerfile is a second source of
        # truth that goes stale on the next bump. If one is ever reintroduced it
        # must at least match pyproject.
        expected = _pyproject_pin("pydantic")
        found = _dockerfile_pins("pydantic")
        assert found == [expected] * len(found), (
            f"Dockerfile pins pydantic=={found} but pyproject pins {expected}."
        )


@pytest.mark.unit
class TestImageInstallsTheLock:
    """The image must install uv.lock, not a fresh resolution."""

    def test_project_is_installed_with_uv_sync_frozen(self):
        syncs = [cmd for cmd in _dockerfile_run_commands() if "uv sync" in cmd]
        assert syncs, "expected the Dockerfile to install the project with `uv sync`"
        for cmd in syncs:
            assert re.search(r"--(locked|frozen)\b", cmd), (
                f"`uv sync` without --locked/--frozen re-resolves: {cmd}"
            )
            assert "--no-dev" in cmd, f"`uv sync` would ship dev dependencies: {cmd}"

    def test_nothing_resolves_the_project_fresh(self):
        # `pip install .`, `pip install -e .` or `uv pip install -e .` ignore
        # uv.lock and pick whatever is newest at build time.
        for cmd in _dockerfile_run_commands():
            assert not re.search(r"pip install\b[^&;]*\s(-e\s+)?\.(\s|$)", cmd), (
                f"Dockerfile installs the project outside the lock: {cmd}"
            )

    def test_uv_image_is_pinned_to_an_exact_version(self):
        match = re.search(r"^FROM ghcr\.io/astral-sh/uv:(\S+)", _dockerfile_text(), re.MULTILINE)
        assert match, "expected uv to be copied from ghcr.io/astral-sh/uv"
        assert re.fullmatch(r"\d+\.\d+\.\d+(@sha256:[0-9a-f]{64})?", match.group(1)), (
            f"uv image tag {match.group(1)!r} is not an exact version"
        )

    def test_image_python_is_tested_in_ci(self):
        # The lock may resolve differently per interpreter (and some locked
        # packages ship no wheels for newer ones), so the image must run a
        # Python the CI test matrix covers.
        versions = _dockerfile_python_versions()
        assert len(versions) == 1, f"all python stages must share one version, got {versions}"
        assert versions <= set(_ci_test_matrix_pythons()), (
            f"Dockerfile uses python {versions} but CI tests {_ci_test_matrix_pythons()}"
        )


@pytest.mark.unit
class TestPinHelpersReadWhatTheyClaim:
    """The guards above are only meaningful if the parsing actually works."""

    def test_pyproject_pin_is_found_and_looks_like_a_version(self):
        assert re.fullmatch(r"[0-9]+\.[0-9]+(\.[0-9]+)?", _pyproject_pin("pydantic"))

    def test_dockerfile_run_commands_are_found(self):
        assert any("uv sync" in cmd for cmd in _dockerfile_run_commands())

    def test_ci_matrix_is_found(self):
        assert _ci_test_matrix_pythons()

    def test_fresh_install_pattern_is_detected(self):
        pattern = r"pip install\b[^&;]*\s(-e\s+)?\.(\s|$)"
        assert re.search(pattern, "uv pip install --system --no-cache-dir -e .")
        assert re.search(pattern, "pip install .")
        assert not re.search(pattern, 'uv pip install --python /opt/venv/bin/python "somepkg"')

    def test_unpinned_package_raises_rather_than_passing_silently(self):
        # A package listed with >= must not be mistaken for a pinned one, or
        # the guard would quietly compare nothing.
        with pytest.raises(AssertionError):
            _pyproject_pin("cryptography")
