# Enterprise integration

This package adapts **OG 0.2.0**, using its constructor, server, verification,
subagents, delegation and MCP lifecycle. It owns the company gateway adapters and
DDT client behavior. Continue changing OG in the root package; enterprise no
longer carries a second factory copy. Nested distribution remains supported.

Reconstructed from `zrscode200-update` commit
`d57a0ea1d10ac53093e51c4011cddf075797eda4`, under
`untitled folder/src/lc_factory`. Its matching manifest, lock and hidden ICS
configuration were missing. These are new migration examples, not recovered
production settings. Real endpoint, corporate TLS/auth and complete ICS workspace
validation must still happen inside the company environment.

## Installation and profile

Use Python 3.12 or later and install matching OG/enterprise wheels into the same
environment. `ddt-agent` starts the enterprise client; `lc-code` starts OG. Both
use OG's graph and workspace isolation. Enterprise declares both distributions
in generated server metadata, including local source paths for editable use.

Merge `examples/config.toml` into the trusted profile's `config.toml` (selected
by `DEEPAGENTS_HOME`). Replace the `.invalid` endpoints, aliases, API model IDs
and context limits with approved values. Supply `COMPANY_LLM_API_KEY` through the
company secret/environment mechanism. Do not put credentials in TOML. The legacy
`lc_factory.merck_models:MerckChatModel` and `MerckAnthropicChatModel` class paths
remain available when enterprise is installed; new configurations can use
`lc_factory_enterprise.merck_models` directly.

Copy `examples/subagents.toml` into the consumer's `.deepagents/subagents.toml`
and merge named policies to preserve the uploaded isolated general-purpose child.
Invalid policies now fail startup/reload. Programmatic clients keep importing
`create_factory_agent` from `lc_factory.assembly`.

Start normally; structured delegation, reload and background tools are enabled
by default:

```sh
ddt-agent --model merck_claude:company-claude
```

