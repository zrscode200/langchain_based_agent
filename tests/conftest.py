"""Shared test fixtures."""

from __future__ import annotations

import os

import pytest

_ISOLATED_ENV_PREFIXES = (
    "LANGSMITH_",
    "LANGCHAIN_",
    "DEEPAGENTS_CODE_",
    # A maintainer dogfooding with `export LC_FACTORY_MIDDLEWARE=...` would
    # otherwise inject their own middleware into every composition the suite
    # builds — including the parity baseline, which must match v0 exactly.
    "LC_FACTORY_",
)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Compose agents in a deterministic, machine-independent environment.

    Two separate hazards:

    - `$HOME`: the assembly creates agent/skills directories and reads
      subagent definitions under `~/.deepagents`, so an unisolated run both
      litters the home directory and makes branch coverage depend on which
      skills and subagents happen to exist locally.
    - Tracing/config vars: `get_langsmith_project_name()` returns a value
      only when a LangSmith key and tracing flag are both set, which decides
      whether `LocalContextMiddleware`'s tracing arguments are observable in
      composed state. Left inherited, the parity suite's reach would depend
      on whose shell it runs in.
    """
    for key in list(os.environ):
        if key.startswith(_ISOLATED_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home
