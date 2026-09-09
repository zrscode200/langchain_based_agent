#!/usr/bin/env python3
"""Permission guard for frozen review/reliance boundaries.

Enforces one Core 2 invariant that cannot hold as instructions alone: a
frozen review/reliance boundary is never rewritten, by any actor.

ON THE OTHER INVARIANT THIS HOOK ONCE TRIED TO ENFORCE
--------------------------------------------------------
Core 2 also states that a delegated contributor never directly modifies
durable program state. This hook cannot enforce that, and an earlier version
of it that tried was silently never firing: no tool-call payload this
runtime produces carries the caller's agent identity, on EITHER hook event.
Traced end to end against the pinned `deepagents_code==0.1.57` through two
independent paths -- the generic `client_lifecycle._invoke`/`HookContext`
path, and the `PermissionRequest`-specific path
(`manager.on_permission_request` -> `client_lifecycle.resolve_permission` ->
`tui.textual_adapter._permission_tool_calls`) -- and both converge:
`PermissionRequestEvent` has exactly two fields (`event`, `call`);
`ToolCallData` carries `id`/`name`/`args`/`mcp_server`, nothing else;
`_permission_tool_calls` recovers only a tool-call id by matching name/args
against mounted rows. `AgentIdentity` is constructed exactly once anywhere in
the package, for the DISPATCHING `task` call's own `SubagentStart` event --
never for a call made *by* the dispatched subagent afterward.

Decided (group GUARD, `work/design/permission-gating-layers.md`,
`work/active-work.md`) not to build the identity plumbing this would need.
The contributor/durable invariant now rests entirely on git history plus the
scientific review process: a contributor write to durable content is not
blocked, but it is not silent either -- it lands as an ordinary uncommitted
diff in a git working tree, so it is visible to `git diff`/`git log` and
recoverable against whatever was last committed, provided someone reviews
before that diff gets folded into history. That review step, not this hook,
is where invariant 1 actually lives.

Registered on TWO hook events, because one alone does not reach every mode:

  PreToolUse        -- runs server-side, in EVERY approval mode including
                        AUTO and YOLO. A deny here replaces the tool call's
                        result with an error naming the reason before the
                        tool ever executes (`hooks/server_middleware.py`,
                        `_pre_tool_outcome`/`_denied_tool_message`) -- a real
                        block, not merely a skipped prompt.
  PermissionRequest  -- runs client-side, MANUAL only. Kept alongside
                        PreToolUse for MANUAL's richer "rejected" UI
                        presentation; enforces the identical rule.

Both events now run the same `decide()` and reach the same verdict for the
same input -- there is no remaining reason for them to differ, since the
only thing that used to distinguish them (agent identity, available on
`PermissionRequest`, absent on `PreToolUse`) no longer drives any check.

THE BUG THIS HOOK ONCE HAD
----------------------------
An earlier version answered every non-denied call with `{"behavior":
"allow"}` on `PermissionRequest`. The runtime reads `allow` as an approval,
not as "no opinion" -- `hooks/permissions.py` maps it straight to
`{"type": "approve"}`, and the tool call proceeds with no prompt. Measured:
`rm -rf workspace/tmp` returned `allow`. Fixed by never emitting a decision
except to deny: omitting `hookSpecificOutput.decision` on `PermissionRequest`
(or `permissionDecision` on `PreToolUse`) is how a hook abstains, and normal
approval then applies. No allow-list was added here to compensate -- "always
fine" belongs in the graph's `interrupt_on` configuration, not in this hook.

WHY A HOOK AND NOT `FilesystemPermission`
------------------------------------------
`FilesystemMiddleware` refuses to construct when permissions are combined with
an execution-capable backend (`filesystem.py:1667`), which is the ddt-agent
default. The rationale is sound: tool-level path rules do not cover `execute`,
so a deny on a frozen path is defeated by `rm -rf` through the shell. Upstream
declines to ship a control that does not hold.

`ServerHooksMiddleware` is installed on subagent stacks too
(`assembly.py:1142`), so one script covers the main companion, delegated
contributors, and the shell.

WHAT THIS IS AND IS NOT
------------------------
For structured filesystem tools (`Write`, `Edit`, `delete`) the path check is
exact. For `Bash` it is a TEXT SCAN of the command, which is a heuristic: it
catches a protected path named in a command and will not catch obfuscation,
variable indirection, or a path reached via `cd`. It raises the cost of an
accidental overwrite; it is not a containment boundary.

Genuine immutability is an OS/storage property -- a read-only mount,
filesystem ACLs, or a store the agent holds no write credential for. What
this hook plus git history gives is prevention of the ordinary case and
tamper-evidence for the rest, which is the scientific requirement: the exact
material that was reviewed stays recoverable.

WIRE CONTRACT
-------------
stdin  : JSON, snake_case. `hook_event_name`, `tool_name`, `tool_input`, `cwd`.
stdout : JSON, camelCase aliases, per `hooks/models/wire.py`. The two events
         use different output shapes -- see `_emit_permission_request` and
         `_emit_pre_tool_use`.
exit 2 : both `PreToolUse` and `PermissionRequest` carry `ExitCodePolicy.DENY`
         (`hooks/capabilities.py`), so a crash or an undecidable input denies
         rather than allows on either event. Fail closed.

Tool names arrive PROJECTED to Claude-compatible form (`hooks/tools.py`):
`execute`->`Bash`, `write_file`->`Write`, `edit_file`->`Edit`,
`read_file`->`Read`. `delete` is unmapped and arrives unchanged.
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
from typing import Any

# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

# Frozen boundaries. Immutable for every actor, including the main companion.
FROZEN_MARKERS = ("/frozen/", "/frozen")

# Wire tool names that can mutate the filesystem, and where the path lives.
PATH_TOOLS: dict[str, tuple[str, ...]] = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "delete": ("file_path", "path", "target"),
}

# Wire tool name whose payload is an opaque shell command.
SHELL_TOOLS = ("Bash",)

# Shell verbs worth scanning for. A read-only command naming a protected path
# is not a violation, so the scan is limited to plausibly mutating ones.
MUTATING_SHELL = re.compile(
    r"\b(rm|mv|cp|dd|truncate|tee|sed|chmod|chown|ln|install|rsync|"
    r"shred|unlink|rmdir|mkdir|touch|git\s+(?:checkout|restore|reset|clean|rm|mv))\b"
    r"|>>?\s*\S",  # output redirection
    re.IGNORECASE,
)

_FROZEN_PATH_MESSAGE = (
    "{path} is inside a frozen review/reliance boundary. A frozen "
    "boundary records the exact material placed before a reviewer "
    "and is never rewritten — later scientific change is recorded "
    "as a new revision that supersedes it for a stated scope. If "
    "the science has moved on, write a new revision and record the "
    "supersession; do not edit the boundary."
)
_FROZEN_SHELL_MESSAGE = (
    "This command appears to modify a frozen review/reliance "
    "boundary. Frozen boundaries are immutable; record a superseding "
    "revision instead. If the command does not actually write there, "
    "narrow it so the protected path is not named."
)


def _norm(path: str) -> str:
    """Normalize to a POSIX-ish comparable form without resolving symlinks."""
    p = str(path).replace("\\", "/").strip()
    p = posixpath.normpath(p)
    return p


def _hits_frozen(path: str) -> bool:
    n = _norm(path)
    return any(m in n for m in FROZEN_MARKERS)


def _paths_from_args(tool: str, args: dict[str, Any]) -> list[str]:
    return [
        str(args[key]) for key in PATH_TOOLS.get(tool, ())
        if isinstance(args.get(key), str) and args[key]
    ]


def decide(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Return ('deny'|'abstain', message).

    Shared by both hook events -- see module docstring for why there is no
    remaining reason for them to diverge. Enforces only the frozen-boundary
    rule, which applies to every actor regardless of identity.

    'abstain' means no objection, NOT approved: normal approval decides.
    """
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") or {}
    if not isinstance(args, dict):
        args = {}

    if tool in PATH_TOOLS:
        for path in _paths_from_args(tool, args):
            if _hits_frozen(path):
                return "deny", _FROZEN_PATH_MESSAGE.format(path=path)
        return "abstain", None

    if tool in SHELL_TOOLS:
        command = str(args.get("command") or "")
        if not command or not MUTATING_SHELL.search(command):
            return "abstain", None
        if any(m.strip("/") in _norm(command) for m in FROZEN_MARKERS):
            return "deny", _FROZEN_SHELL_MESSAGE
        return "abstain", None

    return "abstain", None


