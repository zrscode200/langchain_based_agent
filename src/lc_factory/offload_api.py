"""Bind upstream's HTTP operation app to the factory server runtime.

The released app is reused intact. Its one graph-specific dependency is the
module-global ``get_server_runtime`` callable; rebinding that callable keeps
the app, validation, auth metadata, and persistence behavior upstream-owned
while making every request share its workspace's factory graph and cached
backend/offload operation. The upstream alias now accepts a workspace binding;
rebinding it to the default, no-argument runtime would break workspace routing.
"""

from lc_factory.server_graph import _workspace_runtime
from lc_factory.upstream import import_offload_api

_upstream_offload_api = import_offload_api()
_upstream_offload_api.get_server_runtime = _workspace_runtime

app = _upstream_offload_api.app

__all__ = ["app"]
