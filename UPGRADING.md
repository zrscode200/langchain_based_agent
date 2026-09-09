# Upgrading the upstream pin

`lc_factory` recomposes upstream internals, so a dependency bump is a
**deliberate, reviewed event** — never an incidental `uv lock --upgrade`.
This file is the procedure.

The optional [enterprise package](enterprise/README.md) uses the same OG runtime.
Its gateway/HTTP/SSE, replay panel, nested workspace and wheel launch contracts
must be checked at this pin too. Keep its exact OG dependency and the two package
versions in step when publishing a coordinated release; record wheel hashes.

Enterprise's `maintenance.py` also depends on upstream update-check/auto-upgrade
gates, the cost-tracking refresh starter, and the final server environment builder.
On every bump, verify all automatic callers still use those gates and each server
start/restart uses the patched environment builder. Recheck the LangGraph CLI
analytics and API version-check opt-out flags against the locked versions. Run
`tests/test_enterprise_maintenance.py`, including managed settings that enable
updates, and inspect any new automatic network activity before accepting the pin.

The factory also owns declarative subagent policy composition and JSON-schema
result validation; see [SUBAGENTS.md](SUBAGENTS.md). On a bump, verify SDK fork
middleware merge ordering, filesystem permission/tool composition, QuickJS PTC
capture timing, and LangChain raw JSON-schema validation. The explicit child
model slot must continue to prevent inherited runtime model switches. Default
parity and the actual policy execution tests cover these seams.

[DELEGATION.md](DELEGATION.md) describes opt-in settled dispatch and explicit
background submission. Verify the private QuickJS dispatch/schema helper tuple,
ToolRuntime injection, ExecutionInfo replay identity, exception propagation
through JavaScript, and event completion timing when changing the pins.

[MCP_RESOURCES.md](MCP_RESOURCES.md) describes reload-generation resource
ownership. Verify the pinned MCPSessionManager initialization/invalidation
contract, connection signatures, AnyIO task ownership and discovery return shape.

## The dual pin

`pyproject.toml` pins Code and the SDK as one reviewed unit, plus an explicit
QuickJS version when interpreter fixes are adopted. Normally use exact published
versions and verify their artifacts. The current user-authorized exception pins
both Code and SDK via PEP 508 Git references at
`6c89fe2197a2dfe4f3851cda38565bcadba6066b`; their metadata still says **0.1.66 / 0.7.13**.
QuickJS is the published **0.3.7** release.

Keep source references in project dependencies so built wheels retain them.
`[tool.uv] no-sources = true` prevents the upstream monorepo's editable source
overrides from silently pulling ACP or QuickJS from Git too. The smoke suite
checks each Code/SDK distribution's `direct_url.json` commit and subdirectory,
not just its version. For an internal artifact mirror, review and record the
replacement artifact hashes and provenance before changing that assertion.

**Review both together.** Check Code's SDK requirement even when the SDK version
string stays fixed. Do not mix an updated constructor with old helper packages.

## What the port is coupled to

The port tracks specific upstream files. Diff these between the old and new
tags before touching anything:

