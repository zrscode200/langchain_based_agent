# Version bump record

Every upstream pin bump, what it cost, and how that cost was established.

This exists to answer one question with evidence instead of intuition: **is
recomposition over a pinned dependency actually cheaper than a fork?**
`decisions.md` D3 made that bet explicitly — a fork pays at merge time,
recomposition pays at upgrade time, and upgrade-time cost was judged
preferable. Every row below is a data point on that bet, so each entry records
the *measured* cost rather than an impression of it.

The procedure itself lives in [`UPGRADING.md`](UPGRADING.md). This file is the
history; that file is the method. Keep them separate — the procedure should
stay a constant-length document no matter how many bumps accumulate.

## Summary

| Date | `deepagents-code` | `deepagents` | Port changes | Effort | Commit |
|---|---|---|---|---|---|
| 2026-09-14 | 6c89fe2 source (0.1.66) → **1d3232c0 release commit (0.1.69)** | 6c89fe2 source (0.7.13) → **1d3232c0 release commit (0.7.14)** | **tracing isolation + workspace policy + server tool contract re-applied** | ~30 min | this commit |
| 2026-09-06 | 0.1.66 release → **6c89fe2 source** | 0.7.13 release → **6c89fe2 source** | **workspace snapshots + sandbox ownership; QuickJS 0.3.7** | ~18 min | `6be187b` |
| 2026-09-06 | 0.1.64 → **0.1.66** | 0.7.10 → **0.7.13** | **constructor + workspace runtime + fork seam** | ~36 min | `7bade13` |
| 2026-08-28 | 0.1.54 → **0.1.64** | 0.7.5 → **0.7.10** | **broad runtime rebase** | ~1 hr | `eaf8a12` |
| 2026-08-10 | 0.1.52 → **0.1.54** | 0.7.1 → **0.7.5** | **none** (proven) | <30 min | `cb43e40` |
| 2026-08-06 | 0.1.48 → **0.1.52** | 0.7.0b2 → **0.7.1** | **7 hunks re-applied** | ~1 hr | `304f8a8` |
| 2026-07-27 | 0.1.47 → **0.1.48** | 0.7.0b2 (held) | **none** (inferred) | ~15 min | `45c40bd` |

Two free bumps and five re-applications. The 0.1.66 bump also changes an
extension contract: default forked subagents inherit main middleware. Package
parity alone cannot prove that previously documented injection scopes survive.

---

## 2026-09-14 — 6c89fe2 source → release commit 1d3232c0 (0.1.66 → 0.1.69, SDK 0.7.13 → 0.7.14) — this commit

**Verdict: re-application owed — nine hunks across the assembly shell block,
the server graph and the owned reload helpers; QuickJS stays at 0.3.7.** Both
Code and SDK now pin the upstream release tag commit
`1d3232c0852c47af09119edea10eeec887e4f0da` (tags `deepagents-code==0.1.69` and
`deepagents==0.7.14`; the SDK tag `ded0f118` is an ancestor). Metadata versions
**0.1.69 / 0.7.14** match the PyPI releases published the same day, and the
Git provenance assertions in `tests/test_smoke.py` are retained. Python remains
**>=3.12,<4**.

**Delta-read method:** full Git diff of the pinned revision against the tag in
a read-only upstream clone (108 commits; 39 Code and 6 SDK source files), the
CHANGELOG entries for 0.1.67 through 0.1.69 and 0.7.14, and function-level
diffs of every ported file. Free verdicts below are *proven* by blob identity:
`deepagents/graph.py`, `middleware/subagents.py`, `model_retry.py`, the
extension modules, `server_manager._scaffold_workspace`/`_write_pyproject`,
and the `_build_server_env` key denylist are unchanged in the ported regions.

**Ported:** `create_cli_agent` now defaults its environment from
`active_environment()`, restores launch and project-dotenv LangSmith settings
for `execute` through `restore_user_langsmith_env`, and passes the restored
project as `user_tracing_project`; the three removed tracing helpers left the
boundary. The server graph gains upstream's process-lifetime tracing pin
(`_configure_server_tracing` after `_ensure_bootstrap` and redaction resolution
inside the workspace environment), off-loop sandbox entry (`_open_sandbox`,
`_close_sandbox`), the new `_build_tools` contract (Tavily key only; read-only
built-ins returned and forwarded to `_criteria_context_tools`),
`mcp_server_info` on `ServerRuntime`, two-pass launch binding through
`ServerConfig.resolve_workspace`, and per-request
`_resolve_bound_workspace_config` with project-policy drift and fingerprint
refusals. Those helpers are consumed from upstream through the boundary rather
than copied, so their process-wide state stays single. The owned reload path
(`mcp_reload.build_reloadable_tools`, `MCPToolBundle.read_only_builtins`,
`unpack_tool_bundle`) mirrors the new tool contract and `FactoryRuntime`
replacements carry MCP metadata.

