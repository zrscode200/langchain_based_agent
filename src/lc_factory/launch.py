"""Workspace scaffolding for a ``langgraph dev`` server on the factory graph.

Port of the workspace-scaffolding half of
``deepagents_code.client.launch.server_manager`` at
commit ``6c89fe2197a2dfe4f3851cda38565bcadba6066b`` (version ``0.1.66``).
The scaffold bodies are unchanged from the release. The generated
``langgraph.json`` references ``lc_factory.server_graph:make_graph`` and its
factory offload adapter, while the generated runtime pyproject depends on
``lc_factory`` (which transitively pins ``deepagents-code``).

:func:`scaffold_workspace` is the rebind target for the TUI launch seam — see
:mod:`lc_factory.tui`. Everything else about starting the server (the
``ServerConfig`` env bridge, ``ServerProcess``, ``RemoteAgent``) is upstream's
own code, reached through that seam, so the sessions DB layout and client
protocol stay TUI-compatible by construction.

A standalone launcher was ported alongside this in Group 1 and deleted at the
Group 2 closeout: it duplicated upstream's ``start_server_and_get_agent``,
never had a production caller, and every pin bump would have owed it
re-application. Recover it from history if a non-TUI embedding ever needs one.
"""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from lc_factory.upstream import _write_checkpointer, generate_langgraph_json

_DISTRIBUTION_NAME = "lc_factory"
GRAPH_REF = "lc_factory.server_graph:make_graph"


def scaffold_workspace(work_dir: Path) -> None:
    """Prepare the server working directory for the FACTORY graph.

    Port of upstream ``_scaffold_workspace``; the graph reference points at
    the installed ``lc_factory`` package instead of ``deepagents_code``.
    Also the rebind target for the TUI launch seam (see ``lc_factory.tui``).

    Args:
        work_dir: Temporary directory that will become the server's cwd.
    """
    _write_checkpointer(work_dir)
    _write_pyproject(work_dir)

    # `graph_ref` is a dotted import of the installed `lc_factory` package;
    # `checkpointer_path` stays cwd-relative because checkpointer.py is
    # generated fresh into work_dir and is not an importable package module.
    config_path = generate_langgraph_json(
        work_dir,
        graph_ref=GRAPH_REF,
        checkpointer_path="./checkpointer.py:create_checkpointer",
    )
    # Upstream registers its workspace/offload app only for its built-in graph
    # reference. The factory owns equivalent runtimes, so add the adapter with
    # the same route-auth opt-in after parsing the generated config structurally.
    config = json.loads(config_path.read_text())
    config["http"] = {
        "app": "lc_factory.offload_api:app",
        "enable_custom_route_auth": True,
    }
    config_path.write_text(json.dumps(config, indent=2))


def _write_pyproject(work_dir: Path) -> None:
    """Write a minimal pyproject.toml for the server working directory.

    Args:
        work_dir: Server working directory.
    """
    content = f"""[project]
name = "lc-factory-server-runtime"
version = "0.0.1"
requires-python = ">=3.12"
dependencies = [
    "{_runtime_package_dependency()}",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""
    (work_dir / "pyproject.toml").write_text(content)


def _default_package_project_root() -> Path | None:
    """Return the project root that contains the ``lc_factory`` package.

    src layout: in editable checkouts the package resolves to
    ``<root>/src/lc_factory`` (root is two levels up); for installed wheels it
    resolves to ``site-packages/lc_factory`` (no pyproject above it, so the
    caller falls back to the installed distribution version).

    Returns:
        First ancestor directory holding a ``pyproject.toml``, or ``None``.
    """
    import lc_factory

    package_init = getattr(lc_factory, "__file__", None)
    if package_init is None:
        return None
    package_dir = Path(package_init).resolve().parent
    for candidate in (package_dir.parent, package_dir.parent.parent):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def _runtime_package_dependency(package_root: Path | None = None) -> str:
    """Return the dependency spec for ``lc_factory`` in the server runtime.

    Args:
        package_root: Optional package project root for tests.

    Returns:
        Requirement string for the generated runtime ``pyproject.toml``.
    """
    root = package_root or _default_package_project_root()
    if root is not None and (root / "pyproject.toml").is_file():
        return f"{_DISTRIBUTION_NAME} @ {root.as_uri()}"

    try:
        installed_version = version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _DISTRIBUTION_NAME
    return f"{_DISTRIBUTION_NAME}=={installed_version}"
