"""Exercise the pinned interpreter through a real factory graph and fork."""
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deepagents_code._fake_models import _ToolBindingFakeModel
from lc_factory.assembly import create_factory_agent


def test_factory_fork_has_private_interpreter_state(tmp_path):
    observations = []

    class ObserveInterpreter(AgentMiddleware):
        def before_model(self, state, runtime):
            observations.append((state.get("_quickjs_slot_id"), list(state["messages"])))

    def call(name, arguments, identifier):
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": arguments, "id": identifier, "type": "tool_call",
        }])

    model = _ToolBindingFakeModel(messages=iter([
        call("js_eval", {"code": "globalThis.parentOnly = 42; 1 + 1"}, "parent-js"),
        call("task", {"description": "Check your interpreter", "subagent_type": "general-purpose"}, "delegate"),
        call("js_eval", {"code": "typeof globalThis.parentOnly"}, "child-js"),
        AIMessage(content="child complete"),
        AIMessage(content="parent complete"),
    ]))
    # SDK 0.7.14 enforces the advertised input budget; the fake's 8k default
    # cannot hold the interpreter and delegation tool schemas.
    model.profile = {"tool_calling": True, "max_input_tokens": 1_000_000}
    graph, _ = create_factory_agent(
        model=model, assistant_id="interpreter-isolation", cwd=tmp_path,
        enable_interpreter=True, enable_memory=False, enable_skills=False,
        interactive=False, auto_approve=True, middleware=[ObserveInterpreter()],
    )
    result = graph.invoke({"messages": [HumanMessage(content="Run the interpreter and delegate")]})
    assert result["messages"][-1].content == "parent complete"
    slots = {slot for slot, _ in observations}
    assert None not in slots
    assert len(slots) == 2
    tool_results = {
        message.tool_call_id: message.content
        for _, messages in observations for message in messages
        if isinstance(message, ToolMessage)
    }
    assert "<result>2</result>" in tool_results["parent-js"]
    assert "<result>undefined</result>" in tool_results["child-js"]
