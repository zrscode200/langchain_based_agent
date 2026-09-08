"""Environment contract for the factory, and the guard that protects it.

**This module must not import anything from `lc_factory` or upstream.** It is
imported by `lc_factory/__init__.py` before any other module in the package,
and that ordering is the entire point — see `reserve_middleware_ref_env`.
"""

from __future__ import annotations

import os

MIDDLEWARE_REF_ENV = "LC_FACTORY_MIDDLEWARE"
"""Env var naming a zero-argument callable that supplies factory middleware.

Format is ``"module.path:callable"`` — the same shape upstream already uses for
``graph_ref``, ``checkpointer_path``, and model ``class_path``. Resolved by
:mod:`lc_factory.server_graph`, which owns the semantics; this module owns only
the name and the guard, because both are needed before that module can be
imported.
"""

VERIFICATION_MODEL_ENV = "LC_FACTORY_VERIFICATION_MODEL"


def reserve_middleware_ref_env() -> None:
    """Claim ``LC_FACTORY_MIDDLEWARE`` so no ``.env`` file can introduce it.

    Resolving the reference imports and executes a named module inside the
    server process, so it must come from a real shell export: a committed
    ``.env`` in a cloned repository must never be able to name code that runs
    on ``lc-code`` startup, before any approval gate (decisions.md D4).

    Reading ``os.environ`` does not give that by itself. Upstream loads ``.env``
    files straight into it, on two paths that both precede resolution — the
    client's first access to the lazy ``credentials`` proxy bootstraps from the
    cwd, and the server explicitly reloads credentials from the project context,
    which points at the user's repository even though the server's own cwd is a
    private temporary directory. Upstream keeps a denylist of keys that "turn
    `.env` loading into code execution", but it is a ``frozenset`` and cannot
    know about this variable.

    So this occupies the slot instead: upstream's ``apply_dotenv`` skips any key
    already present in ``os.environ``, so a pre-set value — even an empty one,
    which reads as "unset" — makes the variable unsettable from any ``.env``.

    **Placement is the whole guard, and it is subtle.** The
    ``deepagents_code.config.credentials`` object is a lazy proxy whose first
    field access runs the dotenv bootstrap. The boundary is imported and its
    credential-dependent helpers are used early enough that the package must
    reserve the variable before any submodule can reach them. An earlier
    version of this guard called it from ``server_graph`` module scope and from
    ``tui.main()``; both run *after* that
    import and were silently no-ops against a value the ``.env`` had already
    set. The only placement that works is ``lc_factory/__init__.py``, which
    Python executes before any submodule, and which both entry points
    (``lc-code`` and the generated ``langgraph.json`` graph ref) pass through.

    That is also why `tests/test_server_graph.py` tests this in a **subprocess**:
    any in-process test imports `lc_factory` before it can seed a repository, so
    it is structurally incapable of failing.

    Accepted consequence: the variable is settable only from a real shell
    export — not from any ``.env``, including the user's own global one.
    Separating a global ``.env`` from a project one needs upstream internals the
    import boundary deliberately does not reach for.

    **Precondition, and it reaches beyond this package.** The guard holds only
    while ``lc_factory`` is the first thing in the process to touch
    ``deepagents_code``. If anything imports upstream first, the ``.env`` is
    already loaded and this ``setdefault`` no-ops. Nothing does today — both
    entry points go through this package, and the generated server workspace
    (``checkpointer.py``, ``langgraph.json``) contains no upstream import and no
    pre-import hook. What would break it: an upstream release registering a
    langgraph plugin or entry point that imports ``deepagents_code``, a
    ``sitecustomize``/``.pth`` in the user's environment, or embedding
    ``lc_factory`` in an application that already imported upstream.

    **No test can detect this**, because any test imports ``lc_factory`` first —
    which is the very assumption being made. It is a bump-time check, recorded
    in ``UPGRADING.md``.
    """
    os.environ.setdefault(MIDDLEWARE_REF_ENV, "")
    # Runtime behavior and history grouping must also be host-owned.
    os.environ.setdefault("LC_FACTORY_CAPABILITIES", "")
    os.environ.setdefault("LC_FACTORY_HISTORY_OWNER", "")
    os.environ.setdefault(VERIFICATION_MODEL_ENV, "")
    os.environ.setdefault("LC_FACTORY_SETTLED_DISPATCH", "")
    os.environ.setdefault("LC_FACTORY_INTERPRETER_SUBAGENTS", "")
