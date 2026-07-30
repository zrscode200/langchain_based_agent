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
    assert [item.name for item in resolved] == ["LcFactoryMarkerMiddleware"]


def test_reference_may_return_a_phase_mapping(monkeypatch):
    """Phase choice survives the env round trip, not just the sequence form."""
    monkeypatch.setenv(
        MIDDLEWARE_REF_ENV,
        "lc_factory._testing_middleware:build_phase_keyed_middleware",
    )
    resolved = _factory_middleware()
    assert list(resolved) == ["first"]
    assert [item.name for item in resolved["first"]] == ["LcFactoryMarkerMiddleware"]


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
from lc_factory.upstream import get_server_project_context, settings

# Mirror `_make_graph`: it reloads settings from the project context before
# resolving, which is a second, separate chance for a project `.env` to land in
# os.environ.
ctx = get_server_project_context()
settings.reload_from_environment(
    start_path=ctx.user_cwd if ctx is not None else pathlib.Path.cwd()
)
resolved = sg._factory_middleware()
print("INJECTED" if resolved else "SAFE")
"""


def _run_d4_probe(repo, tmp_path, shape: str, *, sabotage: str = "") -> str:
    """Import lc_factory in a FRESH interpreter, in one of two real shapes.

    Must be a subprocess. The contamination happens at *import* time — upstream's
    config module has a PEP 562 `__getattr__` that bootstraps settings and loads
    `.env` on first attribute access, so simply importing `lc_factory.upstream`
    triggers it. By the time any in-process test function runs, `lc_factory` has
    long since been imported under pytest's own cwd, so an in-process version of
    this test cannot fail no matter what it asserts.

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
        # server performs — i.e. exactly the pre-fix world.
        sabotage=(
            "import lc_factory\n"
            "os.environ.pop('LC_FACTORY_MIDDLEWARE', None)\n"
            "from lc_factory.upstream import settings\n"
            "settings.reload_from_environment(start_path=pathlib.Path.cwd())\n"
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
    from lc_factory.upstream import settings

    monkeypatch.delenv(MIDDLEWARE_REF_ENV, raising=False)
    (tmp_path / ".env").write_text(f"{MIDDLEWARE_REF_ENV}=evil_pkg:pwn\n")
    monkeypatch.chdir(tmp_path)

    reserve_middleware_ref_env()
    assert os.environ[MIDDLEWARE_REF_ENV] == ""
    settings.reload_from_environment(start_path=tmp_path)
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
    assert [item.name for item in resolved] == ["LcFactoryMarkerMiddleware"]


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
