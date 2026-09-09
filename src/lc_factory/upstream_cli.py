"""Import boundary for the CLI entry point, kept separate on purpose.

`deepagents_code.__getattr__` serves `cli_main` lazily so that importing a
submodule does not pull in the whole CLI/client stack. Re-exporting it from
the main boundary module would defeat that: `lc_factory.server_graph` imports
the boundary, so the `langgraph dev` subprocess would load ~2300 modules
(and `main.py`'s module-level warning filters) instead of the ~160 upstream's
own server loads.

Only `lc_factory.tui` imports this module.
"""

from deepagents_code import cli_main
from deepagents_code import app as app_module
from deepagents_code.tui.widgets import subagent_panel as subagent_panel_module

__all__ = ["cli_main", "app_module", "subagent_panel_module"]


def background_approval_widgets():
    """Reuse native file content/diff presentation without foreground decisions."""
    from deepagents_code.tui.widgets import tool_widgets
    return tool_widgets


async def fulfill_background_hook(hooks, payload):
    """Fulfill one captured runtime invocation and own its cancellation.

    Upstream shields ledger operations from cancellation of their caller. A
    background child instead owns this exact snapshot/invocation operation:
    cancellation must stop and drain it without touching other ledger entries.
    Completed entries remain cached so repeated delivery never replays a hook.
    """
    import asyncio

    from deepagents_code.hooks.client import fulfill_hook_invocation
    from deepagents_code.hooks.interrupt import parse_hook_interrupt_payload

    runtime = hooks._runtime
    if runtime is None:
        raise RuntimeError("Received hook invocation without a HooksRuntime")
    request = parse_hook_interrupt_payload(payload)
    if request is None:
        raise RuntimeError("Failed to parse hook interrupt")
    ledger = runtime.fulfillments
    key = (request.snapshot_id, request.invocation_id)
    try:
        return await fulfill_hook_invocation(runtime, request)
    except asyncio.CancelledError:
        async def cancel_exact_invocation():
            async with ledger._lock:
                operation = ledger._in_flight.get(key)
                if operation is not None:
                    operation.cancel()
            if operation is not None:
                await asyncio.gather(operation, return_exceptions=True)

        # A switch followed by shutdown can cancel our caller twice. Cleanup
        # still belongs to the captured runtime, even if hooks._runtime changed.
        cleanup = asyncio.create_task(cancel_exact_invocation())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        raise
