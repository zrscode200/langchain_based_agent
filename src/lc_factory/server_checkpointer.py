"""Server-owned saver/archival lifecycle for opt-in factory capabilities."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager, AsyncExitStack

from lc_factory.archive import ArchiveScope, SQLiteConversationArchive
from lc_factory.archive_saver import ConversationSaver
from lc_factory.upstream import AsyncSqliteSaver, get_thread_workspace

_archive = None


def get_archive():
    if _archive is None:
        raise RuntimeError("Factory history requires lc_factory.server_checkpointer:create_checkpointer")
    return _archive


async def server_scope(session):
    """Derive scope from persisted server ownership, never client metadata."""
    binding = await get_thread_workspace(session)
    if binding is None:
        raise ValueError("History requires a persisted workspace binding")
    return ArchiveScope(workspace_id=binding.workspace_id,
                        owner_id=os.environ.get("LC_FACTORY_HISTORY_OWNER") or session)


@asynccontextmanager
async def create_checkpointer():
    """Open the execution-time saver and close workers before the stores."""
    from lc_factory.runtime import RuntimeOptions
    from lc_factory.server_graph import close_factory_runtimes, factory_checkpoint_committed, cancel_factory_background

    global _archive
    path = os.environ.get("DEEPAGENTS_CODE_SERVER_DB_PATH")
    if not path:
        raise ValueError("DEEPAGENTS_CODE_SERVER_DB_PATH is required")
    async with AsyncExitStack() as stack:
        saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(path))
        if RuntimeOptions.from_environment().history:
            _archive = await stack.enter_async_context(
                SQLiteConversationArchive.from_conn_string(path + ".factory-history.sqlite"))
        saver = ConversationSaver(saver, archive=_archive, scope_resolver=server_scope,
                                  on_commit=factory_checkpoint_committed, before_delete=cancel_factory_background)
        try:
            yield saver
        finally:
            await close_factory_runtimes()
            _archive = None
