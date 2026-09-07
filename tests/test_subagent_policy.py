"""Declarative delegation rejects invalid policy and enforces actual outputs."""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType, SimpleNamespace

import pytest
from deepagents.middleware.filesystem import FilesystemPermission
from deepagents_code._fake_models import _ToolBindingFakeModel
from jsonschema.exceptions import SchemaError, ValidationError
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from lc_factory.structured_results import StructuredResultMiddleware, result_validator
from lc_factory.subagent_policy import (
    checked_specs, prepare_specs,
)
from lc_factory.workspace_subagents import (
    load_subagent_policy, resolve_skill_sources, validate_subagent_policy,
)


def _write_policy(root, content):
    path = root / ".deepagents" / "subagents.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _spec(name="researcher", **kwargs):
    return {"name": name, "description": f"Work assigned to {name}.", **kwargs}


def test_missing_policy_is_inert_but_empty_policy_file_is_not(tmp_path):
    assert load_subagent_policy(None) == {}
    assert load_subagent_policy(tmp_path) == {}
    _write_policy(tmp_path, "")
    with pytest.raises(ValueError, match="subagents"):
        load_subagent_policy(tmp_path)
    _write_policy(tmp_path, "[subagents]\n")
    assert load_subagent_policy(tmp_path) == {}


@pytest.mark.parametrize("content", [
    "[subagents\n",
    "unrecognized = true\n[subagents]\n",
    'subagents = "not a table"\n',
    '[subagents]\nresearcher = "not a table"\n',
    '[subagents.researcher]\nsystem_prompt = "replace the task"\n',
    '[subagents.researcher]\ntools = "read_file"\n',
    '[subagents.researcher]\ntools = ["read_file", "read_file"]\n',
    '[subagents.researcher]\nskills = [""]\n',
    '[subagents.researcher]\nmodel = 42\n',
    '[subagents.researcher]\nmode = "remote"\n',
    '[subagents.researcher]\nresponse_format = "json"\n',
])
def test_present_invalid_policy_never_becomes_inheritance(tmp_path, content):
    _write_policy(tmp_path, content)
    with pytest.raises(ValueError):
        load_subagent_policy(tmp_path)


def test_broken_symlink_and_unreadable_policy_are_errors(tmp_path):
    path = tmp_path / ".deepagents" / "subagents.toml"
    path.parent.mkdir()
    path.symlink_to(tmp_path / "missing-policy.toml")
    with pytest.raises(ValueError, match="broken symlink"):
        load_subagent_policy(tmp_path)
    path.unlink()
    path.mkdir()
    with pytest.raises(ValueError, match="Cannot read subagent policy"):
        load_subagent_policy(tmp_path)


@pytest.mark.parametrize("value", [None, [], {"": {}}, {42: {}}, {"researcher": None}])
def test_policy_names_and_entries_are_validated(value):
    with pytest.raises(ValueError):
        validate_subagent_policy(value)


def test_policy_validation_copies_nested_caller_data():
    supplied = {"researcher": {"tools": ["read_file"], "response_format": {
        "type": "object", "properties": {"answer": {"type": "string"}},
    }}}
    before = deepcopy(supplied)
    checked = validate_subagent_policy(MappingProxyType(supplied))
    checked["researcher"]["tools"].clear()
    checked["researcher"]["response_format"]["properties"]["answer"]["type"] = "integer"
    assert supplied == before


@pytest.mark.parametrize("source", ["../outside/skill", "/outside/skill"])
def test_skill_paths_cannot_escape_workspace(tmp_path, source):
    with pytest.raises(ValueError, match="escapes workspace"):
        resolve_skill_sources("researcher", [source], tmp_path)


def test_skill_containment_follows_symlinks_and_preserves_order(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "outside-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes workspace"):
        resolve_skill_sources("researcher", ["outside-link/skill"], root)
    (root / "skills").mkdir()
    (root / "inside-link").symlink_to(root / "skills", target_is_directory=True)
    assert resolve_skill_sources("researcher", ["inside-link/b", "skills/a"], root) == [
        str(root / "skills" / "b"), str(root / "skills" / "a"),
    ]
    with pytest.raises(ValueError, match="workspace root"):
        resolve_skill_sources("researcher", ["skills/a"], None)


