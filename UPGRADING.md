# Upgrading the upstream pin

`lc_factory` recomposes upstream internals, so a dependency bump is a
**deliberate, reviewed event** — never an incidental `uv lock --upgrade`.
This file is the procedure.

## The dual pin

`pyproject.toml` pins two packages exactly:

- `deepagents-code==<version>` — the v0 baseline whose assembly we port.
- `deepagents==<version>` — pinned to exactly what `deepagents-code`
  requires. Declared directly because `upstream.py` imports it, and because
  uv only resolves a pre-release when it is a direct requirement.

**Bump both together.** They are one unit; a mismatch fails resolution, which
is the intended loud failure.

## What the port is coupled to

The port tracks specific upstream files. Diff these between the old and new
tags before touching anything:

| Upstream file | Ported into |
|---|---|
| `deepagents_code/agent.py` (`create_cli_agent` body) | `src/lc_factory/assembly.py` |
| `deepagents_code/server_graph.py` (`_make_graph`) | `src/lc_factory/server_graph.py` |
| `deepagents_code/client/launch/server_manager.py` | `src/lc_factory/launch.py` |

Everything else is consumed as a library through `src/lc_factory/upstream.py`,
which is the single inventory of upstream symbols we depend on — including
private (underscore) names that carry no semver protection.

## Procedure

1. **Read the release delta.** In a checkout of the upstream monorepo:

   ```sh
   git diff <old-tag>..<new-tag> -- libs/code/deepagents_code/agent.py \
       libs/code/deepagents_code/server_graph.py \
       libs/code/deepagents_code/client/launch/server_manager.py
   ```

   Also skim `libs/code/CHANGELOG.md`. Decide, per hunk, whether it is a
   composition change we must re-apply or an internal change we inherit free.

2. **Update the pins** in `pyproject.toml`, then `uv sync`.

3. **Check the boundary first.** `uv run pytest tests/test_boundary.py` —
   this fails by name on any upstream rename or removal, and is the cheapest
   signal that the surface moved.

4. **Re-apply composition changes** to the ported files, keeping the port
   line-faithful to the new upstream body (plus our deltas, once we have
   any — record each intentional divergence in this file's Divergences
   section as it lands).

5. **Run the parity suite.** `uv run pytest tests/test_parity.py` — it
   compares our composed middleware stack, interrupt gating, subagent wiring,
   backend routes, prompt, and schema against upstream's across a config
   matrix. A mismatch means the port drifted; fix the port, not the test.

6. **Full verification.**

   ```sh
   uv run pytest              # default suite
   uv run pytest -m integration   # live server round trip
   ```

7. **Commit the bump on its own**, with the upstream delta summarized in the
   message. Never bundle a pin bump with feature work.

## Rollback

The pin bump is one commit touching `pyproject.toml`, `uv.lock`, and the
ported files. Revert that commit and `uv sync` to return to the previous
baseline; no state migration is involved.

## Divergences from v0

Intentional differences between our assembly and upstream's, re-verified on
every bump. Keep this list exhaustive — it is what makes step 4 tractable.

- **None yet.** The port is currently line-faithful to
  `deepagents-code==0.1.47` apart from the function rename, imports routed
  through `upstream.py`, and the lazy interpreter import going through
  `upstream.import_code_interpreter()`.
