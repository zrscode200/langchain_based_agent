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
| `deepagents_code/client/launch/server_manager.py` (`_scaffold_workspace` global) | `src/lc_factory/tui.py` — **rebind seam, re-verify every bump** |

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

3. **Check the boundary first.** `uv run pytest tests/test_boundary.py` — it
   resolves every name in `upstream.__all__` plus every entry in
   `upstream.TYPE_ONLY_IMPORTS` (annotation-only names that would otherwise
   break silently, since nothing evaluates them at runtime). This is the
   cheapest signal that the upstream surface moved. Note that runtime names
   also fail earlier, at `import lc_factory.upstream`.

4. **Re-apply composition changes** to the ported files, keeping the port
   line-faithful to the new upstream body (plus our deltas, once we have
   any — record each intentional divergence in this file's Divergences
   section as it lands).

5. **Run the parity suite.** `uv run pytest tests/test_parity.py` — it
   compares our composed agent against upstream's across a config matrix,
   down to middleware *state* (constructor arguments, not just classes),
   interrupt gating including the `when` predicates, subagent wiring, backend
   composition, prompt, schema, and `.with_config`. A mismatch means the port
   drifted; fix the port, not the test.

   The matrix must keep covering the shape the server actually boots with
   (`server_graph.py` always passes goal-criteria and rubric-grader tools) —
   see `test_composition_parity_server_realistic`.

6. **Full verification.**

   ```sh
   uv run pytest              # default suite
   uv run pytest -m integration   # live server round trip
   ```

   Note: the integration module imports upstream's private
   `deepagents_code._testing_models` at module scope, so a rename there
   breaks collection of the *default* suite too — a deliberate early signal,
   but read the traceback before assuming the port drifted.

7. **Commit the bump on its own**, with the upstream delta summarized in the
   message. Never bundle a pin bump with feature work.

## Rollback

The pin bump is one commit touching `pyproject.toml`, `uv.lock`, and the
ported files. Revert that commit and `uv sync` to return to the previous
baseline; no state migration is involved.

## Divergences from v0

Intentional differences between our assembly and upstream's, re-verified on
every bump. Keep this list exhaustive — it is what makes step 4 tractable.

No *behavioral* deltas exist yet — the composed agent is verified identical
to v0 by the parity suite. The structural divergences below are what make the
recomposition possible, and each must be re-verified on a bump:

**`assembly.py`** (otherwise line-faithful to `agent.py:2155-2989`):
- `create_cli_agent` renamed to `create_factory_agent`.
- All upstream imports routed through `upstream.py`.
- The lazy `langchain_quickjs` import goes through
  `upstream.import_code_interpreter()` (laziness preserved).

**`launch.py`** (ported from `server_manager.py`):
- `GRAPH_REF` targets `lc_factory.server_graph:make_graph` instead of
  upstream's `deepagents_code.server_graph:make_graph`.
- `_DISTRIBUTION_NAME` is `lc_factory`, and the generated runtime
  `pyproject.toml` depends on this package rather than `deepagents-code`.
- Upstream's private `_scaffold_workspace` is public `scaffold_workspace`.
- `_default_package_project_root` probes two ancestor levels for
  `pyproject.toml` (src layout) rather than upstream's fixed `parent.parent`.
- `start_factory_server_and_get_agent` drops upstream's third return slot
  (an always-`None` MCP session manager placeholder).

**`tui.py`** — the launch seam, and the port's single most fragile coupling:
- Rebinds the **private upstream module global**
  `server_manager._scaffold_workspace` before calling `cli_main`. Upstream
  resolves that name at call time on every launch path (TUI start, restart,
  cwd switch, headless), which is what makes the rebind sufficient.
- On every bump, re-check that upstream still resolves it as a module global
  and has not added a caller that captures it earlier or imports it by name.
  `tests/test_launch.py::test_tui_main_rebinds_scaffold_seam` covers the
  rebind itself; the grep to run is `_scaffold_workspace` across
  `deepagents_code/`. Upstreaming a pluggable `graph_ref` would retire this.