@pytest.mark.parametrize("supplied", ["researcher", b"researcher", {}, set(), frozenset()])
def test_programmatic_specs_must_have_deterministic_order(supplied):
    with pytest.raises(ValueError, match="ordered sequence"):
        checked_specs(supplied)


def test_programmatic_specs_snapshot_lists_preserve_order_and_trusted_objects():
    class Marker(AgentMiddleware):
        pass

    marker = Marker()

    @tool
    def lookup() -> str:
        """Look up a test fact."""
        return "fact"

    permission = FilesystemPermission(operations=["read"], paths=["/references/**"], mode="allow")
    supplied = (_spec("second", tools=(lookup,), middleware=(marker,), permissions=(permission,),
                      skills=("skills/facts",), fs_tools=("read_file",)), _spec("first"))
    checked = checked_specs(iter(supplied))
    assert [item["name"] for item in checked] == ["second", "first"]
    assert checked[0]["tools"][0] is lookup
    assert checked[0]["middleware"][0] is marker
    checked[0]["tools"].clear()
    checked[0]["middleware"].clear()
    checked[0]["skills"].append("skills/new")
    checked[0]["permissions"][0].paths.append("/new/**")
    assert supplied[0]["tools"] == (lookup,)
    assert supplied[0]["middleware"] == (marker,)
    assert supplied[0]["skills"] == ("skills/facts",)
    assert permission.paths == ["/references/**"]


@pytest.mark.parametrize("extra", [
    {"runnable": object()}, {"graph": object()}, {"url": "https://example.invalid"},
    {"interrupt_on": {}}, {"mode": "remote"}, {"mode": "fresh"},
    {"fs_tools": ["delete_database"]}, {"fs_tools": ["read_file", "read_file"]},
    {"tools": {"read_file"}}, {"middleware": {}}, {"skills": "skills/facts"},
    {"system_prompt": 42},
])
def test_unsupported_programmatic_shapes_are_rejected(extra):
    with pytest.raises(ValueError):
        checked_specs([_spec(**extra)])


@pytest.mark.parametrize("supplied", [
    [object()], [_spec(), _spec()], [{"name": "researcher"}],
    [_spec(name=" ")], [_spec(description=" ")],
])
def test_malformed_and_duplicate_specs_are_rejected(supplied):
    with pytest.raises(ValueError):
        checked_specs(supplied)


@pytest.mark.parametrize("permission", [
    FilesystemPermission(operations=["read"], paths=["/references/**"], mode="interrupt"),
    {"operations": ["read"], "paths": ["/**"], "mode": "allow"},
    SimpleNamespace(mode="allow"),
])
def test_permissions_cannot_supply_approval_routing_or_fake_rules(permission):
    with pytest.raises(ValueError, match="permissions"):
        checked_specs([_spec(permissions=[permission])])


def test_policy_constrains_programmatic_replacements_without_mutating_inputs(tmp_path):
    definitions = [_spec("first", system_prompt="file first"), _spec("second", system_prompt="file second")]
    supplied = checked_specs([_spec("second", system_prompt="host replacement"), _spec("third")])
    policy = validate_subagent_policy({"second": {"tools": ["read_file"], "mode": "isolated",
        "skills": ["skills/references"], "model": "openai:worker"}})
    before = deepcopy((definitions, supplied, policy))
    prepared = prepare_specs(definitions, supplied, policy,
        general_purpose=_spec("general-purpose", mode="fork"), project_root=tmp_path)
    assert [item["name"] for item in prepared] == ["first", "second", "third", "general-purpose"]
    worker = prepared[1]
    assert worker["system_prompt"] == "host replacement"
    assert worker["_factory_allowed_tools"] == frozenset({"read_file"})
    assert worker["mode"] == "isolated"
    assert worker["model"] == "openai:worker"
    assert worker["skills"] == [str(tmp_path / "skills" / "references")]
    assert (definitions, supplied, policy) == before


