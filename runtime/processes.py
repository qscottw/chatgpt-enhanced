from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .core import (
    PROCESS_DIR,
    PROCESS_READ_CHARS,
    PROCESS_REAP_INTERVAL_SECONDS,
    PROCESS_RETENTION_SECONDS,
    check_command_safety,
    now_iso,
    resolve_safe_path,
    safe_id,
    write_json_atomic,
)
from .events import EVENTS
from .trace import current_trace, emit


class ManagedProcess:
    def __init__(
        self,
        process_id: str,
        command: str,
        cwd: Path,
        proc: subprocess.Popen[str],
        log_path: Path,
        origin: dict[str, str] | None,
    ) -> None:
        self.process_id = process_id
        self.command = command
        self.cwd = cwd
        self.proc = proc
        self.log_path = log_path
        self.origin = origin or {}
        self.offset = 0
        self._lock = threading.RLock()
        self.created_at = now_iso()
        self.completed_at: str | None = None
        self.completed_monotonic: float | None = None
        self.exit_event_written = False
        self.completion_persisted = False

    def refresh(self) -> int | None:
        code = self.proc.poll()  # Popen.poll() performs waitpid(WNOHANG), so it also reaps zombies.
        if code is not None:
            if self.proc.stdin is not None and not self.proc.stdin.closed:
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
            if self.completed_at is None:
                self.completed_at = now_iso()
                self.completed_monotonic = time.monotonic()
        return code

    def snapshot(self) -> dict[str, Any]:
        code = self.proc.returncode
        return {
            "process_id": self.process_id,
            "command": self.command,
            "cwd": str(self.cwd),
            "pid": self.proc.pid,
            "running": code is None,
            "exit_code": code,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "log_path": str(self.log_path),
            "origin": self.origin,
        }


