# lc_factory

Agent factory layer over the LangChain ecosystem (LangGraph + deepagents).

`lc_factory` recomposes `deepagents_code.create_cli_agent` (the "v0"
baseline) into an owned assembly over a **pinned** `deepagents-code`
dependency: our factory function, our `make_graph`, and our workspace
scaffolding, composing upstream's importable pieces. Products consume this
layer; it is not itself a product.

## Architecture rules

- Exact pin on `deepagents-code` (see `pyproject.toml`). Bumps are
  deliberate, reviewed events gated by the parity test suite.
- Every upstream import routes through `src/lc_factory/upstream.py` — the
  import boundary and divergence inventory. No direct upstream imports
  anywhere else.
- TUI compatibility seams held by construction: stream contract, resume-state
  channels, interrupt payloads, sessions DB layout, ServerConfig env bridge.

## Development

```sh
uv sync
uv run pytest                 # default suite (parity, boundary, unit)
uv run pytest -m integration  # live server round trip (slow)
lc-code                       # the dcode TUI, wired to the factory graph
```

For daily interactive use, model and approval setup, the baseline TUI dogfood
pass, and optional middleware injection, see
[`USING_LC_CODE.md`](USING_LC_CODE.md).

Bumping the upstream pin follows [`UPGRADING.md`](UPGRADING.md) — the parity
suite gates it. Every bump so far, and what each cost, is recorded in
[`VERSION_BUMPS.md`](VERSION_BUMPS.md).

## Current upstream baseline

Python **3.12+** is required. Code **0.1.66** and SDK **0.7.13** are installed
from the exact upstream revision `6c89fe2197a2dfe4f3851cda38565bcadba6066b`, which includes
post-release fixes absent from the same-numbered PyPI releases. QuickJS is
pinned to the published **0.3.7** package. `uv sync --locked` reproduces this
baseline; smoke tests verify Git provenance as well as package versions.

Both `create_factory_agent` and the owned server assembly include workspace
configuration isolation. The server snapshots each workspace's environment and
credentials, then passes `environ`, `credentials_snapshot`, and `model_result`
to the constructor. Model switching, compaction, grader/classifier models, MCP
discovery, and shell execution use the supported upstream snapshot paths.
For direct embedding, supply an immutable environment and matching credential
snapshot; run model/tool setup inside upstream's `use_environment` scope.

The launch workspace and request paths share one runtime cache. With a configured
process-wide sandbox, a second workspace is refused with HTTP 409, including
after a failed first build. Without that sandbox, distinct workspaces can have
separate cached runtimes. This is configuration isolation; independent tenant
permissions and OS/container isolation remain hosting responsibilities. Plugins
that read global `os.environ` need explicit workspace-aware integration.

The Git requirements survive wheel packaging. Installation currently needs
access to the pinned source, or an internally mirrored, provenance-verified
build. Runtime hosting and model calls can stay on company infrastructure.
See [`UPSTREAM_REVIEW.md`](UPSTREAM_REVIEW.md) for the transfer assessment and
[`VERSION_BUMPS.md`](VERSION_BUMPS.md) for implementation validation.

## Middleware injection

The factory's first capability beyond v0. `create_cli_agent` has no middleware
parameter and a fixed stack order; `create_factory_agent` accepts caller
middleware at documented positions:

```python
from lc_factory.assembly import create_factory_agent

agent, backend = create_factory_agent(
    model="anthropic:claude-sonnet-4-6",
    assistant_id="my-agent",
    middleware=[MyMiddleware()],                 # default phase
    # ...or address phases explicitly:
    # middleware={"first": [Outer()], "last": [Inner()]},
)
```

Three phases — `first`, `before_verification` (default), `last` — each anchored
to middleware the factory always builds, so a phase boundary does not move when
configuration toggles other middleware on or off.

> **Position is an onion.** Earlier phases are *outermost*: their `before_*`
> hooks run first and their `after_*` hooks run **last**. To have the final say
> on the way out, use `first`.

To use it from a running `lc-code` session, point an environment variable at a
zero-argument factory the server can import:

```sh
export LC_FACTORY_MIDDLEWARE="my_package.agent_setup:build_middleware"
lc-code
```

`my_package` must be **installed in the same environment as `lc_factory`**.
The server runs in a subprocess with `PYTHONPATH` stripped and a private
working directory, so a module that is merely on your shell's path will not
be importable there.

