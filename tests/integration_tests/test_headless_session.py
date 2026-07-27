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
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    # The deterministic model's gated write executed through OUR graph.
    assert target.read_text() == "auto-approved"
    # The session persisted in the standard dcode sessions DB location.
    assert (home / ".deepagents" / ".state" / "sessions.db").exists()
