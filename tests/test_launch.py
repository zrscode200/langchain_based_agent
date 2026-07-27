"""Wave 1.2: the micro-launcher scaffolds a factory-graph server workspace."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

from lc_factory import launch


def test_scaffold_workspace_targets_factory_graph(tmp_path):
    launch.scaffold_workspace(tmp_path)

    config = json.loads((tmp_path / "langgraph.json").read_text())
    assert config["graphs"] == {"agent": "lc_factory.server_graph:make_graph"}

    checkpointer = (tmp_path / "checkpointer.py").read_text()
    assert "AsyncSqliteSaver" in checkpointer
    assert "DEEPAGENTS_CODE_SERVER_DB_PATH" in checkpointer

    pyproject = (tmp_path / "pyproject.toml").read_text()
    assert "lc-factory-server-runtime" in pyproject
    assert "lc_factory" in pyproject


def test_runtime_package_dependency_editable_root():
    # Editable checkout: direct file URI to this project root (src layout:
    # three levels up from launch.py).
    root = Path(launch.__file__).resolve().parent.parent.parent
    dep = launch._runtime_package_dependency()
    assert dep == f"lc_factory @ {root.as_uri()}"


def test_runtime_package_dependency_fallback(tmp_path):
    # A root without pyproject.toml falls back to the installed distribution.
    dep = launch._runtime_package_dependency(package_root=tmp_path)
    assert dep == f"lc_factory=={version('lc_factory')}"


def test_tui_main_rebinds_scaffold_seam(monkeypatch):
    """The TUI entry rebinds the launch seam before upstream cli_main runs.

    This is the port's single point of upstream coupling: `tui.main` must
    rebind `server_manager._scaffold_workspace` (resolved by upstream as a
    module global at call time) so every launch path — TUI startup, restart,
    cwd switch, headless — scaffolds the FACTORY graph.
    """
    from lc_factory import tui
    from lc_factory.upstream import server_manager_module

    # Register the original with monkeypatch so teardown restores it.
    monkeypatch.setattr(
        server_manager_module,
        "_scaffold_workspace",
        server_manager_module._scaffold_workspace,
    )

    seen = {}

    def fake_cli_main():
        seen["scaffold"] = server_manager_module._scaffold_workspace

    monkeypatch.setattr(tui, "cli_main", fake_cli_main)
    tui.main()

    assert seen["scaffold"] is launch.scaffold_workspace
