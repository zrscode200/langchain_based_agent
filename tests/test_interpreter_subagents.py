"""Compatibility control over JavaScript delegation without altering native task."""
import json
import os
import subprocess
import sys
from xml.etree import ElementTree

import pytest
from deepagents_code._fake_models import _ToolBindingFakeModel
from langchain_core.messages import AIMessage, ToolMessage
from lc_factory.assembly import create_factory_agent


@pytest.mark.parametrize("enabled", [None, True, False])
async def test_js_delegation_surface_and_native_tool_survive(tmp_path, enabled):
    model = _ToolBindingFakeModel(messages=iter([
        AIMessage(content="", tool_calls=[dict(name="js_eval", id="eval", args={
            "code": "[typeof task, typeof tools.taskSettled]"
        })]), AIMessage("done"),
    ]))
    graph, _ = create_factory_agent(model, "flags", cwd=tmp_path,
        enable_memory=False, enable_skills=False, enable_ask_user=False,
        interactive=False, auto_approve=True, enable_interpreter=True,
        enable_settled_dispatch=True, interpreter_subagents=enabled)
    result = await graph.ainvoke({"messages": [{"role": "user", "content": "Inspect JS"}]})
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    parsed = ElementTree.fromstring(f"<eval>{message.content}</eval>")
    expected = "undefined" if enabled is False else "function"
    assert json.loads(parsed.find("result").text) == [expected, expected]
    assert {"task", "task_settled"} <= graph.nodes["tools"].bound.tools_by_name.keys()


@pytest.mark.parametrize("invalid", [0, "false", [], {}])
def test_constructor_rejects_ambiguous_values(invalid):
    with pytest.raises(ValueError, match="interpreter_subagents"):
        create_factory_agent(_ToolBindingFakeModel(), "flags", interpreter_subagents=invalid)


def test_dotenv_cannot_set_interpreter_dispatch_policy(tmp_path):
    (tmp_path / ".env").write_text("LC_FACTORY_INTERPRETER_SUBAGENTS=0\n")
    environment = dict(os.environ)
    environment.pop("LC_FACTORY_INTERPRETER_SUBAGENTS", None)
    code = '''
import json, os, pathlib, lc_factory
from lc_factory.upstream import credentials
credentials.reload_from_environment(start_path=pathlib.Path.cwd())
guarded = os.environ.get("LC_FACTORY_INTERPRETER_SUBAGENTS")
os.environ.pop("LC_FACTORY_INTERPRETER_SUBAGENTS", None)
credentials.reload_from_environment(start_path=pathlib.Path.cwd())
print(json.dumps([guarded, os.environ.get("LC_FACTORY_INTERPRETER_SUBAGENTS")]))
'''
    result = subprocess.run([sys.executable, "-c", code], env=environment, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == ["", "0"]
