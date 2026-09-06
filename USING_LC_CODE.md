# Using `lc-code`

`lc-code` launches the Deep Agents Code terminal UI against
`lc_factory.create_factory_agent`.

It is the user-facing entry point for:

- working with the factory agent in the shipped Deep Agents TUI;
- checking that the Group 1 factory reconstruction still behaves like the
  original `create_cli_agent`; and
- optionally exercising the Group 2 middleware-injection seam.

## First time using Deep Agents? Start here

You do **not** need to install, launch, or learn `dcode` first. `uv sync`
installs this project's pinned `deepagents-code` dependency, and `lc-code`
launches that dependency's normal TUI against the factory constructor.

The executable is named **`lc-code`**, with a hyphen. There is no `lc_code`
shell command.

The commands in this guide use a POSIX shell and work on macOS, Linux, and
Windows through WSL.

### 1. Check the prerequisites

You need:

- Git;
- a terminal;
- `uv`; and
- a credential for a model provider supported by Deep Agents Code.

Check whether `uv` is installed:

```sh
uv --version
```

If the command is unavailable, install `uv` on macOS or Linux:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart the terminal after installation. Other installation methods, including
Windows PowerShell and Homebrew, are listed in the
[official `uv` installation guide](https://docs.astral.sh/uv/getting-started/installation/).

### 2. Clone and install `lc_factory`

```sh
git clone https://github.com/zrscode200/langchain_based_agent.git
cd langchain_based_agent
uv sync
FACTORY_REPO="$PWD"
```

`uv sync` creates `.venv` and installs `lc-code`, the pinned Deep Agents Code
TUI, and the rest of the project dependencies.

Confirm the command is available:

```sh
"$FACTORY_REPO/.venv/bin/lc-code" --version
```

The output says `deepagents-code`. That is expected: `lc-code` deliberately
reuses the shipped Deep Agents client.

### 3. Launch from a safe practice workspace

The agent works in the directory where you launch it. For a first test, use a
disposable directory rather than this source repository:

```sh
mkdir -p /tmp/lc-factory-first-run
cd /tmp/lc-factory-first-run
unset LC_FACTORY_MIDDLEWARE
"$FACTORY_REPO/.venv/bin/lc-code"
```

`unset LC_FACTORY_MIDDLEWARE` selects the no-injection Group 1 baseline. It is
safe even if you have never configured middleware.

### 4. Configure a model inside the TUI

After the TUI opens:

1. Enter `/auth`, choose your model provider, and follow the credential prompt.
2. Enter `/model` and select a model available from that provider.
3. Check the status bar. For this test, the approval mode should say
   **Manual**. Press Shift+Tab or Ctrl+T until it does.
4. Enter this read-only prompt:

   ```text
   Reply with exactly: lc-code connected
   ```

Receiving the reply confirms that the TUI, server, model, and factory-backed
agent connected successfully.

### 5. Test approval and rejection

Enter:

```text
Create first-run.txt containing exactly factory tui ok, then read it back and
confirm its contents.
```

When the write approval appears, press `y`. The agent should create and read
the file.

Next enter:

```text
Replace first-run.txt with exactly this should not land.
```

Press `n` when the replacement is proposed. Then ask the agent to read the file
without changing it. It should still contain `factory tui ok`.

Enter `/quit` to leave the TUI. You have now exercised `lc-code` through the
same Deep Agents TUI surface as `dcode`, including one approved action and one
rejected action. The detailed baseline procedure and pass criteria appear later
in this guide.

## What changes, and what stays the same

`lc-code` keeps the upstream client and replaces the graph construction behind
it:

| Command | Client | Server graph | Agent constructor |
| --- | --- | --- | --- |
| `dcode` | Deep Agents Code TUI | upstream graph | `create_cli_agent` |
| `lc-code` | the same Deep Agents Code TUI | `lc_factory.server_graph` | `create_factory_agent` |

The TUI, approval screens, slash commands, thread storage, streaming protocol,
and resume behavior are upstream Deep Agents Code behavior. It is therefore
normal for `lc-code --help`, the startup banner, or other UI text to say
`deepagents-code` or show `dcode` examples.

The working directory where you launch the command is the workspace the agent
sees. Do not launch a write-capable dogfood task from the `lc_factory` source
repository unless you intend to let the agent modify that repository.

## Install the project

From a clone of this repository:

```sh
cd /absolute/path/to/langchain_based_agent
uv sync
```

The console script is then available inside the project virtual environment:

```sh
/absolute/path/to/langchain_based_agent/.venv/bin/lc-code --help
```

You can also activate the environment:

```sh
source /absolute/path/to/langchain_based_agent/.venv/bin/activate
lc-code --help
```

The examples below use `FACTORY_REPO` to keep the commands readable. Set it to
your clone:

```sh
FACTORY_REPO=/absolute/path/to/langchain_based_agent
```

## Start an interactive session

Move to the repository or directory where you want the agent to work, then
launch `lc-code`:

```sh
cd /absolute/path/to/your/workspace
"$FACTORY_REPO/.venv/bin/lc-code"
```

To select a model at launch:

```sh
"$FACTORY_REPO/.venv/bin/lc-code" -M "provider:model"
```

To submit an initial prompt as soon as the TUI starts:

```sh
"$FACTORY_REPO/.venv/bin/lc-code" \
  -m "Read this repository and explain its architecture."
```

### First-time model setup

If no usable model or credential is configured:

1. Enter `/auth` to add or manage a provider credential.
2. Enter `/model` to select a model.
3. Submit a small read-only prompt to confirm the model responds.

Credentials, model preferences, and threads use the normal Deep Agents Code
state. `dcode` and `lc-code` are not isolated profiles.

## Use the TUI

Type a prompt and press Enter. Enter `/help` for the complete command and
keyboard reference provided by the pinned Deep Agents Code version.

Useful slash commands include:

| Command | Purpose |
| --- | --- |
| `/auth` | Manage provider and service credentials |
| `/model` | Select a model or edit its settings |
| `/tools` | Show tools available to the agent |
| `/goal` | Set or manage a persistent objective and acceptance criteria |
| `/rubric` | Set explicit grading criteria |
| `/threads` | Browse and resume previous threads |
| `/clear` | Start a new thread with a clear transcript |
| `/restart` | Restart the agent server for the current client session |
| `/version` | Show the installed Deep Agents Code and SDK versions |
| `/quit` | Exit the TUI |

Useful global keys include:

| Key | Action |
| --- | --- |
| Escape | Interrupt active work |
| Ctrl+C | Interrupt, or quit when nothing is running |
| Ctrl+D | Quit |
| Ctrl+O | Expand or collapse recent tool details |
| Shift+Tab or Ctrl+T | Cycle the approval mode |

### Approval modes

The normal default is **Manual**. Confirm the mode in the TUI status bar before
a safety-sensitive run.

- **Manual** presents gated actions for your decision.
- **Auto** uses the upstream classifier-backed approval policy.
- **YOLO** runs gated actions without review after its warning and
  acknowledgement.

Shift+Tab or Ctrl+T normally cycles Manual → Auto → YOLO → Manual. Configuration
or a remote sandbox can remove a mode from that cycle.

When an approval menu is visible:

| Key | Action |
| --- | --- |
| `y` | Approve |
| `n` | Reject |
| Up/Down or `j`/`k`, then Enter | Select an option |
| `e` | Expand the proposed command or arguments |
| Tab | Enter an optional rejection reason |

Do not launch with `-y`/`--auto-approve` or `--yolo` when testing the manual
approval path.

## Goals, rubrics, and resume

Start an interactive goal flow with:

```sh
"$FACTORY_REPO/.venv/bin/lc-code" \
  --goal "Create dogfood.txt with the required content and verify it."
```

The TUI drafts acceptance criteria and asks you to review them before it runs
the goal. `--goal` is interactive and cannot be combined with
`-n`/`--non-interactive`, `-m`/`--message`, or rubric flags.

Within an existing session, use `/goal` and `/rubric`. The command UI shows the
available operations for the pinned upstream version.

Resume the most recent thread:

```sh
"$FACTORY_REPO/.venv/bin/lc-code" -r
```

Resume a known thread:

```sh
"$FACTORY_REPO/.venv/bin/lc-code" -r THREAD_ID
```

You can also select a thread with `/threads`. Because `dcode` and `lc-code`
share Deep Agents Code session storage, use a fresh thread when comparing the
two constructors.

## The baseline dogfood pass

This is the Group 1 acceptance check. Its question is:

> Does the recomposed `create_factory_agent` work through the unchanged,
> shipped Deep Agents TUI the way the original `create_cli_agent` does?

It is deliberately a **no-injection** run. Group 2 middleware is a separate
extension check.

### 1. Prepare an isolated workspace

```sh
FACTORY_REPO=/absolute/path/to/langchain_based_agent
mkdir -p /tmp/lc-factory-dogfood
cd /tmp/lc-factory-dogfood
unset LC_FACTORY_MIDDLEWARE
"$FACTORY_REPO/.venv/bin/lc-code"
```

Using a temporary workspace prevents a dogfood prompt from changing the
factory source tree or another real project.

### 2. Confirm the session setup

Before asking for a write:

1. Configure the provider with `/auth` and `/model` if needed.
2. Confirm the status bar shows the temporary working directory.
3. Confirm the approval mode is **Manual**.
4. Do not enable Auto or YOLO.

The upstream branding in the banner is expected; it is evidence that
`lc-code` is reusing the shipped client, not that the wrong constructor was
selected.

### 3. Exercise an approved action

Enter this prompt:

```text
Create dogfood.txt containing exactly factory tui ok, then read the file back
and confirm its exact contents.
```

When the file write is proposed:

1. Press `e` if you want to inspect the full arguments.
2. Press `y` to approve the write.
3. Let the agent read the file back.
4. Confirm the file contains exactly `factory tui ok`.

You can independently verify it from another shell:

```sh
cat /tmp/lc-factory-dogfood/dogfood.txt
```

### 4. Exercise rejection and recovery

Enter:

```text
Replace dogfood.txt with exactly this should not land.
```

Press `n` when the write is proposed. If the agent proposes a second write,
reject it as well or interrupt the turn. Then ask:

```text
Read dogfood.txt without changing it and report its exact contents.
```

The file should still contain `factory tui ok`, and the session should remain
usable after the rejection.

### 5. Optional resume check

Exit with `/quit`, relaunch with `-r` or select the thread with `/threads`, and
confirm the conversation can be resumed.

### Pass criteria

The baseline dogfood pass succeeds when all of the following are true:

- `lc-code` starts and connects to the factory-backed server;
- responses stream normally in the upstream TUI;
- a gated write produces the normal approval UI;
- approving the write executes it;
- rejecting a later write leaves the file unchanged;
- the agent continues after rejection; and
- optionally, the thread resumes through the normal Deep Agents flow.

This pass does not prove middleware injection. It proves that the Group 1
factory construction works through the shipped TUI, including its approval
interrupt contract.

## Compare directly with upstream `dcode`

For an A/B check, use the two binaries from the same virtual environment, the
same model, the same prompt, fresh threads, and separate workspaces:

```sh
mkdir -p /tmp/dcode-baseline
cd /tmp/dcode-baseline
"$FACTORY_REPO/.venv/bin/dcode" -M "provider:model"
```

Then:

```sh
mkdir -p /tmp/lc-factory-baseline
cd /tmp/lc-factory-baseline
unset LC_FACTORY_MIDDLEWARE
"$FACTORY_REPO/.venv/bin/lc-code" -M "provider:model"
```

Compare the observable workflow: startup, streaming, tools, approval and
rejection, error handling, and resume behavior. Model wording is not expected
to be deterministic.

## What `unset LC_FACTORY_MIDDLEWARE` means

`LC_FACTORY_MIDDLEWARE` is the optional Group 2 launch-time injection setting.

```sh
unset LC_FACTORY_MIDDLEWARE
```

removes that variable from the **current shell**. A subsequently launched
`lc-code` child process receives no middleware reference, so
`create_factory_agent` uses its default Group 1 composition.

`unset` does not:

- uninstall middleware;
- edit a configuration file;
- affect another terminal window;
- change an already-running `lc-code` process; or
- delete any project data.

This is why the baseline procedure includes it: an old exported value in your
shell must not silently turn a Group 1 parity check into a Group 2 extension
check.

To inspect the current shell value:

```sh
printenv LC_FACTORY_MIDDLEWARE
```

No output means there is no non-empty reference to inject.

## Optional Group 2 middleware injection

Run this only after the no-injection baseline is understood.

Set a real shell export to a zero-argument factory:

```sh
export LC_FACTORY_MIDDLEWARE="my_package.agent_setup:build_middleware"
"$FACTORY_REPO/.venv/bin/lc-code"
```

The referenced callable must return either:

- an ordered sequence of middleware, which uses the default
  `before_verification` phase; or
- a mapping whose keys are `first`, `before_verification`, or `last` and whose
  values are ordered middleware sequences.

The named module must be installed in the **same Python environment** as
`lc_factory`. Merely placing it in the current directory or adding it to
`PYTHONPATH` is not sufficient: the server strips `PYTHONPATH` and starts from
a private working directory.

The reference must come from the shell. It intentionally cannot be supplied by
a project or global `.env` file, because resolving it imports and executes
Python code during server startup, before an approval screen could protect
that action. Only point it at code you trust.

Injected middleware through this variable:

- must have unique, non-reserved middleware names; and
- fails startup loudly when the reference or returned value is invalid.

### Reaching subagents and the rubric grader

The same variable covers all three targets. Return a **target-keyed** mapping
instead of a plain sequence:

```python
def build_middleware():
    return {
        "main":      [AuditMain()],
        "subagents": [AuditDelegated()],   # every subagent, incl. general-purpose
        "grader":    [AuditGrader()],      # the rubric grader
    }
```

Each target's value may be a bare sequence or a phase-keyed mapping. Phases
differ per target — the main agent has `first` / `before_verification` /
`last`, while subagents and the grader have only `first` / `last` to address
the edges of their factory middleware block.

Code 0.1.66 defaults the general-purpose subagent to fork mode. It inherits
main middleware, including `AuditMain()` above, by reference. Child `first`
injections follow the inherited parent middleware; they cannot move ahead of
inherited approval or verification. For fresh general-purpose delegation with
independent middleware scopes, export `DEEPAGENTS_CODE_FORKED_SUBAGENTS=false`
before launching `lc-code`. File-defined custom subagents remain fresh at this
pinned release; their parser does not accept a fork-mode setting. See
[`README.md`](README.md#reaching-delegated-work) for ordering and shared-state
details.

Omitting a target leaves it composed exactly as before. The older forms still
work: a bare sequence or a phase-keyed mapping both address the main agent.

A mapping that mixes target keys with phase keys is **rejected**, not guessed —
composing an agent you did not ask for is the failure this variable's error
handling exists to prevent.

One variable rather than one per target is deliberate. Resolving a reference
imports and executes the named module inside the server process, so each
additional variable would need its own `.env` reservation guard — otherwise a
committed `.env` in a cloned repository could name code to run at startup.
Reusing this one adds no new attack surface.

The goal-criteria agent cannot receive middleware by any route: upstream's
constructor takes no middleware argument.

Return to the baseline after the session:

```sh
unset LC_FACTORY_MIDDLEWARE
```

### Verify the transport with the shipped test marker

The package includes an internal test fixture that writes `composed` to a
marker file when the injected middleware actually runs. It is for verification,
not a production middleware:

```sh
FACTORY_REPO=/absolute/path/to/langchain_based_agent
mkdir -p /tmp/lc-factory-middleware-dogfood
cd /tmp/lc-factory-middleware-dogfood
rm -f /tmp/lc-factory-middleware-marker

export LC_FACTORY_TEST_MIDDLEWARE_MARKER=/tmp/lc-factory-middleware-marker
export LC_FACTORY_MIDDLEWARE="lc_factory._testing_middleware:build_marker_middleware"

"$FACTORY_REPO/.venv/bin/lc-code" \
  -m "Reply with exactly middleware transport ok."
```

After the first agent turn runs, inspect the marker from another shell:

```sh
cat /tmp/lc-factory-middleware-marker
```

Expected content:

```text
composed
```

Clean up the shell settings when finished:

```sh
unset LC_FACTORY_MIDDLEWARE
unset LC_FACTORY_TEST_MIDDLEWARE_MARKER
```

## Non-interactive use

The same `lc-code` entry point also supports the upstream one-shot path:

```sh
cd /absolute/path/to/your/workspace
"$FACTORY_REPO/.venv/bin/lc-code" -n "Summarize README.md"
```

Non-interactive execution does not show the Textual approval UI. Local shell
access is disabled by default; use `-S` only when you intentionally want to
allow specified commands. For example:

```sh
"$FACTORY_REPO/.venv/bin/lc-code" \
  -n "Inspect the repository status." \
  -S recommended
```

CLI rubric flags belong to this non-interactive path:

```sh
"$FACTORY_REPO/.venv/bin/lc-code" \
  -n "Summarize README.md." \
  --rubric "The answer identifies the project purpose and current status."
```

The open dogfood gap is specifically about the **interactive TUI**, so a
successful non-interactive run is useful evidence but is not a substitute for
the baseline pass above.

## Troubleshooting

### `lc-code: command not found`

Use the absolute virtual-environment binary:

```sh
"$FACTORY_REPO/.venv/bin/lc-code" --help
```

If it does not exist, return to the factory repository and run `uv sync`.

### The UI says `dcode` or `deepagents-code`

Expected. `lc-code` deliberately reuses the upstream CLI and TUI; only the
server graph construction is redirected.

### The agent opened the wrong repository

Exit, `cd` to the intended workspace, and relaunch. Check the working directory
shown in the status bar before approving file or shell actions.

### No model is available

Use `/auth` and `/model`, or launch with `-M "provider:model"`.

### No approval prompt appears

Confirm:

- the status bar says Manual;
- the session was not launched with `-y`, `--auto-approve`, or `--yolo`; and
- the agent actually proposed a gated action rather than a read-only one.

Use Shift+Tab or Ctrl+T to return to Manual if needed.

### Middleware does not load

Check that:

- `printenv LC_FACTORY_MIDDLEWARE` shows the expected `module:callable`;
- it was exported in the same terminal before `lc-code` started;
- it is not set only in `.env`;
- the module is installed in the factory virtual environment;
- the callable takes no arguments; and
- it returns an ordered middleware sequence or valid phase-keyed mapping.

An invalid reference should stop startup with an error naming
`LC_FACTORY_MIDDLEWARE`; it should not silently fall back to the baseline.

### A resumed thread has unexpected history

`dcode` and `lc-code` share the upstream session database. Use `/clear` for a
new thread or `/threads` to select the intended one. For A/B comparison, start
fresh threads in separate directories.

## What to record from a dogfood run

For a reproducible pass or failure report, capture:

- the project commit under test;
- the output of `lc-code --version`;
- the launch command and working directory;
- whether `LC_FACTORY_MIDDLEWARE` was unset or the exact non-secret reference
  used;
- the selected model and approval mode;
- which approve, reject, and resume steps passed;
- the exact visible error text for any failure; and
- whether the failure happened before connection, during streaming, or at an
  approval interrupt.

For implementation details and the pinned-upstream contract, see
[`README.md`](README.md). For parity and documented divergences, see
[`UPGRADING.md`](UPGRADING.md).
