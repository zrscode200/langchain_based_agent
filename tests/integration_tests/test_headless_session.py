"""Live-server round trip against the factory graph (marker: integration).

Boots the real `langgraph dev` server through the `lc-code` CLI entry with
upstream's deterministic tool-calling model (no API keys), under an isolated
HOME. Excluded from the default pytest run; execute with
`uv run pytest -m integration`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Upstream owns the marker/content contract for its deterministic test model.
from deepagents_code._testing_models import TOP_LEVEL_WRITE_CONTENT

pytestmark = pytest.mark.integration


def _headless_env(home: Path) -> dict[str, str]:
    """Hermetic child env: drop inherited agent/tracing/factory configuration.

    `LC_FACTORY_` is stripped along with the rest, deliberately: a developer
    dogfooding with `export LC_FACTORY_MIDDLEWARE=...` would otherwise have it
    inherited into the *baseline* round trip, which must run with no injection.
    The transport tests set the variable explicitly after calling this, so
    stripping here costs them nothing and removes a machine-dependent failure.
    """
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("DEEPAGENTS_CODE_", "LANGSMITH_", "LANGCHAIN_", "LC_FACTORY_")
        )
    }
    child_env["HOME"] = str(home)
    child_env["DEEPAGENTS_CODE_NO_UPDATE_CHECK"] = "1"
    return child_env


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".deepagents").mkdir(parents=True)
    (home / ".deepagents" / "config.toml").write_text(
        "[models.providers.itest]\n"
        'class_path = "deepagents_code._testing_models:ToolCallingIntegrationChatModel"\n'
        'models = ["fake"]\n'
    )
    return home


def _lc_code() -> Path:
    lc_code = Path(sys.executable).parent / "lc-code"
    assert lc_code.exists(), "lc-code console script not installed (run uv sync)"
    return lc_code


def test_injected_middleware_reaches_a_real_session(tmp_path):
    """The seam's capability reaches the dogfood surface, not just the API.

    Wave 2.1 proved placement in-process. This proves the whole path: an
    `LC_FACTORY_MIDDLEWARE` reference set in the user's environment survives
    `_build_server_env`, is imported inside the server subprocess, and its
    middleware runs in the live graph — the marker file is written from
    `before_agent`, so composition alone would not satisfy it.
    """
    home = _make_home(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    marker = tmp_path / "middleware-ran.txt"

    child_env = _headless_env(home)
    child_env["LC_FACTORY_MIDDLEWARE"] = (
        "lc_factory._testing_middleware:build_marker_middleware"
    )
    child_env["LC_FACTORY_TEST_MIDDLEWARE_MARKER"] = str(marker)

    result = subprocess.run(
        [
            str(_lc_code()),
            "--timeout", "240",
            "-M", "itest:fake",
            "-q", "--no-stream",
            "-n", f"DCA_TEST_WRITE_FILE={workdir / 'out.txt'}",
        ],
        cwd=workdir,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert marker.exists(), (
        "injected middleware never ran in the server process — the "
        "LC_FACTORY_MIDDLEWARE transport is broken end to end"
    )


def test_bad_middleware_reference_fails_startup_loudly(tmp_path):
    """A broken reference must stop the server, not boot without the middleware.

    Asserting the variable name in client output looks weak but is the strong
    form: the server's stderr is redirected into a log file, so
    `LC_FACTORY_MIDDLEWARE` can reach the client's streams *only* through
    `_extract_startup_error_marker`. Seeing it here therefore proves the entire
    `STARTUP_ERROR_MARKER` path, even though the marker itself is stripped
    before the user sees the message. Do not "strengthen" this into asserting
    the raw marker — that would test less.
    """
    home = _make_home(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    child_env = _headless_env(home)
    child_env["LC_FACTORY_MIDDLEWARE"] = "lc_factory.does_not_exist:build"

    result = subprocess.run(
        [
            str(_lc_code()),
            "--timeout", "60",
            "-M", "itest:fake",
            "-q", "--no-stream",
            "-n", "say hello",
        ],
        cwd=workdir,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode != 0, "a broken middleware reference booted anyway"
    combined = result.stdout + result.stderr
    assert "LC_FACTORY_MIDDLEWARE" in combined, (
        f"startup failure did not name the variable: {combined[-2000:]}"
    )


def test_headless_write_file_round_trip(tmp_path):
    home = _make_home(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    target = workdir / "headless_out.txt"

    child_env = _headless_env(home)

    result = subprocess.run(
        [
            str(_lc_code()),
            "--timeout",
            "240",
            "-M",
            "itest:fake",
            "-q",
            "--no-stream",
            "-n",
            f"DCA_TEST_WRITE_FILE={target}",
        ],
        cwd=workdir,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    # Scope, stated precisely: headless with no shell allow-list resolves to
    # `auto_approve=True` upstream (client/non_interactive.py), so this
    # exercises boot + model turn + tool execution + persistence — NOT the
    # approval-interrupt path, which is composed away in this configuration.
    # That the run served the FACTORY graph is proven in-process by
    # tests/test_launch.py::test_upstream_launcher_serves_the_factory_graph.
    assert target.read_text() == TOP_LEVEL_WRITE_CONTENT
    # The session persisted in the standard dcode sessions DB location.
    assert (home / ".deepagents" / ".state" / "sessions.db").exists()