| Upstream file | Ported into |
|---|---|
| `deepagents_code/agent.py` (`create_cli_agent` body) | `src/lc_factory/assembly.py` |
| `deepagents_code/auto_mode.py` (`_classifier_model`, `_review_batch`), `model_retry.py`, optional `langchain_deepseek/chat_models.py` | `src/lc_factory/auto_classifier.py` — classifier-only invocation view for native DeepSeek (`_llm_type == "chat-deepseek"`). Recheck schema/parser validation, configured/inherited model resolution, retry metadata and SDK `extra_body` precedence. Run `tests/test_auto_classifier.py` with the optional DeepSeek adapter installed; skipped provider cases are not compatibility evidence. |
| `deepagents_code/server_graph.py` (`_make_graphs`, `ServerRuntime`, workspace binding and runtime caches) | `src/lc_factory/server_graph.py` — thread/workspace validation, resource-policy cache keys, and the graph/backend/offload runtime must remain one contract. |
| `deepagents_code/offload_api.py` (HTTP app and workspace-aware runtime lookup) | `src/lc_factory/offload_api.py`, plus the generated `http` block in `src/lc_factory/launch.py` — the adapter must resolve the same workspace runtime as graph execution. |
| `deepagents_code/client/launch/server_manager.py` (`_scaffold_workspace`, `_write_pyproject`) | `src/lc_factory/launch.py` — **the live surface; re-apply changes here** |
| `deepagents_code/client/launch/server_manager.py` (`start_server_and_get_agent`) | *Nothing* — a standalone port of this was deleted at the Group 2 closeout. It had no production caller (the TUI seam routes through upstream's own) and owed re-application on every bump. **Nothing to re-apply.** |
| `deepagents_code/client/launch/server_manager.py` (`_scaffold_workspace` global) | `src/lc_factory/tui.py` — **rebind seam, re-verify every bump** |
| `deepagents/graph.py` (`_apply_custom_middleware`, the core/tail split, fork inheritance merge) | `src/lc_factory/assembly.py` — the main and fresh-subagent phase contracts depend on the SDK splice; forks additionally inherit the parent's middleware and retain parent positions on same-name replacement. `tests/test_seam.py` |
| `deepagents/middleware/subagents.py` (fork state/prompt inheritance and final compilation) | `src/lc_factory/assembly.py` — re-check shared middleware instances, private-state inheritance, recursive-delegation guards, and SDK-reserved names added after the graph-level merge, including `_ForkTaskToolMiddleware`. |
| `langchain/agents/factory.py` (hook wiring, duplicate-name check) | `src/lc_factory/assembly.py` — the seam documents `before_*` forward / `after_*` reversed. A reversal inverts every phase guarantee. `tests/test_seam.py` |
| `deepagents_code/config.py` (`credentials`, `_load_dotenv`, runtime state) | `src/lc_factory/_env.py`, `src/lc_factory/assembly.py`, and `src/lc_factory/server_graph.py` — D4 depends on shell-first dotenv precedence and the server must freeze workspace environment/credentials and pass model metadata to the constructor and lazy consumers. |
| `deepagents_code/extensions/hosting.py`, `extensions/runtime.py`, `model_retry.py` | `src/lc_factory/assembly.py`, `src/lc_factory/server_graph.py` — extension composition and retry middleware now cross the seam's positional contract. |
| `deepagents_code/client/launch/server.py` (`_build_server_env` denylist) | `src/lc_factory/server_graph.py` — the `LC_FACTORY_MIDDLEWARE` transport works only because filtering is denylist-based, not prefix-based. Prefix filtering would sever it silently. |

Everything else is consumed as a library through `src/lc_factory/upstream.py`,
which is the single inventory of upstream symbols we depend on — including
private (underscore) names that carry no semver protection.

## Procedure

1. **Read the baseline delta.** For release pins, diff the *published artifact*.
   For an explicitly authorized unreleased fix, pin one full source revision,
   inspect the complete package delta, and verify installed sources against it.
   Never identify unreleased code only by its retained version string.
   The ordinary published-artifact workflow needs no upstream checkout:

   ```sh
   # download + unpack the new wheel/sdist to a scratch dir, then:
   OLD="$(uv run python -c 'import pathlib, deepagents_code; print(pathlib.Path(deepagents_code.__file__).parent)')"
   NEW=<scratch>/deepagents_code-<version>/deepagents_code
   for f in agent.py server_graph.py client/launch/server_manager.py; do
       diff -u "$OLD/$f" "$NEW/$f"
   done
   ```

   When a release-tagged monorepo clone is available, compare the old and new
   tag blobs first, then prove the wheel and sdist sources match the target tag.
   The published artifacts remain authoritative: verify their hashes against
   PyPI metadata, their `Requires-Python` and `Requires-Dist`, and any generated
   build provenance before trusting the clone. A matching version string on
   `main` is insufficient: post-release commits can retain that version while
   changing constructor arguments and runtime behavior.

   Also diff every module inventoried by `src/lc_factory/upstream.py`. At
   minimum, re-read `config.py`, `_server_config.py`, `model_retry.py`, the
   extension modules, goal/rubric/local-context modules, offload modules,
   `client/launch/server.py`, `client/remote_client.py`, workspace binding
   modules, and the SDK graph, subagent middleware, and harness profiles. Skim
   the artifact's `CHANGELOG.md` when one is present.

   **Check changed line ranges before reading hunks.** `agent.py` is large and
   only `create_cli_agent` is ported. Locate that function at both tags instead
   of carrying line numbers forward; `diff -u ... | grep '^@@'` then identifies
   overlap quickly. Decide, per hunk in the ported function, whether it is a
   composition change to re-apply or an internal change inherited free.

2. **Update the pins** in `pyproject.toml` — and the matching constants in
   `tests/test_smoke.py`, which assert installed versions and, for source pins,
   exact provenance. `deepagents`
   may or may not move with `deepagents-code`; check the new release's
   `requires_dist`. Then `uv sync`.

3. **Check the boundary first.** `uv run pytest tests/test_boundary.py` — it
   resolves every name in `upstream.__all__` plus every entry in
   `upstream.TYPE_ONLY_IMPORTS` (annotation-only names that would otherwise
   break silently, since nothing evaluates them at runtime). This is the
   cheapest signal that the upstream surface moved. Note that runtime names
   also fail earlier, at `import lc_factory.upstream`.

4. **Re-apply composition changes** to the ported files, keeping the port
   line-faithful to the new upstream body plus the exhaustive divergence list
   below. Treat the agent, cached `ServerRuntime`, generated HTTP app, and TUI
   scaffold rebind as one runtime contract. Re-check thread/workspace binding,
   persisted workspace policy, runtime cache selection, immutable workspace
   environment/credential snapshots, model metadata, retry and summarization arguments,
   interpreter/store plumbing, extension load/host/shutdown, workspace-aware
   offload publication, and every `# SEAM` marker before calling the port
   current. Preserve upstream defaults, including forked general-purpose
   delegation, and review any resulting changes to the factory's documented
   injection scope and ordering explicitly.

5. **Run the parity suite.** `uv run pytest tests/test_parity.py` — it
   compares our composed agent against upstream's across a config matrix,
   down to middleware *state* (constructor arguments, not just classes),
   interrupt gating including the `when` predicates, subagent wiring and
   context mode, backend composition, prompt, schema, and effective returned
   graph configuration. A mismatch means the port drifted; fix the port, not
   the test. Capture the returned graph's config rather than observing only
   `.with_config` calls: upstream can apply overrides through `.copy`, which
   the old call observer missed. A negative control must detect a changed
   recursion limit regardless of which method applied it.

   The matrix must keep covering the shape the server actually boots with
   (`server_graph.py` always passes goal-criteria and rubric-grader tools) —
   see `test_composition_parity_server_realistic`.

   **The parity suite is a documented-divergence suite, not an equality
   suite.** It asserts that the *default constructor* composition matches v0;
   constructor deltas are opt-in and must not perturb it. The bundled clients
   separately enable background/reload tools and settled dispatch by default
   through `runtime_config.py`. Keep its trusted preferences, optional per-setting
   overrides, lazy diagnostic-safe resolution and server snapshot in sync.
   Existing saved capability lists replace defaults; history stays opt-in.
   Native `task` stays foreground; only `start_background_task` detaches. Check
   trusted preference/env precedence and the shared client/server snapshot when
   updating startup plumbing. An expectation or normalization exemption
   added for the factory is evidence that a delta stopped being opt-in — fix
   the delta, not the test. Raising `_MAX_DEPTH` only to keep newly nested
   upstream state observable is tripwire maintenance, not an exemption; it
   still requires the rich-case truncation guard and drift injection.

   Recheck the factory's background wrapper against the SDK task-tool invocation
   and inherited subgraph checkpoint configuration. Two sequential child pauses
   must resume without repeating a prior protected write, for isolated and fork
   modes. Keep host approval responses bound to owner/job/current interrupt IDs,
   separate from the foreground approval UI and main result acknowledgment.
   Auto-enabled composition has one reviewed factory delta: `subagent_approval.py`
   replaces local child HITL with session-aware Auto and a final admission guard.
   Parity verifies all native constructor settings before projecting this delta;
   its drift-injection check must fail on changed child classifier settings.
   Recheck retained task coroutine binding across native/settled/JavaScript and
   background dispatch, replay-stable child scopes, private state initialization,
   and the native `_process_decision` return contract. Classifier-only history
   must retain trusted original authorization followed by prior child actions,
   excluding inherited parent actions and model-written user-message metadata.
   Native routing and the recorded disposition use one mode snapshot; tool
   admission reads live mode again. Only an actual human approve/edit decision
   creates durable exact-action approval. Keep counters and temporary receipts
   child-local while mode reads target the owner. Re-run child approval tests,
   including DeepSeek transport, classifier failures, live races, tool ceilings,
   and approval resumes, on every upstream change to these seams.
   Recheck child-last middleware ordering for steering: before-model injection,
   actual request delivery, stale after-model proposals and final tool admission
   after Hooks-v2/HITL resume. Stable steering IDs and private checkpoint revision
   must survive continuation; explicit acknowledgments must refer to delivered
   messages. Never fabricate hook replies or restart children to deliver late
   guidance. Verify multi-action approval rejection, fresh published pause IDs,
   structured results, reload and cancellation/terminal inbox races.
   Keep inspection/model-list projections separate from host hook transport:
   PostToolUse payloads contain raw intermediate tool results. Observe failure
   status in both direct ToolMessages and matching Command-carried messages.
   `background_ui.py` uses RemoteAgent's authenticated transport/workspace binding
   and a hidden controller feeding the native dynamic subagent panel; its headless
   tests guard switching, shutdown and stale decisions. `background_activity.py`
   opens a read-only, owner-bound detail window from the native row and keeps
   live updates separate from frozen `TaskReview` approval snapshots. Recheck
   single-flight inspection, narrow layout, scroll position and teardown.
   `background_panel.py` extends
   the currently installed panel class (including enterprise replay handling).
   Recheck native prepare_turn/finalize_running/reset, phase navigation and row
   review at narrow widths. Recheck cancellation against
   the real hook fulfillment ledger, including shielded execution and preservation
   of unrelated foreground hooks. `skill_policy.py` adapts upstream
   skill listing/invocation and restores client patches on exit. Recheck all three
   catalogue paths plus child/reload behavior when those upstream seams change.

   Class-valued constructor arguments, including grader state schemas, are
   fingerprinted by fully qualified class identity and the `repr` of their
   declared annotations. Do not recursively traverse generated Pydantic
   validators or typing caches to compare a schema class. This normalization
   applies equally to upstream and the factory; it is not a factory-specific
   exemption. Keep `_MAX_DEPTH` at 9 unless observed nesting requires a reviewed
   change, and retain negative controls for changed schema identity/annotations
   and subagent mode as well as the rich-case truncation guard. Environment
   fields `_env` and `_environ` are now compared by SHA-256 digest, keeping
   credential values out of assertion output while detecting lost snapshots.
   Retain the explicit workspace case and dropped-environment negative control.

5b. **Run the seam suite.** `uv run pytest tests/test_seam.py` — it asserts the
   injection seam's positional contract against the **final** composed stack
   (after SDK and extension composition, not just the factory block),
   re-derives the SDK's middleware names — core, tail, harness-profile union,
   and fork-only additions during final compilation — and checks
   `_SDK_RESERVED_MIDDLEWARE_NAMES` still covers them. Verify both fresh and
   forked subagents: main middleware is inherited by reference in forks,
   unique child injections follow inherited parent entries, and upstream
   same-name child overrides retain parent positions. Reject injected child
   names that would replace inherited parent middleware, and extension names
   that would replace caller middleware. Prove hook direction by running the
   hooks. These are the upstream behaviors the seam's public promises rest on,
   so a bump that changes any of them fails here rather than silently
   invalidating a documented guarantee.

6. **Full verification.**

   ```sh
   uv run python -c 'import sys; assert sys.version_info[:2] == (3, 12)'
   uv run pytest                  # default suite
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

9. **Record the bump, then commit it on its own.** Add a dated entry and a
   summary row to [`VERSION_BUMPS.md`](VERSION_BUMPS.md) — including wall-clock
   effort, which is the D3 measurement and the whole reason the ledger exists —
   then commit with the upstream delta summarized in the message. Never bundle a
   pin bump with feature work.

### Bump log

Every real bump, what it cost, and how that cost was established, lives in
[`VERSION_BUMPS.md`](VERSION_BUMPS.md) — one dated section per bump plus a
summary table.

It is kept out of this file on purpose. This document is the *method* and
should stay a constant length; the ledger is *history* and grows with every
release. Add the entry there as part of step 9 above, and keep the fields in
that file's "Adding an entry" section so bumps stay comparable — especially
effort and whether a "free" verdict was proven or inferred.

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

Permanent negative controls live in the suite itself
(`test_fingerprint_detects_*`), covering constructor state, dropped arguments,
effective graph configuration, schema fingerprints, and subagent mode. Keep
them effective when changing the observation or normalization code.

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
- **State nested deeper than `_MAX_DEPTH` (9) in `_normalize`.** The cap
  bounds failure output; anything below it reduces to `"<max-depth>"` and is
  invisible. Nothing is truncated at the current pin, and the richest parity
  case asserts that stays true — so if upstream deepens composed state this
  fails a test rather than quietly shrinking coverage. If it fires, either
  raise the cap or record here what is being given up.

All three are *latent* rather than live: they cost nothing today and
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
   Inert when omitted. Every main-agent site in `assembly.py` is marked
   `# SEAM` (deltas 3 and 4 add their own), so grep that marker when
   re-applying an upstream change:
   - `# SEAM (resolve)` — `_normalize_injected_middleware` near the top of the
     body. The three splices below all reference the binding it creates, so
     restoring them without this one is a `NameError`.
   - phase `first` — in the `agent_middleware` list initializer.
   - phase `before_verification` — immediately before the
     `if goal_criteria_tools is not None:` block.
   - phase `last` — after extension middleware and its runtime host are
     composed, immediately before the SDK tail, followed by the single final
     `_validate_injected_middleware` call.

   `_validate_injected_middleware` runs on every composition, not only when
   middleware is injected: its duplicate-name half also guards the factory's
   own stack, and its message distinguishes the two cases — a collision with
   nothing injected is a port defect, most likely surfacing during a bump.

   Each phase anchors to *unconditional* middleware so its boundary does not
   move with configuration. `before_verification` now precedes the
   `CodeModelRetryMiddleware`/rubric tail. Two documented exceptions, both the
   SDK's name-based merge hoisting a factory middleware into an SDK-default slot:
   the compaction middleware (named `SummarizationMiddleware` since 0.1.52)
   always rides the SDK core's summarization slot, and with `fs_tools` set the
   factory's own `FilesystemMiddleware` is hoisted ahead of the `first` phase.
   Since 0.1.52 the approval gate also lives in the factory stack (AutoMode or
   AsyncApproval HITL, ahead of the verification tail) rather than in the SDK
   tail, so `last` middleware sits after HITL in list order.
   `last` is deliberately applied only after 0.1.64 extension composition. An
   extension may replace ordinary upstream middleware by name, but a collision
   with caller-injected main middleware is rejected before that replacement can
   discard the requested phase. `tests/test_seam.py` asserts placement and all
   three collision cases in the final stack.

   These phase promises describe the **main** graph. Since the 0.1.66/0.7.13
   baseline, the default general-purpose subagent forks the parent. The SDK
   inherits this main middleware by reference into that fork, including caller
   injections and extensions; the parameter is no longer main-only. The same
   instance can therefore run in the parent and child, so keep per-run state in
   graph state or key mutable state by run identity. Fresh subagents do not
   inherit this block, and remote async subagents are unaffected.

2. **Factory-reference transport** (`LC_FACTORY_MIDDLEWARE`, wave 2.2;
   extended to all targets by `SEAM-REACH-TRANSPORT`). The server subprocess
   resolves `"module.path:callable"` from the environment and passes the result
   to `create_factory_agent`. Inert when the variable is unset.

   - Lives in `src/lc_factory/server_graph.py` (`_resolve_middleware_ref`,
     `_normalize_targets`, `_factory_middleware`, and the resolve call plus the
     three-target threading in `_make_graphs`).
   - **Three accepted return shapes**, disambiguated by key rather than
     guessed: a bare sequence (main, default phase), a **phase**-keyed mapping
     (main, explicit phases), or a **target**-keyed mapping
     (`main`/`subagents`/`grader`) whose values are either of the first two.
     Target and phase key sets are disjoint, which is what makes this safe; a
     mapping mixing both, or naming something in neither, is **rejected with an
     attributed error** rather than interpreted.
   - **One variable, deliberately.** A variable per target would each need its
     own `.env` reservation in `lc_factory/__init__.py` (decisions.md D4), so
     every new name is new attack surface. The target-keyed form adds none —
     the existing reservation already covers it. **Do not add per-target
     variables without redoing the subprocess D4 coverage for each.**
   - **On a bump, re-check the threading, not just the resolve.** All three
     parameters must still be passed at the `create_factory_agent` call site;
     dropping one silently returns that target to Group 3's state, where the
     capability existed but nothing could reach it. Drift-injection validated:
     severing `subagent_middleware=` reddens
     `test_subagent_middleware_runs_in_a_live_delegated_session`.
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
     load-bearing: the client's lazy `credentials` proxy and explicit credential
     reload API can load `.env` into `os.environ` before transport. The server
     now previews dotenv into an immutable snapshot without mutating that global
     environment. The reservation protects both dotenv precedence paths, while
     `_factory_middleware` still reads the reserved process variable. Upstream
     keeps its
     own denylist of keys that "turn `.env` loading into code execution", but
     it is a `frozenset` and cannot know about ours.

     `lc_factory._env.reserve_middleware_ref_env()` therefore claims the slot
     with an empty value, exploiting the fact that upstream's `apply_dotenv`
     skips keys already present in `os.environ`.

     **Placement is the guard, and it is subtle enough that a first attempt got
     it wrong.** `deepagents_code.config.credentials` is a lazy proxy whose
     first field access runs `_ensure_bootstrap` and loads `.env`. The boundary
     imports and uses credentials early enough that calling the reservation from
     `server_graph` module scope or `tui.main()` would run *after* that and
     silently no-op. It must stay in
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
     `_make_graphs` catches that and emits a `STARTUP_ERROR_MARKER` startup
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

3. **Subagent seam reach** (`subagent_middleware=`, wave 3.1, Group 3). Caller
   middleware on every subagent stack the factory composes, including the
   synthesized `general-purpose` one. Inert when omitted. Async subagents are
   unaffected — they never receive the local stack.

   - **Own phase vocabulary:** `SubagentPhase` is `first`/`last` only. Fresh
     subagent stacks have no verification tail, so there is no common
     `before_verification` boundary across fresh and forked stacks. Default is
     `last`. On a fresh subagent, `first` precedes the factory child middleware,
     including approval, and `last` follows it before the SDK tail.
   - **Forks adopt upstream inheritance and ordering.** The default
     general-purpose subagent is forked unless
     `DEEPAGENTS_CODE_FORKED_SUBAGENTS` disables it. The SDK merges parent custom
     middleware first, then child middleware by name. Existing upstream child
     overrides replace parent entries at the parent's position; a unique child
     `first` injection comes after inherited parent entries, rather than
     outside their approval or verification middleware. Child-only `first` and
     `last` injections retain their relative order, but the fresh-agent phase
     promises do not describe the entire fork stack. Main and child injections
     can both run in the same fork.
   - **Two `# SEAM (subagent ...)` sites**, both inside
     `_subagent_cli_middleware`: `first` in the list initializer, `last`
     immediately before the per-stack validation and `return`.
   - Supporting symbols: `_SUBAGENT_PHASE_ORDER`, `_DEFAULT_SUBAGENT_PHASE`,
     `_normalize_subagent_middleware`, `_validate_subagent_reserved_names`,
     `_validate_subagent_stack`.
   - **The guard is a different problem from the main stack's**, and the
     difference is the whole reason this is its own parameter. Subagent specs
     *are* name-merged — `graph.py` calls
     `_apply_custom_middleware(subagent_base, spec["middleware"],
     core_names=...)` — so the silent-replacement hazard is real, but against a
     **different base** (`FilesystemMiddleware`, summarization,
     `PatchToolCallsMiddleware`, optional `SkillsMiddleware`, profile extras,
     prompt caching). That base is a subset of
     `_SDK_RESERVED_MIDDLEWARE_NAMES`, so the constant is reused and
     **over-rejects** by design. `tests/test_seam.py` derives the real base and
     asserts the subset relation, so an upstream addition fails there. Fork
     compilation also inserts `_ForkTaskToolMiddleware` after the graph-level
     merge; reserve that name and inspect the final compiled stack rather than
     assuming the merge observer sees every SDK-owned entry.
   - **Fork collision guard:** injected child names cannot collide with any
     inherited parent middleware, including parent injections and extensions.
     Reject those collisions before SDK composition rather than allowing
     name-based replacement to discard the caller's intended scope or phase.
     This guard does not prohibit upstream's intentional child overrides.
   - Duplicate checking runs **per composed subagent stack**, not once:
     subagent stacks differ by configuration (`ConfigurableModelMiddleware`
     only without an explicit model, stall recovery only headless, shell
     allow-list only when restrictive, memory guard only with memory), so a
     name unique against one can collide on another.
   - **Injected instances are spliced by reference**, so one object is shared
     across every subagent stack. Documented in the parameter's docstring and
     pinned by identity assertion in `tests/test_seam.py`.
   - **Reachable from `LC_FACTORY_MIDDLEWARE`** via the target-keyed return
     form (see delta 2). Proven end to end by
     `tests/integration_tests/test_headless_session.py`, which delegates
     through `task` and asserts the injected middleware actually *ran* inside
     the subagent — with a negative control proving the marker tracks
     execution, not composition.
   - **On a bump, re-check:** the SDK merge with `core_names`, fork inheritance
     scope and same-name ordering, final fork compilation, and coverage of all
     SDK-owned names by the reserved constant. Keep fresh-mode phase tests,
     fork-mode inheritance/order/collision tests, and shared-instance checks
     distinct so one mode cannot hide a regression in the other.
   - **Local rename in the ported region:** upstream's
     `subagent_middleware = _subagent_cli_middleware(...)` is `subagent_stack`
     here, because `subagent_middleware` is now the factory's own parameter and
     rebinding it would shadow it for the rest of the body. Mechanical to
     re-apply; keep the rename.

4. **Rubric grader seam reach** (`rubric_grader_middleware=`, wave 3.2,
   Group 3). Caller middleware on the rubric grader's own stack. Inert when
   omitted. `GraderPhase` is `first`/`last`; default `last`.

   - **Two `# SEAM (grader ...)` sites**, both around the `grader_middleware`
     list: `first` in the list initializer, `last` immediately before
     `_validate_grader_stack` and the `ReliableRubricMiddleware` construction.
   - **The default phase is load-bearing, not stylistic.** Earlier is
     outermost, so `last` keeps `CodeModelRetryMiddleware` and the three budget middlewares
     (`_ContextToolCallBudgetMiddleware`, `_WebSearchBudgetMiddleware`,
     `_CriteriaContextBudgetMiddleware`) wrapping the injection and therefore
     still counting what it causes. `first` places middleware outside those
     bounds — legitimate, but opt-in.
   - **No reserved-name guard here, deliberately.** Unlike the main and
     subagent stacks, the grader is not assembled by `create_deep_agent`:
     `ReliableRubricMiddleware` forwards the list verbatim to langchain's
     `create_agent`, so there is no SDK base for a name to silently replace.
     Applying `_SDK_RESERVED_MIDDLEWARE_NAMES` here would enforce a rule whose
     reason does not exist on this target. `tests/test_seam.py` pins the
     asymmetry, so unifying the guards has to be a decision rather than a
     tidy-up.
   - **Validation runs at composition time, not lazily.** The grader *agent* is
     built and cached on first grading (`_ensure_grader`); a lazy-only check
     would surface a bad injection mid-evaluation instead of at boot.
   - **Test coupling to re-check on a bump:** `tests/test_seam.py` reads the
     private `ReliableRubricMiddleware._grader_middleware` to observe the
     composed stack. The import boundary does not cover upstream *attributes*,
     only the class, so a rename would not fail `test_boundary.py`. The helper
     asserts the attribute exists with a message saying where to re-point.

5. **`src/lc_factory/_testing_middleware.py` and `_testing_models.py`** — test support shipped inside
   the package, mirroring upstream's own `_testing_models` / `_fake_models`.
   Needed because `_build_server_env` strips `PYTHONPATH` from the server
   interpreter, leaving an installed package as the only place the integration
   test can put a fixture the subprocess will import. (The server's own cwd is
   also on `sys.path`, but it is a private `mkdtemp` the CLI path gives no hook
   to write into — and relying on it would make an untrusted-cwd import path
   load bearing. See the modules' docstrings.) Production code imports neither.
   The model adapter changes only deterministic fixture dispatch: it selects
   the latest human task and subsequent tool results so inherited parent markers
   cannot trigger recursive delegation in a fork. Keep the runtime's full
   inherited history intact, retain a fresh-mode case using upstream's model,
   and preserve the no-delegation negative control.

**Structural divergences** — what makes the recomposition possible. Each must
be re-verified on a bump:

**`assembly.py`** (otherwise line-faithful to `create_cli_agent` at the pin):
- `create_cli_agent` renamed to `create_factory_agent`.
- All upstream imports routed through `upstream.py`.
- The lazy `langchain_quickjs` import goes through
  `upstream.import_code_interpreter()` (laziness preserved).
- The seam: the `middleware` parameter, `FactoryPhase`, `_PHASE_ORDER`,
  `_SDK_RESERVED_MIDDLEWARE_NAMES`, `_normalize_injected_middleware`,
  `_validate_injected_middleware`, and every `# SEAM` site — main agent
  (delta 1), subagents (delta 3), and rubric grader (delta 4). Grep the
  marker rather than carrying a site count forward.
- Upstream extension replacement stays intact for its own stack, but a
  same-named extension cannot silently discard caller middleware; main `last`
  is applied after the extension runtime and final validation follows it.

**`server_graph.py`** (otherwise line-faithful to upstream's runtime factory):
- Resolves `LC_FACTORY_MIDDLEWARE` before model, MCP, sandbox, or extension
  setup and threads all three targets to `create_factory_agent`.
- Returns the same cached `ServerRuntime` shape as upstream, including the
  backend-published offload operation, and preserves extension bind/shutdown.
- Validates execution's persisted thread/workspace binding and resolves the
  runtime through the workspace resource-policy cache. Preserve configuration
  fingerprint checks, cache keys, locking, and eviction behavior alongside the
  default runtime used for non-execution graph access. Both paths now share
  the same cache and initialization lock. Preserve the process-lifetime sandbox
  claim after failed construction and HTTP 409/503 behavior before thread creation.

**`offload_api.py`**:
- Reuses upstream's Starlette app rather than copying its security-sensitive
  operation implementation, rebinding that module's `get_server_runtime` alias
  to the factory's `_workspace_runtime(binding)`. Rebinding it to the default
  no-argument runtime would route offload requests to the wrong workspace.

**`launch.py`** (ported from `server_manager.py`):
- `GRAPH_REF` targets `lc_factory.server_graph:make_graph` instead of
  upstream's `deepagents_code.server_graph:make_graph`.
- `_DISTRIBUTION_NAME` is `lc_factory`, and the generated runtime
  `pyproject.toml` depends on this package rather than `deepagents-code`.
- Because upstream registers its HTTP app only for the built-in graph ref, the
  factory structurally adds `lc_factory.offload_api:app` with custom-route auth
  enabled after generating `langgraph.json`. Preserve the upstream workspace
  API configuration with the offload routes: the client must bind a workspace
  before executing the factory graph.
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
  eagerly loads the agent/runtime boundary. Upstream's
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

## Optional runtime adaptation checks

`TALON_ADAPTATIONS.md` records the adapted Talon/Code source and owned behavior.
On an upstream bump, also verify `ConversationSaver` against delta checkpoints,
committed pending messages, cancellation and thread deletion; private
`_DeltaSnapshot` is now part of the import-boundary tripwire. Check that stateless
MCP tools capture immutable connections with `session_manager=None`. Re-run the
real MCP reconfiguration test and approval-resume generation tests: plain graph
composition parity cannot detect these failures. Background detachment depends
on the pinned SDK task tool and the innermost factory seam; verify both main and
child approval tests and fork suppression. Server capabilities require the
factory execution-time saver, proved by the live optional-capabilities test.
## Owned verification model seam

`verification_model=` is an OG-only constructor argument. Preserve selection
across criteria/fallback/grader and the no-option parity path on a pin bump.
`verification.FixedVerificationAgent` strips only main-model selection fields
from nested criteria context, retaining approvals, workspace and hook metadata.
Recheck `GoalCriteriaMiddleware`'s invoke/ainvoke contract and
`CLIContextSchema`/`ConfigurableModelMiddleware` whenever those upstream modules
change. Configured selection uses the current user/managed snapshots and
retry-owned `create_model`; never apply its ModelResult to main runtime state.
`tests/test_verification.py` owns behavior checks. Usage: [VERIFICATION.md](VERIFICATION.md).
