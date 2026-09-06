# Upstream transfer review — 2026-09-06

**Implemented in this repository.** The review below was completed against the
refreshed upstream checkout, then the user authorized transferring the four
relevant updates together. Code and SDK now use exact Git revision
`6c89fe2197a2dfe4f3851cda38565bcadba6066b`; QuickJS is the published **0.3.7**
release. The constructor and server ports are updated, preserving all three
middleware injection targets. See [the implementation record](VERSION_BUMPS.md).

The assessment details below distinguish what was known during review from
what was subsequently changed and verified.

## Compared versions

- Factory baseline: commit `7bade13`, Code **0.1.66**, SDK **0.7.13**,
  QuickJS **0.3.4**.
- Upstream checkout: fast-forwarded from `34005a4ef` to
  `6c89fe2197a2dfe4f3851cda38565bcadba6066b`.
- Code release baseline: `3812967c84d90619848f3185f22470204fc88bd8`.
- SDK release baseline: `1aae3746682a65c837c5dd0f165b685253fe9465`.
- At review time, public PyPI metadata listed [Code 0.1.66](https://pypi.org/project/deepagents-code/0.1.66/)
  and [SDK 0.7.13](https://pypi.org/project/deepagents/0.7.13/) as the latest releases.
  [QuickJS 0.3.7](https://pypi.org/project/langchain-quickjs/0.3.7/) was published
  on September 6 at 03:07 UTC.

The checkout retaining the same Code/SDK version strings does not make its
post-release changes part of those published wheels. Pulling the reference
repository alone does not update installed dependencies. The implementation
therefore changed the project dependencies and regenerated the lockfile.

## Reviewed transfers (first four implemented)

| Priority | Change | Benefit | Transfer path |
|---|---|---|---|
| First independent update, when using the interpreter | QuickJS 0.3.4 → 0.3.7 | Separate interpreter state for parent/forked agents; native streaming of tools called from JavaScript; less middleware trace payload | Published dependency update within Code's existing `>=0.3.4,<0.4.0` range. Existing factory construction works without a rewrite. |
| High for a server hosting multiple workspaces | Workspace-scoped environment and credentials, [#5980](https://github.com/langchain-ai/deepagents/commit/678c11559d7534ee5e16ceebfacf01eeb6536ac7) | Prevents supported internal consumers from using another workspace's project settings, credentials, endpoints, or tracing configuration | Adopt a future Code release and port its constructor/server changes together, or build and pin a complete patched Code artifact if needed sooner. |
| High when a sandbox-enabled server can receive different workspaces | Sandbox ownership and runtime-cache fix, [#5979](https://github.com/langchain-ai/deepagents/commit/cb4c973172f7838eefa328ea0c6ca334b6aaab98) | Refuses cross-workspace use of a process-wide sandbox; reuses startup runtime for the launch workspace; reports conflicts/build failures through HTTP | Smaller cohesive backport, but still spans owned server code and reused workspace/HTTP modules. |
| Useful correctness fixes | SDK [empty edit-target rejection](https://github.com/langchain-ai/deepagents/commit/1281b04f7eafaf654982b99a3c93f72744208eda) and [blank read-window handling](https://github.com/langchain-ai/deepagents/commit/a892a0ee26d045256b6d3b0c224b3de663c83eb5) | Rejects ambiguous `edit_file(old_string="")`; avoids describing a file as empty when only its requested page contains blank lines | Future SDK package update or a pinned patched SDK. No factory constructor port needed. |
| Separate product work | Talon background agents, MCP/subagent reload, conversation history | Potential patterns for a continuously running work-agent service | These live in a different application package. They do not arrive through Code's constructor or the SDK dependency. |

The SDK delta contains **no new implementation** of its agent graph, subagent
coordination, memory, summarization, rubric verification, or harness profiles.
Code's `model_retry.py` and `reliable_rubric.py` are also unchanged. This range
does not establish new general long-horizon reasoning capabilities.

## Why the workspace fixes must move together

The environment-isolation change adds `environ`, `credentials_snapshot`, and
`model_result` to `create_cli_agent`. Those arguments rely on coordinated
changes across configuration, server construction, model switching, classifier,
grader, compaction, shell/sandbox creation, tools, and MCP interpolation. The
commit changes 23 files including tests. The published 0.1.66 helpers do not accept
all of these new contracts; the pinned source helpers now installed here do.

Each validated workspace receives an immutable environment snapshot. Supported
lazy model paths retain it, which matters when a long-running task compacts or
changes models later. Stored credentials remain paired with their stored
endpoint, and shell/tool environments stop relying on subsequent process-wide
dotenv changes. The factory's shell-only `LC_FACTORY_MIDDLEWARE` reservation
and all three injection targets must survive this migration.

This isolates supported configuration consumers; it does not isolate arbitrary
extensions that read `os.environ`, implement tenant authentication, or turn a
local shell into an OS sandbox. Its urgency is greatest when differently
configured workspaces share a server process.

The sandbox fix claims one workspace for the process lifetime, including after
a failed first build. It does not provision separate sandboxes per workspace.
Non-sandbox servers retain multiple-workspace support. It also combines the
startup and workspace runtime paths and serializes cold initialization, without
serializing all agent execution. Copying only the cache code would omit the
new HTTP conflict/error behavior.

## QuickJS compatibility check and limits

The published wheel was downloaded to a temporary directory, SHA-256 verified
against PyPI, and its Python sources compared with the release checkout:

`50f384b209f0f0f472c043024e2c74276900352591422df1aeae7a0fbcf8566f`

All its runtime requirements are satisfied by the current environment:
SDK 0.7.13, LangChain 1.4.0, LangChain Core 1.6.1, LangGraph 1.2.11,
quickjs-rs 0.2.5, and bsdiff4 1.2.6. No production packages were replaced during that initial probe; the subsequent
implementation installed the pinned Code/SDK source and QuickJS 0.3.7.

Two isolated execution probes passed:

1. The existing `create_factory_agent` compiled with the released QuickJS wheel
   and executed `js_eval("1 + 1")`, returning `<result>2</result>`.
2. Candidate QuickJS source with the installed SDK invoked an actual fork;
   parent and child received distinct private interpreter slot IDs. A separate
   probe confirmed that public state reaches a fork while `PrivateStateAttr`
   state is filtered by the compiled input schema.

These prove basic compatibility and interpreter-slot separation. They are not
a full package-upgrade regression run or proof of process isolation.

QuickJS 0.3.6 also introduced optional snapshot HMAC verification. **A dependency
bump alone does not enable it**: the factory currently supplies no
`snapshot_signing_key`. Enabling signing needs explicit configuration and
constructor wiring, or separately configured middleware. Native PTC streaming
also does not add per-tool approval enforcement; the existing PTC allowlist
remains relevant. See the [release changelog](https://github.com/langchain-ai/deepagents/blob/6c89fe2197a2dfe4f3851cda38565bcadba6066b/libs/partners/quickjs/CHANGELOG.md).

One inherited docstring was corrected during this review: fresh subagents do
not receive the built-in interpreter, but default forks already inherit main
middleware, including the interpreter when enabled. That is existing behavior.

## Original recommendation and implementation decision

1. Make QuickJS 0.3.7 a separate, targeted dependency update with interpreter
   execution/fork regression checks and the normal factory validation gates.
2. Plan the two workspace fixes as one coherent Code migration. Prefer the
   next published release; if needed sooner, pin a reviewed patched artifact
   or exact source revision and record that explicit unreleased baseline.
3. Acquire the SDK filesystem fixes through the SDK version required by that
   release, or through a separately reviewed patch when their urgency warrants it.

The user authorized implementing these together rather than waiting for another
release. `pyproject.toml` contains full PEP 508 Git references, and built factory
wheels retain those references. `uv` source overrides are disabled so ACP and
QuickJS remain published packages. The installed Code/SDK Python sources match
the pinned Git revision, and the source-provenance smoke assertions distinguish
this build from the older same-numbered releases.

Final validation: **200 default tests**, **7 live-server tests**, and **16 targeted
upstream filesystem regressions** passed. Added coverage includes concurrent
workspace snapshots, delayed model wrappers, actual shell environments, sandbox
ownership after failed initialization, unified startup caching, HTTP 409/503
before thread creation, and an actual factory fork with distinct interpreter
slots. The wheel and sdist build successfully; offline locked synchronization
succeeds from the populated cache.