def test_unknown_policy_target_and_fork_skills_are_rejected(tmp_path):
    default = _spec("general-purpose", mode="fork")
    with pytest.raises(ValueError, match="unknown local agents"):
        prepare_specs([], [], {"misspelled": {"tools": []}}, general_purpose=default, project_root=tmp_path)
    with pytest.raises(ValueError, match="cannot declare skills under fork"):
        prepare_specs([], [], {"general-purpose": {"skills": []}}, general_purpose=default, project_root=tmp_path)
    with pytest.raises(ValueError, match="Duplicate file subagent"):
        prepare_specs([_spec(), _spec()], [], {}, general_purpose=default, project_root=tmp_path)


_RESULT_SCHEMA = {
    "title": "WorkerResult", "type": "object", "required": ["score", "sources"],
    "additionalProperties": False,
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "sources": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    },
}


@pytest.mark.parametrize("invalid", [
    {"score": "3", "sources": ["source"]}, {"score": 0, "sources": ["source"]},
    {"score": 6, "sources": ["source"]}, {"score": 3, "sources": []},
    {"score": 3, "sources": [42]}, {"score": 3},
    {"score": 3, "sources": ["source"], "extra": True}, "plain text",
])
def test_actual_values_must_satisfy_declared_schema(invalid):
    validator = result_validator(_RESULT_SCHEMA)
    with pytest.raises(ValidationError):
        validator.validate(invalid)
    with pytest.raises(ValueError, match="invalid structured result"):
        StructuredResultMiddleware(_RESULT_SCHEMA, agent_name="researcher").after_agent(
            {"structured_response": invalid}, None,
        )


def test_schema_snapshot_local_references_and_missing_results():
    schema = {"$defs": {"count": {"type": "integer", "minimum": 1}}, "$ref": "#/$defs/count"}
    validator = result_validator(schema)
    schema["$defs"]["count"]["minimum"] = 100
    validator.validate(2)
    with pytest.raises(ValidationError):
        validator.validate(0)
    with pytest.raises(SchemaError):
        result_validator({"type": "not-a-json-schema-type"})
    with pytest.raises(ValueError, match="did not produce its structured result"):
        StructuredResultMiddleware(_RESULT_SCHEMA, agent_name="researcher").after_agent({}, None)


def test_remote_schema_refs_fail_without_network_or_payload_in_error(monkeypatch):
    import socket
    import urllib.request

    def cannot_fetch(*args, **kwargs):
        pytest.fail("JSON schema validation attempted a remote fetch")

    monkeypatch.setattr(socket, "create_connection", cannot_fetch)
    monkeypatch.setattr(urllib.request, "urlopen", cannot_fetch)
    middleware = StructuredResultMiddleware(
        {"$ref": "https://schemas.invalid/private?credential=schema-secret"}, agent_name="researcher",
    )
    with pytest.raises(ValueError, match="invalid structured result") as error:
        middleware.after_agent({"structured_response": "private-response"}, None)
    assert "private-response" not in str(error.value)
    assert "schema-secret" not in str(error.value)


@pytest.mark.parametrize("async_run", [False, True])
@pytest.mark.parametrize("valid", [False, True])
async def test_real_tool_strategy_graph_checks_structured_values(async_run, valid):
    value = {"score": 3 if valid else "three", "sources": ["reference"]}
    model = _ToolBindingFakeModel(messages=iter([AIMessage(content="", tool_calls=[{
        "name": "WorkerResult", "args": value, "id": "result-1",
    }])]))
    graph = create_agent(model, tools=[], response_format=ToolStrategy(_RESULT_SCHEMA),
        middleware=[StructuredResultMiddleware(_RESULT_SCHEMA, agent_name="researcher")])
    inputs = {"messages": [{"role": "user", "content": "Return a scored result."}]}
    if valid:
        result = await graph.ainvoke(inputs) if async_run else graph.invoke(inputs)
        assert result["structured_response"] == value
    else:
        with pytest.raises(ValueError, match="invalid structured result"):
            if async_run:
                await graph.ainvoke(inputs)
            else:
                graph.invoke(inputs)


def _factory_options(tmp_path, **kwargs):
    return dict(assistant_id="subagent-policy-test", cwd=tmp_path, enable_memory=False,
                enable_skills=False, enable_ask_user=False, enable_shell=False,
                interactive=False, auto_approve=True, **kwargs)


def _call(name, arguments, identifier):
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": identifier}])


