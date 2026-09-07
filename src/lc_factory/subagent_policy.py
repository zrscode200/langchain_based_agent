"""Owned declarative subagent composition and tool enforcement.

Policies constrain model/ToolNode/PTC access. Host-supplied Python tools and
middleware remain trusted application code, not a filesystem sandbox.
"""
from __future__ import annotations

from collections.abc import Mapping, Set
from copy import deepcopy
from dataclasses import replace
from typing import get_args

from lc_factory.upstream import AgentMiddleware, FilesystemMiddleware, FilesystemPermission, FsToolName, ToolMessage, ToolStrategy
from lc_factory.structured_results import StructuredResultMiddleware
from lc_factory.workspace_subagents import resolve_skill_sources, string_list

SPEC_KEYS = frozenset({"name", "description", "system_prompt", "mode", "tools", "model",
                      "middleware", "skills", "permissions", "response_format", "fs_tools"})
FS_NAMES = frozenset(get_args(FsToolName))
POLICY_MIDDLEWARE_NAMES = frozenset({"FactoryToolPolicy", "FactoryToolPolicyFinal", "StructuredResultMiddleware"})


def tool_name(tool):
    if isinstance(tool, Mapping):
        return tool.get("name") or tool.get("function", {}).get("name")
    return getattr(tool, "name", None) or getattr(tool, "__name__", None)


