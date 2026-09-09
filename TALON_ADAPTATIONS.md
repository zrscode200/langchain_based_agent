# Optional runtime capabilities

The factory adapts three capabilities from Talon: configuration reload,
background delegation, and searchable conversation history. They run around
`create_factory_agent`, so the fully wired Deep Agents Code harness and the
main/subagent/grader middleware seams remain the foundation. Direct factory
calls keep their default behavior. The bundled `lc-code` and `ddt-agent` clients
enable background and reload tools by default, along with foreground structured
delegation (`task_settled`). Searchable history remains opt-in.

The source reference is Deep Agents commit
`6c89fe2197a2dfe4f3851cda38565bcadba6066b`, `libs/talon` version 0.0.6.
`archive.py` and `archive_saver.py` adapt Talon's corresponding files;
`background.py` adapts its worker registry and task-detachment pattern.
`mcp_reload.py` adapts the Code server's tool discovery. Copyright LangChain,
Inc.; the MIT notice is retained in `LICENSE`. No Talon package, channel,
OAuth server, cron scheduler, or hosted LangChain service is required.

## Use with lc-code

Start either client normally; no exports are needed for background work,
configuration reload or structured delegation:

```sh
lc-code
```

Ask the main agent to use `start_background_task` to delegate while continuing
the conversation. Native `task`, `task_settled` and JavaScript `task()` remain
foreground operations. Background availability does not make every delegation
asynchronous. The TUI shows task status and completion notifications independently
of the main run. Completed results reach the main agent on its next conversation
turn; no automatic model turn is started while idle.

Save preferences in the trusted user/managed `config.toml` selected by
`DEEPAGENTS_HOME`, alongside any other `[lc_factory]` settings:

```toml
[lc_factory]
# These are the bundled client defaults; saving them is optional.
capabilities = ["background", "reload"]
settled_dispatch = true
interpreter_subagents = true
```

| Preference | Effect |
| --- | --- |
| `capabilities` | Complete list of runtime features. Add `"history"` to enable the archive; use `[]` to disable background, reload and history tools. |
| `settled_dispatch` | Enables foreground `task_settled`, including its JavaScript bridge when the interpreter and its subagents are enabled. Set `false` to disable it. |
| `interpreter_subagents` | Preserves existing JavaScript subagent support. Set `false` to withhold JS `task()` and task/task_settled PTC bindings while keeping native delegation available. |

The settings are independent: `capabilities = []` does not disable native
`task_settled` or JavaScript delegation. To disable all three optional selections,
also set both booleans to `false`. Only `background`, `reload` and `history` are
accepted in the list; the two flags must be TOML booleans. Existing saved lists
are honored as complete selections and do not gain reload automatically.
Missing settings use defaults; malformed or unreadable configuration fails agent
startup, while help/version and diagnostic commands remain accessible. Project
configuration files cannot select these settings.

