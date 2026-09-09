# Running the ICS Product Companion

There is no build and no install step. Both connectors are self-contained PEP 723 scripts run
through `uv run --script`, and `mcp` is pinned to `1.28.1` to match the runtime's own pin.

Three things stand between a fresh clone and a working session, and the second one is easy to
miss because nothing fails loudly when you skip it.

## 1. Prerequisites

- `ddt-agent` on the path (`lc_factory`)
- `uv` — the connectors resolve their own dependencies through it
- Network access to `qa.muse.merck.com`, `eutils.ncbi.nlm.nih.gov` and `www.ebi.ac.uk`

## 2. Repoint `.mcp.json` at your clone

**Do this before the first launch.** Both connector entries carry an absolute path:

```json
"cd '/absolute/path/to/product-companion' && exec /opt/homebrew/bin/uv run --script tools/ics_muse/server.py"
```

Edit both `mcpServers.*.args` to wherever you cloned, and the `uv` path if yours differs.

The absolute path is deliberate, not laziness. This host **silently ignores a top-level `cwd`
key**, so a stdio server inherits the working directory of whatever launched it. The TUI client's
cwd is the workspace, but the langgraph server that actually builds the agent graph runs in a
throwaway scaffold directory. A relative path therefore succeeds during the client's discovery
pass — which is what `/tools` and the approval prompt report — and fails in the server, leaving
the tools **visible but not callable**.

If the path points at a *different* workspace that happens to exist, nothing fails at all: the
connectors run that workspace's code against its scope config, and the session looks healthy.

## 3. Capture a MUSE SESSION cookie

MUSE QA authenticates these calls with a browser `SESSION` cookie. Full procedure in
[`auth/README.md`](auth/README.md); the short form, from the repository root:

```sh
python3 auth/muse_session.py capture   # opens a small window; paste the cookie value
python3 auth/muse_session.py check     # expect: ACTIVE
```

The cookie is stored outside the repository at
`$XDG_CONFIG_HOME/ics-companion/secrets/muse-qa-session`, mode `0600`. Neither the utility nor
the connector ever prints it.

It expires after about three hours of inactivity. Keep it warm in another terminal:

```sh
python3 auth/muse_session.py serve     # one read-only GET every 30 minutes
```

Keepalive can preserve an active session; it cannot renew an expired one. On expiry, `capture`
again.

## Launch

```sh
cd /path/to/product-companion
unset LC_FACTORY_MIDDLEWARE
ddt-agent --trust-project-mcp
```

The working directory **is** the companion. Launch from the repository root — the runtime
resolves `project_root` from the git root, and project-scoped skill and subagent discovery
depends on it.

Inside the TUI: `/auth` and `/model` if needed. Keep approval mode on **Manual**.

### `--trust-project-mcp` is required, not optional

Project `.mcp.json` servers are trust-gated. Without the flag the agent loads **zero** of them.

The in-TUI approval prompt is not a substitute: it pins approval to a **sha256 of `.mcp.json`**,
so every edit to the connector config silently invalidates it and the next launch drops back to
no connectors. The flag is a launch-time decision with nothing to invalidate.

### Do not trust `/tools` to confirm the connectors arrived

`/tools` is rendered by the *client* process from its own MCP discovery. The agent is composed in
a separate server subprocess that discovers independently. Usually they agree, but they are two
results and not one — this workspace has already hit a session where `/tools` listed every
connector tool while the agent could call none of them.

Confirm from the agent side. Ask it to call a small read-only tool and see whether it routes:

```text
ics-muse_muse_population   sources: ["scited"]   distribution: false
```

`scited` is the smallest programme population of the eight sources, so that is the cheapest real
MUSE call available. Without a live SESSION cookie it answers with retrieval reported
unavailable — never an empty result, which is itself the thing worth seeing.

## Verifying

The offline suite is the main check. It needs no network and no cookie:

```sh
./tools/contract_map_check.py
```

It runs eight harnesses and asserts each reported **exactly** its pinned assertion total, so a
harness that silently stops running fails the suite rather than shrinking it quietly. Its own
checks are the ninth contributor to the count. Current total: **1,135**.

Individual harnesses, if you want one in isolation:

```sh
./tools/startup_cache_check.py       # startup cache, scope loading, every guard sabotaged
./tools/request_build_check.py       # request construction
./tools/response_assembly_check.py   # what a result may and may not claim
./tools/handle_check.py              # population handles
./tools/document_read_check.py       # muse_read
./tools/external_contract_check.py   ./tools/external_behavior_check.py
./tools/external_adapter_check.py    # ics-public-science, network-free
```

