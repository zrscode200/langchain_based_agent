"""Wave 2.2: the factory-reference transport.

`LC_FACTORY_MIDDLEWARE="module:callable"` is how caller middleware reaches the
server subprocess. These cover resolution and, more importantly, that every
failure mode is loud — a server that boots without the caller's middleware
would serve a differently composed agent than they asked for, silently.

The end-to-end proof (a real `lc-code` run) lives in
`tests/integration_tests/test_headless_session.py`; it cannot live here because
the transport only exists across a process boundary.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from lc_factory.server_graph import (
    MIDDLEWARE_REF_ENV,
    _STARTUP_ERROR_MARKER,
    _factory_middleware,
    _print_startup_error,
    _resolve_middleware_ref,
    reserve_middleware_ref_env,
)


def _returns_none():
    """A factory that forgot its `return` statement."""
    [object()]


def _returns_a_string():
    return "not middleware"


def _needs_arguments(config):
    return [config]


def _explodes():
    msg = "factory blew up"
    raise RuntimeError(msg)


def _yields_middleware():
    """A factory returning a one-shot iterable — legal seam input."""
    from lc_factory._testing_middleware import build_marker_middleware

    return (item for item in build_marker_middleware())


def _yields_middleware_in_mapping():
    from lc_factory._testing_middleware import build_marker_middleware

    return {"first": (item for item in build_marker_middleware())}


def _returns_bare_instance_in_mapping():
    from lc_factory._testing_middleware import build_marker_middleware

    return {"first": build_marker_middleware()[0]}


def test_unset_variable_yields_no_injection(monkeypatch):
    """Absent configuration must compose exactly as it did before the seam."""
    monkeypatch.delenv(MIDDLEWARE_REF_ENV, raising=False)
    assert _factory_middleware() is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_variable_yields_no_injection(monkeypatch, blank):
    """An exported-but-empty variable is 'unset', not a malformed reference."""
    monkeypatch.setenv(MIDDLEWARE_REF_ENV, blank)
    assert _factory_middleware() is None


def test_valid_reference_is_imported_and_called(monkeypatch):
    monkeypatch.setenv(
        MIDDLEWARE_REF_ENV,
        "lc_factory._testing_middleware:build_marker_middleware",
    )
    resolved = _factory_middleware()
    # The transport returns NORMALIZED middleware per target, not the factory's
    # raw return: a bare sequence still means the main agent, default phase.
    # See test_one_shot_factory_results_are_materialized_not_dropped for why
    # normalization happens here rather than downstream.
    assert [item.name for item in resolved["main"]["before_verification"]] == [
        "LcFactoryMarkerMiddleware"
    ]
    # And the other targets stay empty, so an unaddressed target composes
    # exactly as it did before this variable existed.
    assert all(not items for items in resolved["subagents"].values())
    assert all(not items for items in resolved["grader"].values())


def test_reference_may_return_a_phase_mapping(monkeypatch):
    """Phase choice survives the env round trip, not just the sequence form."""
    monkeypatch.setenv(
        MIDDLEWARE_REF_ENV,
        "lc_factory._testing_middleware:build_phase_keyed_middleware",
    )
    resolved = _factory_middleware()
    main = resolved["main"]
    assert [item.name for item in main["first"]] == ["LcFactoryMarkerMiddleware"]
    assert main["before_verification"] == []
    assert main["last"] == []


def test_one_shot_factory_results_are_materialized_not_dropped():
    """Regression: validation consumed the factory's one-shot return.

    The resolve validated by normalizing — which materializes generators via
    `list()` — then DISCARDED the normalized result and returned the exhausted
    original, so a generator-returning factory booted a server with the
    caller's middleware silently absent (every phase empty at the assembly).
    The transport's whole contract is that this failure class is impossible.
    The resolve must return the materialized normalization.
    """
    resolved = _resolve_middleware_ref(f"{__name__}:_yields_middleware")
    assert [item.name for item in resolved["main"]["before_verification"]] == [
        "LcFactoryMarkerMiddleware"
    ]

    keyed = _resolve_middleware_ref(f"{__name__}:_yields_middleware_in_mapping")
    assert [item.name for item in keyed["main"]["first"]] == [
        "LcFactoryMarkerMiddleware"
    ]


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("no_separator", "must be 'module.path:callable'"),
        (":build_marker_middleware", "must be 'module.path:callable'"),
        ("lc_factory._testing_middleware:", "must be 'module.path:callable'"),
        ("lc_factory.does_not_exist:thing", "cannot import module"),
        ("lc_factory._testing_middleware:missing", "has no attribute"),
        ("lc_factory._testing_middleware:MARKER_PATH_ENV", "is not callable"),
        # A factory that forgets `return` — the likeliest authoring mistake in
        # a zero-argument factory, and the one that would otherwise boot a
        # working-but-wrong agent with no signal at all.
        (f"{__name__}:_returns_none", "returned None"),
        (f"{__name__}:_returns_a_string", "returned str"),
        # A factory that takes arguments: raises TypeError, which must still be
        # attributed to the variable rather than surfacing bare.
        (f"{__name__}:_needs_arguments", "raised TypeError"),
        (f"{__name__}:_explodes", "raised RuntimeError"),
        # A bare instance as a phase value — the natural typo. `list()` raises
        # TypeError, which the normalizer must convert to the documented
        # ValueError or it would bypass this funnel and surface unattributed.
        (f"{__name__}:_returns_bare_instance_in_mapping", "not iterable"),
    ],
)
def test_every_bad_reference_raises_rather_than_degrading(ref, expected):
    """No malformed reference may resolve to 'compose without it'.

    Silently booting an agent the caller did not ask for is the failure class
    this whole group has been eliminating, so each case is enumerated.
    """
    with pytest.raises(ValueError, match=expected):
        _resolve_middleware_ref(ref)


def test_failures_name_the_variable(monkeypatch):
    """The message must point at the knob, since it is set outside the app."""
    monkeypatch.setenv(MIDDLEWARE_REF_ENV, "definitely_not_a_module:factory")
    with pytest.raises(ValueError, match=MIDDLEWARE_REF_ENV):
        _factory_middleware()


def test_startup_error_marker_flattens_multiline_message(capsys):
    """The parent parser consumes one marked line; keep the full human output."""
    from deepagents_code.client.launch.server import _extract_startup_error_marker

    _print_startup_error("first line\nsecond line")
    stderr = capsys.readouterr().err

    assert stderr.splitlines() == [
        "first line",
        "second line",
        f"{_STARTUP_ERROR_MARKER}first line second line",
    ]
    assert _extract_startup_error_marker(stderr) == "first line second line"


def test_the_transport_survives_the_upstream_server_env_filter(monkeypatch):
    """The variable must actually reach the server subprocess.

    The whole transport rests on `_build_server_env` filtering by an explicit
    key denylist rather than by prefix. If an upstream release ever adds a
    prefix sweep for non-`DEEPAGENTS_` variables, the transport is severed and
    every user silently boots without their middleware — with the default
    suite fully green, because the only other coverage is the integration
    marker, which `addopts` excludes. This is that tripwire, and it costs
    ~0.1s.
    """
    from deepagents_code.client.launch.server import _build_server_env

    monkeypatch.setenv(MIDDLEWARE_REF_ENV, "pkg.mod:build")
    relayed = _build_server_env()
    assert relayed.get(MIDDLEWARE_REF_ENV) == "pkg.mod:build", (
        "upstream's server env filter no longer relays LC_FACTORY_* — the "
        "middleware transport is severed and injection silently stops working"
    )
    # Guard the guard: PYTHONPATH is denylisted, so if that is also relayed the
    # filter has stopped filtering and this assertion proves nothing.
    monkeypatch.setenv("PYTHONPATH", "/tmp/should-not-be-relayed")
    assert "PYTHONPATH" not in _build_server_env()


_D4_PROBE = """
import os, sys, pathlib
{sabotage}
import lc_factory.server_graph as sg
from lc_factory.upstream import credentials, get_server_project_context

