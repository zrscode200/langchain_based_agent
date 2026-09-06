"""Shared test fixtures."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

# Code 0.1.66 freezes its profile paths when _paths is first imported. Fixtures
# run after test-module imports, so changing HOME only in the fixture is too
# late to keep collection (and all later agent construction) out of the user's
# profile. Establish a temporary launch profile before importing upstream.
_launch_directory = tempfile.TemporaryDirectory(prefix="lc-factory-pytest-")
_launch_home = Path(_launch_directory.name).resolve() / "home"
_launch_home.mkdir()
_launch_environment = pytest.MonkeyPatch()
_launch_environment.setenv("HOME", str(_launch_home))
_launch_environment.setenv("DEEPAGENTS_HOME", str(_launch_home / ".deepagents"))
_launch_environment.setenv("DEEPAGENTS_HOME_IS_DEFAULT", "1")


def pytest_unconfigure(config):
    """Restore the embedding process's environment after pytest finishes."""
    _launch_environment.undo()
    _launch_directory.cleanup()


def _replace_path_snapshot(snapshot):
    """Rebind upstream's imported snapshots and profile-derived constants.

    Some modules use ``from _paths import PATHS``; others freeze paths such as
    model_config.DEFAULT_CONFIG_PATH and config._GLOBAL_DOTENV_PATH. Updating
    only _paths.PATHS would leave those readers on the preceding test profile.
    Restrict rebinding to upstream/factory modules and paths under that profile;
    installation paths keep their ordinary meaning.
    """
    from deepagents_code import _paths, model_config
    from deepagents_code.configuration.service import invalidate_config_sources

    previous = _paths.PATHS
    for name, module in tuple(sys.modules.items()):
        if not name.startswith(("deepagents_code", "lc_factory")):
            continue
        if not isinstance(module, ModuleType):
            continue
        for attribute, value in tuple(vars(module).items()):
            if value is previous:
                setattr(module, attribute, snapshot)
            elif isinstance(value, Path) and value.is_relative_to(previous.profile.root):
                setattr(
                    module,
                    attribute,
                    snapshot.profile.root / value.relative_to(previous.profile.root),
                )
    # ModelConfig.load() returns its process-wide default cache before reading
    # the configured path. The managed snapshot and resolver have independent
    # caches too. Every transition (including teardown) must invalidate all of
    # them or the fresh path can still serve the preceding profile's settings.
    model_config.clear_caches()
    invalidate_config_sources()
    return previous


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

    - Profile paths: upstream captures them at launch, so both HOME and its
      immutable snapshot must be redirected. Each test gets a separate profile
      for agent files, skills, configuration, history, and child processes.
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
    monkeypatch.setenv("DEEPAGENTS_HOME", str(home / ".deepagents"))
    monkeypatch.setenv("DEEPAGENTS_HOME_IS_DEFAULT", "1")

    from deepagents_code import _paths, config, model_config

    snapshot = _paths._capture_paths(None, launch_home=home)
    previous = _replace_path_snapshot(snapshot)
    # These checks pin the two independent consumers that previously escaped
    # HOME-only isolation, and prove the child environment matches the parent.
    assert _paths.get_agent_dir("lc-factory-fixture-probe").is_relative_to(home)
    assert model_config.DEFAULT_CONFIG_PATH == snapshot.profile.config_file
    assert config._GLOBAL_DOTENV_PATH == snapshot.profile.dotenv_file
    assert model_config._default_config_cache is None
    child_env = {}
    _paths.export_profile_env(child_env)
    assert child_env["DEEPAGENTS_HOME"] == os.environ["DEEPAGENTS_HOME"]
    try:
        yield home
    finally:
        # Undo test-local patches first, then also restore modules imported
        # during the test. Otherwise a late imported PATHS alias can outlive its
        # fixture and contaminate the next test's construction.
        monkeypatch.undo()
        _replace_path_snapshot(previous)
