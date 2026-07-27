# lc_factory

Agent factory layer over the LangChain ecosystem (LangGraph + deepagents).

`lc_factory` recomposes `deepagents_code.create_cli_agent` (the "v0"
baseline) into an owned assembly over a **pinned** `deepagents-code`
dependency: our factory function, our `make_graph`, and a micro-launcher,
composing upstream's importable pieces. Products consume this layer; it is
not itself a product.

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

Bumping the upstream pin follows [`UPGRADING.md`](UPGRADING.md) — the parity
suite gates it.

## Planned deltas over v0

Ratified backlog (content chosen per iteration): middleware injection seam,
configurable verification, archetype presets, headless eval harness.

## Status

**Group 1 (Factory Skeleton) complete.** The factory reaches parity with v0:
the ported assembly, its own `make_graph`, and a micro-launcher run under the
upstream deepagents-code TUI and headless CLI via the `lc-code` entry point.
Group 1 deliberately adds no behavioral deltas — the divergence inventory in
[`UPGRADING.md`](UPGRADING.md) is exhaustive and structural only.

Verified: composition parity against v0 across a config matrix (with negative
controls), import-boundary integrity, a live headless session through the
real upstream client, and an in-process proof that upstream's launcher
serves the factory graph.

Not yet verified by automation: the interactive Textual TUI in a terminal,
the approval-interrupt path end-to-end (proven once manually), and rubric
verdicts with a real model.
