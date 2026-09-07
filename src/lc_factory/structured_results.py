"""JSON-schema validation without remote schema retrieval."""
from __future__ import annotations

from copy import deepcopy

from jsonschema import Draft202012Validator
from referencing import Registry

from lc_factory.upstream import AgentMiddleware


def result_validator(schema):
    schema = deepcopy(schema)
    Draft202012Validator.check_schema(schema)
    # Empty registry deliberately denies retrieval of external schema resources.
    return Draft202012Validator(schema, registry=Registry())


class StructuredResultMiddleware(AgentMiddleware):
    """Require actual validated output; raw JSON ToolStrategy does not validate."""
    def __init__(self, schema, *, agent_name):
        self.validator = result_validator(schema)
        self.agent_name = agent_name

    def after_agent(self, state, runtime):
        if "structured_response" not in state:
            raise ValueError(f"Subagent {self.agent_name!r} did not produce its structured result")
        try:
            self.validator.validate(state["structured_response"])
        except Exception as exc:
            # Do not interpolate the response or a remote schema URL into errors.
            raise ValueError(f"Subagent {self.agent_name!r} returned an invalid structured result") from exc
        return None

    async def aafter_agent(self, state, runtime):
        return self.after_agent(state, runtime)
