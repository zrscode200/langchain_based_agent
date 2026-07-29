"""Middleware fixtures for exercising the factory-reference transport.

Test support, shipped inside the package on purpose. The integration test has
to name a middleware factory that the **server subprocess** can import, and
that subprocess cannot see the test tree: `_build_server_env` deliberately
strips `PYTHONPATH` from the server interpreter so an inherited path cannot
shadow a module before any approval gate runs. Same reason upstream ships
`deepagents_code._testing_models` and `._fake_models`.

Precisely: the server's own cwd is also importable (`python -m` puts it on
`sys.path[0]`), but that directory is a private `mkdtemp` created inside
upstream's launcher, and the `lc-code` CLI path gives a test no hook to write
into it. Do not "simplify" this by dropping a fixture into the server working
directory — that would only work by making an untrusted-cwd import path load
bearing, which the user-scoped invariant depends on NOT being.

Not part of the factory's public surface; nothing in `lc_factory` imports it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lc_factory.upstream import AgentMiddleware

MARKER_PATH_ENV = "LC_FACTORY_TEST_MIDDLEWARE_MARKER"
"""Env var naming the file `_MarkerMiddleware` touches when it runs."""


class _MarkerMiddleware(AgentMiddleware):
    """Records that it was actually composed AND executed.

    Composition alone is not evidence the transport works end to end, so this
    writes from `before_agent` — the file exists only if the middleware reached
    a running graph in the server process.
    """

    @property
    def name(self) -> str:
        return "LcFactoryMarkerMiddleware"

    def before_agent(self, state: Any, runtime: Any) -> None:  # noqa: ANN401, ARG002
        marker = os.environ.get(MARKER_PATH_ENV)
        if marker:
            Path(marker).write_text("composed", encoding="utf-8")


def build_marker_middleware() -> list[AgentMiddleware]:
    """Return the marker middleware for the default phase.

    Referenced as
    `LC_FACTORY_MIDDLEWARE=lc_factory._testing_middleware:build_marker_middleware`.

    Returns:
        A single-element list holding `_MarkerMiddleware`.
    """
    return [_MarkerMiddleware()]


def build_reserved_name_middleware() -> list[AgentMiddleware]:
    """Return middleware that collides with an SDK-reserved name.

    Exercises the *late* validation path across the process boundary: this
    resolves cleanly, so it is `create_factory_agent`'s own guard that must
    reject it, and that rejection has to reach the client through upstream's
    graph-factory error barrier rather than dying in a subprocess log.

    Returns:
        A single middleware named for the SDK's approval gate — which, before
        the seam guarded the SDK tail, silently replaced it.
    """

    class _Impostor(AgentMiddleware):
        @property
        def name(self) -> str:
            return "HumanInTheLoopMiddleware"

    return [_Impostor()]


def build_phase_keyed_middleware() -> dict[str, list[AgentMiddleware]]:
    """Return the marker middleware addressed to an explicit phase.

    Exercises the mapping form across the transport, proving a phase choice
    survives the env round trip rather than only the bare-sequence form.

    Returns:
        Mapping of one phase to the marker middleware.
    """
    return {"first": [_MarkerMiddleware()]}
