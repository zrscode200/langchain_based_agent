"""Owned subagent policies through the actual factory and SDK graphs."""
import pytest
from dataclasses import replace
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from deepagents_code._fake_models import _ToolBindingFakeModel

from lc_factory.assembly import create_factory_agent
from lc_factory.runtime import FactoryRuntime, RuntimeOptions
from lc_factory.upstream import InterpreterConfig


def call(name, arguments, identifier):
    return AIMessage(content="", tool_calls=[dict(name=name, args=arguments, id=identifier, type="tool_call")])


def args(tmp_path, **kwargs):
    return dict(assistant_id="policy-test", cwd=tmp_path, enable_memory=False,
                enable_skills=False, enable_ask_user=False, interactive=False,
                auto_approve=True, **kwargs)


@pytest.mark.parametrize("mode", ["isolated", "fork"])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_forbidden_calls_do_not_execute(tmp_path, mode, asynchronous):
    executed, observed = [], []

    @tool
    def secret_source() -> str:
        """Read a restricted information source."""
        executed.append("secret")
        return "secret"

    class Observe(AgentMiddleware):
        def before_model(self, state, runtime):
            observed.extend(m for m in state["messages"] if isinstance(m, ToolMessage))

    target = tmp_path / "forbidden.txt"
    model = _ToolBindingFakeModel(messages=iter([
        call("task", {"description": "Try the restricted tools", "subagent_type": "child"}, "delegate"),
        call("secret_source", {}, "secret"),
        call("write_file", {"file_path": str(target), "content": "bad"}, "write"),
        call("read_file", {"file_path": str(tmp_path / "source.txt")}, "read"),
        AIMessage("child done"), AIMessage("parent done"),
    ]))
    (tmp_path / "source.txt").write_text("private-file")
    graph, _ = create_factory_agent(**args(tmp_path, model=model, tools=[secret_source],
        subagents=[dict(name="child", description="Child", mode=mode)],
        subagent_policy={"child": {"tools": []}}, subagent_middleware=[Observe()]))
    inputs = {"messages": [HumanMessage("delegate")]}
    result = await graph.ainvoke(inputs) if asynchronous else graph.invoke(inputs)
    assert result["messages"][-1].content == "parent done"
    assert not executed and not target.exists()
    child_results = {m.tool_call_id: str(m.content) for m in observed}
    assert all(identifier in child_results for identifier in ("secret", "read", "write"))
    assert "private-file" not in child_results["read"]
    assert all("not permitted" in child_results[i] or "not a valid tool" in child_results[i]
               for i in ("secret", "read", "write"))


def test_restricted_fork_filters_ptc_after_early_tool_injection(tmp_path):
    executed, observed = [], []

    @tool
    def forbidden() -> str:
        """Forbidden early injected source."""
        executed.append("forbidden")
        return "bad"

    @tool
    def allowed() -> str:
        """Allowed source."""
        executed.append("allowed")
        return "allowed-value"

    class EarlyInjection(AgentMiddleware):
        tools = [forbidden]

        def wrap_model_call(self, request, handler):
            return handler(request.override(tools=[*request.tools, forbidden]))

    class Observe(AgentMiddleware):
        def before_model(self, state, runtime):
            observed.extend(m for m in state["messages"] if isinstance(m, ToolMessage))

    model = _ToolBindingFakeModel(messages=iter([
        call("task", {"description": "Inspect tools", "subagent_type": "child"}, "delegate"),
        call("js_eval", {"code": "[typeof tools.forbidden, await tools.allowed({})]"}, "probe"),
        call("js_eval", {"code": "await task({description: 'Escalate', subagentType: 'general-purpose'})"}, "escape"),
        AIMessage("child done"), AIMessage("parent done"),
    ]))
    graph, _ = create_factory_agent(**args(tmp_path, model=model, tools=[allowed],
        enable_interpreter=True, interpreter_config=replace(
            InterpreterConfig.from_resolver(), ptc=["allowed", "forbidden"]),
        middleware={"first": [EarlyInjection()]},
        subagent_middleware=[Observe()],
        subagents=[dict(name="child", description="Child", mode="fork")],
        subagent_policy={"child": {"tools": ["js_eval", "allowed"]}}))
    graph.invoke({"messages": [HumanMessage("delegate")]})
    probe = next(str(m.content) for m in observed if m.tool_call_id == "probe")
    assert "undefined" in probe and "allowed-value" in probe
    escape = next(str(m.content) for m in observed if m.tool_call_id == "escape")
    assert "error" in escape.lower() and "task" in escape
    assert executed == ["allowed"]


