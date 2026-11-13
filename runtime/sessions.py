from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .core import SESSION_DIR, now_iso, read_json, resolve_safe_path, write_json_atomic


def _session_path(session_id: str) -> Path:
    if not session_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in session_id):
        raise ValueError("Invalid session_id")
    return SESSION_DIR / f"{session_id}.json"


def create_session(cwd: str = ".", task: str = "") -> dict[str, Any]:
    working = resolve_safe_path(cwd)
    if not working.is_dir():
        raise NotADirectoryError(str(working))
    session_id = uuid.uuid4().hex[:12]
    data = {
        "session_id": session_id,
        "cwd": str(working),
        "task": task,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "notes": [],
    }
    write_json_atomic(_session_path(session_id), data)
    return data


def get_session(session_id: str) -> dict[str, Any]:
    path = _session_path(session_id)
    if not path.exists():
        raise KeyError(f"Unknown session_id: {session_id}")
    return read_json(path)


def update_session(session_id: str, *, cwd: str | None = None, task: str | None = None, note: str | None = None) -> dict[str, Any]:
    path = _session_path(session_id)
    data = get_session(session_id)
    if cwd is not None:
        working = resolve_safe_path(cwd)
        if not working.is_dir():
            raise NotADirectoryError(str(working))
        data["cwd"] = str(working)
    if task is not None:
        data["task"] = task
    if note:
        data.setdefault("notes", []).append({"at": now_iso(), "text": note})
    data["updated_at"] = now_iso()
    write_json_atomic(path, data)
    return data


def list_sessions(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    items: list[dict[str, Any]] = []
    for path in sorted(SESSION_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            items.append(read_json(path))
        except (OSError, json.JSONDecodeError):
            continue
    return {"sessions": items}
