"""Archive acceptance, adapted from Talon's MIT-licensed archive tests."""
import asyncio

import pytest
from deepagents.graph import DeepAgentState
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph

from lc_factory.archive import ArchiveScope, SQLiteConversationArchive
from lc_factory.archive_saver import ConversationSaver

SCOPE = ArchiveScope(workspace_id="workspace", owner_id="alice")
OTHER = ArchiveScope(workspace_id="workspace", owner_id="bob")

async def scope(session):
    return OTHER if session == "other" else SCOPE

async def save(saver, session="session", text="orchard", namespace=""):
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [HumanMessage(text, id="message")]}
    return await saver.aput({"configurable": {"thread_id": session, "checkpoint_ns": namespace},
                            "metadata": OTHER}, checkpoint, {}, {})


async def test_persistence_scope_revision_and_archive_exclusion(tmp_path):
    path = str(tmp_path / "archive.sqlite")
    async with SQLiteConversationArchive.from_conn_string(path) as archive:
        saver = ConversationSaver(InMemorySaver(), archive=archive, scope_resolver=scope)
        await save(saver)
        await save(saver)
        await save(saver, text="orchard revised")
        await save(saver, "other", "private orchard")
        await save(saver, namespace="child", text="child secret")
        await archive.append(SCOPE, "session", "now", [ToolMessage("retrieved secret", name="search_conversations", tool_call_id="c")])
    async with SQLiteConversationArchive.from_conn_string(path) as archive:
        hits = await archive.entries(SCOPE, query="orchard")
        assert len(hits) == 2  # distinct revisions, not duplicate checkpoints
        assert await archive.entries(SCOPE, session_id="other") == []
        assert await archive.entries(SCOPE, query='orchard" OR "private') == []
        assert await archive.entries(SCOPE, query="secret") == []
        with pytest.raises(ValueError, match="different"):
            await archive.append(OTHER, "session", "now", [])


@pytest.mark.parametrize("schema", [MessagesState, DeepAgentState])
async def test_committed_messages_survive_compaction(tmp_path, schema):
    async with SQLiteConversationArchive.from_conn_string(str(tmp_path / "archive.sqlite")) as archive:
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as backend:
            saver = ConversationSaver(backend, archive=archive, scope_resolver=scope)
            builder = StateGraph(schema)
            builder.add_node("reply", lambda _: {"messages": [AIMessage("Noted", id="reply")]})
            builder.add_edge(START, "reply")
            builder.add_edge("reply", END)
            graph = builder.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "session"}}
            await graph.ainvoke({"messages": [HumanMessage("old orchard", id="original")]}, config)
            await graph.aupdate_state(config, {"messages": [RemoveMessage(id="original")]})
            assert all(m.id != "original" for m in (await graph.aget_state(config)).values["messages"])
            assert len(await archive.entries(SCOPE, query="orchard")) == 1
            assert len(await archive.entries(SCOPE)) == 2


async def test_delete_failure_remains_retryable_and_scope_safe(tmp_path, monkeypatch):
    backend = InMemorySaver()
    async with SQLiteConversationArchive.from_conn_string(str(tmp_path / "archive.sqlite")) as archive:
        saver = ConversationSaver(backend, archive=archive, scope_resolver=scope)
        owned, other = await save(saver), await save(saver, "other")
        delete = backend.adelete_thread
        async def fail(_):
            raise OSError("backend unavailable")
        monkeypatch.setattr(backend, "adelete_thread", fail)
        with pytest.raises(OSError):
            await saver.clear_history(SCOPE)
        assert await archive.sessions(SCOPE) == ["session"]
        monkeypatch.setattr(backend, "adelete_thread", delete)
        await saver.clear_history(SCOPE)
        assert await saver.aget(owned) is None
        assert await saver.aget(other)
        assert await archive.entries(SCOPE) == []
        assert await archive.entries(OTHER)


async def test_cancelled_checkpoint_finishes_archive_before_propagating(tmp_path, monkeypatch):
    async with SQLiteConversationArchive.from_conn_string(str(tmp_path / "archive.sqlite")) as archive:
        saver = ConversationSaver(InMemorySaver(), archive=archive, scope_resolver=scope)
        started, finish = asyncio.Event(), asyncio.Event()
        original = archive.append
        async def append(scope, session, timestamp, messages):
            if messages:
                started.set()
                await finish.wait()
            await original(scope, session, timestamp, messages)
        monkeypatch.setattr(archive, "append", append)
        task = asyncio.create_task(save(saver))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(await archive.entries(SCOPE)) == 1


async def test_factory_history_tools_and_deletion_are_wired(tmp_path):
    from deepagents_code._fake_models import _ToolBindingFakeModel
    from lc_factory.runtime import FactoryRuntime, RuntimeOptions
    from lc_factory.background import Job
    async with SQLiteConversationArchive.from_conn_string(str(tmp_path / "history.sqlite")) as archive:
        model = _ToolBindingFakeModel(messages=iter([
            AIMessage("noted"),
            AIMessage("", tool_calls=[{"name": "search_conversations", "args": {"query": "orchard"}, "id": "search", "type": "tool_call"}]),
            AIMessage("found it"),
        ]))
        async with await FactoryRuntime.create(agent_kwargs=dict(
                model=model, assistant_id="history-test", cwd=tmp_path,
                interactive=False, auto_approve=True, enable_memory=False,
                enable_skills=False, enable_ask_user=False, checkpointer=InMemorySaver()),
                options=RuntimeOptions(history=True, background=True), archive=archive,
                workspace_id="workspace", owner_id="alice") as runtime:
            await runtime.ainvoke({"messages": [HumanMessage("remember orchard")]}, {"configurable": {"thread_id": "one"}})
            result = await runtime.ainvoke({"messages": [HumanMessage("search history")]}, {"configurable": {"thread_id": "two"}})
            retrieved = [m for m in result["messages"] if isinstance(m, ToolMessage)]
            assert "remember orchard" in retrieved[-1].content
            assert len(await archive.entries(SCOPE, query="orchard")) == 2  # original user text plus AI search call; no indexed retrieval response
            runtime.background.jobs["pending"] = Job("one", "worker", result="pending", status="completed")
            await runtime.delete_conversation("one")
            assert await archive.entries(SCOPE, session_id="one") == []
            assert runtime.background.list("one") == []
            assert await archive.entries(SCOPE, session_id="two")
            assert runtime._turn_locks == {}