**Inherited free:** the offload API workspace endpoint (session claim versus
project policy, request-field allowlist, `validate_only`, MCP payload) through
the rebound `get_server_runtime`; the unchanged `RemoteAgent.abind_workspace`
signature used by the web launcher; `create_model`'s new
`bind_preserved_thinking` default; the Auto classifier's Anthropic JSON-schema
path (the DeepSeek view still wraps `with_structured_output`); server reuse
across workspace switches; thread resume age limits; stale Anthropic
thinking-block handling; effort-aware caching; new picker entries (DeepSeek
V4.1 Flash, GLM 5.3, GPT-6 Astra); dotenv source attribution; SDK `read_file`,
`ls` and `glob` formatting, partial tool-call patching and bounded compaction
recovery. Transitive updates: langchain-core 1.6.1 → 1.6.3, langchain-anthropic
1.7.2, langchain-openai 1.6.2, langgraph-api 0.14.1, langgraph-runtime-inmem
0.34.1, langsmith 0.12.4, cryptography 50.0.1.

**Behavior changes to note:** post-binding validation now refuses drifted
*project-scoped* policy and changed fingerprints; a tampered session field in
a persisted binding is no longer a refusal on its own (upstream contract;
`test_workspace_runtime_rejects_changed_config_before_build` now parametrizes
`project_policy`). Web search binds whenever a Tavily key is configured, even
empty, and reports the missing key at call time. The SDK now enforces the
advertised input budget, so two tests using the 8k-token fake model declare a
large profile, as the background tests already did.

**Guard re-checks:** the three D4 checks (shell-first dotenv precedence probe,
frozen shell environment, lazy model wrapper) pass in `test_server_graph.py`
and `test_workspace_isolation.py`; SDK fork merge semantics and reserved names
pass in `test_seam.py`; `_build_server_env` remains an explicit key denylist
and still relays `LC_FACTORY_MIDDLEWARE` (the transport tripwire now seeds the
complete launch LangSmith capture upstream requires); the scaffold rebind seam
is untouched (`server_manager`'s only hunk is in `start_server_and_get_agent`,
which has no port). Enterprise `maintenance.py` gates are unchanged; its test
now patches `config._dotenv_loaded_values`, where the loader record lives.

**Verification:** Python 3.12.12; boundary 5 and smoke 5 pass; default suite
**937 passed, 36 skipped, 10 live tests deselected** in 137 s.
`tests/test_parity.py` was **not edited** (47 pass); drift injection swapping
the `ShellAllowListMiddleware` allow-list turns 2 parity tests red as required.
Wheel and sdist build; wheel metadata keeps both Git pins and QuickJS 0.3.7.
`git diff --check` is clean. Live-server suite **10 passed, 973 deselected**
(9 in one run, plus the enterprise distribution check after building both
wheels into `dist/og-enterprise`, its documented precondition).

**Effort:** about 30 minutes wall-clock from worktree creation at
2026-09-15 02:09 UTC through this ledger entry, covering dependency
resolution, both ported modules, five test adjustments and full verification.
The preceding upstream delta assessment took roughly 25 minutes and is
excluded, matching the previous entry's convention.

**Watches:** the Talon-derived modules (`archive*.py`, background workers)
remain adapted from 6c89fe2 while `deepagents-talon` moved to 0.0.8; review
separately. Upstream's `aswitch_workspace` (server reuse across cwd switches)
meets the factory's per-owner background jobs and skill policy roots only
through the unchanged binding contract; exercise it in the TUI. The web UI
could surface `mcp_server_info` from the bind response.
`_resolve_bound_workspace_config` reads the extension trust store on every
request, as upstream does.

---

## 2026-09-06 — reviewed source baseline 6c89fe2 + QuickJS 0.3.7 — `6be187b`

