from __future__ import annotations

if __name__ == '__main__':
    from runtime.startup import bootstrap
    bootstrap()

import os
import asyncio
import time
from dataclasses import asdict
from typing import Any

from mcp.server import MCPServer

from runtime.tasks import TaskStore, TERMINAL, wait_deadline
from runtime.service import TaskService
from runtime.core import MAX_READ_BYTES, ROOT, STATE_DIR, atomic_write_text, resolve_safe_path
from runtime.events import EVENTS
from runtime.executor import catalog as executor_catalog_impl
from runtime.executor import describe as executor_describe_impl
from runtime.files import insert_text as runtime_insert_text
from runtime.files import read_file_range as runtime_read_file_range
from runtime.files import replace_text as runtime_replace_text
from runtime.git import git_diff as runtime_git_diff
from runtime.git import git_status as runtime_git_status
from runtime.patches import apply_patch as runtime_apply_patch
from runtime.sessions import create_session, get_session, list_sessions, update_session
from runtime.trace import TraceMiddleware, current_trace, emit


HOST = os.getenv("REMOTE_HOST", "127.0.0.1")
PORT = int(os.getenv("REMOTE_PORT", "8200"))
RUNTIME_VERSION = "chatgpt-enhanced-v3"
TASKS = TaskStore()
SERVICE = TaskService(TASKS)

# IMPORTANT: This public MCP schema is intentionally conservative and stable.
# Future capabilities should normally be added under actions/ and reached through
# executor_catalog/executor_run, rather than by adding new MCP tools.
mcp = MCPServer(
    "chatgpt-enhanced",
    log_level=os.getenv("REMOTE_LOG_LEVEL", "INFO"),
    middleware=[TraceMiddleware()],
)


@mcp.tool()
def ping() -> dict[str, Any]:
    """Check whether ChatGPT can reach the remote runtime."""
    trace = current_trace()
    return {
        "status": "ok",
        "message": "Hello from chatgpt-enhanced",
        "runtime": RUNTIME_VERSION,
        "trace": asdict(trace) if trace else None,
    }


@mcp.tool()
def runtime_status() -> dict[str, Any]:
    """Return local runtime identity, state paths, current trace identity, and dynamic-action count."""
    trace = current_trace()
    action_catalog = executor_catalog_impl(include_schema=False)
    return {
        "runtime": RUNTIME_VERSION,
        "root": str(ROOT),
        "state_dir": str(STATE_DIR),
        "trace": asdict(trace) if trace else None,
        "dynamic_actions": action_catalog["count"],
        "action_catalog_errors": action_catalog["errors"],
    }


@mcp.tool()
def progress(message: str, phase: str = "", status: str = "working") -> dict[str, Any]:
    """Publish a high-level progress note to the per-chat event stream and local monitor UI."""
    event = emit(
        "progress",
        summary=message,
        message=message,
        phase=phase,
        status=status,
    )
    return {"ok": event is not None, "event": event}


@mcp.tool()
def executor_catalog(query: str = "", include_schema: bool = True) -> dict[str, Any]:
    """Discover dynamic actions available to executor_run. Rescans disk on every call, so actions hot-reload without changing MCP schema."""
    return executor_catalog_impl(query=query, include_schema=include_schema)


@mcp.tool()
def executor_describe(action: str) -> dict[str, Any]:
    """Return the current manifest for one dynamic executor action."""
    return executor_describe_impl(action)


async def _wait_for_task(result: dict, deadline: float) -> dict:
    while not result["done"] and time.monotonic() < deadline:
        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
        result = await asyncio.to_thread(lambda: TASKS.response(TASKS.get(SERVICE.scope, result["task_id"])))
    return result


