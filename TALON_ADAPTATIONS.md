# Optional runtime capabilities

The factory adapts three capabilities from Talon: configuration reload,
background delegation, and searchable conversation history. They run around
`create_factory_agent`, so the fully wired Deep Agents Code harness and the
main/subagent/grader middleware seams remain the foundation. Existing factory
calls and `lc-code` launches keep their default behavior.

The source reference is Deep Agents commit
`6c89fe2197a2dfe4f3851cda38565bcadba6066b`, `libs/talon` version 0.0.6.
`archive.py` and `archive_saver.py` adapt Talon's corresponding files;
`background.py` adapts its worker registry and task-detachment pattern.
`mcp_reload.py` adapts the Code server's tool discovery. Copyright LangChain,
Inc.; the MIT notice is retained in `LICENSE`. No Talon package, channel,
OAuth server, cron scheduler, or hosted LangChain service is required.

## Use with lc-code

Set capabilities in the process environment before starting the CLI:

```sh
export LC_FACTORY_CAPABILITIES=reload,background,history
lc-code
```

Select any comma-separated subset. These settings are deliberately reserved
before project `.env` loading, just like `LC_FACTORY_MIDDLEWARE`; a repository
cannot enable them through its `.env`. The existing model configuration,
including your company's model endpoint, remains applicable.

History defaults to the current thread within its persisted workspace. For a
local, single-user server that should search across `/new` conversations, also
set a stable host-owned grouping value:

```sh
export LC_FACTORY_HISTORY_OWNER=local-user
```

This value is a history grouping key, not authentication. Use a separate server
per identity, or adapt the trusted `server_scope` callback to an authenticated
host identity for shared deployments. Do not supply an identity from model
arguments or arbitrary client metadata. Different workspaces remain separate.
The server archive is stored beside the existing sessions database with the
suffix `.factory-history.sqlite`.

The generated server checkpointer points to
`lc_factory.server_checkpointer:create_checkpointer` when capabilities are on.
A custom server launcher must use that factory as its execution-time
checkpointer too: it records committed history, releases completed graph
versions, and closes background workers before closing storage. The factory
still returns an actual compiled LangGraph graph to the server.

## Direct embedding

The asynchronous `FactoryRuntime` owns graph selection and workers. The host
owns the supplied model, saver, archive, sandbox, and MCP provider. Keep those
resources open until the runtime exits:

```python
from lc_factory.runtime import FactoryRuntime, RuntimeOptions
from lc_factory.archive import SQLiteConversationArchive
from lc_factory.upstream import AsyncSqliteSaver

# `model` is your configured chat model; `reload_tools` is described below.
async with AsyncSqliteSaver.from_conn_string("checkpoints.sqlite") as saver:
    async with SQLiteConversationArchive.from_conn_string("history.sqlite") as archive:
        async with await FactoryRuntime.create(
            agent_kwargs={
                "model": model,
                "assistant_id": "work-agent",
                "cwd": "/approved/workspace",
                "checkpointer": saver,
                "enable_ask_user": False,
                # Supply middleware/subagent_middleware/rubric_grader_middleware
                # here using the existing constructor contracts.
            },
            options=RuntimeOptions(reload=True, background=True, history=True),
            workspace_id="workspace-identity",
            owner_id="authenticated-user-identity",
            archive=archive,
            reload_tools=reload_tools,
        ) as runtime:
            config = {"configurable": {"thread_id": "conversation-1"}}
            result = await runtime.ainvoke(
                {"messages": [{"role": "user", "content": "Investigate this topic"}]},
                config,
            )
            # Handle result["__interrupt__"] with your normal approval UI.
            # Resume with Command(resume=...) through runtime.ainvoke.
```

`runtime.astream(input, config, **kwargs)` forwards graph streaming options.
Calls on the same embedded thread are serialized. Use the managed invocation
methods; calling `runtime.current.agent` directly bypasses that host lifecycle.
The plain `create_factory_agent` remains available for consumers that own it.
When history is enabled, the runtime wraps the supplied async checkpointer in
`ConversationSaver`. It does not replace your storage backend.

## Reload behavior