**Verdict: coordinated constructor and server re-application.** The user
explicitly authorized bringing the reviewed upstream fixes into this repository.
Both Code and SDK now pin full revision
`6c89fe2197a2dfe4f3851cda38565bcadba6066b` from the official upstream Git
repository. Metadata versions remain **0.1.66 / 0.7.13**; these are unreleased
source builds, not the same-numbered PyPI artifacts. QuickJS moves from
**0.3.4 to the published 0.3.7**. Python remains **>=3.12,<4**.

**Delta and provenance:** reviewed the complete Code delta from release
`3812967c84d90619848f3185f22470204fc88bd8` and SDK delta from release
`1aae3746682a65c837c5dd0f165b685253fe9465`. Installed Code's **257** and SDK's
**53** Python source files match the exact Git archive byte for byte.
Distribution `direct_url.json` records the full revision and correct package
subdirectories; smoke tests now enforce both. QuickJS's wheel SHA-256 is
`50f384b209f0f0f472c043024e2c74276900352591422df1aeae7a0fbcf8566f`, previously
verified against PyPI metadata and now recorded in `uv.lock`.

**Ported:** constructor arguments `environ`, `credentials_snapshot`, and
`model_result`; workspace-aware model middleware, compaction, classifier,
grader/criteria construction, prompt metadata, and shell environment/tracing
restoration. The server builds immutable workspace snapshots without global
dotenv mutation, scopes setup with `use_environment`, supplies tool credentials,
and carries all three snapshots into `create_factory_agent`. All three factory
middleware targets and their existing scope/order guards are preserved.

**Server ownership:** one initialization lock and shared launch/workspace cache;
process-lifetime sandbox ownership, including after failed construction; and
workspace conflict errors. The existing offload adapter automatically consumes
the updated upstream HTTP implementation, providing 409 on conflict and 503 on
startup exit before creating thread state. Scaffolding and the TUI rebind still
match upstream. Configuration, model loading, tool/MCP helpers, and HTTP code
arrive through the source-pinned dependency instead of additional local copies.

**Other adopted fixes:** SDK rejects empty edit targets and correctly formats
blank windows of nonempty files. QuickJS has private per-agent interpreter state
and native PTC streaming. The optional snapshot HMAC feature remains unconfigured;
this update supplies no signing key. Talon product features were not transferred.

**Tripwire maintenance:** environment fields `_env` and `_environ` are now
fingerprinted by SHA-256 digest rather than omitted or treated as opaque.
An explicit workspace case passes parity; dropping the factory model middleware's
snapshot makes its fingerprint diverge. Existing recursion/schema/fork/argument
negative controls and the depth-9 rich-case guard pass. No factory-specific
normalization exemption was added. Server tests were updated for the new lock
and snapshot API instead of mocking the removed credential reload.

**Verification:** Python 3.12.12; **200 passed, 7 deselected** in the default suite
(parity 31, seam 94, boundary/smoke 10, server/launch 53, workspace/interpreter 9,
assembly 3). Live-server suite **7 passed, 200 deselected**. The new tests prove
concurrent workspace configuration separation, frozen shell execution and lazy
model wrappers after a process environment change, sandbox refusal after failed
initialization, startup cache reuse, error markers, and the factory HTTP adapter's
409/503 ordering. A real factory graph executes JavaScript, forks, and verifies
distinct interpreter slots and absent parent JavaScript globals in the child.

Additionally, **16 targeted upstream SDK regressions passed** across utils,
filesystem/state/store/sandbox backends and filesystem middleware (700 unrelated
tests deselected). These run against this repository's installed environment.
Wheel and sdist build successfully. Wheel metadata retains both full Git
references and QuickJS 0.3.7; `uv sync --locked --offline` succeeds from the populated
cache. Disabling upstream monorepo source overrides keeps ACP 0.0.11 and QuickJS
0.3.7 on registry artifacts. The sdist excludes local `.code-workspace` files.
`git diff --check` passes.

**Effort:** approximately 18 minutes of implementation and verification from
2026-09-06 05:26:58 UTC through final packaging, excluding the earlier transfer
assessment. Includes source installation, both owned ports, regression tests,
provenance verification, and documentation; initial dependency investigation
before that timestamp is not included.