@mcp.tool()
async def executor_run(action: str, idempotency_key: str, arguments_json: str = "{}",
                       wait_timeout_seconds: float = 110) -> dict[str, Any]:
    """Submit an action with no execution-time limit. Reuse the SAME key after any lost response; queued/running is not a failure."""
    deadline = wait_deadline(wait_timeout_seconds)
    result = await asyncio.to_thread(SERVICE.action, action, arguments_json, idempotency_key, time.monotonic())
    return await _wait_for_task(result, deadline)


@mcp.tool()
async def task_get(task_id: str, wait_timeout_seconds: float = 0) -> dict[str, Any]:
    """Replay persisted task state/result, optionally waiting at most 110 seconds. unknown requires inspection, never automatic resubmission."""
    deadline = wait_deadline(wait_timeout_seconds)
    result = await asyncio.to_thread(lambda: TASKS.response(TASKS.get(SERVICE.scope, task_id)))
    return await _wait_for_task(result, deadline)


@mcp.tool()
def task_lookup(idempotency_key: str) -> dict[str, Any]:
    """Recover task_id after a lost submission response using the original idempotency_key."""
    row = TASKS.lookup(SERVICE.scope, idempotency_key)
    return TASKS.response(row) if row else {"found": False, "message": "No retained task. Retry the original submission with the SAME key."}


@mcp.tool()
def task_logs(task_id: str, stream: str = "stdout", offset: int = 0, limit: int = 20000) -> dict[str, Any]:
    """Read replayable stdout or stderr by BYTE offset. Repeating an offset is safe; next_offset advances explicitly. data_base64 preserves split UTF-8 bytes exactly."""
    return TASKS.logs(SERVICE.scope, task_id, stream, offset, limit)


@mcp.tool()
def task_cancel(task_id: str) -> dict[str, Any]:
    """Idempotently request task cancellation. Does not undo side effects; query task_get for final state."""
    return TASKS.response(TASKS.cancel(SERVICE.scope, task_id))


@mcp.tool()
def task_list(limit: int = 50) -> dict[str, Any]:
    """List recent durable tasks, including tasks created before server restarts."""
    with TASKS.connect() as db:
        rows = db.execute("SELECT * FROM tasks WHERE scope=? ORDER BY rowid DESC LIMIT ?", (SERVICE.scope, max(1, min(limit, 200)))).fetchall()
    return {"tasks": [TASKS.response(dict(row)) for row in rows]}


@mcp.tool()
def chat_list(limit: int = 100) -> dict[str, Any]:
    """List locally observed chats/trace groups ordered by latest activity."""
    return {"chats": EVENTS.list_chats(limit)}


@mcp.tool()
def chat_tail(chat_id: str, limit: int = 200) -> dict[str, Any]:
    """Read recent structured events for one locally observed chat/trace group."""
    return {"chat_id": chat_id, "events": EVENTS.tail(chat_id, limit)}


@mcp.tool()
def chat_set_title(chat_id: str, title: str) -> dict[str, Any]:
    """Set a local display title used by the monitor UI for an observed chat."""
    return EVENTS.set_title(chat_id, title)


# Compatibility/runtime primitives. Keep their public signatures stable once the
# OpenAI plugin is created; put future higher-level capabilities in actions/.
@mcp.tool()
def list_directory(path: str = ".") -> dict[str, Any]:
    """List files and directories. The path must stay inside REMOTE_ROOT."""
    target = resolve_safe_path(path)
    if not target.exists():
        raise FileNotFoundError(f"Path does not exist: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"Not a directory: {target}")
    entries: list[dict[str, Any]] = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            size = item.lstat().st_size
        except OSError:
            size = None
        if item.is_symlink():
            item_type = "symlink"
        elif item.is_dir():
            item_type = "directory"
        elif item.is_file():
            item_type = "file"
        else:
            item_type = "other"
        entries.append({"name": item.name, "path": str(item), "type": item_type, "size_bytes": size})
    return {"path": str(target), "entries": entries}


@mcp.tool()
def read_file(path: str) -> dict[str, Any]:
    """Read a UTF-8 text file under REMOTE_ROOT, up to REMOTE_MAX_READ_BYTES."""
    target = resolve_safe_path(path)
    if not target.exists():
        raise FileNotFoundError(f"File does not exist: {target}")
    if not target.is_file():
        raise ValueError(f"Not a regular file: {target}")
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"File too large: {size} bytes; maximum is {MAX_READ_BYTES}")
    return {"path": str(target), "size_bytes": size, "content": target.read_text(encoding="utf-8", errors="replace")}


