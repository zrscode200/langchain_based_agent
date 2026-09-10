"""Small workspace-bound catalog adapter; the browser UI is TypeScript.

This shares the constructed graph's tool inventory and the factory's skill
policy. It never accepts arbitrary paths for skill invocation.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.routing import Route

from lc_factory.skill_policy import (
    SelectedSkillBackend, discover_project_skills, resolve_skill_policy,
    skill_policy_root,
)
from lc_factory.upstream import (
    PluginSkillsMiddleware, ProjectContext, ServerConfig,
    WorkspaceConflictError, get_skill_sources, require_thread_workspace,
    web_skill_helpers,
)


def discover(binding):
    root = skill_policy_root(binding.cwd)
    policy = resolve_skill_policy(root)
    if policy.mode == "project":
        return discover_project_skills(root, policy)
    context = ProjectContext(user_cwd=Path(binding.cwd), project_root=Path(binding.project_root) if binding.project_root else None)
    sources = get_skill_sources(ServerConfig.from_env().assistant_id, context)
    roots = [Path(s[0]).resolve() for s in sources]
    middleware = PluginSkillsMiddleware(backend=SelectedSkillBackend(sources), sources=sources)
    update = middleware.before_agent({}, None, {}) or {}
    rows = update.get("skills_metadata", [])
    return rows, roots


def skill_message(binding, key, args):
    if not isinstance(key, str) or not isinstance(args, str) or len(args) > 100_000:
        raise ValueError("Invalid skill invocation")
    rows, roots = discover(binding)
    # Path is only an opaque catalog key, never a caller-selected read target.
    row = next((r for r in rows if r["path"] == key), None)
    if row is None or not roots:
        raise ValueError("Skill is no longer in this workspace's catalog")
    load_content, envelope = web_skill_helpers()
    content = load_content(row["path"], allowed_roots=roots)
    if content is None or len(content.encode("utf-8")) > 1_048_576:
        raise ValueError("Skill content is unavailable or too large")
    wrapped = envelope(row, content, args)
    return {"role": "user", "content": wrapped.prompt, **wrapped.message_kwargs}


async def catalog_runtime(binding, thread_id):
    """Observe an effective graph without pinning an idle thread generation.

    The factory owns generation lifetimes; the web adapter only reads its
    existing pin under the same lock. Selecting session=None may refresh the
    current graph, but must not reserve it for a future user turn.
    """
    from lc_factory.server_graph import (
        _factory_runtime_owners, _workspace_runtime, _workspace_runtimes,
    )
    runtime = await _workspace_runtime(binding)
    base = _workspace_runtimes.get(binding.resource_key)
    owner = _factory_runtime_owners.get(id(base.agent)) if base is not None else None
    if owner is None:
        return runtime
    async with owner._lock:
        if owner.closed:
            raise RuntimeError("Workspace runtime closed")
        return owner._thread_generations.get(thread_id, owner.current)


async def web_operation(request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Expected object")
        binding = await require_thread_workspace(request.path_params["thread_id"], body.get("workspace"))
        operation = body.get("operation")
        if operation == "invoke":
            return JSONResponse(await asyncio.to_thread(skill_message, binding, body.get("skill"), body.get("args", "")))
        if operation != "catalog":
            raise ValueError("Unknown operation")
        runtime = await catalog_runtime(binding, request.path_params["thread_id"])
        node = runtime.agent.nodes.get("tools")
        tools = getattr(getattr(node, "bound", None), "tools_by_name", {})
        rows, _ = await asyncio.to_thread(discover, binding)
        config = ServerConfig.from_env()
        return JSONResponse({
            "skills": [{k: str(row.get(k, "")) for k in ("name", "description", "path", "source")} for row in rows],
            "tools": [{"name": tool.name, "description": tool.description} for tool in tools.values()],
            "model": config.model,
            "capabilities": {"ask_user": config.enable_ask_user, "shell": config.enable_shell,
                             "hooks": False, "workspace": binding.cwd},
            "notice": "Configured command hooks require the native TUI. Model availability and credentials are validated by the agent server.",
        })
    except WorkspaceConflictError:
        return JSONResponse({"detail": "Workspace binding changed"}, status_code=409)
    except (ValueError, TypeError, PermissionError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    except (RuntimeError, SystemExit):
        return JSONResponse({"detail": "Workspace runtime unavailable"}, status_code=503)


def install_web_routes(app):
    route = "/lc-factory/threads/{thread_id}/web"
    if not any(getattr(item, "path", None) == route for item in app.routes):
        app.router.routes.append(Route(route, web_operation, methods=["POST"]))