**Limits:** configuration isolation covers supported consumers, not arbitrary
plugins reading `os.environ`, tenant authorization, or OS process isolation.
A sandbox-enabled process still serves one workspace. Company model endpoints,
real-provider reasoning quality, external MCP services, and AWS deployment were
not exercised. Git source access (or reviewed internal mirrors) is needed to
install this baseline; runtime hosting does not require a LangChain service.

---

## 2026-09-06 — 0.1.64 → 0.1.66 (SDK 0.7.10 → 0.7.13) — `7bade13`

**Verdict: re-application owed.** The released constructor has 10 changed
hunks (55 additions / 16 deletions). `_make_graphs` has eight changed hunks;
`make_graph` changes its execution contract and gains a workspace runtime
cache. The scaffold body and TUI rebind still match their upstream call sites.

**Delta-read method:** downloaded PyPI wheels and sdists, checked metadata and
SHA-256 hashes, and compared package Python sources to release tags. All 257
Code and 53 SDK source files match across wheel, sdist, and tag, excluding
generated `_build_info.py`; installed sources also match the verified wheels.
Code's generated `BUILD_COMMIT="3812967"` identifies release commit
`3812967c84d90619848f3185f22470204fc88bd8`; the SDK tag points to
`1aae3746682a65c837c5dd0f165b685253fe9465`. The local development checkout
contains later changes despite sharing the release version, so it was not used
as the port baseline. Code requires Python `>=3.12,<4` and SDK exactly 0.7.13.

| Artifact | SHA-256 |
|---|---|
| Code wheel | `4f8ff4033abeb59511292ea574005ad25d001eed7a001f7370c076cdc3a15d19` |
| Code sdist | `320491fd11ade2241270cf0925318eac9d1f5a01999ffc9fb2f143a8625d9e5b` |
| SDK wheel | `d717ee8ee092a91c475a6124ed578d5e4154d54120769a1081bfe1aece87c248` |
| SDK sdist | `99fc1855b1387c1c6e6035ba3600edbd933f59c39a4863ef3a7d5a0272ea67ab` |

**Ported changes:** lazy summarization-model selection; default general-purpose
forks; task error middleware; runtime grader model selection with strict
resolution, state schema, and message/state preparation. Server graph execution
now requires a durable thread/workspace binding and uses a 32-entry runtime
cache. The offload adapter follows that workspace runtime; model/provider
classifier settings and all three middleware targets survive the server path.

**Seam migration:** retain upstream's default fork with no-injection parity.
Main injections are inherited by reference. Unique child `first` entries follow
the inherited main block; upstream child overrides retain parent positions.
Fresh subagents preserve the previous scope/order contract. Document
`DEEPAGENTS_CODE_FORKED_SUBAGENTS=false` for fresh general-purpose delegation.
Reject child injections colliding with inherited parent names, and reserve the
SDK's `_ForkTaskToolMiddleware` name. Fresh and fork tests inspect each mode
separately, including the final fork compilation.

**Tripwire maintenance:** `tests/test_parity.py` needed instrumentation changes.
Its former `.with_config` spy missed graph copies, so it now checks the returned
graph's effective configuration using a minimal real compiled graph. Subagent
mode and class-valued grader schemas are visible; schema fingerprints preserve
qualified identity and declared annotations without walking generated Pydantic
internals. Depth stays 9 and the rich-case truncation guard passes. Negative
controls cover recursion, fork mode, schema, and existing constructor drift;
no factory-specific comparison exemption was added. The summarization-model
configuration joins the parity matrix.

**Guard re-checks:** SDK main core/tail/profile merging is unchanged; fork
inheritance adds a separate merge. LangChain 1.4.0 preserves before/after hook
direction and wrapper ordering; its factory diff changes one model-to-tools
routing condition. The environment denylist, first-write-wins dotenv behavior,
factory reservation before upstream use, private server working directory, and
scaffold global rebind still hold. Test isolation now establishes profile paths
before collection and replaces each test's immutable path snapshot, since
changing HOME alone no longer isolates Code's launch-time paths. Model and
configuration-service caches are cleared on both profile transitions; a
two-profile probe confirmed cache invalidation and reproduced stale settings
when that reset was disabled.

**Live fixture migration:** upstream's deterministic marker model scans the
entire conversation. In a fork it sees the parent's delegation marker again,
requests recursive delegation, and stops when the runtime refuses it. A local
test-only adapter scopes marker dispatch to the latest human task and its tool
results; real runtime history remains intact. Positive delegation is tested in
both fresh mode with the upstream fake and default fork mode with the adapter,
asserting exact file contents. The no-delegation negative control remains on
the default fork configuration.

