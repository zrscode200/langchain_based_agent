# Agent Workspace

A React and TypeScript client for `langchain_based_agent`, with a local Node
adapter and the existing Python/LangGraph agent backend. The TUI remains available.

The interface keeps conversation central. Tasks, files, agent definition and
memory open in a contextual panel; files can open beside the chat. No model API
keys are sent to the browser.

## Start locally

Requirements: the project's existing Python environment, Node 22.12+ and pnpm.
The frontend dependencies are pinned in `web/package.json` and `web/pnpm-lock.yaml`.
Install them once (follow your environment's dependency-install policy):

```sh
cd web
pnpm install --frozen-lockfile --ignore-scripts
pnpm build
```

Start the agent backend from the repository's installed Python environment:

```sh
python -m lc_factory.web_backend --cwd /absolute/path/to/your/project --port 2024
```

The optional `--model provider:model` flag chooses the startup model. Existing
factory/environment configuration supplies MCP, tools, delegation, runtime
options, model credentials and persistence. This launcher reuses the TUI's
server scaffold and process lifecycle, with Manual approvals and `ask_user`
enabled. It does not install or reconfigure the Python environment.

In another terminal, from `web/`:

```sh
pnpm start --workspace /absolute/path/to/your/project --backend http://127.0.0.1:2024
```

Open **http://127.0.0.1:3100**. Repeat `--workspace /absolute/path/to/another/project`
to offer multiple projects. The launcher canonicalizes these directories; the
browser cannot add arbitrary directories. Use `--port` for another frontend port.

If backend port 2024 is occupied, the backend launcher stops its own fallback
process and fails clearly; it never silently hands the frontend a different
server. Choose another backend port (for example `--port 2025`) and use the
matching `--backend http://127.0.0.1:2025` URL.

For frontend development, use `pnpm dev` with the same arguments. Vite runs
inside the same Node server, so API calls stay on the same origin. Reload after
edits; hot module replacement is deliberately disabled.

You can attach to an existing local factory server by specifying its origin.
The `web_api.py` adapter must be installed on that backend for catalog/skill
invocation. If needed, set `LC_WEB_API_KEY` in the **Node server's environment**;
it forwards the key only to that fixed backend. Remote hosting, reverse proxies,
multi-user access, and remote sandbox file browsing are not supported by this
local launcher.

## Everyday workflow

- Choose a project and start or resume a conversation. Search titles with
  **⌘/Ctrl K**. Rename or export a conversation from its `…` menu.
- Type a message; **Enter** sends and **Shift+Enter** adds a line. Typed drafts
  and the last selected conversation are remembered in this browser.
- Open **Agent definition** to discover skills and tools. **Use skill** attaches
  the real server-discovered skill to your next message. Its instructions and
  invocation metadata use the harness's existing format.
- **Tasks** shows the current plan, background jobs, results and pending input.
  Select a task for its retained transcript, tool activity and cancellation.
  **Guide via agent** prepares a message to the main agent; review and send it.
- Real approval requests display the action and arguments. Select decisions,
  then submit. Questions support text, single choice, multiple selections,
  custom choices and optional answers. Stale requests are refreshed, never
  silently replayed.
- **Project files** previews Markdown, code/text and inert HTML. Use **Reference
  in chat** to name a file in your message. The agent reads it through its own
  tools. The browser does not upload file contents or automatically grant edits.
- **Memory & notes** browses existing project files. It introduces no separate
  memory database or inferred graph.
- **Session settings** selects the next turn's model and the live Manual, Auto
  or YOLO approval mode. Mode changes affect running background agents at their
  next approval boundary, as in the TUI. The effective model and saved state are
  available in **Agent definition → Context**.
- **Compact conversation** in the `…` menu invokes the existing server-owned
  offload operation. It can call the configured summarization model. Cancellation
  is available; archives and retention remain backend policy.

## Capability coverage and boundaries

| Harness capability | Web behavior |
| --- | --- |
| Workspace isolation | Uses exact server-issued binding; verifies conversation ownership for every operation |
| Main conversation, reasoning and tool output | Streams root conversation; expands provider-exposed reasoning and tool arguments/results |
| Skills, instruction files and tools | Server catalog; explicit invocation; project guidance and state inspection |
| Shell, MCP, interpreter, subagents and verification middleware | Existing agent tools execute through the backend; calls and approvals appear in conversation |
| Manual / Auto / YOLO | Reads and writes live approval store; literal user text is tagged with upstream authorization metadata |
| Human questions | Upstream text, single-choice, multi-select and optional-answer format |
| Background jobs | Status, cancellation, approvals, paged transcript, findings and guidance delivery |
| Background outcome delivery | Delivered at the main agent's next normal turn; the Tasks panel can compose an explicit review request |
| Interrupted main turn | Explicit continuation from its saved checkpoint, preserving the original turn identity |
| Reconnect | Joins the existing resumable run, or refreshes saved state; never resubmits the original message automatically |
| Model switching / compaction | Next-turn model identifier; native server offload and cancellation |
| Session history | Uses the server thread catalog and checkpoints; browser stores selection and drafts only |
| Native command hooks / extension lifecycle UI | Requires the native TUI; unexpected hook interrupts remain blocked and are not simulated as approvals |
| Image uploads, terminal emulation, checkpoint editing/forking, command palette for every CLI maintenance command | Not included; retain the TUI/CLI for those workflows |

This client does not claim full native TUI parity. The adapter covers the core
agent-workspace workflows above. In particular, it does not run configured
client-side command hooks or automatically initiate the TUI's idle background
wake. Existing backend capabilities are not replaced with browser-owned state.

## Recovery and storage

The backend is authoritative. A browser disconnect leaves an already accepted
run on the server (`on_disconnect: continue`). **Reconnect** attaches to that
run using its event cursor when available. A server restart can lose replay
buffers and the in-memory thread catalog; checkpoint retention depends on
factory configuration. A listed conversation is not a promise of durable
cross-restart indexing.

Background transcripts are process-retained and capped/paged by the harness.
The viewer displays its retention notices. Browser drafts use local storage on
this machine; conversation export is an explicit JSON download.

Keep the conversation open while compacting if you need its cancellation
control. The browser does not recover an in-flight compaction operation after
navigation or reload; refresh the saved checkpoint before considering a retry.

## Local security boundary

The Node server listens on `127.0.0.1`. It validates the exact Host/Origin,
rejects cross-site requests, uses a per-process token for API access, and exposes
a narrow backend route allowlist. Backend credentials stay server-side. This is
a single-user local application, not an authentication system for public hosting.

File reads are restricted to configured roots, reject traversal and symlinks,
check the opened file identity, exclude common credential paths and bound reads
to 1 MiB and directory listings to 500 entries. The exclusion list is not a
secret scanner: choose workspace roots deliberately. The agent's own filesystem
permissions remain the harness's responsibility; this browser boundary does not
turn the host filesystem into an OS sandbox.

Markdown does not execute HTML. External Markdown images are withheld. HTML
previews use a sandboxed iframe with scripts, external resources, forms and
same-origin access disabled. Only the explicit preview shows HTML; chat, tools,
transcripts and source views render text.

## Verification

```sh
cd web
pnpm test
pnpm build
pnpm test:browser
```

Browser tests use installed Chrome by default (`PLAYWRIGHT_CHANNEL` can select
another installed channel). They serve built assets through Playwright request
fixtures and do not need a listening application server or a real model.
Build before running them. `tests/preview.ts` can generate a clearly labelled,
self-contained sample artifact for visual review; it is not connected to a model.

Python adapter/launcher tests:

```sh
python -m pytest tests/test_web_api.py
```

Development verification in the implementation session included TypeScript,
production build, Node protocol/security tests and in-process Python tests.
Live backend/model calls and browser visual/E2E checks were unavailable under
that session's permissions. Treat the browser suite as supplied checks awaiting
execution, not evidence that those checks passed.
