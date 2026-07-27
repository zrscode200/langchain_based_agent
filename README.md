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

## Planned deltas over v0

Ratified backlog (content chosen per iteration): middleware injection seam,
configurable verification, archetype presets, headless eval harness.

## Status

Wave 1.1: packaging skeleton over the pinned baseline. The assembly port
(wave 1.2) follows.
