#!/usr/bin/env python3
"""Minimal stdio MCP client for exercising the ICS connectors by hand.

Holds stdin open until each response arrives, so async tool calls complete.

Usage:
    python3 tools/mcp_probe.py <server.py> <tool_name> '<json args>'
    python3 tools/mcp_probe.py <server.py> --list
"""

from __future__ import annotations

import json
import subprocess
import sys
import time


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    script, tool = sys.argv[1], sys.argv[2]
    args = sys.argv[3] if len(sys.argv) > 3 else "{}"

    proc = subprocess.Popen(
        ["uv", "run", "--script", script],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    assert proc.stdin and proc.stdout

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def await_id(want: int, timeout: float = 180.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want:
                return msg
        return None

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "ics-probe", "version": "1"}}})
    if await_id(1) is None:
        print("initialize failed")
        proc.kill()
        return 1
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    if tool == "--list":
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = await_id(2)
        tools = (resp or {}).get("result", {}).get("tools", [])
        for entry in tools:
            print(f"{entry['name']}\n    {entry.get('description','').split(chr(10))[0][:110]}")
    else:
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": tool, "arguments": json.loads(args)}})
        resp = await_id(2)
        if resp is None:
            print("no response before timeout")
            proc.kill()
            return 1
        if "error" in resp:
            print("ERROR:", json.dumps(resp["error"], indent=2))
        for chunk in resp.get("result", {}).get("content", []):
            if chunk.get("type") == "text":
                try:
                    print(json.dumps(json.loads(chunk["text"]), indent=1))
                except json.JSONDecodeError:
                    print(chunk["text"])

    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