**Verification:** Python 3.12.12; exact installed release pair; LangChain 1.4.0.
Final default suite **187 passed, 7 deselected** (including parity **29**, seam
**94**, server/launch **53**, and boundary/smoke **8**). Final live suite
**7 passed, 187 deselected**, covering fresh and forked delegation, ordinary
execution/persistence, transport, and startup failures. A separate intentional
recursion-limit mutation fails parity specifically on `graph_config` (1 instead
of 42), proving the corrected tripwire. Source and contract review findings
were resolved before the final runs. `uv build --offline` produced the wheel
and sdist; `git diff --check` is clean.

**Effort:** approximately 36 minutes from artifact verification starting at
2026-09-06 04:34:41 UTC through final verification and packaging, including
the fork contract migration, fixture repair, and independent review.

**Watches:** upstream fork mode remains beta; inherited middleware instances
must not keep unkeyed per-agent mutable state. Third-party registered harness
profiles remain outside the static reserved-name set. D4 still assumes the
factory loads first, and factory-reference resolution still runs on the server
event loop. Real-provider quality, a live extension-enabled server, and a live
HTTP offload session are outside this bump's validation.

---

## 2026-08-28 — 0.1.54 → 0.1.64 (SDK 0.7.5 → 0.7.10) — `eaf8a12`

**Verdict: re-application owed across every coupled runtime surface.** This was
not a lock-only bump: 905 additions / 270 deletions landed across upstream's
ported agent, server graph, launcher, and server-manager files, before their
transitive private dependencies are counted. All ten factory `# SEAM` sites
were re-applied against the released assembly.

**Delta-read method:** exact PyPI wheel and sdist hashes matched PyPI metadata;
the SDK tag, sdist, and wheel package sources were byte-identical. The code tag,
sdist, and wheel package sources were byte-identical apart from the expected
generated `_build_info.py`; its `BUILD_COMMIT="d8686f7"` identifies release tag
commit `d8686f74df7b8f838ec6679086c6291e3096a39e`. The coupled SDK tag is
`d26b53ad12265375c6dcf6b1c1501e0a4adf0ea3`. Artifact metadata requires Python
`>=3.12,<4` and exactly `deepagents==0.7.10`.

**Re-applied upstream runtime changes:** credential snapshots replace the old
settings surface; model creation publishes to `RuntimeState`; model allow-list
policy and `CodeModelRetryMiddleware` now cover main, nested, criteria, and
grader paths; interpreter settings are resolver snapshots; `BaseStore` and
recursion behavior thread through compilation. The server now caches a named
`ServerRuntime` containing graph, backend, and server-owned offload operation,
and owns Python-extension loading, host binding, and shutdown.

**Factory-specific reconciliation:** all external imports remain behind
`upstream.py`; all three middleware targets remain reachable through the one D4
reserved variable. Main `last` moved after extension middleware/runtime hosting,
and a new guard rejects extension replacement of caller middleware in every
main phase. Retry became the verification-tail anchor and sits inside subagent
and grader stacks without changing their documented defaults. The factory
reuses upstream's offload app through a narrow runtime rebind and adds the
authenticated custom HTTP block structurally to its generated workspace.

**Tripwire maintenance:** `tests/test_parity.py` required one instrumentation
change, `_MAX_DEPTH` 6 → 9, because 0.1.64 nests model metadata three levels
deeper. The rich repository case proves no state is truncated at 9; no parity
expectation or factory exemption changed. Seam coverage added extension
collision and final-position cases. D4 subprocess probes moved from removed
`settings.reload_from_environment` to `credentials.reload_from_environment`.

**Guard re-checks:** dotenv loading remains first-write-wins for an existing
`os.environ` key; `lc_factory/__init__.py` still reserves before boundary use;
the generated checkpointer and `langgraph.json` cannot pre-import upstream or
load an env file; `_build_server_env` remains an explicit denylist and relays
`LC_FACTORY_MIDDLEWARE`; the TUI still resolves `_scaffold_workspace` through
the rebound module global. SDK core/tail/profile names and subagent merge rules
remain covered by `tests/test_seam.py`.

