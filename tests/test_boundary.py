"""Wave 1.3: import-boundary integrity — the pin-bump tripwire."""

from __future__ import annotations

import ast
import importlib
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


def test_type_only_boundary_symbols_resolve():
    """Annotation-only upstream names are inside the tripwire too.

    These live in `if TYPE_CHECKING:` and are never evaluated at runtime, so
    an upstream rename would otherwise break silently — there is no type
    checker in this project's pipeline to catch it.
    """
    import lc_factory.upstream as upstream

    missing = []
    for module_name, symbol in upstream.TYPE_ONLY_IMPORTS:
        module = importlib.import_module(module_name)
        if not hasattr(module, symbol):
            missing.append(f"{module_name}.{symbol}")
    assert not missing, f"type-only upstream symbols missing at pin: {missing}"


def test_type_only_list_matches_type_checking_block():
    """`TYPE_ONLY_IMPORTS` stays in sync with the TYPE_CHECKING imports."""
    import lc_factory.upstream as upstream

    source = Path(upstream.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    declared = {(module, symbol) for module, symbol in upstream.TYPE_ONLY_IMPORTS}

    actual: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and getattr(node.test, "id", "") == "TYPE_CHECKING"):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom) and child.module:
                actual.update((child.module, alias.name) for alias in child.names)

    assert actual == declared, (
        "TYPE_ONLY_IMPORTS drifted from the TYPE_CHECKING block: "
        f"only-in-block={sorted(actual - declared)}, "
        f"only-in-list={sorted(declared - actual)}"
    )


def test_runtime_upstream_imports_are_all_exported():
    """Every upstream name bound at runtime in the boundary is in `__all__`.

    Without this, a symbol could be imported into the boundary yet sit
    outside the sweep that guards pin bumps.
    """
    import lc_factory.upstream as upstream

    source = Path(upstream.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    exported = set(upstream.__all__)

    unexported = []
    for node in tree.body:  # module level only — skips the TYPE_CHECKING block
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            BANNED_PREFIXES
        ):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound not in exported:
                    unexported.append(f"{node.module}.{alias.name} (as {bound})")
    assert not unexported, f"upstream imports missing from __all__: {unexported}"


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
