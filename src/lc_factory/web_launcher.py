"""Owned web process and CLI adapter. No package installation or browser opening."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import sys
from urllib.parse import urlsplit


class WebLaunchError(RuntimeError):
    pass


def assets() -> Path:
    root = Path(__file__).resolve().parent / "_web"
    if not (root / "server/main.mjs").is_file() or not (root / "dist/index.html").is_file():
        raise WebLaunchError(
            "This installation has no bundled web frontend. Install a release wheel with web assets, "
            "or build this checkout once: cd web && npm run build:launcher."
        )
    return root


async def node_runtime() -> str:
    node = shutil.which("node")
    if node is None:
        raise WebLaunchError("Web frontend requires Node.js 22.12 or newer on PATH.")
    proc = await asyncio.create_subprocess_exec(node, "--version", stdout=asyncio.subprocess.PIPE)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), 5)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    try:
        version = tuple(int(v) for v in output.decode().strip().removeprefix("v").split(".")[:2])
    except ValueError:
        version = ()
    if proc.returncode or version < (22, 12):
        raise WebLaunchError("Web frontend requires Node.js 22.12 or newer on PATH.")
    return node


def transport(agent):
    """Use effective SDK credentials, including keys resolved from its environment."""
    return agent._get_graph()._validate_client().http.client


def backend_connection(agent):
    client = transport(agent)
    url = str(client.base_url).rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise WebLaunchError("Web frontend requires the current agent's local backend.")
    # Host/content framing belongs to the new Node request. All other effective
    # authentication headers stay server-side, never in the URL or bootstrap.
    headers = {k: v for k, v in client.headers.items() if k.lower() not in {
        "host", "content-length", "content-type", "accept-encoding", "connection",
    }}
    return url, headers


class WebProcess:
    def __init__(self, process, url):
        self.process, self.url = process, url
        self._control = asyncio.Lock()
        self._sequence = 0

    @classmethod
    async def start(cls, *, agent, cwd, thread_id, attached=False, approval_mode="manual", runtime_context=None):
        root, node = assets(), await node_runtime()
        backend, headers = backend_connection(agent)
        env = dict(os.environ)
        env["LC_WEB_BACKEND_HEADERS"] = json.dumps(headers)
        env["LC_WEB_RUNTIME_CONTEXT"] = json.dumps(runtime_context or {})
        # The native SDK transport is the authority; don't inherit a stale key
        # from an unrelated manual frontend launch.
        env.pop("LC_WEB_API_KEY", None)
        command = [node, str(root / "server/main.mjs"), "--workspace", str(Path(cwd).resolve()),
                   "--backend", backend, "--port", "0", "--managed", "--thread", thread_id,
                   "--approval-mode", str(approval_mode)]
        if attached:
            command.append("--attached")
        process = await asyncio.create_subprocess_exec(
            *command, cwd=str(root), env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        instance = cls(process, "")
        try:
            ready = await instance._read(timeout=20)
            url = ready.get("url", "")
            parsed = urlsplit(url)
            if ready.get("event") != "ready" or parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
                raise WebLaunchError("Frontend did not report a valid local address.")
            instance.url = url
            return instance
        except BaseException:
            await instance.stop()
            raise

    async def _read(self, timeout=15):
        line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        if not line:
            raise WebLaunchError("The web frontend stopped unexpectedly. Re-run the launcher to reconnect.")
        try:
            return json.loads(line)
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebLaunchError("Invalid frontend startup/control response. Rebuild the web assets.") from exc

    async def control(self, command):
        async with self._control:
            self._sequence += 1
            self.process.stdin.write((json.dumps({"command": command, "id": self._sequence}) + "\n").encode())
            await self.process.stdin.drain()
            ack = await self._read(timeout=40)
            if ack != {"event": "ack", "id": self._sequence}:
                raise WebLaunchError("Frontend control was not acknowledged; keep using the terminal's handoff screen.")

    async def stop(self):
        if self.process.returncode is not None:
            return
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except asyncio.TimeoutError:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()


async def bind(agent, thread_id, cwd):
    workspace = await agent.abind_workspace({"configurable": {"thread_id": thread_id}}, str(Path(cwd).resolve()))
    if workspace.get("cwd") != str(Path(cwd).resolve()):
        raise WebLaunchError("The current backend belongs to a different workspace.")
    return workspace


async def set_mode(agent, thread_id, mode):
    from lc_factory.upstream_cli import web_client_modules
    _, _, _, approval = web_client_modules()
    if await approval.awrite_approval_mode(agent, thread_id, mode=approval.ApprovalMode(mode)) is None:
        raise WebLaunchError("The backend cannot persist the conversation's approval mode.")


def require_supported_hooks(hooks):
    from lc_factory.upstream_cli import web_client_modules
    _, _, hook_types, _ = web_client_modules()
    if any(hooks.has_handlers(event) for event in hook_types.HookEvent):
        raise WebLaunchError("This session has native command hooks. Keep using the TUI; the web client does not yet execute those hooks.")


async def supervise_children(web, server):
    while web.process.returncode is None:
        if not server.running:
            raise WebLaunchError("The agent backend stopped. Its frontend has been stopped too; restart the launcher to reconnect.")
        await asyncio.sleep(0.5)
    raise WebLaunchError("The web frontend stopped; its owned backend has been stopped too.")


async def run_web_app(**options):
    """Reuse upstream's resolved CLI options without constructing the Textual UI."""
    from lc_factory.upstream import server_manager_module
    from lc_factory.upstream_cli import app_module, web_client_modules
    _, hooks_module, _, approval = web_client_modules()
    for name in ("resume_thread", "initial_prompt", "initial_skill", "initial_goal", "startup_cmd"):
        if options.get(name) is not None:
            raise WebLaunchError(f"{name.replace('_', ' ')} requires the TUI. Launch the TUI first, then use /web-frontend when it is idle.")
    if options.get("defer_server_start"):
        raise WebLaunchError("Configure your model and credentials in the TUI before launching --web-frontend.")
    if options["server_kwargs"].get("sandbox_type", "none") != "none":
        raise WebLaunchError("Web frontend currently supports local workspaces. Use the TUI for sandbox sessions.")
    assets()
    await node_runtime()  # Fail before starting a backend.
    cwd = str(Path(options.get("cwd") or Path.cwd()).resolve())
    thread_id = options["thread_id"]
    mode = options.get("approval_mode") or "manual"
    hooks = hooks_module.HooksManager.create(
        cwd=Path(cwd), identity=lambda: hooks_module.HookSessionIdentity(thread_id, approval.ApprovalMode(mode)),
        trust=options.get("hook_trust"),
    )
    require_supported_hooks(hooks)
    kwargs = {**options["server_kwargs"], "cwd": cwd, "host": "127.0.0.1", "port": 0}
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        loop.add_signal_handler(sig, stopped.set)
    try:
        # Cancelling startup also enters server_session's owned-process cleanup.
        async def serve():
            async with server_manager_module.server_session(**kwargs) as (agent, server):
                await bind(agent, thread_id, cwd)
                await set_mode(agent, thread_id, mode)
                web = await WebProcess.start(agent=agent, cwd=cwd, thread_id=thread_id, approval_mode=mode)
                try:
                    print(f"\nWeb frontend: {web.url}\nProject: {cwd}\nKeep this terminal open. Ctrl+C stops this frontend and its agent backend.\n", flush=True)
                    await supervise_children(web, server)
                finally:
                    await web.stop()
        task = asyncio.create_task(serve())
        stop_task = asyncio.create_task(stopped.wait())
        try:
            done, _ = await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
        finally:
            task.cancel(); stop_task.cancel()
            await asyncio.gather(task, stop_task, return_exceptions=True)
    finally:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)
    return app_module.AppResult(return_code=0, thread_id=thread_id)