def _emit_permission_request(behavior: str, message: str | None) -> None:
    """Emit a `PermissionRequest` decision, or no decision at all.

    Omitting `hookSpecificOutput` entirely is how this event abstains --
    there is no third enum value: the decision union admits only
    `allow`/`deny` (`hooks/models/wire.py`). Emitting `allow` would resolve
    the call as approved and skip the user, which is the bug this replaced.
    """
    if behavior != "deny":
        json.dump({"continue": True}, sys.stdout)
        return
    decision: dict[str, Any] = {"behavior": "deny"}
    if message:
        decision["message"] = message
    json.dump(
        {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision,
            },
        },
        sys.stdout,
    )


def _emit_pre_tool_use(behavior: str, message: str | None) -> None:
    """Emit a `PreToolUse` decision, or no decision at all.

    Omitting `permissionDecision` is this event's abstain -- normal approval
    (including AUTO's classifier) then still applies
    (`hook_decided_permission`, `hooks/server_middleware.py`).
    """
    if behavior != "deny":
        json.dump({"continue": True}, sys.stdout)
        return
    output: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
    }
    if message:
        output["permissionDecisionReason"] = message
    json.dump({"continue": True, "hookSpecificOutput": output}, sys.stdout)


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except (ValueError, OSError) as exc:
        # Fail closed: exit 2 denies under ExitCodePolicy.DENY on either event.
        print(f"durable-content guard could not read its input: {exc}", file=sys.stderr)
        return 2

    event = payload.get("hook_event_name")
    if event not in ("PermissionRequest", "PreToolUse"):
        # Not ours; no opinion, not an approval.
        json.dump({"continue": True}, sys.stdout)
        return 0

    try:
        behavior, message = decide(payload)
    except Exception as exc:  # noqa: BLE001 - must never crash open
        print(f"durable-content guard failed to decide: {exc}", file=sys.stderr)
        return 2

    if event == "PermissionRequest":
        _emit_permission_request(behavior, message)
    else:
        _emit_pre_tool_use(behavior, message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