`tools/muse_contract_check.py` is deliberately **not** in the suite — it is a live check and
needs an active SESSION cookie.

Exercise a connector directly over stdio, outside the agent:

```sh
python3 tools/mcp_probe.py tools/ics_public_science/server.py --list
python3 tools/mcp_probe.py tools/ics_public_science/server.py \
  literature_search \
  '{"source":"pubmed","query":"\"DLL3 T-cell engager\"","target":"title_abstract","limit":3}'
ICS_PROGRAM=MK-6070 python3 tools/mcp_probe.py tools/ics_muse/server.py \
  muse_population '{"sources":["scited"],"distribution":false}'
```

The auth utility has its own tests:

```sh
python3 auth/test_muse_session.py
```

## Optional configuration

`.mcp.json` sets none of these, so by default the external connector sends no contact identity,
no email and no API key, uses the OS trust store, and applies the built-in disclosure policy.
Anything configured here is **sent to a public provider** and must itself be public-safe.

| Variable | Purpose |
| --- | --- |
| `ICS_EXTERNAL_CONTACT` | Public contact identity in both providers' User-Agent; also the fallback NCBI email |
| `ICS_NCBI_TOOL` | Public NCBI E-utilities tool name; defaults to `ics-program-companion` |
| `ICS_NCBI_EMAIL` | Public email sent to NCBI; overrides `ICS_EXTERNAL_CONTACT` for that parameter |
| `ICS_NCBI_API_KEY` | Optional API key sent only to NCBI |
| `ICS_CA_BUNDLE` | Local trusted CA bundle for both providers; verification is never disabled |
| `ICS_DISCLOSURE_DENY_FILE` | Local file of additional newline-separated deny regexes |
| `ICS_DISCLOSURE_ALLOW_TERMS` | Local newline-separated public-term exceptions |

MUSE connector variables, set in `.mcp.json`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ICS_PROGRAM` | `MK-6070` | The active programme. One programme per workspace |
| `ICS_MUSE_BASE_URL` | `https://qa.muse.merck.com` | |
| `ICS_MUSE_CALL_BUDGET` | `500` | Max upstream calls per **server lifetime**, shared by the companion and every subagent |
| `ICS_MUSE_DEADLINE_S` | `900` | Wall-clock bound on **one tool call** |
| `ICS_MUSE_SESSION_FILE` | `$XDG_CONFIG_HOME/ics-companion/secrets/muse-qa-session` | |
| `ICS_PROGRAM_SCOPE_DIR` | `config/program-scope` | Overridden by the harnesses to a pinned fixture |

## Troubleshooting

**HTTP 401 from MUSE.** The SESSION expired or was never captured. `python3
auth/muse_session.py check` distinguishes the two. Note this is an *authentication failure*, not
an empty result — the connector says so explicitly rather than returning zero records.

**Do not use `/version` or `/health` to test auth.** Both return 200 with a garbage cookie *and*
with no cookie at all. The only valid probe is `GET /resources/v1/data-updates`, which is what
`check` uses.

**Tools visible but not callable.** The `.mcp.json` absolute paths, almost always — see step 2.

**Tools absent entirely.** `--trust-project-mcp` was omitted, or `.mcp.json` was edited after an
in-TUI approval.

**TLS failures.** Corporate interception re-signs several of these hosts. The MUSE connector uses
the OS trust store via `truststore` and reports `tls_verification_failure`; the external
connector uses the configured or default verified trust path and reports `tls_failure`. Neither
disables verification, and neither reports a false empty result. Point `ICS_CA_BUNDLE` at your
trusted bundle if the external connector cannot build a chain.

**`deadline_exceeded` with no outbound request.** `ICS_MUSE_DEADLINE_S` bounds one tool call, not
the process. Seeing it repeatedly without any HTTP traffic means the deadline was not reset at an
entry point — a connector defect, not a token problem. A cookie failure has its own distinct
`authentication_failure` reason, and the deadline check runs *before* the cookie is read.

## Before you commit

```sh
git add <files>            # stage first
./tools/precommit_scan.py  # its own step, never chained off git add
```

The scan reads staged added lines against eight patterns plus the live SESSION cookie value. It
**aborts** on an empty stage rather than reporting a pass, because a scan over nothing is the most
dangerous kind of green.
