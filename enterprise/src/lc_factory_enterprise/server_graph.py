"""Enterprise workspace policy with OG's complete graph/runtime implementation."""
from .workspace import install_workspace_policy

# The server revalidates workspace claims independently of client capture.
# Install the same deterministic policy before any binding can be constructed.
install_workspace_policy()

from lc_factory.server_graph import make_graph

__all__ = ["make_graph"]
