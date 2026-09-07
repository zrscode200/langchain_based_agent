# Adapted from deepagents_talon at 6c89fe2197a2dfe4f3851cda38565bcadba6066b.
# Copyright (c) LangChain, Inc. MIT License; see LICENSE and TALON_ADAPTATIONS.md.
"""Compose conversation archives with async LangGraph checkpointers.

Warning:
    Experimental API; subject to change with the Talon runtime.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar, cast
from collections.abc import Awaitable, Callable

from lc_factory.upstream import BaseMessage, convert_to_messages
from lc_factory.upstream import BaseCheckpointSaver
from lc_factory.upstream import _DeltaSnapshot

from lc_factory.archive import ArchiveScope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection, Mapping, Sequence

    from lc_factory.upstream import MessageLikeRepresentation
    from lc_factory.upstream import RunnableConfig
    from lc_factory.upstream import ConfigurableFieldSpec
    from lc_factory.upstream import (
        ChannelVersions,
        Checkpoint,
        CheckpointMetadata,
        CheckpointTuple,
        DeltaChannelHistory,
    )

    from lc_factory.archive import SQLiteConversationArchive

V = TypeVar("V", int, float, str)


class ConversationSaver(BaseCheckpointSaver[V]):
    """Add an archive to any saver implementing the async LangGraph contract.

    The caller owns both stores and their lifetimes. Use one wrapper per archive
    within a factory host. Synchronous writes and administrative copy/prune APIs
    are deliberately unsupported so they cannot silently bypass the archive.

    Args:
        checkpointer: Backend for checkpoints, pending writes, and thread deletion.
        archive: Independent archive storage with idempotent writes and deletion.
    """

    def __init__(
        self, checkpointer: BaseCheckpointSaver[V], *, archive: SQLiteConversationArchive | None,
        scope_resolver: Callable[[str], Awaitable[ArchiveScope]], on_commit=None, before_delete=None
    ) -> None:
        """Wrap the saver without taking ownership of either store."""
        super().__init__(serde=checkpointer.serde)
        self.checkpointer = checkpointer
        self.archive = archive
        self.scope_resolver = scope_resolver
        self.on_commit = on_commit
        self.before_delete = before_delete
        self._lock = asyncio.Lock()

    @property
    def config_specs(self) -> list[ConfigurableFieldSpec]:
        """Preserve the underlying backend configuration contract."""
        return self.checkpointer.config_specs

    def get_next_version(self, current: V | None, channel: None) -> V:
        """Generate versions using the backend format.

        Args:
            current: Current channel version.
            channel: Deprecated LangGraph argument.
        """
        return self.checkpointer.get_next_version(current, channel)

    def with_allowlist(self, extra_allowlist: Collection[tuple[str, ...]]) -> ConversationSaver[V]:
        """Apply graph serialization permissions to the actual checkpoint backend.

        Args:
            extra_allowlist: Additional message and state types approved by the graph.
        """
        clone = ConversationSaver(
            self.checkpointer.with_allowlist(extra_allowlist), archive=self.archive,
            scope_resolver=self.scope_resolver, on_commit=self.on_commit,
            before_delete=self.before_delete
        )
        clone._lock = self._lock
        return clone

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Read checkpoint state from the backend.

        Args:
            config: Checkpoint identifier.
        """
        return await self.checkpointer.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, object] | None = None,  # noqa: A002  # LangGraph saver API.
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List backend checkpoints with their original filters and ordering.

        Args:
            config: Thread and namespace filter.
            filter: Metadata filter.
            before: Upper checkpoint bound.
            limit: Maximum results.

        Yields:
            Backend checkpoint tuples.
        """
        async for item in self.checkpointer.alist(
            config, filter=filter, before=before, limit=limit
        ):
            yield item

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        """Preserve backend delta reconstruction and optimizations.

        Args:
            config: Target checkpoint identifier.
            channels: Channels to reconstruct.
        """
        return await self.checkpointer.aget_delta_channel_history(config=config, channels=channels)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Save a checkpoint, then archive its committed message revisions.

        Archive failures propagate after checkpoint persistence; retry the same
        call to repair the archive without duplicate revisions. There is no
        transaction spanning the two stores. Cancellation waits for both writes
        to finish before propagating, so reset cannot race an unfinished archive write.

        Args:
            config: Checkpoint configuration with thread ID resolved by the trusted host callback.
            checkpoint: Snapshot to persist.
            metadata: Backend checkpoint metadata.
            new_versions: Updated channel versions.
        """
        async with self._lock:
            task = asyncio.create_task(self._put(config, checkpoint, metadata, new_versions))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            result = task.result()
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def _put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Finish both writes under the caller's lock, even when the caller is cancelled."""
        session = str(config["configurable"]["thread_id"])
        scope = (await self.scope_resolver(session)
                 if self.archive is not None and not config["configurable"].get("checkpoint_ns") else None)
        if scope is not None:
            await self.archive.append(scope, session, checkpoint["ts"], [])
            messages = await self._messages(config, checkpoint)
        result = await self.checkpointer.aput(config, checkpoint, metadata, new_versions)
        if scope is not None:
            await self.archive.append(scope, session, checkpoint["ts"], messages)
        if self.on_commit is not None and not config["configurable"].get("checkpoint_ns"):
            self.on_commit(session, checkpoint, new_versions)
        return result

    async def _messages(self, config: RunnableConfig, checkpoint: Checkpoint) -> list[BaseMessage]:
        messages: list[BaseMessage] = []
        if config["configurable"].get("checkpoint_id"):
            parent = await self.checkpointer.aget_tuple(config)
            if parent is not None:
                for _, channel, value in parent.pending_writes or []:
                    if channel == "messages":
                        messages.extend(_messages(value))
        messages.extend(_messages(checkpoint["channel_values"].get("messages", [])))
        return messages

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, object]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Persist pending writes; archive them when their next checkpoint commits.

        Args:
            config: Parent checkpoint configuration.
            writes: Channel updates.
            task_id: Task identifier.
            task_path: Backend task path.
        """
        async with self._lock:
            await self.checkpointer.aput_writes(config, writes, task_id, task_path)

    async def clear_history(self, scope: ArchiveScope) -> None:
        """Delete owned backend threads before removing their archive registrations.

        A failure leaves the current and remaining session registrations available
        for retry, including after restart. The host must stop chat workers first.

        Args:
            scope: Trusted channel/chat pair to erase.
        """
        async with self._lock:
            if self.archive is None:
                return
            for session in await self.archive.sessions(scope):
                await self._delete_session(session)

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete a backend thread and its archive, in retryable order.

        Args:
            thread_id: Trusted thread identifier to erase.
        """
        async with self._lock:
            await self._delete_session(thread_id)

    async def _delete_session(self, session: str) -> None:
        if self.before_delete is not None:
            await self.before_delete(session)
        await self.checkpointer.adelete_thread(session)
        if self.archive is not None:
            await self.archive.delete_session(session)


def _messages(value: object) -> list[BaseMessage]:
    if isinstance(value, _DeltaSnapshot):
        value = value.value
    values = value if isinstance(value, list) else [value]
    return convert_to_messages(cast("list[MessageLikeRepresentation]", values))