@contextmanager
def client_web_frontend():
    """Invocation-scoped CLI and slash-command extension for OG and enterprise."""
    from lc_factory.upstream_cli import app_module, web_client_modules
    from lc_factory.web_tui import web_tui_command
    main_module, _, _, _ = web_client_modules()
    original_argv, original_parse = sys.argv, main_module.parse_args
    original_run = app_module.run_textual_app
    original_update = main_module._run_startup_auto_update
    args = list(sys.argv)
    boundary = args.index("--") if "--" in args else len(args)
    enabled = "--web-frontend" in args[1:boundary]
    try:
        if enabled:
            sys.argv = [value for i, value in enumerate(args) if not (0 < i < boundary and value == "--web-frontend")]
            app_module.run_textual_app = run_web_app
            # This process must continue using the selected/pinned installation.
            main_module._run_startup_auto_update = lambda console: None
            def parse():
                result = original_parse()
                unsupported = ("command", "non_interactive_message", "resume_thread", "initial_prompt", "initial_skill", "initial_goal", "goal", "startup_cmd", "update", "install", "uninstall", "auto_update", "default_model", "clear_default_model", "stdin", "rubric", "rubric_model", "rubric_max_iterations", "acp")
                active = [name for name in unsupported if getattr(result, name, None) is not None and getattr(result, name, None) is not False]
                if getattr(result, "sandbox", "none") not in {None, "none"}:
                    active.append("sandbox")
                if active:
                    raise SystemExit("--web-frontend starts an interactive browser session; these options require the TUI or their native command: " + ", ".join(active))
                return result
            main_module.parse_args = parse
        if any(value in {"--help", "-h"} for value in args[1:boundary]):
            print("Web UI: --web-frontend starts this project in your browser (prints a URL).\nIn the TUI: /web-frontend hands the current conversation to the browser.\n")
        with web_tui_command():
            yield
    finally:
        sys.argv = original_argv
        main_module.parse_args = original_parse
        app_module.run_textual_app = original_run
        main_module._run_startup_auto_update = original_update