@mcp.tool()
def read_file_range(path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
    """Read a line range with line numbers, useful for precise code edits."""
    return runtime_read_file_range(path, start_line, end_line)


@mcp.tool()
def write_file(path: str, content: str) -> dict[str, Any]:
    """Atomically write UTF-8 text under REMOTE_ROOT. Existing files are replaced."""
    target = resolve_safe_path(path)
    written = atomic_write_text(target, content)
    return {"path": str(target), "bytes_written": written}


@mcp.tool()
def replace_text(path: str, old_text: str, new_text: str, expected_replacements: int = 1) -> dict[str, Any]:
    """Replace an exact text fragment only when it occurs the expected number of times; returns a unified diff."""
    return runtime_replace_text(path, old_text, new_text, expected_replacements)


@mcp.tool()
def insert_text(path: str, line: int, text: str, before: bool = False) -> dict[str, Any]:
    """Insert text before or after a 1-based line and return a unified diff."""
    return runtime_insert_text(path, line, text, before=before)


@mcp.tool()
def apply_patch(patch: str, cwd: str = ".") -> dict[str, Any]:
    """Validate and apply a unified diff with git apply; patch paths must stay inside cwd."""
    return runtime_apply_patch(patch, cwd)


@mcp.tool()
async def run_command(command: str, idempotency_key: str, cwd: str = ".",
                      wait_timeout_seconds: float = 110, allow_dangerous: bool = False) -> dict[str, Any]:
    """Submit Bash with durable idempotency and no execution-time limit. Waiting expires without killing the task."""
    deadline = wait_deadline(wait_timeout_seconds)
    result = await asyncio.to_thread(SERVICE.command, command, idempotency_key, cwd, allow_dangerous, time.monotonic())
    return await _wait_for_task(result, deadline)


@mcp.tool()
async def process_start(command: str, idempotency_key: str, cwd: str = ".", allow_dangerous: bool = False) -> dict[str, Any]:
    """Submit a noninteractive background task with no execution-time limit and return immediately."""
    return await run_command(command, idempotency_key, cwd, wait_timeout_seconds=0, allow_dangerous=allow_dangerous)


@mcp.tool()
def git_status(cwd: str = ".") -> dict[str, Any]:
    """Return structured git branch and working-tree changes."""
    return runtime_git_status(cwd)


@mcp.tool()
def git_diff(cwd: str = ".", staged: bool = False, path: str | None = None) -> dict[str, Any]:
    """Return a context-friendly git diff; large diffs are preserved on disk."""
    return runtime_git_diff(cwd, staged=staged, path=path)


@mcp.tool()
def session_create(cwd: str = ".", task: str = "") -> dict[str, Any]:
    """Create lightweight persistent metadata for a coding task."""
    return create_session(cwd, task)


@mcp.tool()
def session_get(session_id: str) -> dict[str, Any]:
    """Read a saved coding-session record."""
    return get_session(session_id)


@mcp.tool()
def session_update(session_id: str, cwd: str | None = None, task: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Update coding-session cwd/task and optionally append a note."""
    return update_session(session_id, cwd=cwd, task=task, note=note)


@mcp.tool()
def session_list(limit: int = 50) -> dict[str, Any]:
    """List recent saved coding sessions."""
    return list_sessions(limit)


if __name__ == "__main__":
    EVENTS.recover_stale_calls()
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        json_response=True,
        host=HOST,
        port=PORT,
    )