The main agent receives `reload_mcp_configuration` and
`reload_subagent_configuration`. Both queue one complete configuration refresh
for the next eligible turn; currently they refresh MCP and local/remote agent
configuration together. The host can also call `runtime.request_reload()`.
`generation` and `last_reload_error` expose reload outcome to an embedding.

A direct embedding's async `reload_tools()` returns
`(all_tools, mcp_server_info, mcp_tools)`; it must return a new, internally
consistent snapshot and raise on failure. Without this callback, the MCP reload
tool reports that no provider is configured; subagent reload still works.
`reload_async_subagents` optionally supplies a synchronous remote-definition
loader. Explicit `subagent_definitions` remains the low-level constructor seam
for hosts providing their own already-validated snapshot.

The runtime validates every `agents/<name>/AGENTS.md` before compiling, rejects
malformed or duplicate definitions within a directory, and preserves upstream's
project-over-user precedence. It refreshes task schemas, MCP tool bindings and
the read-only tools available to goal criteria and rubric grading together.
All three caller middleware targets survive rebuild. Model/workspace credential
snapshots, sandbox and extensions are retained. Extension installation, model
configuration changes and environment changes require restarting the runtime.

Candidate failures keep the previous graph and leave the request pending for a
later retry. Existing invocations and background workers retain their compiled
graph. An interrupted thread retains its version until the approved/resumed
turn completes; another thread can use the newer version meanwhile. These graph
references live in the current process. After a server restart, the host must
keep configuration compatible with any checkpoint it intends to resume.

For `lc-code` with reload enabled, MCP tools use fresh sessions per call, with
connection settings captured in each version. This avoids rebinding an old
graph to a new endpoint or mutating the upstream global connection cache.
It trades connection reuse and session-local MCP state for reload isolation.
A custom embedding provider must preserve the same old-tool isolation and own
any retained client resources until no old invocation can use them.

## Background work and history

With background enabled, local `task` calls return an ID immediately.
`list_background_tasks` and `cancel_background_task` only address the current
conversation's work. Existing remote async tools retain the SDK's behavior.
Forks cannot recursively detach work or access the main agent's runtime controls.
Factory approvals and hooks run before detachment, and child tools keep their
compiled approval controls. A child interrupt becomes `needs_approval` and never
a successful empty result; detached child approval/resume is not implemented.
Restart that task through an interactive foreground flow when approval is needed.

Defaults are four running local jobs, 128 retained jobs, a one-hour job timeout,
and 64,000 characters per result. Jobs are in memory and are cancelled at runtime
shutdown. They do not survive a process restart. Hook observers around the main
`task` see the immediate dispatch result; job status tracks actual completion.

Results arrive on the next main-agent turn. A failed/cancelled delivery keeps
results pending; committed completion acknowledges them. For an embedding:

```python
pending = await runtime.background.wait("conversation-1")
# Schedule a normal runtime.ainvoke/astream turn when your host is ready.
# The runtime injects pending results as data automatically.
```

There is no automatic idle-turn scheduler or extra UI channel. The server's
existing run serialization remains responsible for interactive execution.

History tools are `search_conversations`, `list_conversations`, and
`read_conversation`. Search uses SQLite FTS5 with literal query text, bounded
pages, and trusted workspace/owner filtering. It archives committed message
revisions before they disappear through compaction. Retrieval tool responses
are excluded from the index. This is searchable history, not an automatic
long-term memory selection policy or a replacement for upstream memory tools.

`runtime.delete_conversation(thread_id)` stops that embedded conversation's
work and deletes its checkpoint/archive. Server hosts use their authenticated
thread-deletion endpoint; the saver cancels associated background jobs first.
Checkpoint deletion precedes archive removal so a failed deletion can be retried.
There is no automatic retention period: the host owns retention and encryption.
Checkpoint and archive writes use separate transactions; an archive failure is
reported after checkpoint persistence, and retry is idempotent.

Enabled server runtimes retain their workspace resources, up to the existing
32-workspace capacity, instead of evicting live jobs. Use separate processes
when that limit is reached. This is a reusable factory runtime, not a complete
multi-tenant hosting or durable job scheduling service.
