"""Wave 1.2: the micro-launcher scaffolds a factory-graph server workspace."""

from __future__ import annotations

import json

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
    dep = launch._runtime_package_dependency()
    # Editable checkout: direct file URI to this project root.
    assert dep.startswith("lc_factory @ file://")
    assert dep.endswith("langchain_based_agent")


def test_runtime_package_dependency_fallback(tmp_path):
    # A root without pyproject.toml falls back to the installed distribution.
    dep = launch._runtime_package_dependency(package_root=tmp_path)
    assert dep == "lc_factory==0.1.0"