# Exercise the still-supported client/explicit reload API under both dotenv
# discovery shapes. The server now uses immutable snapshots instead; its real
# snapshot path is covered in test_workspace_isolation.py.
ctx = get_server_project_context()
credentials.reload_from_environment(
    start_path=ctx.user_cwd if ctx is not None else pathlib.Path.cwd()
)
resolved = sg._factory_middleware()
print("INJECTED" if resolved else "SAFE")
"""


def _run_d4_probe(repo, tmp_path, shape: str, *, sabotage: str = "") -> str:
    """Import lc_factory in a FRESH interpreter, in one of two real shapes.

    Must be a subprocess. The contamination happens at *import* time — upstream's
    credential resolution loads `.env` into `os.environ`. By the time any
    in-process test runs, the lazy credential proxy has already been resolved
    under pytest's cwd, so an in-process version cannot reproduce either real
    startup shape reliably.

    The two shapes traverse *different upstream code* and must both be covered:

    - `client`: cwd is the repository, so upstream bootstraps via
      `find_dotenv()` searching upward.
    - `server`: cwd is a private temp dir (as the launcher makes it) and the
      repository is reached only through `DEEPAGENTS_CODE_SERVER_CWD`, so
      upstream goes down `_find_dotenv_from_start_path` instead. The private cwd
      buys nothing — this is the shape that was actually exploitable.
    """
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != MIDDLEWARE_REF_ENV}
    if shape == "server":
        cwd = tmp_path / "server_workdir"
        cwd.mkdir(exist_ok=True)
        env["DEEPAGENTS_CODE_SERVER_CWD"] = str(repo)
    else:
        cwd = repo
        env.pop("DEEPAGENTS_CODE_SERVER_CWD", None)

    result = subprocess.run(
        [sys.executable, "-c", _D4_PROBE.format(sabotage=sabotage)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip()


def _seed_hostile_repo(tmp_path):
    repo = tmp_path / "cloned_repo"
    repo.mkdir()
    (repo / ".env").write_text(
        f"{MIDDLEWARE_REF_ENV}=lc_factory._testing_middleware:build_marker_middleware\n"
        f"LC_FACTORY_TEST_MIDDLEWARE_MARKER={tmp_path / 'pwned.txt'}\n"
    )
    return repo


@pytest.mark.parametrize("shape", ["client", "server"])
def test_a_project_dotenv_cannot_supply_the_reference(tmp_path, shape):
    """decisions.md D4, pinned where it can actually fail.

    A `.env` travels with a cloned repository and upstream loads it straight
    into `os.environ`, so without a guard `git clone && lc-code` lets the repo
    name a module for the server to import and call before any approval gate.
    Upstream denies a fixed set of keys from `.env` for exactly this reason; it
    cannot know about ours, and its denylist is a frozenset.

    Both process shapes are covered because they reach different upstream code,
    and the one that was actually exploitable is the server shape.
    """
    assert _run_d4_probe(_seed_hostile_repo(tmp_path), tmp_path, shape) == "SAFE", (
        f"a project .env supplied the middleware reference in the {shape} "
        f"shape — cloning a repository and running lc-code inside it would "
        f"execute code it chose, before any approval gate (decisions.md D4)"
    )


@pytest.mark.parametrize("shape", ["client", "server"])
def test_the_d4_probe_can_actually_fail(tmp_path, shape):
    """Negative control: prove the guard above is load-bearing.

    Three earlier versions of this pin passed against vulnerable code — one
    skipped the `.env` reload, one called the reservation from a test body in a
    process where the import had already happened, and one removed the
    reservation at a call site that was already too late, measuring the
    mechanism rather than its placement. Each validated a *sub-sequence* of the
    real lifecycle instead of the lifecycle.

    So the control is parametrized too: a control covering only one shape has
    the same blind spot as a pin covering only one shape.
    """
    injected = _run_d4_probe(
        _seed_hostile_repo(tmp_path),
        tmp_path,
        shape,
        # Undo the reservation, then re-trigger the project .env load the
        # client/reload API supports — i.e. the pre-reservation world.
        sabotage=(
            "import lc_factory\n"
            "os.environ.pop('LC_FACTORY_MIDDLEWARE', None)\n"
            "from lc_factory.upstream import credentials\n"
            "credentials.reload_from_environment(start_path=pathlib.Path.cwd())\n"
        ),
    )
    assert injected == "INJECTED", (
        "the D4 probe reports SAFE even with the reservation removed — it is "
        "not testing the guard, and a green result proves nothing"
    )


def test_other_project_local_shapes_are_not_consulted_either(tmp_path, monkeypatch):
    """No file-based source exists at all — the safe property is an absence.

    Bounded by construction: this enumerates the shapes someone might plausibly
    wire up later, not every conceivable filename.
    """
    monkeypatch.delenv(MIDDLEWARE_REF_ENV, raising=False)
    ref = "lc_factory._testing_middleware:build_marker_middleware"
    (tmp_path / "lc_factory.toml").write_text(f'middleware = "{ref}"\n')
    (tmp_path / ".lc-factory").write_text(ref)
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.lc_factory]\nmiddleware = "{ref}"\n'
    )
    monkeypatch.chdir(tmp_path)

    assert _factory_middleware() is None


def test_reservation_makes_the_variable_unsettable_from_a_dotenv(tmp_path, monkeypatch):
    """Pin the mechanism, not just the outcome.

    The guard works because upstream's `apply_dotenv` skips keys already in
    `os.environ`. If that precedence ever changes, the outcome test above still
    needs to fail for a comprehensible reason — this says which assumption broke.
    """
    from lc_factory.upstream import credentials

    monkeypatch.delenv(MIDDLEWARE_REF_ENV, raising=False)
    (tmp_path / ".env").write_text(f"{MIDDLEWARE_REF_ENV}=evil_pkg:pwn\n")
    monkeypatch.chdir(tmp_path)

    reserve_middleware_ref_env()
    assert os.environ[MIDDLEWARE_REF_ENV] == ""
    credentials.reload_from_environment(start_path=tmp_path)
    assert os.environ[MIDDLEWARE_REF_ENV] == "", (
        "upstream's .env loader no longer skips keys already present in "
        "os.environ — the reservation guard is void and a project .env can "
        "again name code for the server to import"
    )


def test_a_shell_export_still_works(tmp_path, monkeypatch):
    """The reservation must not break the legitimate path it protects."""
    monkeypatch.setenv(
        MIDDLEWARE_REF_ENV,
        "lc_factory._testing_middleware:build_marker_middleware",
    )
    reserve_middleware_ref_env()  # setdefault must not clobber a real value
    resolved = _factory_middleware()
    assert [item.name for item in resolved["main"]["before_verification"]] == [
        "LcFactoryMarkerMiddleware"
    ]


def test_no_module_writes_the_reference_into_the_environment():
    """Nothing in `src/` may populate the variable on the caller's behalf.

    The behavioral pin above covers the resolver reading a file. This covers
    the other direction: a module reading a project file and exporting the
    result into `os.environ`, which would defeat the invariant without the
    resolver changing at all.
    """
    import ast
    from pathlib import Path

    import lc_factory

    _MUTATORS = {"setdefault", "update", "pop", "popitem", "clear", "__setitem__"}

    def _is_the_reservation(node: ast.Call) -> bool:
        """Allow exactly `os.environ.setdefault(MIDDLEWARE_REF_ENV, "")`.

        The reservation is the guard itself, not a hole in it: it claims the
        slot with an EMPTY value, which reads as unset, so it can only ever
        prevent a `.env` from supplying a reference — never supply one.
        Anything that puts a real value in is still an offender.
        """
        return (
            node.func.attr == "setdefault"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "MIDDLEWARE_REF_ENV"
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == ""
        )

    def _is_environ(node: ast.AST) -> bool:
        """True for `os.environ` and for a bare `environ` from `from os import`."""
        return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
            isinstance(node, ast.Name) and node.id == "environ"
        )

    package_dir = Path(lc_factory.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `os.environ[K] = v`
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and _is_environ(target.value):
                        offenders.append(f"{path.name}:{node.lineno} (assign)")
            # `os.environ.setdefault(...)` / `.update(...)` / ..., and
            # `os.putenv(...)` — every mutating route, not just subscripting.
            # Reads (`get`, `copy`, ...) are fine and deliberately not listed.
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if _is_environ(node.func.value) and node.func.attr in _MUTATORS:
                    if _is_the_reservation(node):
                        continue
                    offenders.append(
                        f"{path.name}:{node.lineno} (environ.{node.func.attr})"
                    )
                elif node.func.attr == "putenv":
                    offenders.append(f"{path.name}:{node.lineno} (putenv)")
    assert not offenders, (
        f"module(s) mutate the process environment: {offenders}. Verify none "
        f"can set {MIDDLEWARE_REF_ENV} from a project-local source "
        f"(decisions.md D4). If a future wave legitimately needs an "
        f"environment write, narrow this test to allowlist that key — do not "
        f"delete it."
    )


# --- SEAM-REACH-TRANSPORT: target-keyed returns ----------------------------
#
# Group 3 gave the factory `subagent_middleware=` and
# `rubric_grader_middleware=`, but `server_graph` passed neither, so `lc-code`
# — the only caller of `create_factory_agent` outside tests — could not reach
# them. decisions.md D4 rejects exactly that library-only shape. These cover
# the fix: one variable, target-keyed, no new `.env` surface.


def _targets_all_three():
    from lc_factory._testing_middleware import build_marker_middleware

    return {
        "main": build_marker_middleware(),
        "subagents": {"first": build_marker_middleware()},
        "grader": build_marker_middleware(),
    }


def _targets_subagents_only():
    from lc_factory._testing_middleware import build_marker_middleware

    return {"subagents": build_marker_middleware()}


def _targets_mixed_with_phase_keys():
    """The ambiguous shape: target keys and phase keys at the same level."""
    from lc_factory._testing_middleware import build_marker_middleware

    return {"main": build_marker_middleware(), "first": build_marker_middleware()}


def _targets_unknown_key():
    from lc_factory._testing_middleware import build_marker_middleware

    return {"criteria": build_marker_middleware()}


def test_target_keyed_return_reaches_every_target():
    resolved = _resolve_middleware_ref(f"{__name__}:_targets_all_three")
    assert [m.name for m in resolved["main"]["before_verification"]] == [
        "LcFactoryMarkerMiddleware"
    ]
    assert [m.name for m in resolved["subagents"]["first"]] == [
        "LcFactoryMarkerMiddleware"
    ]
    # Each target keeps its OWN default phase: `last` for subagents and grader,
    # `before_verification` for the main agent. A shared default would quietly
    # move injections on two of the three.
    assert [m.name for m in resolved["grader"]["last"]] == [
        "LcFactoryMarkerMiddleware"
    ]


def test_addressing_one_target_leaves_the_others_inert():
    resolved = _resolve_middleware_ref(f"{__name__}:_targets_subagents_only")
    assert [m.name for m in resolved["subagents"]["last"]] == [
        "LcFactoryMarkerMiddleware"
    ]
    assert all(not items for items in resolved["main"].values())
    assert all(not items for items in resolved["grader"].values())


def test_mixing_target_and_phase_keys_is_rejected_not_guessed():
    """The whole disambiguation rests on the two key sets being disjoint.

    A mapping using both is genuinely ambiguous, and guessing would compose an
    agent the caller did not ask for — silently. That is the failure class this
    transport exists to prevent, so it must be an error.
    """
    with pytest.raises(ValueError, match="mixing target keys") as excinfo:
        _resolve_middleware_ref(f"{__name__}:_targets_mixed_with_phase_keys")
    assert MIDDLEWARE_REF_ENV in str(excinfo.value)
    assert "first" in str(excinfo.value)


def test_unknown_key_falls_through_to_main_and_names_valid_phases():
    """A non-target key is treated as a (bad) phase key for the main agent.

    `criteria` is the plausible wrong guess — the goal-criteria agent is not a
    target and cannot be, since upstream gives it no middleware parameter. The
    error must say so by listing what IS valid rather than silently dropping it.
    """
    with pytest.raises(ValueError, match="Unknown middleware phase") as excinfo:
        _resolve_middleware_ref(f"{__name__}:_targets_unknown_key")
    assert "criteria" in str(excinfo.value)
    assert "before_verification" in str(excinfo.value)


def test_per_target_errors_name_the_target():
    """With three targets, "unusable middleware" alone is not actionable."""

    def _bad_subagent_phase():
        return {"subagents": {"before_verification": []}}

    import sys

    sys.modules[__name__]._bad_subagent_phase = _bad_subagent_phase
    with pytest.raises(ValueError, match="for target 'subagents'") as excinfo:
        _resolve_middleware_ref(f"{__name__}:_bad_subagent_phase")
    assert MIDDLEWARE_REF_ENV in str(excinfo.value)


def test_graph_factory_runtime_annotation_is_resolvable():
    """LangGraph resolves the annotation to recognize a context-aware factory."""
    from typing import get_type_hints

    from lc_factory import server_graph

    hints = get_type_hints(server_graph.make_graph)
    assert hints["runtime"] == (
        server_graph.LangGraphServerRuntime[server_graph.CLIContextSchema] | None
    )


async def test_graph_discovery_keeps_default_runtime(monkeypatch):
    from types import SimpleNamespace

    from lc_factory import server_graph

    graph = object()

    async def get_runtime():
        return SimpleNamespace(agent=graph)

    monkeypatch.setattr(server_graph, "_get_runtime", get_runtime)
    assert await server_graph.make_graph() is graph
    assert await server_graph.make_graph(
        runtime=SimpleNamespace(execution_runtime=None)
    ) is graph


@pytest.mark.parametrize(
    ("config", "context"),
    [(None, {}), ({"configurable": {"thread_id": ""}}, {}), ({}, None)],
)
async def test_execution_requires_thread_and_context(config, context):
    from types import SimpleNamespace

    from lc_factory import server_graph

    runtime = SimpleNamespace(execution_runtime=SimpleNamespace(context=context))
    with pytest.raises(ValueError, match="thread id and workspace context"):
        await server_graph.make_graph(config=config, runtime=runtime)


async def test_execution_validates_durable_workspace_before_selecting_graph(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from deepagents_code.workspace import WorkspaceConflictError, bind_thread_workspace

    from lc_factory import server_graph

    monkeypatch.setenv("DEEPAGENTS_CODE_SERVER_DB_PATH", str(tmp_path / "sessions.db"))
    binding = await bind_thread_workspace("factory-thread", str(tmp_path))
    graph = object()
    selected = []

    async def workspace_runtime(verified_binding):
        selected.append(verified_binding)
        return SimpleNamespace(agent=graph)

    monkeypatch.setattr(server_graph, "_workspace_runtime", workspace_runtime)
    context = {"workspace": binding.to_payload()}
    runtime = SimpleNamespace(execution_runtime=SimpleNamespace(context=context))
    config = {"configurable": {"thread_id": "factory-thread"}}
    assert await server_graph.make_graph(config=config, runtime=runtime) is graph
    assert selected == [binding]

    context["workspace"]["resource_key"] = "another-workspace"
    with pytest.raises(WorkspaceConflictError, match="does not match"):
        await server_graph.make_graph(config=config, runtime=runtime)
    assert selected == [binding]


async def test_workspace_runtime_reuses_resources_and_separates_workspaces(
    monkeypatch, tmp_path
):
    import asyncio
    from collections import OrderedDict

    from deepagents_code.workspace import resolve_workspace

    from lc_factory import server_graph

    monkeypatch.setattr(server_graph, "_workspace_runtimes", OrderedDict())
    monkeypatch.setattr(server_graph, "_workspace_runtime_lock", asyncio.Lock())
    monkeypatch.setattr(server_graph, "_MAX_WORKSPACE_RUNTIMES", 2)
    config = server_graph.ServerConfig.from_env()
    built = []

    async def build(*, config_override, project_context_override):
        await asyncio.sleep(0)  # Overlap requests while initialization holds its lock.
        assert config_override.cwd == str(project_context_override.user_cwd)
        result = object()
        built.append((config_override.cwd, result))
        return result

    monkeypatch.setattr(server_graph, "_make_graphs", build)
    bindings = []
    for name in ("first", "second", "third"):
        cwd = tmp_path / name
        cwd.mkdir()
        bindings.append(resolve_workspace(
            str(cwd),
            config.to_workspace_payload(),
            config_fingerprint=config.workspace_fingerprint(),
        ))

    first, duplicate = await asyncio.gather(
        server_graph._workspace_runtime(bindings[0]),
        server_graph._workspace_runtime(bindings[0]),
    )
    assert first is duplicate
    assert len(built) == 1
    second = await server_graph._workspace_runtime(bindings[1])
    assert second is not first
    assert await server_graph._workspace_runtime(bindings[0]) is first
    await server_graph._workspace_runtime(bindings[2])
    assert bindings[1].resource_key not in server_graph._workspace_runtimes
    assert bindings[0].resource_key in server_graph._workspace_runtimes


@pytest.mark.parametrize("drift", ["fingerprint", "resource_policy"])
async def test_workspace_runtime_rejects_changed_config_before_build(
    monkeypatch, tmp_path, drift
):
    from collections import OrderedDict
    from dataclasses import replace

    from deepagents_code.workspace import resolve_workspace

    from lc_factory import server_graph

    monkeypatch.setattr(server_graph, "_workspace_runtimes", OrderedDict())
    monkeypatch.setattr(server_graph, "_workspace_runtime_lock", asyncio.Lock())
    config = server_graph.ServerConfig.from_env()
    binding = resolve_workspace(
        str(tmp_path),
        config.to_workspace_payload(),
        config_fingerprint=config.workspace_fingerprint(),
    )
    if drift == "fingerprint":
        binding = replace(binding, config_fingerprint="stale-fingerprint")
    else:
        binding = replace(binding, workspace_config_json='{"enable_shell": false}')

    async def unexpected_build(**kwargs):
        pytest.fail("A changed workspace policy reached graph construction")

    monkeypatch.setattr(server_graph, "_make_graphs", unexpected_build)
    with pytest.raises(RuntimeError, match="configuration changed"):
        await server_graph._workspace_runtime(binding)


async def test_overridden_graph_build_keeps_all_middleware_targets_and_model_policy(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from lc_factory import assembly, server_graph

    main, subagents, grader = object(), object(), object()
    calls = {}
    graph, backend, offload = object(), object(), object()
    config = server_graph.ServerConfig(
        model="test:worker",
        summarization_model="test:summarizer",
        auto_classifier_model="test:reviewer",
        enable_interpreter=False,
    )
    project_context = server_graph.ProjectContext(user_cwd=tmp_path, project_root=None)
    result = SimpleNamespace(
        model=object(), provider="test", model_retries=2, cli_max_retries=3,
        apply_to_runtime_state=lambda: calls.setdefault("model_applied", True),
    )

    async def build_tools(received_config, received_context, **kwargs):
        assert received_config is config
        assert received_context is project_context
        calls["tool_credentials"] = kwargs
        return [], [], []

    def build_agent(**kwargs):
        calls["assembly"] = kwargs
        return graph, backend

    def resolve_classifier(provider, classifier):
        assert (provider, classifier) == ("test", "test:reviewer")
        return "resolved:reviewer"

    monkeypatch.setattr(server_graph, "configure_langsmith_secret_redaction", lambda: None)
    monkeypatch.setattr(server_graph, "_factory_middleware", lambda: {
        "main": main, "subagents": subagents, "grader": grader,
    })
    monkeypatch.setattr(server_graph, "create_model", lambda *args, **kwargs: result)
    monkeypatch.setattr(server_graph, "_build_tools", build_tools)
    monkeypatch.setattr(server_graph, "load_async_subagents", lambda: [])
    monkeypatch.setattr(server_graph, "is_memory_auto_save_enabled", lambda: False)
    monkeypatch.setattr(server_graph, "resolve_auto_classifier_model_for_provider", resolve_classifier)
    monkeypatch.setattr(server_graph, "offload_operation_from", lambda value: offload)
    monkeypatch.setattr(assembly, "create_factory_agent", build_agent)

    runtime = await server_graph._make_graphs(
        config_override=config, project_context_override=project_context,
    )
    assert (runtime.agent, runtime.backend, runtime.offload) == (graph, backend, offload)
    assert calls["model_applied"] is True
    kwargs = calls["assembly"]
    assert calls["tool_credentials"] == {
        "has_tavily": kwargs["credentials_snapshot"].has_tavily,
        "tavily_api_key": kwargs["credentials_snapshot"].tavily_api_key,
    }
    assert kwargs["model_result"] is result
    with pytest.raises(TypeError):
        kwargs["environ"]["mutated"] = "forbidden"
    assert kwargs["middleware"] is main
    assert kwargs["subagent_middleware"] is subagents
    assert kwargs["rubric_grader_middleware"] is grader
    assert kwargs["summarization_model"] == "test:summarizer"
    assert kwargs["auto_classifier_model"] == "resolved:reviewer"
    assert kwargs["project_context"] is project_context