Reload remains an explicit tool action, applied on the next turn; there is no
file watcher. MCP generations stay alive until shutdown, with at most eight
retained generations by default. Restart after reaching that limit. See
[reload lifecycle](#reload-behavior) for resource ownership details.

JavaScript delegation retains its existing approval behavior: it bypasses the
parent's per-tool approval wrapper, while child tool approvals remain active.
Native `task_settled` uses the native `task` approval predicate. See
[delegation policy](DELEGATION.md#tool-policy-and-approval).

Shell overrides are optional and replace only the corresponding saved setting:

```sh
export LC_FACTORY_CAPABILITIES=reload,background,history
export LC_FACTORY_SETTLED_DISPATCH=1
export LC_FACTORY_INTERPRETER_SUBAGENTS=0
lc-code
```

Select any comma-separated subset, or `LC_FACTORY_CAPABILITIES=none` to disable
the runtime capability list. The two boolean variables accept `1/0`,
`true/false`, `yes/no`, or `on/off` (case-insensitive). Invalid values fail
startup. An absent/empty variable uses its saved preference and then its client
default. The clients resolve once when first starting an agent server and forward
that snapshot to the scaffold and server; restart to change the selection. This
also applies to their headless entry path. Direct server hosts still select capabilities
explicitly through the environment; Python embeddings pass `RuntimeOptions`.

These settings are reserved before project `.env` loading, just like
`LC_FACTORY_MIDDLEWARE`; a repository cannot enable them through its `.env`.
The existing model configuration, including your company's endpoint, remains
applicable. Client exit restores the previous environment override.

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

The asynchronous `FactoryRuntime` owns graph selection, workers, and MCP
resources transferred through `MCPToolBundle`. The host owns the supplied model,
saver, archive, sandbox, and any MCP resources returned through ordinary tuple
loaders. Keep host-owned resources open until the runtime exits. See
[MCP resources](MCP_RESOURCES.md) for the ownership and shutdown contract.

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

A direct embedding's async `reload_tools()` returns an `MCPToolBundle` or an
ordinary `(all_tools, mcp_server_info, mcp_tools)` tuple. Both support three-value
unpacking; bundles transfer resource ownership to the runtime, while tuple
loaders leave resource ownership with the host. Pass an initial bundle through
`initial_resources` when creating the runtime. Each reload must return a new,
internally consistent snapshot and raise on failure. Without this callback, the
MCP reload tool reports that no provider is configured; subagent reload still works.
`reload_async_subagents` optionally supplies a synchronous remote-definition
loader. Explicit `subagent_definitions` remains the low-level constructor seam
for hosts providing their own already-validated snapshot.

Initial startup retains upstream's tolerant file discovery and warnings for
skipped definitions. Explicit reloads validate every `agents/<name>/AGENTS.md`
before compiling, reject malformed or duplicate definitions and unexpected
Markdown files in the agents directory, and preserve upstream's project-over-user
precedence. It refreshes task schemas, MCP tool bindings and
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

For `lc-code` with reload enabled, each MCP configuration generation owns
persistent sessions with captured connection settings. Repeated calls reuse
the connector process and its session state; old graphs keep their original
bindings. Failed candidates close their resources. Successful MCP bundles stay
open until runtime shutdown, with a default limit of eight retained MCP
generations; reaching the limit rejects further reloads before discovery.
Runtime shutdown stops active work and workers before closing the sessions.
An intentional reload or reconnection can reset connector-local caches and
budget counters. See [MCP resources](MCP_RESOURCES.md) for limits and lifecycle
details. Custom tuple loaders must preserve old-tool isolation and keep their
host-owned resources open until no old invocation can use them.

## Background work and history

With background enabled, `start_background_task` returns an ID immediately.
Native `task`, `task_settled` and JavaScript `task()` wait for the child result.
`list_background_tasks`, `inspect_background_task`, `steer_background_task` and
`cancel_background_task` only address the current conversation's work. Existing
remote async tools retain the SDK's behavior.
Forks cannot recursively detach work or access the main agent's runtime controls.
Factory approvals and hooks run before detachment, and child tools keep their
compiled approval controls. A child interrupt becomes `needs_approval` with its
exact pending action requests. Select Background in the dynamic subagents panel
and click a task row (or select it with left/right and press Enter) for its live
activity window. **Review** opens a frozen approval snapshot; **Cancel task**
stops the selected child. Ctrl+T expands or collapses the shared panel.
Background rows survive main-turn completion and cancellation. The retained child checkpoint resumes without
replaying completed steps. Waiting for approval is not a final result and is not
acknowledged by the main agent's result-delivery path. Server hooks use their
existing client-owned fulfillment path; unsupported input requests and requests
exceeding the review panel's 24,000-character display limit remain blocked and
can be cancelled. The TUI never resumes the parent graph to settle a child.

Factory-built children support bounded main-agent inspection and steering.
Inspection includes assignment/status/result, recent observable tool names and
execution states, explicit findings from `report_background_task`, and steering
delivery state. It does not expose private reasoning, model transcripts or raw
tool outputs. The read-only TUI activity window uses the same inspection data;
there is no direct user-to-child message box or host steering endpoint.

`steer_background_task(task_id, message)` queues a correction for the child's
next model step. An already admitted tool normally finishes. A message is
**queued** when accepted, **delivered** when included in a child model request,
and **acknowledged** only when the child explicitly reports its message ID through
`report_background_task`. Delivery does not imply compliance. Activity retains
128 recent records with at most 2,048 characters per report; steering accepts
32 messages of at most 2,000 characters per job. A completed or cancelled child
closes its inbox. Any remaining undelivered message records that terminal outcome;
the child is never restarted to deliver it. A custom task tool submitted directly
to the background registry without factory instrumentation advertises unsupported
steering and retains its ordinary background lifecycle. This does not expand the
factory constructor's supported subagent definitions.

Steering conservatively supersedes earlier action proposals. Supported stale
approval batches are rejected through the same checkpoint continuation, and
old user responses are refused. Hooks keep their genuine invocation/snapshot
response path; steering cannot grant approval or manufacture hook responses.
A final tool admission check blocks obsolete proposals after a hook pause.
Unknown input requests remain paused. These controls do not turn arbitrary
shell processes, downloads or remote asynchronous jobs into steerable agents.

Defaults are four running local jobs, 128 retained jobs, a one-hour execution budget,
and 64,000 characters per result. Jobs are in memory and are cancelled at runtime
shutdown. They do not survive a process restart. Hook observers around
`start_background_task` see the immediate dispatch result; job status tracks
actual completion. Hooks around native `task` see its foreground result.

Results arrive on the next main-agent turn. A failed/cancelled delivery keeps
results pending; committed completion acknowledges them. For an embedding:

```python
pending = await runtime.background.wait("conversation-1")
# Schedule a normal runtime.ainvoke/astream turn when your host is ready.
# The runtime injects pending results as data automatically.
# wait also wakes for an approval/input pause, with no completed result.
jobs = runtime.background.list("conversation-1")
# A trusted host UI can call runtime.background.resume(owner, task_id,
#     {interrupt_id: {"decisions": [{"type": "approve"}]}}).
# Pass exactly the current interrupt IDs and one allowed decision per action.
# resume is deliberately not an agent tool: a model cannot approve its own work.
```

Waiting for approval does not consume the execution budget. Resume respects the
running-job limit; stale, duplicate, wrong-owner and disallowed decisions are
rejected before execution. Dedicated workspace-bound HTTP operations serve TUI
status/approval/cancel requests while the main conversation remains usable.
There is no automatic idle-turn scheduler. Jobs and paused checkpoints disappear
when the server closes; main-agent live inspection/steering is not implemented.

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