@pytest.mark.parametrize("async_run", [False, True])
async def test_explicit_fork_model_stays_fixed_when_parent_runtime_model_changes(
    monkeypatch, tmp_path, async_run,
):
    from deepagents_code import config
    from lc_factory.assembly import create_factory_agent
    from lc_factory.upstream import CLIContextSchema

    observed, selections = [], []

    class ChildProbe(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            observed.append(request)
            return handler(request)

        async def awrap_model_call(self, request, handler):
            observed.append(request)
            return await handler(request)

    # The construction model must never be called after the runtime switch.
    construction_model = _ToolBindingFakeModel(messages=iter([]))
    runtime_model = _ToolBindingFakeModel(messages=iter([
        _call("task", {"description": "Use your specialty", "subagent_type": "specialist"}, "delegate"),
        AIMessage("parent completed"),
    ]))
    child_model = _ToolBindingFakeModel(messages=iter([AIMessage("specialist completed")]))

    def select_runtime(ref, **kwargs):
        assert ref == "openai:runtime-parent"
        selections.append(ref)
        return config.ModelResult(model=runtime_model, model_name="runtime-parent", provider="openai")

    monkeypatch.setattr(config, "create_model", select_runtime)
    graph, _ = create_factory_agent(**_factory_options(
        tmp_path, model=construction_model,
        subagents=[_spec("specialist", mode="fork", model=child_model, middleware=[ChildProbe()])],
    ))
    context = CLIContextSchema(model="openai:runtime-parent", model_params={"temperature": 0.73},
                               workspace={"id": "fixed-child-workspace"})
    inputs = {"messages": [{"role": "user", "content": "Delegate to the specialist."}]}
    result = await graph.ainvoke(inputs, context=context) if async_run else graph.invoke(inputs, context=context)
    assert selections
    assert result["messages"][-1].content == "parent completed"
    delegated = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "delegate")
    assert "specialist completed" in str(delegated.content)
    assert len(observed) == 1
    assert observed[0].model is child_model
    assert "temperature" not in observed[0].model_settings
    assert observed[0].runtime.context.workspace == {"id": "fixed-child-workspace"}


@pytest.mark.parametrize("mode", ["isolated", "fork"])
@pytest.mark.parametrize("async_run", [False, True])
async def test_child_permissions_and_global_filesystem_ceiling_both_apply(tmp_path, mode, async_run):
    from lc_factory.assembly import create_factory_agent

    public = tmp_path / "public.txt"
    private = tmp_path / "private.txt"
    target = tmp_path / "forbidden-write.txt"
    public.write_text("public-evidence")
    private.write_text("private-evidence")
    observed = []

    class ChildMessages(AgentMiddleware):
        def before_model(self, state, runtime):
            observed.extend(m for m in state["messages"] if isinstance(m, ToolMessage))

    model = _ToolBindingFakeModel(messages=iter([
        _call("task", {"description": "Inspect your allowed sources", "subagent_type": "researcher"}, "delegate"),
        _call("read_file", {"file_path": str(public)}, "public"),
        _call("read_file", {"file_path": str(private)}, "private"),
        _call("write_file", {"file_path": str(target), "content": "bad"}, "write"),
        AIMessage("child completed"), AIMessage("parent completed"),
    ]))
    permission = FilesystemPermission(operations=["read"], paths=[str(private)], mode="deny")
    graph, _ = create_factory_agent(**_factory_options(
        tmp_path, model=model, fs_tools=["read_file"],
        subagents=[_spec("researcher", mode=mode, fs_tools=["read_file", "write_file"],
                        permissions=[permission], middleware=[ChildMessages()])],
    ))
    inputs = {"messages": [{"role": "user", "content": "Delegate the review."}]}
    result = await graph.ainvoke(inputs) if async_run else graph.invoke(inputs)
    assert result["messages"][-1].content == "parent completed"
    responses = {m.tool_call_id: str(m.content) for m in observed}
    assert "public-evidence" in responses["public"]
    assert "private-evidence" not in responses["private"]
    assert "denied" in responses["private"].lower()
    assert "not permitted" in responses["write"] or "not a valid tool" in responses["write"]
    assert not target.exists()
    assert permission.paths == [str(private)]