def checked_specs(specs):
    if specs is None:
        return []
    if isinstance(specs, (str, bytes, Mapping, Set)):
        raise ValueError("subagents must be an ordered sequence of declarative specs")
    result, seen = [], set()
    for raw in specs:
        if not isinstance(raw, Mapping):
            raise ValueError("Each subagent must be a declarative mapping")
        if unknown := set(raw) - SPEC_KEYS:
            raise ValueError(f"Subagent {raw.get('name')!r} has unsupported keys {sorted(unknown, key=str)}; "
                             "compiled/remote kinds and interrupt_on are not supported here")
        for key in ("name", "description"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ValueError(f"Subagent requires a non-empty {key}")
        name = raw["name"]
        if name in seen:
            raise ValueError(f"Duplicate programmatic subagent {name!r}")
        seen.add(name)
        spec = dict(raw)
        for key in ("tools", "middleware", "skills", "permissions", "fs_tools"):
            if key in spec:
                if isinstance(spec[key], (str, bytes, Mapping, Set)) or spec[key] is None:
                    raise ValueError(f"Subagent {name!r} {key} must be an ordered sequence")
                spec[key] = list(spec[key])
        if "system_prompt" in spec and not isinstance(spec["system_prompt"], str):
            raise ValueError(f"Subagent {name!r} system_prompt must be a string")
        if "mode" in spec and spec["mode"] not in ("isolated", "fork"):
            raise ValueError(f"Subagent {name!r} mode must be isolated or fork")
        for permission in spec.get("permissions", []):
            if not isinstance(permission, FilesystemPermission) or permission.mode not in ("allow", "deny"):
                raise ValueError(f"Subagent {name!r} permissions must be allow/deny FilesystemPermission objects; "
                                 "the factory owns approval routing")
        if "permissions" in spec:
            spec["permissions"] = deepcopy(spec["permissions"])
        if "skills" in spec:
            spec["skills"] = string_list(spec["skills"], f"Subagent {name!r} skills")
        if "fs_tools" in spec:
            spec["fs_tools"] = checked_fs_names(spec["fs_tools"], name)
        result.append(spec)
    return result


def checked_fs_names(names, agent_name):
    names = string_list(names, f"Subagent {agent_name!r} fs_tools")
    if unknown := set(names) - FS_NAMES:
        raise ValueError(f"Subagent {agent_name!r} has unknown filesystem tools: {sorted(unknown)}")
    return names


def prepare_specs(definitions, supplied, policy, *, general_purpose, project_root):
    """Merge file definitions and host specs without mutating caller input."""
    merged = {}
    for meta in definitions:
        name = meta["name"]
        if name in merged:
            raise ValueError(f"Duplicate file subagent {name!r}")
        merged[name] = {key: meta[key] for key in ("name", "description", "system_prompt", "model") if key in meta}
    # Host specs replace the file definition. Policy then constrains either
    # construction path; using Python specs must not bypass declared policy.
    for spec in supplied:
        merged[spec["name"]] = dict(spec)
    merged.setdefault(general_purpose["name"], dict(general_purpose))
    if unknown := set(policy) - set(merged):
        raise ValueError(f"Subagent policy names unknown local agents: {sorted(unknown)}")
    for name, entry in policy.items():
        spec = merged[name]
        for key, value in entry.items():
            if key == "tools":
                spec["_factory_allowed_tools"] = frozenset(value)
            elif key == "skills":
                spec["skills"] = resolve_skill_sources(name, value, project_root)
            else:
                spec[key] = deepcopy(value)
    for spec in merged.values():
        if spec.get("mode") == "fork" and "skills" in spec:
            raise ValueError(f"Subagent {spec['name']!r} cannot declare skills under fork mode; choose isolated")
    return list(merged.values())


class FactoryToolPolicy(AgentMiddleware):
    """Filter before QuickJS binds PTC tools and reject explicit forbidden calls."""
    def __init__(self, *, allowed=None, denied=(), final=False):
        self.allowed = None if allowed is None else frozenset(allowed)
        self.denied = frozenset(denied)
        self.final = final

    @property
    def name(self):
        return "FactoryToolPolicyFinal" if self.final else "FactoryToolPolicy"

    def permits(self, name):
        return name not in self.denied and (self.allowed is None or name in self.allowed)

    def wrap_model_call(self, request, handler):
        return handler(request.override(tools=[t for t in request.tools if self.permits(tool_name(t))]))

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(tools=[t for t in request.tools if self.permits(tool_name(t))]))

    def _denial(self, request):
        if not self.permits(request.tool_call["name"]):
            return ToolMessage(content="Tool is not permitted by this subagent's policy.",
                               tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error")
        return None

    def wrap_tool_call(self, request, handler):
        denial = self._denial(request)
        return denial if denial is not None else handler(self._filtered_runtime(request))

    async def awrap_tool_call(self, request, handler):
        denial = self._denial(request)
        return denial if denial is not None else await handler(self._filtered_runtime(request))

    def _filtered_runtime(self, request):
        # QuickJS's built-in task() discovers its raw task tool at eval time
        # through ToolRuntime.tools, separately from PTC's model-time binding.
        # Other dispatch helpers must see the same effective capability list.
        return replace(request, runtime=replace(request.runtime, tools=[
            tool for tool in request.runtime.tools if self.permits(tool_name(tool))
        ]))


class FixedSubagentModel(AgentMiddleware):
    """Replace a fork's inherited model-switch middleware with a fixed-model slot."""
    @property
    def name(self):
        return "ConfigurableModelMiddleware"

    def wrap_model_call(self, request, handler):
        return handler(request)

    async def awrap_model_call(self, request, handler):
        return await handler(request)


def finish_specs(specs, *, main_middleware, tools, backend, fs_tools, descriptions):
    """Install policies against the final caller/extension tool catalog."""
    base_catalog = {tool_name(t): t for t in tools if tool_name(t)}
    catalog = dict(base_catalog)
    for middleware in main_middleware:
        for tool in getattr(middleware, "tools", ()):
            if tool_name(tool):
                catalog.setdefault(tool_name(tool), tool)
    needs_parent_slot = False
    for spec in specs:
        name = spec["name"]
        allowed = spec.pop("_factory_allowed_tools", None)
        child_fs = spec.pop("fs_tools", None)
        extra_names = spec.pop("_factory_extra_middleware_names", set())
        if spec.get("mode") == "fork":
            inherited = {m.name for m in main_middleware}
            if collision := inherited & extra_names:
                raise ValueError(f"Subagent {name!r} middleware replaces inherited names: {sorted(collision)}")
        if allowed is not None:
            # Isolated agents do not inherit main middleware lifecycles (e.g.
            # QuickJS slot setup). Borrowing only such a middleware's tool
            # closure would expose a capability that cannot actually run.
            child_catalog = dict(catalog if spec.get("mode") == "fork" else base_catalog)
            for tool in [*spec.get("tools", []),
                         *(t for m in spec["middleware"] for t in getattr(m, "tools", ()))]:
                if tool_name(tool):
                    child_catalog[tool_name(tool)] = tool
            generated = {"task"} if spec.get("mode") == "fork" else set()
            if unknown := set(allowed) - set(child_catalog) - FS_NAMES - generated:
                raise ValueError(f"Subagent {name!r} requests unknown tools: {sorted(unknown)}")
            spec["tools"] = [child_catalog[n] for n in sorted(allowed) if n in child_catalog and n not in FS_NAMES]
            requested_fs = set(allowed) & FS_NAMES
            child_fs = sorted(requested_fs if child_fs is None else requested_fs & set(child_fs))
        denied = set()
        if child_fs is not None:
            effective = set(child_fs) & (FS_NAMES if fs_tools is None else set(fs_tools))
            denied = set(FS_NAMES) - effective
        else:
            effective = FS_NAMES if fs_tools is None else set(fs_tools)
        if child_fs is not None or "permissions" in spec:
            spec["middleware"] = [m for m in spec["middleware"] if m.name != "FilesystemMiddleware"]
            spec["middleware"].append(FilesystemMiddleware(
                backend=backend, tools=sorted(effective | {"read_file"}),
                custom_tool_descriptions=descriptions(spec.get("model")),
                _permissions=spec.get("permissions") or None,
            ))
        if allowed is not None or denied:
            spec["middleware"].insert(0, FactoryToolPolicy(allowed=allowed, denied=denied))
            spec["middleware"].append(FactoryToolPolicy(allowed=allowed, denied=denied, final=True))
            needs_parent_slot |= spec.get("mode") == "fork"
        response_format = spec.get("response_format")
        schema = response_format if isinstance(response_format, Mapping) else getattr(response_format, "schema", None)
        if isinstance(schema, Mapping):
            if isinstance(response_format, Mapping):
                spec["response_format"] = ToolStrategy(deepcopy(dict(schema)))
            spec["middleware"].append(StructuredResultMiddleware(dict(schema), agent_name=name))
    if needs_parent_slot:
        # A child-only new name would land after inherited QuickJS. Same-name
        # replacement preserves this early position in each restricted fork.
        # Place immediately before capture, after preceding caller middleware
        # has had the chance to add tools. The inner guard filters later adds.
        index = next((i for i, m in enumerate(main_middleware)
                      if m.name == "CodeInterpreterMiddleware"), 0)
        main_middleware.insert(index, FactoryToolPolicy())