It must also be a real shell export. Resolving the reference imports and
executes that module inside the server process, so `lc_factory` claims the
variable before any `.env` file can set it — otherwise a committed `.env` in a
repository you cloned could name code to run on `lc-code` startup. That
deliberately rules out `.env` as a source, including your own global one.

### Reaching delegated work

`middleware=` covers the main agent and is inherited by forked subagents.
Two further parameters reach the stacks
the factory composes for delegated work:

```python
agent, backend = create_factory_agent(
    model="anthropic:claude-sonnet-4-6",
    assistant_id="my-agent",
    middleware=[ParentAudit()],                    # main agent and its forks
    subagent_middleware=[EverySubagent()],         # every subagent stack
    rubric_grader_middleware=[GraderOnly()],       # the rubric grader
)
```

Both use `first` and `last`, defaulting to `last`. On fresh subagents,
`first` precedes the factory's subagent middleware and `last` follows it.
On the grader, `last` sits inside the budget middleware that bounds spending.

Starting with Code **0.1.66**, the synthesized general-purpose subagent uses
upstream's **fork** mode by default. It inherits the parent's conversation,
state, and main middleware. Its `first` phase places new child middleware
after the inherited parent block; it cannot move ahead of inherited approval
or verification middleware. `last` follows the child additions. Upstream child
overrides of inherited middleware retain the parent's positions. Injected child
names that collide with inherited main names are rejected to prevent silent
replacement.

For independent general-purpose subagents with the previous middleware scope
and ordering, explicitly set this before constructing or launching the agent:

```sh
export DEEPAGENTS_CODE_FORKED_SUBAGENTS=false
```

This flag controls the synthesized general-purpose agent. File-defined custom
subagents remain fresh at this pinned release; their parser does not accept a
fork-mode setting.

- **Subagent middleware is spliced by reference**, so one instance is shared
  across every subagent stack. Forks also share inherited main middleware
  instances. Keep middleware stateless or key its state by execution context.
- **The goal-criteria agent stays unreachable.** Upstream's
  `_create_goal_criteria_agent` takes no middleware argument.

All three targets are reachable from a running `lc-code` session too. The
referenced callable may return a **target-keyed** mapping:

```python
# my_package/agent_setup.py
def build_middleware():
    return {
        "main":      [AuditMain()],
        "subagents": [AuditDelegated()],       # or {"first": [...]}
        "grader":    [AuditGrader()],
    }
```

```sh
export LC_FACTORY_MIDDLEWARE="my_package.agent_setup:build_middleware"
lc-code
```

One variable, deliberately — each additional one would need its own `.env`
reservation guard, and that is real attack surface. The existing forms still
work unchanged: a bare sequence or a phase-keyed mapping both mean the main
agent. A mapping mixing target keys with phase keys is rejected rather than
guessed.

## Planned deltas over v0

Ratified backlog (content chosen per iteration): ~~middleware injection
seam~~ (done), configurable verification, archetype presets, headless eval
harness.

## Status

**Groups 1–3 complete.** Group 1 built the skeleton — the ported assembly, its
own `make_graph`, and scaffolding that runs under the upstream TUI and headless
CLI via `lc-code` — at exact parity with v0. Group 2 added the middleware
injection seam and the transport that carries it into a live session. Group 3
extended that seam to the delegation targets the factory composes: subagent
stacks and the rubric grader.

Parity is now a *documented-divergence* contract rather than plain equality:
the default composition stays byte-identical to v0 (the parity suite proves
this unmodified), and every deliberate divergence is enumerated in
[`UPGRADING.md`](UPGRADING.md).

Verified: composition parity against v0 across a config matrix with negative
controls; seam placement in the final composed stack for all three targets —
each observed at the point where its final order actually exists — plus hook
ordering proven by execution; per-target guards derived from the real SDK
stacks rather than assumed; import-boundary integrity; and live headless
sessions covering both a successful injection and a startup failure on a bad
reference.

Not yet verified by automation: the interactive Textual TUI in a terminal, the
approval-interrupt path end-to-end (proven once manually), and rubric verdicts
with a real model.

## License

`lc_factory` is licensed under the [MIT License](LICENSE). Portions are derived
from LangChain's MIT-licensed `deepagents` project; its copyright notice is
retained in the license.

Optional [runtime capabilities](TALON_ADAPTATIONS.md) add MCP/subagent reload,
background delegation, and searchable checkpoint history around the existing
factory. Enable them explicitly for `lc-code` or use `FactoryRuntime` when
embedding; the default constructor remains compatible with the pinned harness.
