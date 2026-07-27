"""TUI entry: run the upstream deepagents-code TUI against the factory graph.

The upstream app calls ``server_manager.start_server_and_get_agent``, which
resolves ``_scaffold_workspace`` as a module global at call time (both for
the direct scaffold call and for ``ServerProcess(scaffold=...)``). Rebinding
that single symbol repoints the generated ``langgraph.json`` at
``lc_factory.server_graph:make_graph`` while every other client behavior —
approvals, resume, sessions, headless mode — stays stock upstream. The
headless/non-interactive path flows through the same seam.

This is the launch seam recorded in decisions.md D3. An upstream PR making
the graph factory pluggable would remove the rebind entirely.
"""

from __future__ import annotations

from lc_factory import launch
from lc_factory.upstream import cli_main, server_manager_module


def main() -> None:
    """Launch the dcode TUI (or headless CLI) wired to the factory graph."""
    server_manager_module._scaffold_workspace = launch.scaffold_workspace  # noqa: SLF001
    cli_main()