class ProcessManager:
    def __init__(self) -> None:
        self._items: dict[str, ManagedProcess] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reaper_loop, daemon=True, name="remote-process-reaper")
        self._reaper.start()

    @staticmethod
    def _origin() -> dict[str, str] | None:
        trace = current_trace()
        if trace is None:
            return None
        return {
            "chat_id": trace.chat_id,
            "turn_id": trace.turn_id,
            "call_id": trace.call_id,
            "method": trace.method,
            "tool": trace.tool or "",
        }

    def start(self, command: str, cwd: str = ".", *, allow_dangerous: bool = False) -> dict[str, Any]:
        check_command_safety(command, allow_dangerous=allow_dangerous)
        working_directory = resolve_safe_path(cwd)
        if not working_directory.is_dir():
            raise NotADirectoryError(str(working_directory))
        process_id = uuid.uuid4().hex[:12]
        trace = current_trace()
        if trace is not None:
            log_dir = EVENTS.artifacts_dir(trace.chat_id, "processes")
            log_path = log_dir / f"{safe_id(trace.turn_id)}-{process_id}.log"
        else:
            log_path = PROCESS_DIR / f"{process_id}.log"
        log_handle = open(log_path, "a+", encoding="utf-8", buffering=1)
        try:
            proc = subprocess.Popen(
                ["/bin/bash", "-lc", command],
                cwd=str(working_directory),
                stdin=subprocess.PIPE,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
                bufsize=1,
            )
        finally:
            log_handle.close()
        item = ManagedProcess(process_id, command, working_directory, proc, log_path, self._origin())
        with self._lock:
            self._items[process_id] = item
        self._persist_meta(item)
        emit(
            "process_start",
            summary=command[:500],
            process_id=process_id,
            pid=proc.pid,
            command=command,
            cwd=str(working_directory),
            log_path=str(log_path),
        )
        time.sleep(0.03)
        return {**item.snapshot(), **self.read(process_id, max_chars=PROCESS_READ_CHARS)}

    def _persist_meta(self, item: ManagedProcess) -> None:
        with item._lock:
            write_json_atomic(PROCESS_DIR / f"{item.process_id}.json", item.snapshot())

    def _get(self, process_id: str) -> ManagedProcess:
        with self._lock:
            item = self._items.get(process_id)
        if item is None:
            raise KeyError(
                f"Unknown or non-live process_id: {process_id}. Completed handles are retained for "
                f"about {PROCESS_RETENTION_SECONDS}s and do not survive a remote service restart."
            )
        return item

    def _write_exit_event(self, item: ManagedProcess) -> None:
        if item.exit_event_written or item.completed_at is None:
            return
        origin = item.origin
        chat_id = origin.get("chat_id") if origin else None
        if not chat_id:
            return
        EVENTS.append(
            chat_id,
            {
                "type": "process_exit",
                "event_id": f"process:{item.process_id}:exit",
                "turn_id": origin.get("turn_id"),
                "call_id": origin.get("call_id"),
                "method": origin.get("method"),
                "tool": origin.get("tool"),
                "process_id": item.process_id,
                "pid": item.proc.pid,
                "command": item.command,
                "cwd": str(item.cwd),
                "exit_code": item.proc.returncode,
                "completed_at": item.completed_at,
                "log_path": str(item.log_path),
                "summary": f"background process exit={item.proc.returncode}: {item.command[:300]}",
            },
        )

        item.exit_event_written = True

    def _refresh_item(self, item: ManagedProcess) -> None:
        with item._lock:
            item.refresh()
            if item.completed_at is not None:
                if not item.completion_persisted:
                    self._persist_meta(item)
                    item.completion_persisted = True
                self._write_exit_event(item)

    def _reaper_loop(self) -> None:
        while not self._stop.wait(PROCESS_REAP_INTERVAL_SECONDS):
            now = time.monotonic()
            with self._lock:
                items = list(self._items.items())
            expired: list[str] = []
            for process_id, item in items:
                try:
                    self._refresh_item(item)
                    if (
                        item.completed_monotonic is not None
                        and now - item.completed_monotonic > PROCESS_RETENTION_SECONDS
                    ):
                        expired.append(process_id)
                except Exception:
                    continue
            if expired:
                with self._lock:
                    for process_id in expired:
                        self._items.pop(process_id, None)

    def read(self, process_id: str, max_chars: int = PROCESS_READ_CHARS) -> dict[str, Any]:
        item = self._get(process_id)
        max_chars = max(200, min(int(max_chars), 200000))
        with item._lock:
            self._refresh_item(item)
            with open(item.log_path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(item.offset)
                text = handle.read(max_chars)
                item.offset = handle.tell()
                more = handle.read(1) != ""
            snap = item.snapshot()
            self._persist_meta(item)
            self._write_exit_event(item)
        return {
            "output": text,
            "more_available": more,
            "next_offset": item.offset,
            "running": snap["running"],
            "exit_code": snap["exit_code"],
            "log_path": str(item.log_path),
        }

    def write(self, process_id: str, data: str) -> dict[str, Any]:
        item = self._get(process_id)
        self._refresh_item(item)
        if item.proc.poll() is not None:
            raise RuntimeError(f"Process {process_id} already exited with code {item.proc.returncode}")
        if item.proc.stdin is None:
            raise RuntimeError("Process stdin is unavailable")
        item.proc.stdin.write(data)
        item.proc.stdin.flush()
        emit("process_write", process_id=process_id, bytes_written=len(data.encode("utf-8")))
        return {"process_id": process_id, "bytes_written": len(data.encode("utf-8")), "running": True}

    def signal(self, process_id: str, sig: str = "INT") -> dict[str, Any]:
        item = self._get(process_id)
        self._refresh_item(item)
        if item.proc.poll() is not None:
            return item.snapshot()
        sig_name = sig.upper().removeprefix("SIG")
        mapping = {"INT": signal.SIGINT, "TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "HUP": signal.SIGHUP}
        if sig_name not in mapping:
            raise ValueError(f"Unsupported signal {sig}; choose one of {sorted(mapping)}")
        try:
            # Signal the process group even if descendants detached from the shell's foreground job.
            os.killpg(item.proc.pid, mapping[sig_name])
        except ProcessLookupError:
            pass
        emit("process_signal", process_id=process_id, signal=sig_name, pid=item.proc.pid)
        time.sleep(0.05)
        self._refresh_item(item)
        self._persist_meta(item)
        return item.snapshot()

    def list(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._items.values())
        snapshots = []
        for item in items:
            self._refresh_item(item)
            snapshots.append(item.snapshot())
        return {"processes": snapshots}


PROCESS_MANAGER = ProcessManager()
