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
| `deepagents_code/client/launch/server_manager.py` (`_scaffold_workspace`, `_write_pyproject`) | `src/lc_factory/launch.py` — **the live surface; re-apply changes here** |
| `deepagents_code/client/launch/server_manager.py` (`start_server_and_get_agent`) | *Nothing* — a standalone port of this was deleted at the Group 2 closeout. It had no production caller (the TUI seam routes through upstream's own) and owed re-application on every bump. **Nothing to re-apply.** |
| `deepagents_code/client/launch/server_manager.py` (`_scaffold_workspace` global) | `src/lc_factory/tui.py` — **rebind seam, re-verify every bump** |
| `deepagents/graph.py` (`_apply_custom_middleware`, the core/tail split) | `src/lc_factory/assembly.py` — the injection seam's **positional contract** depends on it: our block must keep landing contiguous and order-preserving, and the SDK's reserved name set (core, tail, and harness-profile extras) must not gain a member. `tests/test_seam.py` |
| `langchain/agents/factory.py` (hook wiring, duplicate-name check) | `src/lc_factory/assembly.py` — the seam documents `before_*` forward / `after_*` reversed. A reversal inverts every phase guarantee. `tests/test_seam.py` |
| `deepagents_code/client/launch/server.py` (`_build_server_env` denylist) | `src/lc_factory/server_graph.py` — the `LC_FACTORY_MIDDLEWARE` transport works only because filtering is denylist-based, not prefix-based. Prefix filtering would sever it silently. |

Everything else is consumed as a library through `src/lc_factory/upstream.py`,
which is the single inventory of upstream symbols we depend on — including
private (underscore) names that carry no semver protection.

## Procedure

1. **Read the release delta.** Diff the *published artifact*, not the
   monorepo — the wheel is what we pin, and this needs no upstream checkout:

   ```sh
   # download + unpack the new sdist to a scratch dir, then:
   OLD=.venv/lib/python3.11/site-packages/deepagents_code
   NEW=<scratch>/deepagents_code-<version>/deepagents_code
   for f in agent.py server_graph.py client/launch/server_manager.py; do
       diff -u "$OLD/$f" "$NEW/$f"
   done
   ```

   Also diff the modules the boundary imports from (`auto_mode.py`,
   `goal_rubric.py`, `reliable_rubric.py`, `offload_middleware.py`,
   `local_context.py`, `_server_config.py`, `client/launch/server.py`,
   `client/remote_client.py`) and skim the sdist's `CHANGELOG.md`.

   **Check the changed line ranges before reading hunks.** `agent.py` is a
   ~3000-line file and we ported only `create_cli_agent` (roughly lines
   2155-2989). A change to that file usually is not a change to our region —
   `diff -u ... | grep '^@@'` answers this in one step. Decide, per hunk in
   our region, whether it is a composition change we must re-apply or an
   internal change we inherit free.

2. **Update the pins** in `pyproject.toml` — and the matching constants in
   `tests/test_smoke.py`, which assert the installed versions. `deepagents`
   may or may not move with `deepagents-code`; check the new release's
   `requires_dist`. Then `uv sync`.

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

   **The parity suite is a documented-divergence suite, not an equality
   suite.** It asserts that the *default* composition matches v0; deltas are
   opt-in and must not perturb it. If a change to the port forces an edit to
   `test_parity.py`, that is the signal a delta has stopped being opt-in —
   fix the delta, not the test.

5b. **Run the seam suite.** `uv run pytest tests/test_seam.py` — it asserts the
   injection seam's positional contract against the **final** composed stack
   (after the SDK's own merge, not just the factory block), re-derives the
   SDK's middleware names — core, tail, AND the harness-profile union — and
   checks `_SDK_RESERVED_MIDDLEWARE_NAMES` still covers them, and proves hook direction by running the hooks. These are the
   upstream behaviors the seam's public promises rest on, so a bump that
   changes any of them fails here rather than silently inverting a documented
   guarantee.

6. **Full verification.**

   ```sh
   uv run pytest              # default suite
   uv run pytest -m integration   # live server round trip
   ```

   Note: the integration module imports upstream's private
   `deepagents_code._testing_models` at module scope, so a rename there
   breaks collection of the *default* suite too — a deliberate early signal,
   but read the traceback before assuming the port drifted.

7. **Re-check the tripwire.** Run one drift injection (see below) to confirm
   the parity suite still fails when it should at the new version. A suite
   that quietly stopped working looks exactly like a clean bump.

8. **Update provenance strings** — the "Verified against" line in
   `upstream.py` and the version note in `assembly.py`.

9. **Commit the bump on its own**, with the upstream delta summarized in the
   message. Never bundle a pin bump with feature work.

### Bump log

Real bumps and what they cost, so the recurring maintenance burden of the
recomposition strategy is measured rather than guessed.

| Bump | Port changes needed | Notes |
|---|---|---|
| 0.1.47 → 0.1.48 | **none** | `agent.py` changed only in the agent-directory discovery helpers (~lines 1105-1271), far outside the ported region. `auto_mode.py` changed classifier failure wording only. `server_graph.py` and `server_manager.py` byte-identical. Upstream also migrated legacy hooks to v2 events — inherited free. |
| 0.1.48 → 0.1.52 (SDK 0.7.0b2 → 0.7.1) | **yes — first real re-application** | 7 hunks in the ported region: `auto_classifier_model` threaded end-to-end (new `ServerConfig` field → `_make_graph` → assembly); `CostTrackingMiddleware` on the main and nested subagent stacks; server-owned Hooks v2 `ServerHooksMiddleware` on both stacks (GA in 0.1.52); HITL restructure (`interrupt_on` now always `{}`, approval gate moved from the SDK tail into the factory stack); `_make_graph` settings bootstrap moved off the event loop via `asyncio.to_thread`. Boundary grew 4 symbols. Stack-shape fallout: compaction renamed to `SummarizationMiddleware` and hoists into the SDK core slot; `last` no longer precedes HITL — 2 seam tests re-pinned, seam docs updated. **`tests/test_parity.py` passed unmodified.** SDK `_apply_custom_middleware` untouched; `_build_server_env` denylist, `apply_dotenv` skip-if-present, scaffold rebind seam, and private-mkdtemp work_dir all re-verified. Drift injection still turns parity red. Under an hour end to end. |
| 0.1.52 → 0.1.54 (SDK 0.7.1 → 0.7.5) | **none** | Second free bump, and the first where "free" was *proven* rather than inferred from hunk ranges: `agent.py`, `server_graph.py`, `server_manager.py`, `client/launch/server.py` and SDK `graph.py` are all **byte-identical between the two tags** (blob-hash compared, then cross-checked identical to the installed wheel). SDK `profiles/` untouched, so the harness-profile name union is unchanged and `_SDK_RESERVED_MIDDLEWARE_NAMES` still covers it. Every changed upstream module is consumed as a library through the boundary: `config.py` (+180/-9, MCP shutdown-race log filtering + terminal trace metadata), `cost_tracking.py` (+690/-3, pricing coverage), `hooks/server_middleware.py` (+282/-48, `PostToolUseFailure` routing + no post-tool hook replay), `config_manifest.py` (+43/-1), `offload_middleware.py` (+4/-2, archive routing), SDK `middleware/filesystem.py` (+285, delete-permission semantics), SDK `backends/protocol.py` (+23, new `ExecuteArtifact`). Transitive: `langgraph-checkpoint-sqlite` 3.1.0 → 3.1.1. D4 bump-check #1 re-verified in source (`config.py:392` still skips keys already in `os.environ`). Boundary 8, parity **25/25 unmodified**, seam 53, full **124**, integration **4**, drift injection still red. Delta read ran off the monorepo clone rather than an sdist download — cheaper, and sound because the tags were cross-checked byte-identical to the wheel. Under 30 minutes end to end. |

## Validating the tripwire itself

The parity suite is the only thing standing between a pin bump and a silent
behavioral change, so it needs its own check: **does it still fail when it
should?** A suite that cannot fail is worse than none, because it converts
"we didn't look" into "we verified."

The technique is drift injection: monkeypatch the port to make a mistake a
maintainer would plausibly make while re-applying an upstream change, then
confirm the suite goes red. Do this whenever you change `_fingerprint`,
`_normalize`, or the matrix — those edits can silently remove coverage. (One
did: making nested compiled graphs opaque, to shrink failure output, dropped
criteria-agent coverage entirely.)

Recipe — a throwaway pytest plugin, run with `-p`:

```python
# /tmp/drift/dc.py    →    PYTHONPATH=/tmp/drift uv run pytest tests/test_parity.py -p dc
def pytest_configure(config):
    import lc_factory.assembly as A
    from lc_factory.upstream import ShellAllowListMiddleware as S
    A.ShellAllowListMiddleware = lambda _allow: S(["sudo", "curl", "rm"])
```

Patch `lc_factory.assembly` for symbols the assembly imports at module scope,
and `lc_factory.upstream` for ones it imports lazily inside the function
(e.g. `_create_goal_criteria_agent`). Patching only the port — never
upstream — is what makes the two sides diverge.

Three permanent negative controls live in the suite itself
(`test_fingerprint_detects_*`), so the most important cases are protected
from regression without any manual step.

### Known blind spots

Verified by injection and deliberately accepted. Re-check these first if you
suspect the suite is missing something:

- **`repository_root` and `auto_mode_enabled` passed to
  `_create_goal_criteria_agent`.** They shape the nested criteria agent's
  system prompt and interrupt predicates, neither of which the graph summary
  reaches (it compares node names and tool names). Catching them means
  fingerprinting the nested agent's prompt and interrupt configs.
- **`async_subagents`, and the resolved `tools`/`mcp_tools` lists.** No
  matrix case supplies them.
- **State nested deeper than `_MAX_DEPTH` (6) in `_normalize`.** The cap
  bounds failure output; anything below it reduces to `"<max-depth>"` and is
  invisible. Nothing is truncated at the current pin, and the richest parity
  case asserts that stays true — so if upstream deepens composed state this
  fails a test rather than quietly shrinking coverage. If it fires, either
  raise the cap or record here what is being given up.

Both of the above are *latent* rather than live: they cost nothing today and
would begin costing silently after an upstream change. That is the failure
mode this section exists to make findable — the summarized-graph tool list
had the same shape (it kept only the last tool-bearing node until it was
changed to union across them), and opaque nested graphs had it before that.

Two ingredients are needed before criteria-agent arguments are observable at
all: `goal_criteria_tools` (or the middleware is not installed) *and*
`project_context` (or the nested agent gets no repository backend, so it has
no filesystem tools to differ in). `test_composition_parity_criteria_agent_with_repository`
supplies both; each of its guard assertions exists to fail loudly if a future
upstream stops surfacing that state, rather than letting the case quietly
stop testing anything.

## Rollback

The pin bump is one commit touching `pyproject.toml`, `uv.lock`, and the
ported files. Revert that commit and `uv sync` to return to the previous
baseline; no state migration is involved.

## Divergences from v0

Intentional differences between our assembly and upstream's, re-verified on
every bump. Keep this list exhaustive — it is what makes step 4 tractable.

**Behavioral deltas** (opt-in; the default composition stays identical to v0,
which is what `tests/test_parity.py` asserts *unmodified*):

1. **Middleware injection seam** (`middleware=`, wave 2.1). The factory accepts
   caller-supplied middleware, which `create_cli_agent` structurally cannot.
   Inert when omitted. **Four sites** in `assembly.py`, each marked `# SEAM` —
   grep that marker to find them all when re-applying an upstream change:
   - `# SEAM (resolve)` — `_normalize_injected_middleware` near the top of the
     body. The three splices below all reference the binding it creates, so
     restoring them without this one is a `NameError`.
   - phase `first` — in the `agent_middleware` list initializer.
   - phase `before_verification` — immediately before the
     `if goal_criteria_tools is not None:` block.
   - phase `last` — after `ReliableRubricMiddleware` is appended, followed by
     the single `_validate_injected_middleware` call.

   `_validate_injected_middleware` runs on every composition, not only when
   middleware is injected: its duplicate-name half also guards the factory's
   own stack, and its message distinguishes the two cases — a collision with
   nothing injected is a port defect, most likely surfacing during a bump.

   Each phase anchors to *unconditional* middleware so its boundary does not
   move with configuration. Two documented exceptions, both the SDK's
   name-based merge hoisting a factory middleware into an SDK-default slot:
   the compaction middleware (named `SummarizationMiddleware` since 0.1.52)
   always rides the SDK core's summarization slot, and with `fs_tools` set the
   factory's own `FilesystemMiddleware` is hoisted ahead of the `first` phase.
   Since 0.1.52 the approval gate also lives in the factory stack (AutoMode or
   AsyncApproval HITL, ahead of the verification tail) rather than in the SDK
   tail, so `last` middleware sits after HITL in list order.
   `tests/test_seam.py` asserts placement in the final composed stack, so a
   re-application that moves a site fails there.

2. **Factory-reference transport** (`LC_FACTORY_MIDDLEWARE`, wave 2.2). The
   server subprocess resolves `"module.path:callable"` from the environment and
   passes the result to `create_factory_agent(middleware=...)`. Inert when the
   variable is unset.

   - Lives in `src/lc_factory/server_graph.py` (`_resolve_middleware_ref`,
     `_factory_middleware`, and the resolve call in `_make_graph`).
   - **Deliberately outside `ServerConfig`.** The upstream
     `DEEPAGENTS_CODE_SERVER_*` contract stays byte-identical; the variable
     reaches the subprocess only because `_build_server_env` filters by an
     explicit key denylist rather than by prefix. **On a bump, re-check that
     denylist** — a switch to prefix filtering, or the addition of an
     `LC_`/non-`DEEPAGENTS` sweep, would silently sever the transport and the
     agent would compose without the caller's middleware.
   - **Security invariant:** the reference must come from a real shell export.
     Resolving it imports and executes that module inside the server process,
     so a project-local source would let an untrusted repository run code
     merely because `lc-code` was launched inside it (decisions.md D4).

     This is *enforced*, not merely unimplemented, and the enforcement is
     load-bearing: upstream loads `.env` files straight into `os.environ` on
     two paths that both precede resolution — the client's settings bootstrap
     searches upward from the cwd, and the server's
     `settings.reload_from_environment` re-reads the project directory. Either
     would otherwise let a committed `.env` name the module. Upstream keeps its
     own denylist of keys that "turn `.env` loading into code execution", but
     it is a `frozenset` and cannot know about ours.

     `lc_factory._env.reserve_middleware_ref_env()` therefore claims the slot
     with an empty value, exploiting the fact that upstream's `apply_dotenv`
     skips keys already present in `os.environ`.

     **Placement is the guard, and it is subtle enough that a first attempt got
     it wrong.** `deepagents_code.config` has a module-level PEP 562
     `__getattr__` that bootstraps settings and loads `.env` on first attribute
     access — so merely importing `lc_factory.upstream` already contaminates
     the environment. Calling the reservation from `server_graph` module scope
     or `tui.main()` runs *after* that and silently no-ops. It must stay in
     **`lc_factory/__init__.py`**, which Python executes before any submodule
     and which both entry points pass through, and `_env.py` must stay free of
     any `lc_factory`/upstream import so `__init__` can reach it without
     pulling the boundary.

     **On a bump, re-check three things:**
     1. `apply_dotenv` still skips keys already present in `os.environ` — if it
        starts overwriting, the guard is void.
     2. Nothing has moved an upstream import ahead of the reservation in
        `__init__.py`, and `_env.py` still imports only stdlib.
     3. **Nothing imports `deepagents_code` before `lc_factory`.** The guard
        assumes it wins the race, and that assumption reaches outside this
        package: an upstream release registering a langgraph plugin or entry
        point that imports `deepagents_code` would defeat it, as would a
        `sitecustomize`/`.pth`, or embedding `lc_factory` in an application
        that already imported upstream. Verified clear at this pin — the
        generated workspace (`checkpointer.py`, `langgraph.json`) has no
        upstream import and no pre-import hook; those two workspace
        sub-properties are now pinned by `tests/test_launch.py::
        test_generated_workspace_cannot_preempt_the_reservation`. The
        process-level precondition itself remains untestable: any test
        imports `lc_factory` first, which is the assumption itself.

     `tests/test_server_graph.py` pins the outcome in a **subprocess** — an
     in-process test imports `lc_factory` before it can seed a repository and is
     structurally incapable of failing — across both process shapes (client:
     cwd is the repo; server: cwd is a private temp dir and the repo arrives via
     `DEEPAGENTS_CODE_SERVER_CWD`), each with a negative control that removes
     the reservation and asserts the probe goes red. The two shapes traverse
     different upstream code, and the server shape is the one that was
     exploitable.

     Accepted consequence: the variable cannot be set from *any* `.env`,
     including the user's own global one. Separating a global `.env` from a
     project one needs upstream internals we do not reach for.
   - `_resolve_middleware_ref` converts every failure **it detects** —
     malformed value, an import that fails or itself raises, missing or
     unreadable attribute, non-callable, a factory that raises, an unordered
     `set` return, and an unusable return (notably `None`, i.e. a forgotten
     `return`) — into one `ValueError` whose message names the variable.
     `_make_graph` catches that and emits a `STARTUP_ERROR_MARKER` startup
     failure. The single conversion point is deliberate: upstream's
     `_build_graph_factory` barrier would also catch a stray exception and fail
     startup, but the user would see e.g. a bare `TypeError` with nothing
     connecting it to a variable they set outside the app.
     `_print_startup_error` preserves the full message in human stderr but
     flattens the machine-readable marker to one line because the parent
     extractor consumes a single marked line. Upstream's ported helper assumes
     hardcoded one-line messages; the factory-reference path can carry arbitrary
     exception text.

     Validation failures raised *later* by `create_factory_agent` (unknown
     phase, non-middleware entries, reserved or duplicate names) are **not**
     funnelled through here — they are self-describing and reach the user via
     that same upstream barrier.
   - **Second-order dependency on the server cwd.** The server runs
     `python -m langgraph_cli` with `cwd=work_dir`, and `-m` puts cwd on
     `sys.path[0]`. Today `launch.py` uses a private `mkdtemp`, so nothing
     untrusted is importable and the user-scoped invariant holds. If `work_dir`
     ever became the user's project directory, a globally exported
     `LC_FACTORY_MIDDLEWARE=mymw:build` would resolve `mymw.py` out of whatever
     repository the user happened to be in — exactly the shadowing the
     `PYTHONPATH` strip exists to prevent. Re-check on any change to how the
     server working directory is chosen.

3. **`src/lc_factory/_testing_middleware.py`** — test support shipped inside
   the package, mirroring upstream's own `_testing_models` / `_fake_models`.
   Needed because `_build_server_env` strips `PYTHONPATH` from the server
   interpreter, leaving an installed package as the only place the integration
   test can put a fixture the subprocess will import. (The server's own cwd is
   also on `sys.path`, but it is a private `mkdtemp` the CLI path gives no hook
   to write into — and relying on it would make an untrusted-cwd import path
   load bearing. See that module's docstring.) Nothing in `lc_factory` imports
   it.

**Structural divergences** — what makes the recomposition possible. Each must
be re-verified on a bump:

**`assembly.py`** (otherwise line-faithful to `agent.py:2155-2989`):
- `create_cli_agent` renamed to `create_factory_agent`.
- All upstream imports routed through `upstream.py`.
- The lazy `langchain_quickjs` import goes through
  `upstream.import_code_interpreter()` (laziness preserved).
- The seam: the `middleware` parameter, `FactoryPhase`, `_PHASE_ORDER`,
  `_SDK_RESERVED_MIDDLEWARE_NAMES`, `_normalize_injected_middleware`,
  `_validate_injected_middleware`, and the four `# SEAM` sites above.

**`launch.py`** (ported from `server_manager.py`):
- `GRAPH_REF` targets `lc_factory.server_graph:make_graph` instead of
  upstream's `deepagents_code.server_graph:make_graph`.
- `_DISTRIBUTION_NAME` is `lc_factory`, and the generated runtime
  `pyproject.toml` depends on this package rather than `deepagents-code`.
- Upstream's private `_scaffold_workspace` is public `scaffold_workspace`.
- `_default_package_project_root` probes two ancestor levels for
  `pyproject.toml` (src layout) rather than upstream's fixed `parent.parent`.
- The module scaffolds only. A standalone launcher
  (`start_factory_server_and_get_agent` / `factory_server_session`) was ported
  in Group 1 and **deleted at the Group 2 closeout** — no production caller,
  and it duplicated upstream code we would have owed re-application on every
  bump. Its removal also retired five boundary re-exports
  (`_EPHEMERAL_PORT`, `_capture_project_context`,
  `_preflight_validate_mcp_config`, `_set_or_clear_server_env`,
  `emit_preserved_log_notices`), four of them private and therefore
  unprotected — so the bump tripwire no longer fails on names nothing uses.

**Import timing** (no behavioral effect, but a real difference):
- `lc_factory.server_graph` imports the boundary at module scope, which
  eagerly loads `deepagents_code.agent` (~2350 modules). Upstream's
  `server_graph` defers that to the first `make_graph()` call. Total work is
  identical — the server builds the graph immediately either way, and the
  process ends in the same state (same warning filters) — but the cost is
  paid at import rather than at first build.
- `cli_main` is deliberately isolated in `lc_factory/upstream_cli.py`,
  imported only by `tui.py`. Re-exporting it from `upstream.py` would pull
  `deepagents_code.main` (and its module-level global warning filters) into
  the server subprocess, which upstream's server never loads.
- `lc-code` startup is slower than `dcode` for the same reason: upstream's
  `dcode -v` fast path depends on lazy imports the boundary flattens.

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
