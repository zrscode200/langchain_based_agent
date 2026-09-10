"""Start the existing factory/LangGraph backend for a browser client.

The frontend and browser adapter live in web/ (React/TypeScript and Node).
This entry only reuses the TUI's backend launch seam and lifecycle.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import signal
from urllib.parse import urlsplit

from lc_factory import launch
from lc_factory.runtime_config import client_runtime_environment
from lc_factory.upstream import server_manager_module, start_server_and_get_agent


async def serve(args):
    previous = server_manager_module._scaffold_workspace
    server_manager_module._scaffold_workspace = launch.scaffold_workspace
    server = None
    try:
        with client_runtime_environment():
            _, server, _ = await start_server_and_get_agent(
                assistant_id="agent", cwd=str(Path(args.cwd).resolve()),
                host="127.0.0.1", port=args.port, model_name=args.model,
                auto_approve=False, enable_ask_user=True, interactive=True,
            )
            if urlsplit(server.url).port != args.port:
                raise RuntimeError(
                    f"Requested backend port {args.port} is occupied. "
                    "Choose another --port and use that same port in the frontend --backend URL. "
                    "The newly created fallback server has been stopped."
                )
            print(f"Agent backend ready: {server.url}", flush=True)
            stopped = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stopped.set)
            await stopped.wait()
    finally:
        if server is not None:
            server.stop()
        server_manager_module._scaffold_workspace = previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", required=True, help="Initial agent workspace")
    parser.add_argument("--port", type=int, default=2024)
    parser.add_argument("--model", help="Optional provider:model; otherwise use existing configuration")
    args = parser.parse_args()
    if not Path(args.cwd).is_dir():
        parser.error("--cwd must name an existing directory")
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
