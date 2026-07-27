"""Import boundary: every upstream symbol lc_factory touches, in one place.

All lc_factory code MUST import upstream (``deepagents-code`` / ``deepagents``)
symbols through this module — never directly. On a pin bump, upstream breakage
surfaces here first, and this file doubles as the divergence inventory.

Verified against deepagents-code==0.1.47 (monorepo commit 8da0ccb13).

Wave 1.1 stub: assembly-level anchors only. The private-helper surface the
ported assembly needs (interrupt map builders, grader tool factories,
compaction middleware, etc.) is added in wave 1.2 alongside the port itself.
"""

# --- SDK (deepagents): the layer the recomposed assembly targets ---
from deepagents import create_deep_agent

# --- app/server config bridge (DEEPAGENTS_CODE_SERVER_* env contract) ---
from deepagents_code._server_config import ServerConfig

# --- v0 assembly: the reference implementation the factory recomposes ---
from deepagents_code.agent import create_cli_agent

# --- launch machinery reused by the micro-launcher (wave 1.2) ---
from deepagents_code.client.launch.server import (
    ServerProcess,
    generate_langgraph_json,
)
from deepagents_code.client.launch.server_manager import start_server_and_get_agent

# --- client seam (TUI-compatible remote graph client) ---
from deepagents_code.client.remote_client import RemoteAgent

__all__ = [
    "RemoteAgent",
    "ServerConfig",
    "ServerProcess",
    "create_cli_agent",
    "create_deep_agent",
    "generate_langgraph_json",
    "start_server_and_get_agent",
]
