"""Use the same workspace identity for enterprise offload and graph requests."""
from .workspace import install_workspace_policy
from .maintenance import install_server_maintenance_policy

install_workspace_policy()
install_server_maintenance_policy()

from lc_factory.offload_api import app

__all__ = ["app"]
