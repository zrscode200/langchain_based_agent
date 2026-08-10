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

Injected middleware reaches the main agent only; subagents, the goal-criteria
agent, and the rubric grader keep their own stacks.

## Planned deltas over v0

Ratified backlog (content chosen per iteration): ~~middleware injection
seam~~ (done), configurable verification, archetype presets, headless eval
harness.

## Status

**Groups 1 and 2 complete.** Group 1 built the skeleton — the ported assembly,
its own `make_graph`, and scaffolding that runs under the upstream TUI and
headless CLI via `lc-code` — at exact parity with v0. Group 2 added the
middleware injection seam and the transport that carries it into a live
session.

Parity is now a *documented-divergence* contract rather than plain equality:
the default composition stays byte-identical to v0 (the parity suite proves
this unmodified), and every deliberate divergence is enumerated in
[`UPGRADING.md`](UPGRADING.md).

Verified: composition parity against v0 across a config matrix with negative
controls; seam placement in the final composed stack plus hook ordering proven
by execution; import-boundary integrity; and live headless sessions covering
both a successful injection and a startup failure on a bad reference.

Not yet verified by automation: the interactive Textual TUI in a terminal, the
approval-interrupt path end-to-end (proven once manually), and rubric verdicts
with a real model.

## License

`lc_factory` is licensed under the [MIT License](LICENSE). Portions are derived
from LangChain's MIT-licensed `deepagents` project; its copyright notice is
retained in the license.
