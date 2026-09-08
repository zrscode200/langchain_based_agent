# MCP resources across reloads

With `LC_FACTORY_CAPABILITIES=reload`, the factory gives each successful MCP
configuration generation its own persistent session manager. Repeated calls to
a stdio connector reuse its process and session instead of starting a process
for every call. The default non-reload server path retains its upstream behavior.

Each server connection has one long-lived owner task. That task opens,
initializes, and closes the transport's AnyIO scopes. Caller cancellation does
not cancel shared initialization. Failed startup can be retried; transport
invalidation compares the expected session identity so a stale failure cannot
close a newer connection. Configuration is frozen before the first call, and
changing it requires a new generation. Ordinary configuration containers are
copied; trusted OAuth/transport handles retain their normal lifecycle.

`build_reloadable_tools` returns `MCPToolBundle`, which still unpacks as
`tools, server_info, mcp_tools`. It also provides async `close()`. The server
hands the initial bundle to `FactoryRuntime`; runtime reloads take ownership of
subsequent bundles. Any rejected, failed, or cancelled candidate closes its
resources. Early server setup failures close the initial bundle too.

```python
bundle = await build_reloadable_tools(config, project_context)
tools, server_info, mcp_tools = bundle
runtime = await FactoryRuntime.create(
    agent_kwargs={**agent_kwargs, "tools": tools, "mcp_tools": mcp_tools,
                  "mcp_server_info": server_info},
    initial_resources=bundle,
    reload_tools=load_next_bundle,
    options=RuntimeOptions(reload=True),
    workspace_id=workspace_id,
)
try:
    result = await runtime.ainvoke(inputs, config)
finally:
    await runtime.close()
```

Direct embeddings can keep returning ordinary three-value tuples from their
loader, in which case the embedding continues to own those external resources.
Owned MCP generations must run on the event loop that owns their transports;
use the async runtime/server APIs.

## Retention and shutdown

Old graphs, interrupted threads, and background jobs keep their original
compiled tools. Successful MCP bundles remain open until runtime shutdown.
The raw server graph interface does not provide complete run leases, so a
checkpoint completion is not enough evidence to retire a bundle safely.

Retention defaults to eight MCP generations per runtime. At that limit a reload
is rejected before discovery starts, with restart guidance; existing work keeps
running. Direct hosts can set `max_retained_generations` when constructing the
runtime. Empty no-MCP bundles do not consume slots. This bounds retained MCP
resources, not every possible caller-owned graph, model, or store.

Stop serving new runs before closing a host. Runtime close cancels embedded
active work and background workers before closing retained sessions. The
connector owner performs teardown in its own task with a five-second timeout;
transport teardown failures are logged. Resource retention is deliberately
conservative. Prompt retirement requires real leases across server calls,
interrupts, and workers.

## Connector state and budgets

A connector's in-process cache and counters survive ordinary calls within one
session. An intentional reload or transport reconnection creates a new process
and may reset them. The enterprise MUSE connector's budget remains a per-process
budget; this change does not turn it into a durable cross-reload spending limit.
Enforce such limits in a shared ledger or the information service when required.

Discovery still uses a throwaway session before execution's persistent session,
so connector startup/cache warming can occur during both. Self-describing MUSE
handles remain usable across process restarts; process-local caches and budget
counters do not. Separate generations and workspaces receive their own resolved
connection configurations, rather than rebinding an existing global manager.