**Verification and review:** Python **3.12.12** with the exact installed pair;
boundary/smoke **8**; initial focused boundary, smoke, assembly, parity, seam,
D4/server, and launch **165**. The integrated gate found one
`confirmed-in-scope` test gap: the cancel route did not prove that positive
`/offload` execution reaches the factory runtime. A focused ASGI test now runs
the real upstream operation route through the adapter-bound runtime and
server-owned offload operation; the launch suite is **9 passed**. Final default
is **167 passed, 6 deselected** and live integration is **6 passed**. The first
integration run exposed the new inherited `DEEPAGENTS_HOME` input, so its
hermetic filter now strips the whole `DEEPAGENTS_` prefix; all six then passed.
The documented shell allow-list drift injection produced the expected **2
failed / 23 passed**, and clean parity returned to **25 passed**. `uv build`
produces both sdist and wheel, and `git diff --check` is clean. The gate passes
with two explicit residuals: no real LangGraph Server offload session and no
live extension-enabled server session in this maintenance wave.

**Effort:** approximately one hour through the initial full green run; final
review/verification time is included in this maintenance wave rather than split
into feature work.

**Watches carried forward:** third-party harness profiles registered after
import remain outside the static reserved-name set; D4 still assumes
`lc_factory` is first to touch upstream; resolving the factory reference still
runs on the server event loop; the grader test still observes the private
`ReliableRubricMiddleware._grader_middleware` attribute.

---

## 2026-08-10 — 0.1.52 → 0.1.54 (SDK 0.7.1 → 0.7.5) — `cb43e40`

**Verdict: free. Zero port changes.**

Run deliberately before Group 3 Entry, following the recorded practice of
bumping while the port is line-faithful so any breakage is unambiguously
upstream's change rather than our divergence.

**Delta-read method — new, and the durable part of this entry.** Instead of
downloading the sdist and reading hunk ranges, the read ran off a local
monorepo clone and compared **blob hashes** between the `deepagents-code==0.1.52`
and `==0.1.54` tags, then cross-checked each file byte-identical to the
installed wheel. That upgrades "the changed lines look like they missed our
region" into "the file is provably unchanged." Cheaper than the sdist
procedure and strictly stronger. Prefer it whenever a monorepo clone is
available; fall back to the sdist diff otherwise.

**Ported / coupled files — all byte-identical between tags:** `agent.py`,
`server_graph.py`, `client/launch/server_manager.py`,
`client/launch/server.py`, SDK `graph.py`. SDK `profiles/` untouched, so the
harness-profile middleware name union is unchanged and
`_SDK_RESERVED_MIDDLEWARE_NAMES` still covers it.

**Changed upstream, all consumed as library through the boundary:**

| Module | Δ | What |
|---|---|---|
| `config.py` | +180/−9 | MCP shutdown-race log filtering, terminal trace metadata |
| `cost_tracking.py` | +690/−3 | pricing coverage, local fallback overrides |
| `hooks/server_middleware.py` | +282/−48 | `PostToolUseFailure` routing, no post-tool hook replay |
| `config_manifest.py` | +43/−1 | — |
| `offload_middleware.py` | +4/−2 | archive routing preserved |
| SDK `middleware/filesystem.py` | +285 | delete-permission semantics |
| SDK `backends/protocol.py` | +23 | new `ExecuteArtifact` on `ToolMessage.artifact` |

Transitive: `langgraph-checkpoint-sqlite` 3.1.0 → 3.1.1. No symbol named by
`upstream.py` moved.

**Guard re-checks:** D4 #1 verified in source — `config.py:392` still reads
`if value is None or key in os.environ: continue`, so the reservation guard's
mechanism holds. #2 and #3 unchanged: no upstream import moved ahead of the
reservation, and no new entry point imports `deepagents_code` first.

**Verification:** boundary 5, smoke 3, parity **25/25 with the suite
unmodified**, seam 53, full default **124**, integration **4**. Drift injection
(shell allow-list widening) still reddens parity — 2 failed / 23 passed.

**Review gate:** none. Judged below threshold: zero source composition delta,
and the verification battery is stronger evidence than reviewing two docstring
diffs. Recorded as a deliberate call, not skipped debt.

**Watch, not yet live:** SDK `filesystem.py`'s delete-permission work is gated
on `self._permissions`, and dcode still passes no filesystem `permissions`
(`agent.py` unchanged), so `assembly.py`'s `fs_tools` NOTE about threading
`_permissions` stays accurate — but it is one upstream decision away from
mattering. Re-read it next bump.

