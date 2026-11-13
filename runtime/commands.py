from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from .core import (
    MAX_OUTPUT_CHARS,
    OUTPUT_DIR,
    check_command_safety,
    now_iso,
    resolve_safe_path,
    safe_id,
)
from .events import EVENTS
from .trace import current_trace, emit


class _BoundedBytes:
    """Keep bounded head+tail bytes while counting the complete stream."""

    def __init__(self, limit: int) -> None:
        self.limit = max(200, int(limit))
        self.head_limit = int(self.limit * 0.6)
        self.tail_limit = self.limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if len(self.head) < self.head_limit:
            take = min(self.head_limit - len(self.head), len(chunk))
            self.head.extend(chunk[:take])
            chunk = chunk[take:]
        if chunk and self.tail_limit > 0:
            self.tail.extend(chunk)
            if len(self.tail) > self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    def render(self, full_path: Path) -> tuple[str, bool]:
        if self.total <= self.limit:
            data = bytes(self.head) + bytes(self.tail)
            return data.decode("utf-8", errors="replace"), False
        omitted = max(0, self.total - len(self.head) - len(self.tail))
        marker = (
            f"\n\n...[truncated about {omitted} bytes; full output: {full_path}]...\n\n"
        ).encode()
        data = bytes(self.head) + marker + bytes(self.tail)
        return data.decode("utf-8", errors="replace"), True


def _log_paths(label: str) -> tuple[Path, Path]:
    trace = current_trace()
    suffix = uuid.uuid4().hex[:8]
    if trace is not None:
        base_dir = EVENTS.artifacts_dir(trace.chat_id, "commands")
        base = f"{safe_id(trace.turn_id)}-{safe_id(trace.call_id)}-{suffix}-{safe_id(label)}"
    else:
        base_dir = OUTPUT_DIR
        base = f"{time.strftime('%Y%m%d-%H%M%S')}-{suffix}-{safe_id(label)}"
    return base_dir / f"{base}.stdout.log", base_dir / f"{base}.stderr.log"


def _drain(stream: BinaryIO, target: Path, capture: _BoundedBytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb", buffering=0) as out:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            out.write(chunk)
            capture.add(chunk)


def run_command(
    command: str,
    cwd: str = ".",
    *,
    allow_dangerous: bool = False,
) -> dict[str, Any]:
    """Run Bash synchronously while streaming full stdout/stderr to disk with bounded RAM use."""
    check_command_safety(command, allow_dangerous=allow_dangerous)
    working_directory = resolve_safe_path(cwd)
    if not working_directory.exists():
        raise FileNotFoundError(f"Working directory does not exist: {working_directory}")
    if not working_directory.is_dir():
        raise NotADirectoryError(f"Working directory is not a directory: {working_directory}")

    stdout_path, stderr_path = _log_paths("command")
    stdout_capture = _BoundedBytes(MAX_OUTPUT_CHARS)
    stderr_capture = _BoundedBytes(MAX_OUTPUT_CHARS)
    started = time.monotonic()
    started_at = now_iso()

    proc = subprocess.Popen(
        ["/bin/bash", "-lc", command],
        cwd=str(working_directory),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stdout_thread = threading.Thread(
        target=_drain, args=(proc.stdout, stdout_path, stdout_capture), daemon=True, name=f"remote-stdout-{proc.pid}"
    )
    stderr_thread = threading.Thread(
        target=_drain, args=(proc.stderr, stderr_path, stderr_capture), daemon=True, name=f"remote-stderr-{proc.pid}"
    )
    stdout_thread.start()
    stderr_thread.start()

    emit(
        "command_start",
        summary=command[:500],
        command=command,
        cwd=str(working_directory),
        pid=proc.pid,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )

    try:
        proc.wait()
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        try:
            proc.stdout.close()
            proc.stderr.close()
        except Exception:
            pass

    duration_ms = (time.monotonic() - started) * 1000
    stdout_text, stdout_truncated = stdout_capture.render(stdout_path)
    stderr_text, stderr_truncated = stderr_capture.render(stderr_path)
    ok = proc.returncode == 0
    kind = "success" if proc.returncode == 0 else "command_failed"

    emit(
        "command_end",
        summary=f"exit={proc.returncode} {command[:300]}",
        command=command,
        cwd=str(working_directory),
        pid=proc.pid,
        ok=ok,
        kind=kind,
        exit_code=proc.returncode,
        duration_ms=round(duration_ms, 3),
        stdout_bytes=stdout_capture.total,
        stderr_bytes=stderr_capture.total,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )

    return {
        "ok": ok,
        "kind": kind,
        "command": command,
        "cwd": str(working_directory),
        "pid": proc.pid,
        "exit_code": proc.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "stdout_full_path": str(stdout_path),
        "stderr_full_path": str(stderr_path),
        "stdout_bytes": stdout_capture.total,
        "stderr_bytes": stderr_capture.total,
        "duration_ms": round(duration_ms, 3),
        "started_at": started_at,
        "ended_at": now_iso(),
    }