Background tools are available by default, without a capability export. Native
`task` waits for its child; `start_background_task` explicitly detaches work so
the conversation can continue. Save `[lc_factory].capabilities` in the trusted
profile to add history or use an empty list to disable runtime capabilities.
`[lc_factory].settled_dispatch` and `.interpreter_subagents` separately control
structured and JavaScript delegation; both default to `true`.
`LC_FACTORY_CAPABILITIES` remains a complete override for the runtime list
(`none` disables background, reload and history).
See [runtime configuration](../TALON_ADAPTATIONS.md#use-with-lc-code).

Interpreter activation/PTC uses current upstream CLI/profile settings.
The trusted `[lc_factory].interpreter_subagents = false` preference (Python
`interpreter_subagents=False`), or the optional host-only
`LC_FACTORY_INTERPRETER_SUBAGENTS=0` override, withholds built-in JS task and the
task/task_settled PTC names. Native delegation remains available. Bare Python
constructors and direct server hosting retain explicit opt-in for settled dispatch.
Verification uses `[lc_factory].verification_model` or host
`LC_FACTORY_VERIFICATION_MODEL`; an explicit rubric model wins for grading.
See root VERIFICATION, SUBAGENTS, DELEGATION and MCP_RESOURCES guides.

## Automatic network activity

`ddt-agent` disables these four automatic activities for every enterprise launch:

- LangGraph CLI usage analytics sent to Supabase.
- TUI and LangGraph API version checks against PyPI.
- Automatic upstream package upgrades, including cached updates at startup.
- Background pricing-catalog downloads from GitHub. Cost estimation continues
  using bundled pricing or locally configured overrides.

No shell exports are needed. The launcher applies `LANGGRAPH_CLI_NO_ANALYTICS=1`,
`LANGGRAPH_NO_VERSION_CHECK=1`, `DEEPAGENTS_CODE_NO_UPDATE_CHECK=1`,
`DEEPAGENTS_CODE_AUTO_UPDATE=0` and `DEEPAGENTS_CODE_PRICES_AUTO_UPDATE=0` before
loading the upstream CLI and pins them in each server startup/restart environment.
Enterprise also disables the update gates and pricing-refresh starter, so saved
user/managed settings and runtime environment overrides cannot re-enable these
automatic paths. Upstream configuration commands may still display saved values;
the enterprise launch policy takes precedence. No profile files are rewritten.
Client adaptations restore on exit; `lc-code` keeps its existing behavior.

The enterprise graph/offload entry points also suppress pricing refresh. A
separately hosted server must set the two `LANGGRAPH_*` flags in its own launch
environment, since CLI analytics/version checks run before graph import.

This policy does not make the application offline. Model endpoints, MCP, network
tools, shell commands, hooks and extensions can still send data. LangSmith and
Datadog retain their own configuration, and explicit update/install commands or
optional tool provisioning retain their existing behavior. Validation covers
the installed upstream maintenance paths and child environment; it is not a
packet-level audit of every dependency or your company deployment.

## Preserved and changed behavior

- Credentials are captured from the active workspace at construction. Explicit
  empty/missing keys fail locally. `base_url` maps to `api_root`; conflicting
  values fail. `api_model` selects the URL, while `model` stays the body alias.
- OG owns retries. Provider retry counters default to zero; the example declares
  the retry parameter so host settings cannot multiply loops. Standalone callers
  may explicitly set adapter `max_retries`. Its budget covers backoff only;
  network request time is additional.
- 408/429/5xx and connection failures may reach host retries. Read timeouts and
  interrupted accepted streams are authoritatively nonretryable, including their
  exception cause chains. There is no independent SSE reopen loop. HTTP response
  metadata is retained for status/Retry-After. GPT uses a blocking request with
  LangChain async/stream fallback; Claude supports native SSE.
- Claude emits standard reasoning blocks through the server/client/archive path;
  replay strips reasoning from gateway requests. Provider-native response_format
  is unsupported: use ToolStrategy, as OG verification and declared subagents do.
  No control-byte sentinels, global message-class patch or old injection repair.
  GPT translates required tool selection to the OpenAI wire vocabulary. Claude
  disables thinking for a request that forces a tool (including ToolStrategy),
  preserving the structured-result requirement; ordinary calls retain reasoning.
- The client selects the nearest Git or `.deepagents` boundary, excluding the
  home configuration directory. OG persists this workspace binding. Replay rows
  retain logical IDs and elapsed time; foreground eval completion affects only
  that eval's rows. Client branding/panel rebinds restore on CLI exit.

The enterprise graph and offload entry points install that same workspace-root
policy in the server before delegating to OG. Configure hosted enterprise
services with `lc_factory_enterprise.server_graph:make_graph` and
`lc_factory_enterprise.offload_api:app`. These are thin proxies, not separate
runtime implementations. A dedicated enterprise server applies this root policy
to all its workspaces; OG's own entry points retain the default Git-root policy.

Background jobs are process-local and stop at shutdown. Reload retains old MCP
bindings, capped at eight MCP generations. New connector processes reset their
process-local budgets/cache, including MUSE's; this is not shared budget accounting.
The ICS consumer stays separate. Restore its missing `.deepagents` and `.mcp.json`
from company-owned sources before enabling it.

## Internal distribution

The root `uv.lock` is the reviewed dependency closure, including requests 2.34.2;
enterprise adds no third-party dependency beyond it. Build both Deep Agents
packages from source `6c89fe2197a2dfe4f3851cda38565bcadba6066b`, not merely matching
nominal versions 0.1.66/0.7.13. Root metadata retains these Git provenance pins.

In an approved build environment, build the two local packages and all locked
dependencies into a platform-specific wheelhouse. Record source commits, wheel
SHA-256 hashes and the root lock together, then transfer internally. A complete
approved wheelhouse can be installed without resolving GitHub dependencies:

```sh
python -m pip install --no-index --no-deps /approved/wheelhouse/*.whl
python -m pip check
ddt-agent --version
```

The generated server uses the installed environment. Company services can host
OG's graph and these adapters without LangSmith hosting. This repository does
not provision AWS, certify production deployment, or supply a complete offline
wheelhouse for every target platform.
