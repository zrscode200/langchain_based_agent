"""Wave 1.3: import-boundary integrity — the pin-bump tripwire."""

from __future__ import annotations

import ast
from pathlib import Path

import lc_factory

BANNED_PREFIXES = ("deepagents", "langchain", "langgraph")


def test_every_boundary_symbol_resolves():
    """Every name the boundary claims to re-export exists at the pin.

    On a pin bump, an upstream rename/removal fails HERE first, by name.
    """
    import lc_factory.upstream as upstream

    missing = [name for name in upstream.__all__ if not hasattr(upstream, name)]
    assert not missing, f"boundary symbols missing at pinned version: {missing}"


def test_no_direct_upstream_imports_outside_boundary():
    """src/lc_factory imports upstream ONLY through upstream.py."""
    package_dir = Path(lc_factory.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        if path.name == "upstream.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(BANNED_PREFIXES):
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(BANNED_PREFIXES):
                    offenders.append(f"{path.name}:{node.lineno} from {module}")
    assert not offenders, f"direct upstream imports outside the boundary: {offenders}"
