# Delegation results and background submission

The bundled `lc-code` and `ddt-agent` clients enable the native `task_settled`
tool by default. Save `[lc_factory].settled_dispatch = false` in the trusted
profile to disable it, or use the optional host-owned
`LC_FACTORY_SETTLED_DISPATCH=0` override. See
[client preferences](TALON_ADAPTATIONS.md#use-with-lc-code).

Direct Python callers opt in with `enable_settled_dispatch=True` on
`create_factory_agent`; direct server hosts use `LC_FACTORY_SETTLED_DISPATCH=1`.
Repository dotenv files cannot select it. Native `task` keeps its existing
foreground semantics.

```python
graph, backend = create_factory_agent(
    model=model, assistant_id="worker", cwd=workspace,
    enable_interpreter=True, enable_settled_dispatch=True,
)
```

The model can call the native tool with `description`, `subagentType`, optional
`label`, and optional `responseSchema`. Inside JavaScript:

```javascript
const results = await Promise.all(items.map(item => tools.taskSettled({
  description: item.prompt,
  subagentType: "analyst",
  responseSchema: {
    title: "Finding", type: "object",
    properties: {summary: {type: "string"}}, required: ["summary"]
  }
})));
const successes = results.filter(r => r.ok).map(r => r.value);
const failures = results.filter(r => !r.ok).map(r => r.error);
```

Success is `{ok: true, value}`. Ordinary failure is
`{ok: false, error: {type, message}}`. Unknown local agents, recursive child
dispatch, missing permitted dispatch tools, invalid JSON, schema violations,
and oversized results are failures. A requested schema uses `ToolStrategy` and
independent offline Draft 2020-12 validation. A child's declared output schema
still applies. Results are limited to 64,000 serialized characters; errors to
2,048 message characters. Save large outputs as artifacts and return references.
The helper confirms successful execution and schema conformance, not factual
correctness. Bare JavaScript `task()` retains the upstream result semantics.

Approval interruptions and cancellation propagate instead of becoming ordinary
failure data. The interpreter boundary records delegated approval interruptions
and re-raises them even if JavaScript catches the rejection. Normal eval return
emits `phase_complete`; an interrupted eval does not. A phase ending means its
foreground batch ended, not that all outcomes succeeded. Structured validation
failure emits an error event instead of a premature completion event.

Lifecycle row IDs use bounded replay bookkeeping. Matching logical dispatches
retain IDs across node replay while repeated sibling payloads stay separate.
An exact eval ID is carried in copied call configuration, so parallel evals do
not share a guessed phase. Without sufficient runtime execution metadata, IDs
fall back to upstream-generated IDs. Stream-writer failures do not fail work.
The pinned interpreter allows one active eval per REPL slot; concurrent evals
in the same slot can return `ConcurrentEval`. Independent graph contexts have
separate slots.

Concurrency is capped at 32 active settled dispatches per built dispatcher and
event loop. Separate synchronous invocation loops have separate limits; this is
not a process-wide or distributed quota. The interpreter's existing PTC call,
time, memory, and output budgets also apply.

## Tool policy and approval

The settled helper is registered as a regular tool and selected for PTC by name.
It therefore passes through the same capability filtering as other tools.
Direct native calls use the same approval predicate as native `task`. Enabling
settled dispatch with interpreter subagents enabled exposes its JavaScript bridge;
as with upstream JavaScript `task()`, that route bypasses the parent's per-tool
approval wrapper. Child tool approvals remain active. This is separate from
the default safe PTC preset and does not add background submission to that preset.
The bundled clients retain JavaScript subagent support by default; set trusted
`[lc_factory].interpreter_subagents = false` (or the optional host override
`LC_FACTORY_INTERPRETER_SUBAGENTS=0`) to withhold JavaScript delegation.
With `auto_mode_enabled=True`, local children follow the owning session's live
mode: Manual uses the existing approval rules, Auto reviews child actions with
the session's classifier, and YOLO bypasses ordinary approval prompts. This
applies to native, settled, JavaScript and background dispatch, for isolated and
forked children. An explicit child model does not replace the session's approval
classifier. Each child keeps separate decisions, failure counters and temporary
artifact ownership. Explicit `auto_approve=True` and authoritative hook decisions
retain their normal semantics; declared tool and filesystem restrictions remain.

Running children read mode changes at their next approval boundary. A change
after review can skip an unexecuted action and require a fresh proposal; it does
not cancel an action already executing. Exact human approvals remain valid for
the reviewed action. Existing paused approval dialogs still require a response.

Auto receives the original user authorization captured when delegation starts,
including active user-set goal/rubric directives. Assignments, summaries and
main-agent steering cannot grant additional consent. Missing or oversized
authorization, missing session model context, and unavailable approval state
fall back to Manual. Classifier failures retain the normal retry/human-fallback
policy. A parent's `ask_user` receipt is not transferred into child consent.
Direct synchronous task invocation is unsupported for this async Auto layer.
Remote async agents retain their own upstream approval behavior.

Forks retain the SDK's recursive-delegation refusal. Allowing `js_eval` alone
does not grant `task` or `task_settled`: both model-time bindings and runtime
tool lookup are filtered by [subagent policies](SUBAGENTS.md). Python tool and
middleware implementations remain trusted host code.

## Background work

The bundled `lc-code` and `ddt-agent` clients enable background tools by default.
Start either client normally and ask for a background task. Save or override
capability preferences as described in [runtime configuration](TALON_ADAPTATIONS.md#use-with-lc-code).
Python embeddings enable them with `RuntimeOptions(background=True)`; direct
server hosts can use `LC_FACTORY_CAPABILITIES=background`.

The runtime exposes `start_background_task(description, subagent_type)`. It
returns `{ok: true, task_id, status: "running"}` after successful submission, or
an error envelope if submission is unavailable. Unknown local names fail before
a worker starts. Parent delegation approval precedes submission, and protected
child actions still require their own approval.

| Surface | Behavior with background capability enabled |
| --- | --- |
| Native `task` | Wait for foreground completion and return the child result. |
| `start_background_task` | Explicit submission with a structured running handle. |
| Native/JavaScript `task_settled` | Wait for foreground completion, return an outcome. |
| Bare JavaScript `task()` | Existing foreground behavior. |
| Remote async tools | Upstream start/check/cancel behavior. |

Migration: earlier background-enabled runtimes intercepted native `task` and
detached it. Call `start_background_task` explicitly wherever detachment is
intended; native `task` now always preserves foreground delegation semantics.

All background submission uses one quota, snapshot, scheduling, and shutdown
path. Jobs retain their original compiled dispatcher across configuration
reloads. Defaults remain four running jobs, 128 retained jobs, and one hour of
execution per job. Additional jobs queue FIFO; queue and approval wait time do
not consume the execution budget. Approved resumptions can queue too. A full
retention table evicts acknowledged finished jobs before rejecting submission
with a specific capacity explanation. Only the owning conversation can list, cancel, or receive results; children
cannot submit further background jobs.

`list_background_tasks` keeps the existing `result` string and adds `outcome`.
Completed schema-backed results have a parsed structured value. Failed,
cancelled and timed-out jobs have `ok: false`; approval/input pauses have no outcome yet. Oversized results
fail instead of returning truncated content as a claimed success. Result text
arrives at the next main-model boundary, including resumed turns. Bundled TUIs
also wake an idle main agent, respecting user input, pauses and cancellation.
Results are child data and supply no new user authorization. Consumption is
acknowledged after committed completion; host status/detail reads never consume it.
The dynamic subagents panel surfaces paused approvals and resumes the same job
through the existing approval dialog. These are local in-memory jobs and are cancelled on shutdown;
durable recovery and distributed workers are outside this capability.