@pytest.mark.parametrize("via_js", [False, True])
def test_restricted_fork_can_retain_task_without_bypassing_sdk_recursion_limit(tmp_path, via_js):
    observed = []

    class Observe(AgentMiddleware):
        def before_model(self, state, runtime):
            observed.extend(m for m in state["messages"] if isinstance(m, ToolMessage))

    nested = (call("js_eval", {"code": "await task({description:'Leaf work', subagentType:'leaf'})"}, "nested")
              if via_js else call("task", {"description": "Leaf work", "subagent_type": "leaf"}, "nested"))
    model = _ToolBindingFakeModel(messages=iter([
        call("task", {"description": "Delegate the work", "subagent_type": "child"}, "root"),
        nested, AIMessage("child result"), AIMessage("parent result"),
    ]))
    graph, _ = create_factory_agent(**args(tmp_path, model=model, enable_interpreter=via_js,
        subagent_middleware=[Observe()],
        subagents=[dict(name="child", description="Child", mode="fork"),
                   dict(name="leaf", description="Leaf", mode="isolated")],
        subagent_policy={"child": {"tools": ["task", *(["js_eval"] if via_js else [])]},
                         "leaf": {"tools": []}}))
    result = graph.invoke({"messages": [HumanMessage("delegate")]})
    assert result["messages"][-1].content == "parent result", [(m.tool_call_id, m.content) for m in observed]
    assert any(m.tool_call_id == "nested" and "cannot delegate" in str(m.content) for m in observed)


async def test_new_spec_and_policy_preserve_child_approval_and_resume(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    target = tmp_path / "approved.txt"
    model = _ToolBindingFakeModel(messages=iter([
        call("task", {"description": "Write the approved file", "subagent_type": "child"}, "delegate"),
        call("write_file", {"file_path": str(target), "content": "approved"}, "write"),
        AIMessage("child done"), AIMessage("parent done"),
    ]))
    kwargs = args(tmp_path, model=model, checkpointer=InMemorySaver(),
        subagents=[dict(name="child", description="Child", mode="isolated")],
        subagent_policy={"child": {"tools": ["write_file"]}})
    kwargs.update(interactive=True, auto_approve=False)
    graph, _ = create_factory_agent(**kwargs)
    config = {"configurable": {"thread_id": "approval"}}
    result = await graph.ainvoke({"messages": [HumanMessage("delegate")]}, config)
    assert result.get("__interrupt__") and not target.exists()
    approval = Command(resume={"decisions": [{"type": "approve"}]})
    result = await graph.ainvoke(approval, config)
    assert result.get("__interrupt__") and not target.exists()
    result = await graph.ainvoke(approval, config)
    assert not result.get("__interrupt__")
    assert target.read_text() == "approved"
    assert result["messages"][-1].content == "parent done"


async def test_invalid_policy_reload_retains_old_generation(tmp_path):
    directory = tmp_path / ".deepagents"
    directory.mkdir()
    path = directory / "subagents.toml"
    path.write_text('[subagents.child]\ntools = []\n')
    async with await FactoryRuntime.create(
        agent_kwargs=args(tmp_path, model=_ToolBindingFakeModel(),
                          subagents=[dict(name="child", description="Child")]),
        options=RuntimeOptions(reload=True), workspace_id="workspace",
    ) as runtime:
        original = runtime.current
        path.write_text('[subagents.child]\ntools = ["missing_source"]\n')
        runtime.request_reload()
        assert await runtime.select() is original
        assert runtime.kwargs["subagent_policy"] == {"child": {"tools": []}}
        assert runtime.last_reload_error == "ValueError"
