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


def reserve_middleware_ref_env() -> None:
    """Claim ``LC_FACTORY_MIDDLEWARE`` so no ``.env`` file can introduce it.

    Resolving the reference imports and executes a named module inside the
    server process, so it must come from a real shell export: a committed
    ``.env`` in a cloned repository must never be able to name code that runs
    on ``lc-code`` startup, before any approval gate (decisions.md D4).

    Reading ``os.environ`` does not give that by itself. Upstream loads ``.env``
    files straight into it, on two paths that both precede resolution — the
    client's settings bootstrap searches upward from the cwd, and the server's
    bootstrap uses the project context, which points at the user's repository
    even though the server's own cwd is a private temporary directory. Upstream
    keeps a denylist of keys that "turn `.env` loading into code execution", but
    it is a ``frozenset`` and cannot know about this variable.

    So this occupies the slot instead: upstream's ``apply_dotenv`` skips any key
    already present in ``os.environ``, so a pre-set value — even an empty one,
    which reads as "unset" — makes the variable unsettable from any ``.env``.

    **Placement is the whole guard, and it is subtle.** Importing
    ``deepagents_code.config`` *is* touching ``settings``: that module has a
    module-level PEP 562 ``__getattr__`` which bootstraps and loads ``.env`` on
    first attribute access. So merely importing ``lc_factory.upstream`` — which
    every other module in this package does — already contaminates
    ``os.environ``. An earlier version of this guard called it from
    ``server_graph`` module scope and from ``tui.main()``; both run *after* that
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
    """
    os.environ.setdefault(MIDDLEWARE_REF_ENV, "")
