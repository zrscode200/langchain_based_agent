"""MCP discovery with immutable, stateless transports for reload generations.

Adapted from deepagents_code.server_graph at the pinned revision. MIT licensed.
The regular server's global session cache cannot be reconfigured safely.
"""
import asyncio

from lc_factory.upstream import (
    create_web_search_tool, fetch_url, get_current_thread_id,
    discover_plugin_mcp_configs, resolve_and_load_mcp_tools,
)


async def build_reloadable_tools(config, project_context, *, has_tavily=False, tavily_api_key=None):
    tools = [fetch_url, get_current_thread_id]
    if has_tavily:
        tools.append(create_web_search_tool(tavily_api_key or ""))
    if config.no_mcp:
        return tools, None, []
    project_dir = (project_context.project_root or project_context.user_cwd
                   if project_context is not None else None)
    plugins = await asyncio.to_thread(discover_plugin_mcp_configs, project_dir=project_dir)
    mcp_tools, _, info = await resolve_and_load_mcp_tools(
        explicit_config_path=config.mcp_config_path, no_mcp=False,
        trust_project_mcp=config.trust_project_mcp, project_context=project_context,
        additional_configs=plugins, stateless=True, session_manager=None,
    )
    # Each returned tool owns its captured connection config and opens/closes a
    # fresh session per call. Old graphs never observe a candidate's endpoints.
    return [*tools, *mcp_tools], info, mcp_tools


def load_async_subagent_snapshot():
    """Read remote definitions strictly while retaining managed configuration policy."""
    from lc_factory.upstream import get_config_sources
    sources = get_config_sources()
    if not sources.user.status.usable or not sources.managed.status.usable:
        raise ValueError("Cannot reload unreadable subagent configuration")
    data, _ = sources.merged()
    section = data.get("async_subagents", {})
    if not isinstance(section, dict):
        raise ValueError("async_subagents must be a table")
    agents = []
    for name, spec in section.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(spec, dict):
            raise ValueError("Invalid async subagent definition")
        for key in ("description", "graph_id"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise ValueError(f"Async subagent {name} requires {key}")
        result = {"name": name, "description": spec["description"], "graph_id": spec["graph_id"]}
        if "url" in spec:
            if not isinstance(spec["url"], str) or not spec["url"].strip():
                raise ValueError(f"Async subagent {name} has an invalid URL")
            result["url"] = spec["url"]
        if "headers" in spec:
            headers = spec["headers"]
            if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
                raise ValueError(f"Async subagent {name} has invalid headers")
            result["headers"] = dict(headers)
        agents.append(result)
    return agents
