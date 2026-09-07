"""Strict, data-only workspace policies for named factory subagents."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
import tomllib

POLICY_KEYS = frozenset({"tools", "skills", "model", "response_format", "mode"})


def string_list(value, label):
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} contains duplicates")
    return list(value)


def validate_subagent_policy(value):
    if not isinstance(value, Mapping):
        raise ValueError("subagent_policy must be a mapping of names to policies")
    result = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(raw, Mapping):
            raise ValueError("Every subagent policy needs a non-empty name and a table")
        if unknown := set(raw) - POLICY_KEYS:
            raise ValueError(f"Subagent {name!r} has unknown policy keys: {sorted(unknown, key=str)}")
        entry = deepcopy(dict(raw))
        for key in ("tools", "skills"):
            if key in entry:
                entry[key] = string_list(entry[key], f"Subagent {name!r} {key}")
        if "model" in entry and (not isinstance(entry["model"], str) or not entry["model"].strip()):
            raise ValueError(f"Subagent {name!r} model must be a non-empty reference")
        if "mode" in entry and entry["mode"] not in ("isolated", "fork"):
            raise ValueError(f"Subagent {name!r} mode must be isolated or fork")
        if "response_format" in entry and not isinstance(entry["response_format"], dict):
            raise ValueError(f"Subagent {name!r} response_format must be a JSON schema table")
        result[name] = entry
    return result


def load_subagent_policy(project_root: Path | None):
    """Missing means inheritance; present invalid policy never means inheritance."""
    if project_root is None:
        return {}
    path = Path(project_root) / ".deepagents" / "subagents.toml"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError(f"Subagent policy is a broken symlink: {path}") from None
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Cannot read subagent policy {path}: {type(exc).__name__}") from exc
    if set(data) != {"subagents"}:
        raise ValueError(f"{path} must contain only the [subagents] policy table")
    try:
        return validate_subagent_policy(data["subagents"])
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def resolve_skill_sources(name, sources, project_root):
    if project_root is None:
        raise ValueError(f"Subagent {name!r} skills require a workspace root")
    root = Path(project_root).resolve()
    resolved = []
    for source in sources:
        path = (root / source).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"Subagent {name!r} skill path escapes workspace: {source!r}")
        resolved.append(str(path))
    return resolved
