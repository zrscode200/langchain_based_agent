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

import pytest

from lc_factory.server_graph import (
    MIDDLEWARE_REF_ENV,
    _factory_middleware,
    _resolve_middleware_ref,
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


def test_no_project_local_file_can_supply_the_reference(tmp_path, monkeypatch):
    """The reference is user-scoped by construction — pinned behaviorally.

    decisions.md D4 binds this: resolving a config-named module executes it in
    the server process, so a project-local source would let an untrusted
    repository run code merely because `lc-code` was launched inside it.

    The safe property is the *absence* of a feature, so this seeds a directory
    with every plausible project-local config shape someone might later wire
    up, and asserts none of them is consulted.
    """
    monkeypatch.delenv(MIDDLEWARE_REF_ENV, raising=False)
    ref = "lc_factory._testing_middleware:build_marker_middleware"
    (tmp_path / ".env").write_text(f"{MIDDLEWARE_REF_ENV}={ref}\n")
    (tmp_path / "lc_factory.toml").write_text(f'middleware = "{ref}"\n')
    (tmp_path / ".lc-factory").write_text(ref)
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.lc_factory]\nmiddleware = "{ref}"\n'
    )
    monkeypatch.chdir(tmp_path)

    assert _factory_middleware() is None, (
        "a project-local file supplied the middleware reference — entering an "
        "untrusted repository would now execute its code (decisions.md D4)"
    )


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
