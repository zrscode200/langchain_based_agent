"""Verification remains a separate model choice across construction and runs."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from types import MappingProxyType

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from lc_factory import assembly, upstream, verification
from lc_factory._env import VERIFICATION_MODEL_ENV


def _model(*messages):
    from deepagents_code._fake_models import _ToolBindingFakeModel

    return _ToolBindingFakeModel(messages=iter(messages))


def _capture_factory(monkeypatch, tmp_path, **kwargs):
    """Intercept only the outer SDK compile; nested verification graphs are real."""
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(dict)
    builder.add_edge(START, END)
    graph = builder.compile()
    captured = {}

    def compile_outer(**options):
        captured.update(options)
        return graph

    monkeypatch.setattr(assembly, "create_deep_agent", compile_outer)
    assembly.create_factory_agent(
        assistant_id="verification-test", cwd=tmp_path,
        goal_criteria_tools=[], enable_memory=False, enable_skills=False,
        **kwargs,
    )
    assert captured, "Factory no longer reaches the captured SDK compiler"
    return captured


@pytest.mark.parametrize(
    ("dedicated", "rubric_override"),
    [(False, False), (True, False), (True, True), (False, True)],
)
def test_constructor_model_precedence(monkeypatch, tmp_path, dedicated, rubric_override):
    main, verifier, rubric = _model(), _model(), _model()
    calls = {}
    criteria_builder = upstream._create_goal_criteria_agent
    fallback_builder = upstream.create_goal_criteria_fallback_agent
    rubric_builder = assembly.ReliableRubricMiddleware

    def criteria(**kwargs):
        calls["criteria"] = kwargs
        return criteria_builder(**kwargs)

    def fallback(**kwargs):
        calls["fallback"] = kwargs
        return fallback_builder(**kwargs)

    def grader(**kwargs):
        calls["grader"] = kwargs
        return rubric_builder(**kwargs)

    monkeypatch.setattr(upstream, "_create_goal_criteria_agent", criteria)
    monkeypatch.setattr(upstream, "create_goal_criteria_fallback_agent", fallback)
    monkeypatch.setattr(assembly, "ReliableRubricMiddleware", grader)
    captured = _capture_factory(
        monkeypatch, tmp_path, model=main,
        verification_model=verifier if dedicated else None,
        rubric_model=rubric if rubric_override else None,
    )
    expected = verifier if dedicated else main
    assert calls["criteria"]["model"] is expected
    assert calls["fallback"]["model"] is expected
    assert calls["grader"]["model"] is (rubric if rubric_override else expected)
    assert calls["grader"]["inherit_main_model"] is (not dedicated and not rubric_override)
    assert captured["model"] is main


class _RequestProbe(AgentMiddleware):
    def __init__(self, observed):
        self.observed = observed

    def wrap_model_call(self, request, handler):
        self.observed.append(request)
        return handler(request)

    async def awrap_model_call(self, request, handler):
        self.observed.append(request)
        return await handler(request)


@pytest.mark.parametrize("nested", ["criteria", "fallback"])
@pytest.mark.parametrize("async_run", [False, True])
async def test_fixed_model_survives_runtime_selection_without_losing_context(
    monkeypatch, tmp_path, nested, async_run,
):
    """Exercise graphs actually installed by the constructor, not a dummy wrapper."""
    import langchain.agents
    from deepagents_code import config

    observed = []
    original_create = langchain.agents.create_agent

    def create_with_probe(**kwargs):
        if kwargs.get("name") in {"goal_criteria_agent", "goal_criteria_fallback_agent"}:
            kwargs["middleware"] = [*kwargs["middleware"], _RequestProbe(observed)]
        return original_create(**kwargs)

    monkeypatch.setattr(langchain.agents, "create_agent", create_with_probe)
    verifier = _model(AIMessage(content="", tool_calls=[{
        "name": "GoalProposal", "id": "proposal-1",
        "args": {"objective": "Check the report", "criteria": "- Evidence is traceable."},
    }]))
    captured = _capture_factory(
        monkeypatch, tmp_path, model=_model(), verification_model=verifier,
    )
    middleware = next(
        item for item in captured["middleware"]
        if isinstance(item, upstream.GoalCriteriaMiddleware)
    )
    graph = middleware._criteria_agent if nested == "criteria" else middleware._fallback_agent

    def refuse_runtime_switch(*args, **kwargs):
        pytest.fail("Runtime /model selection reached a fixed verification graph")

    monkeypatch.setattr(config, "create_model", refuse_runtime_switch)
    context = upstream.CLIContextSchema(
        model="openai:main-switched", model_params={"temperature": 0.93},
        summarization_model="openai:main-summary", profile_overrides={"max_input_tokens": 17},
        model_context_limit=17, classifier_model="openai:approval-classifier",
        approval_mode="manual", auto_approve=False, approval_mode_key="approval-probe",
        thread_id="verification-thread", turn_id="verification-turn",
        hooks_snapshot_id="hooks-probe", hooks_server_events=["PreToolUse"],
        prompt_id="prompt-probe", workspace={"id": "workspace-probe", "cwd": str(tmp_path)},
    )
    original = dataclasses.asdict(context)
    # Exercise both local dataclass and remote API payload forms.
    payload = dataclasses.asdict(context) if async_run else context
    inputs = {"messages": [{"role": "user", "content": "Check the report"}]}
    if async_run:
        result = await graph.ainvoke(inputs, context=payload)
    else:
        result = graph.invoke(inputs, context=payload)
    assert result["structured_response"].objective == "Check the report"
    assert len(observed) == 1
    request = observed[0]
    assert request.model is verifier
    actual = dataclasses.asdict(request.runtime.context)
    expected = dict(original, model=None, model_params={}, summarization_model=None,
                    profile_overrides={}, model_context_limit=None)
    assert actual == expected
    assert "temperature" not in request.model_settings
    assert dataclasses.asdict(context) == original, "Nested invocation mutated parent context"


def _write_config(home, text):
    from deepagents_code.configuration.service import invalidate_config_sources
    from deepagents_code.model_config import clear_caches

    path = home / ".deepagents" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    clear_caches()
    invalidate_config_sources()


@pytest.mark.parametrize("shell", [False, True])
def test_trusted_configuration_selection_and_retry_metadata(
    isolated_environment, monkeypatch, shell,
):
    from deepagents_code.config import MODEL_RETRIES_ATTR, ModelResult, use_environment

    _write_config(isolated_environment, '''
[lc_factory]
verification_model = "openai:config-verifier"
[models.providers.openai]
models = ["config-verifier", "shell-verifier"]
[retries.openai]
max_retries = 7
''')
    # A real SDK constructor is safe here: no model is invoked. Both the API key
    # and endpoint must come from the captured workspace, not the process.
    monkeypatch.setenv("OPENAI_API_KEY", "process-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://process.invalid/v1")
    environment = MappingProxyType({
        "OPENAI_API_KEY": "workspace-test-key",
        "OPENAI_BASE_URL": "https://workspace.invalid/v1",
        VERIFICATION_MODEL_ENV: " openai:shell-verifier " if shell else "",
    })

    def cannot_change_main(*args):
        pytest.fail("Verification selection changed main-model runtime state")

    monkeypatch.setattr(ModelResult, "apply_to_runtime_state", cannot_change_main)
    with use_environment(environment):
        model = verification.configured_verification_model(environment)
        overridden = verification.configured_verification_model(environment, cli_max_retries=2)
    assert model.model_name == ("shell-verifier" if shell else "config-verifier")
    assert model.openai_api_key.get_secret_value() == "workspace-test-key"
    assert model.openai_api_base == "https://workspace.invalid/v1"
    assert model.max_retries == 0
    assert getattr(model, MODEL_RETRIES_ATTR) == 7
    assert getattr(overridden, MODEL_RETRIES_ATTR) == 2
    assert os.environ["OPENAI_API_KEY"] == "process-test-key"


@pytest.mark.parametrize("ref", ["unknown:verifier", "openai:not-declared"])
def test_configured_model_rejects_undeclared_pairs(isolated_environment, monkeypatch, ref):
    _write_config(isolated_environment, '[models.providers.openai]\nmodels = ["declared"]\n')

    def cannot_construct(*args, **kwargs):
        pytest.fail("Undeclared verification model reached the provider constructor")

    monkeypatch.setattr(verification, "create_model", cannot_construct)
    with pytest.raises(ValueError, match="configured provider/model pair"):
        verification.configured_verification_model({VERIFICATION_MODEL_ENV: ref})


@pytest.mark.parametrize("entry", ["constructor", "configuration"])
def test_verification_policy_denial_precedes_resolution(monkeypatch, tmp_path, entry):
    from deepagents_code.model_config import ModelNotAllowedError

    policy = upstream.ModelConfig(
        providers={"openai": {"models": ["allowed", "blocked"]}},
        allowed_models=("openai:allowed",), allowed_models_source="managed-policy-probe",
    )
    monkeypatch.setattr(upstream.ModelConfig, "load", classmethod(lambda cls: policy))

    def cannot_construct(*args, **kwargs):
        pytest.fail("Blocked verification model reached resolution")

    monkeypatch.setattr(upstream, "create_model", cannot_construct)
    monkeypatch.setattr(verification, "create_model", cannot_construct)
    with pytest.raises(ModelNotAllowedError, match="verification_model"):
        if entry == "constructor":
            _capture_factory(
                monkeypatch, tmp_path, model=_model(), verification_model="openai:blocked",
            )
        else:
            verification.configured_verification_model({VERIFICATION_MODEL_ENV: "openai:blocked"})


def test_constructor_resolves_verifier_with_workspace_credentials_and_retry_budget(
    monkeypatch, tmp_path,
):
    from deepagents_code.config import MODEL_RETRIES_ATTR

    monkeypatch.setenv("OPENAI_API_KEY", "process-test-key")
    main = _model()
    captured = _capture_factory(
        monkeypatch, tmp_path, model=main, verification_model="openai:verifier-probe",
        environ={"OPENAI_API_KEY": "workspace-test-key", "OPENAI_BASE_URL": "https://workspace.invalid/v1"},
        cli_max_retries=3,
    )
    rubric = next(
        item for item in captured["middleware"]
        if isinstance(item, assembly.ReliableRubricMiddleware)
    )
    verifier = rubric._model
    assert verifier is not main
    assert verifier.openai_api_key.get_secret_value() == "workspace-test-key"
    assert verifier.openai_api_base == "https://workspace.invalid/v1"
    assert verifier.max_retries == 0
    assert getattr(verifier, MODEL_RETRIES_ATTR) == 3


@pytest.mark.parametrize("value", ['""', '42', '["openai:verifier"]'])
def test_invalid_configured_verification_model_fails_closed(isolated_environment, value):
    _write_config(isolated_environment, f"[lc_factory]\nverification_model = {value}\n")
    with pytest.raises(ValueError, match="verification"):
        verification.configured_verification_model({})


@pytest.mark.parametrize("failure", ["corrupt", "unreadable"])
def test_unusable_user_config_cannot_silently_select_main_model(
    isolated_environment, monkeypatch, failure,
):
    """An empty failed snapshot is not evidence that no verifier was configured."""
    path = isolated_environment / ".deepagents" / "config.toml"
    if failure == "corrupt":
        _write_config(isolated_environment, "[models\n")
    else:
        # A directory at the file path makes the real reader raise OSError on
        # every platform, including privileged runners where chmod is ineffective.
        path.mkdir(parents=True)
    sources = verification.get_config_sources()
    assert sources.user.status.health.value == failure.upper()
    assert not sources.user.status.usable
    assert sources.user.data == {}

    def cannot_resolve_model(*args, **kwargs):
        pytest.fail("Unusable configuration reached model resolution")

    monkeypatch.setattr(verification, "create_model", cannot_resolve_model)
    with pytest.raises(ValueError, match="Cannot read verification settings from invalid configuration"):
        verification.configured_verification_model({})


@pytest.mark.parametrize("value", ['"wrong shape"', "42", "[]"])
def test_invalid_factory_config_table_cannot_silently_select_main_model(
    isolated_environment, value,
):
    _write_config(isolated_environment, f"lc_factory = {value}\n")
    # This file parses successfully. The factory must validate its own table
    # instead of interpreting a non-mapping as an absent verification setting.
    assert verification.get_config_sources().user.status.usable
    with pytest.raises(ValueError, match=r"\[lc_factory\] must be a table"):
        verification.configured_verification_model({})


def test_absent_verification_configuration_is_inert():
    assert verification.configured_verification_model({}) is None


@pytest.mark.parametrize("shape", ["client", "server"])
def test_dotenv_cannot_choose_verification_model_in_a_fresh_process(tmp_path, shape):
    repo = tmp_path / "untrusted-repository"
    repo.mkdir()
    (repo / ".env").write_text(f"{VERIFICATION_MODEL_ENV}=openai:untrusted\n")
    env = dict(os.environ)
    env.pop(VERIFICATION_MODEL_ENV, None)
    if shape == "server":
        cwd = tmp_path / "server-directory"
        cwd.mkdir()
        env["DEEPAGENTS_CODE_SERVER_CWD"] = str(repo)
    else:
        cwd = repo
        env.pop("DEEPAGENTS_CODE_SERVER_CWD", None)
    probe = '''
import json, os, pathlib
import lc_factory
from lc_factory.upstream import credentials
root = pathlib.Path(os.environ.get("DEEPAGENTS_CODE_SERVER_CWD", os.getcwd()))
credentials.reload_from_environment(start_path=root)
guarded = os.environ.get("LC_FACTORY_VERIFICATION_MODEL")
# Negative control: prove that this exact path really loads the hostile file.
os.environ.pop("LC_FACTORY_VERIFICATION_MODEL", None)
credentials.reload_from_environment(start_path=root)
print(json.dumps([guarded, os.environ.get("LC_FACTORY_VERIFICATION_MODEL")]))
'''
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == ["", "openai:untrusted"]
