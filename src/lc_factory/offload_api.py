"""Bind upstream's HTTP operation app to the factory server runtime.

The released app is reused intact. Its one graph-specific dependency is the
module-global ``get_server_runtime`` callable; rebinding that callable keeps
the app, validation, auth metadata, and persistence behavior upstream-owned
while making every request share the factory graph's cached backend/offload
operation.
"""

from lc_factory.server_graph import get_server_runtime
from lc_factory.upstream import import_offload_api

_upstream_offload_api = import_offload_api()
_upstream_offload_api.get_server_runtime = get_server_runtime

app = _upstream_offload_api.app

__all__ = ["app"]