---

## 2026-08-06 — 0.1.48 → 0.1.52 (SDK 0.7.0b2 → 0.7.1) — `304f8a8`

**Verdict: re-application owed. 7 hunks in the ported region. The real test of D3.**

Three upstream arcs landed in the ported region at once:

- **Hooks v2 GA** — `ServerHooksMiddleware` on the main and nested subagent
  stacks, plus the **HITL restructure**: `interrupt_on` to the SDK is now
  always `{}` and the approval middleware is appended in-stack, so
  `PreToolUse` resolves before approval routing.
- **Session cost tracking** — `CostTrackingMiddleware` on both stacks.
- **Auto classifier configuration** — `auto_classifier_model` threaded
  end-to-end: new `ServerConfig` field → `_make_graph` → assembly.

Plus `_make_graph`'s settings bootstrap moving off the event loop via
`asyncio.to_thread`. The import boundary grew 4 symbols.

**Stack-shape fallout:** compaction was renamed to `SummarizationMiddleware`
and now hoists into the SDK core's summarization slot by name-based merge; and
HITL left the SDK tail, so the seam's `last` phase no longer precedes the
approval gate. Two seam tests re-pinned and the seam docs updated.
**`tests/test_parity.py` passed unmodified** — the delta stayed opt-in.

**Verification:** boundary 5, parity **24/24 unmodified**, seam 52, full 119,
integration 4; drift injection still red. `_apply_custom_middleware`,
`_build_server_env` denylist, `apply_dotenv` skip-if-present, scaffold rebind
seam, and private-`mkdtemp` `work_dir` all re-verified.

**Review gate:** reviewer subagent, `pass-with-notes`. Finding 1 fixed
pre-commit; finding 2 became `G2-TRANSPORT-EVENTLOOP-WATCH`; finding 3 noted.

**What it said about D3:** about an hour, end to end, for a five-release
three-arc delta that touched composition directly — with the parity suite
proving the result rather than hope. That is the number the recomposition bet
should be judged on, and it is favorable.

---

## 2026-07-27 — 0.1.47 → 0.1.48 (SDK 0.7.0b2 held) — `45c40bd`

**Verdict: free. Zero port changes.**

Run deliberately at the Group 1 checkpoint, while the port was still fully
line-faithful — the cheapest possible conditions, chosen so the machinery
built in Group 1 got exercised for real before any delta existed.

`agent.py` did change, but only in the agent-directory discovery helpers
(~lines 1105-1271), outside the ported `create_cli_agent` region (~2155-2989).
`auto_mode.py` changed classifier failure-message wording. `server_graph.py`
and `server_manager.py` were byte-identical. `deepagents` stayed at 0.7.0b2.

**Verification:** boundary tripwire green (all 90 runtime + 13 type-only
symbols resolve), parity green across the matrix, 38 unit + 1 integration
green, `lc-code` reports 0.1.48. Drift injection re-run to confirm the suite
still fails when it should.

**Free capability:** upstream migrated legacy hooks to v2 events (#4971) —
which had been on the future-group watch list as "would arrive free if
upstream wires it." It did, retiring that item and removing hook-shaped
capabilities from the delta backlog.

**Honest caveat recorded at the time:** this bump was easy in a way a *fork*
would also have found easy — upstream's churn simply did not overlap our
region. It proved the machinery answers the question fast, not that
recomposition is cheaper. That had to wait for 0.1.52.

---

## Adding an entry

Append a new dated section at the top, add a row to the summary table, and
keep these fields so bumps stay comparable:

- **Verdict** — free, or re-application owed (with hunk count).
- **Delta-read method** — and whether "free" was *proven* (blob identity) or
  *inferred* (hunk ranges missed the region). These are not equally strong.
- **What changed** in the ported region, and what was inherited free.
- **Guard re-checks** — the three D4 checks, the SDK merge semantics, the
  `_build_server_env` denylist, the scaffold rebind seam.
- **Verification** — suite counts, and explicitly whether `test_parity.py`
  needed editing. If it did, a delta stopped being opt-in; that is a finding,
  not a chore.
- **Effort**, wall-clock. This is the D3 measurement; do not omit it.
- **Watches** carried forward.
