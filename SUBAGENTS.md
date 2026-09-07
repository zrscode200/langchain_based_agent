# Factory subagents

`create_factory_agent` owns the fully wired child stack: approvals, model
selection/retries, cost tracking, hooks, and managed-memory protection. Optional
declarative specs extend that stack without copying the constructor:

```python
graph, backend = create_factory_agent(
    model=main_model,
    assistant_id="work-agent",
    cwd=workspace,
    tools=[search_source],
    subagents=[{
        "name": "analyst",
        "description": "Investigate one assigned question",
        "system_prompt": "Check evidence and report uncertainties.",
        "model": specialist_model,
        "mode": "isolated",
        "response_format": {
            "title": "Finding",
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    }],
    subagent_policy={"analyst": {"tools": ["search_source"]}},
)
```

Names and descriptions are required. A programmatic spec replaces a same-named
file definition as a whole; duplicate programmatic names fail. The default
`general-purpose` child remains unless explicitly replaced. `subagent_definitions`
continues to accept a host-validated metadata snapshot. Remote definitions still
use `async_subagents`; local/remote duplicate names fail. Compiled runnable/graph
specs and per-child `interrupt_on` overrides are not accepted by this constructor.

Supported programmatic fields are `name`, `description`, `system_prompt`, `model`,
`tools`, `middleware`, `mode`, `skills`, `permissions`, `response_format`, and
`fs_tools`. Inputs are copied; model, tool, and middleware objects remain trusted
host objects shared by reference. Per-spec middleware is inside factory approval
routing, before the shared `subagent_middleware` last phase. Shared first/last
injection phases retain their existing behavior. Reserved names and collisions
with a fork's inherited middleware are rejected.

## Workspace policy

When `subagent_policy=None`, the factory loads `.deepagents/subagents.toml` from
the workspace root. A missing file means inheritance. An existing invalid file
fails startup or rejects the reload candidate. An explicit mapping, including
`{}`, overrides discovery. Only these keys are accepted:

```toml
[subagents.analyst]
tools = ["search_source", "read_file"]
skills = [".deepagents/skills/evidence"]
model = "company:analyst"
mode = "isolated"

[subagents.analyst.response_format]
title = "Finding"
type = "object"
required = ["summary"]
additionalProperties = false

[subagents.analyst.response_format.properties.summary]
type = "string"
```

Policy applies after both file and programmatic definitions are merged. Unknown
agent names, keys, or unavailable tool names fail instead of silently inheriting.
Workspace skill paths must stay under the workspace after symlink resolution.
Host-provided skill sources remain trusted configuration.

| Setting | Meaning |
| --- | --- |
| `tools` omitted | SDK tool inheritance; other declared restrictions still apply. |
| `tools = []` | No callable tools, including filesystem scaffolding. |
| `tools = [...]` | Exhaustive allowed names, including MCP/plugin/FS/interpreter tools. |
| `mode = "isolated"` | Fresh child context and factory child middleware. |
| `mode = "fork"` | Parent context and middleware inherited under the pinned SDK's fork behavior. |
| explicit child model | Policy-checked string or trusted prebuilt model; parent runtime switches cannot replace it. |

Programmatic `tools` alone follows the SDK's explicit-tool semantics; it does not
remove middleware-provided tools. Use the policy list for an exhaustive boundary.
An isolated child can select supplied tools and its own middleware's tools; it
cannot borrow a parent-only middleware tool such as `js_eval` without its lifecycle.
A fork inherits that lifecycle. Declaring `skills`, even an empty list, on a fork
fails because it cannot disable the parent's inherited skill machinery.
Fork policies may include the SDK-generated `task` tool, but the pinned SDK
still refuses recursive child delegation at execution time. Allowing a tool does
not override that upstream constraint; multi-level recursive workers require a
separate orchestration design.

`fs_tools` restricts one child and intersects with the constructor's global
`fs_tools` ceiling. Allow/deny `FilesystemPermission` objects are preserved;
interrupt rules are rejected because the factory owns async approvals. Upstream
restrictions on permissions with execution-capable backends still apply; a file
permission rule does not make arbitrary shell execution safe.

Tools are filtered before interpreter binding and immediately before model use;
sync/async execution guards also reject forged forbidden calls. The injected
runtime tool list is filtered too, covering JavaScript's built-in `task()` and
helpers that discover tools at execution time. The SDK-required
internal `read_file` scaffolding does not grant a call when policy disallows it.
Python tools and middleware remain trusted application code. These restrictions
are agent capability controls, not process or tenant isolation. Authorizing an
interpreter, shell, or a tool that internally contacts other services authorizes
that capability; a tool-name list does not constrain its internal Python behavior.

## Structured results and reload

JSON-schema dictionaries are wrapped in `ToolStrategy` for provider compatibility.
At this pin LangChain parses raw JSON-schema output without checking its values;
the factory validates the actual `structured_response` with Draft 2020-12 before
accepting completion. Missing/invalid results raise an error. Pydantic and other
upstream supported response strategies retain their native validation. No remote
schema retrieval is enabled; unresolved external references fail validation.
This supplies a validated result, not a truth/evidence guarantee.

When runtime reload is enabled, definitions and workspace policies are read into
the candidate generation together. Any parse, model, tool, or graph error retains
the previous graph and policy. Explicit policy snapshots remain explicit across
reloads. Already-running and interrupted work keeps its original generation.
