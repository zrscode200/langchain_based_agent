"""Workspace-bound host controls for in-memory background tasks."""
from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.routing import Route

from lc_factory.upstream import require_thread_workspace, WorkspaceConflictError


async def background(request):
    owner = request.path_params["thread_id"]
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Request must be an object")
        binding = await require_thread_workspace(owner, body.get("workspace"))
        from lc_factory.server_graph import background_for_workspace
        tasks = await background_for_workspace(binding)
        operation = body.get("operation", "list")
        if tasks is None:
            if operation != "list":
                return JSONResponse({"detail": "Background tasks are unavailable"}, status_code=409)
            return JSONResponse({"tasks": [], "enabled": False})
        if operation in ("inspect", "conversation", "message", "resume", "cancel"):
            task_id = body.get("task_id")
            if not isinstance(task_id, str) or task_id not in tasks.jobs or tasks.jobs[task_id].owner != owner:
                return JSONResponse({"detail": "Unknown task for this conversation"}, status_code=404)
            if operation == "conversation":
                task = tasks.host_inspect(owner, task_id)
                task["conversation_records"] = tasks.jobs[task_id].transcript.structured(
                    before=body.get("before"), after=body.get("after"))
                return JSONResponse({"task": task, "enabled": True})
            if operation == "message":
                identity = body.get("message_id")
                if not isinstance(identity, str):
                    raise ValueError("Invalid message")
                return JSONResponse(tasks.jobs[task_id].transcript.record_page(
                    identity, offset=body.get("offset", 0), revision=body.get("revision")))
            if operation == "inspect":
                return JSONResponse({"task": tasks.host_inspect(owner, task_id,
                    transcript_page=body.get("transcript_page", -1)), "enabled": True})
            if operation == "resume":
                tasks.resume(owner, task_id, body.get("responses"))
            else:
                await tasks.cancel(owner, task_id=task_id)
        elif operation != "list":
            raise ValueError("Unknown background operation")
        return JSONResponse({"tasks": tasks.host_list(owner), "enabled": True,
                             "pending_results": list(tasks.pending(owner)), "capacity": tasks.capacity()})
    except WorkspaceConflictError:
        return JSONResponse({"detail": "Workspace does not match this conversation"}, status_code=409)
    except (ValueError, TypeError):
        return JSONResponse({"detail": "Invalid or stale background request"}, status_code=422)
    except (RuntimeError, SystemExit):
        return JSONResponse({"detail": "Background runtime is unavailable"}, status_code=503)


def install_background_routes(app):
    path = "/lc-factory/threads/{thread_id}/background"
    if not any(getattr(route, "path", None) == path for route in app.routes):
        app.router.routes.append(Route(path, background, methods=["POST"]))
