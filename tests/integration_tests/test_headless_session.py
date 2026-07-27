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


def test_headless_write_file_round_trip(tmp_path):
    home = tmp_path / "home"
    (home / ".deepagents").mkdir(parents=True)
    (home / ".deepagents" / "config.toml").write_text(
        "[models.providers.itest]\n"
        'class_path = "deepagents_code._testing_models:ToolCallingIntegrationChatModel"\n'
        'models = ["fake"]\n'
    )
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    target = workdir / "headless_out.txt"

    lc_code = Path(sys.executable).parent / "lc-code"
    assert lc_code.exists(), "lc-code console script not installed (run uv sync)"

    # Hermetic child env: drop inherited agent/tracing configuration so the
    # run does not depend on the developer's or CI's environment.
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DEEPAGENTS_CODE_", "LANGSMITH_", "LANGCHAIN_"))
    }
    child_env["HOME"] = str(home)
    child_env["DEEPAGENTS_CODE_NO_UPDATE_CHECK"] = "1"

    result = subprocess.run(
        [
            str(lc_code),
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
    # The deterministic model's gated write executed. That it ran through the
    # FACTORY graph (not upstream's) is proven in-process by
    # tests/test_launch.py's scaffold-target and seam-rebind tests.
    assert target.read_text() == TOP_LEVEL_WRITE_CONTENT
    # The session persisted in the standard dcode sessions DB location.
    assert (home / ".deepagents" / ".state" / "sessions.db").exists()
