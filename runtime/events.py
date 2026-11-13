from __future__ import annotations

import hashlib
import os
import fcntl
from contextlib import contextmanager
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from .core import CHAT_DIR, atomic_write_text, now_iso, now_ns, read_json, safe_id, write_json_atomic


class EventStore:
    """Append-only per-chat event store plus per-chat artifacts and compact metadata."""

    def __init__(self, root: Path = CHAT_DIR) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @contextmanager
    def _locked(self):
        with self._lock, (self.root / ".events.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def recover_stale_calls(self) -> None:
        """Explicit server-start recovery. Preserve calls belonging to live server instances."""
        for chat in self.list_chats(1000):
            for call, owner in chat.get("active_call_owners", {}).items():
                if owner and process_identity(owner.get("pid", -1)) != owner.get("identity"):
                    self.append(chat["chat_id"], {"type": "mcp_end", "call_id": call,
                        "ok": False, "request_ok": False, "summary": "Server exited before response",
                        "event_id": f"recovery:{chat['chat_id']}:{call}:{owner.get('identity')}"})

    def chat_dir(self, chat_id: str) -> Path:
        path = self.root / safe_id(chat_id, fallback="unattributed")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def artifacts_dir(self, chat_id: str, kind: str = "artifacts") -> Path:
        path = self.chat_dir(chat_id) / safe_id(kind, fallback="artifacts")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def store_payload(
        self,
        chat_id: str,
        *,
        turn_id: str,
        call_id: str,
        label: str,
        text: str,
    ) -> dict[str, Any]:
        """Persist a large tool argument outside events.jsonl and return audit metadata."""
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        filename = (
            f"{safe_id(turn_id)}-{safe_id(call_id)}-{safe_id(label)}-"
            f"{digest[:12]}-{uuid.uuid4().hex[:6]}.txt"
        )
        target = self.artifacts_dir(chat_id, "payloads") / filename
        atomic_write_text(target, text)
        preview = text if len(text) <= 500 else text[:350] + f"...[{len(text)-500} chars omitted]..." + text[-150:]
        return {
            "payload_path": str(target),
            "chars": len(text),
            "bytes": len(encoded),
            "sha256": digest,
            "preview": preview,
        }

    def append(self, chat_id: str, event: dict[str, Any]) -> dict[str, Any]:
        chat_id = safe_id(chat_id, fallback="unattributed")
        payload = dict(event)
        payload.setdefault("time", now_iso())
        payload.setdefault("time_ns", now_ns())
        payload["chat_id"] = chat_id
        chat_dir = self.chat_dir(chat_id)
        event_path = chat_dir / "events.jsonl"
        meta_path = chat_dir / "meta.json"

        explicit_id = payload.get("event_id")
        with self._locked():
            # Outbox retries must repair metadata as well as avoid duplicate event lines.
            previous = []
            duplicate = False
            if explicit_id and event_path.exists():
                for line in event_path.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    previous.append(event)
                    duplicate |= event.get("event_id") == explicit_id
            if not duplicate:
                payload.setdefault("event_id", uuid.uuid4().hex)
                # A killed writer may leave a partial final record. Separate it from
                # this retry so one damaged record cannot swallow future events.
                if event_path.exists() and event_path.stat().st_size:
                    with event_path.open("rb+") as repair:
                        repair.seek(-1, 2)
                        if repair.read(1) != b"\n":
                            repair.seek(0, 2)
                            repair.write(b"\n")
                with event_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            try:
                meta = read_json(meta_path) if meta_path.exists() else {}
            except (OSError, ValueError):
                meta = {}
            if duplicate:
                meta = {"title": meta["title"]} if "title" in meta else {}
                for event in previous:
                    self._update_meta(meta, event)
            else:
                self._update_meta(meta, payload)
            write_json_atomic(meta_path, meta)
        return payload

    @staticmethod
    def _update_meta(meta: dict, payload: dict) -> None:
        meta.setdefault("chat_id", payload["chat_id"])
        meta.setdefault("created_at", payload["time"])
        meta["updated_at"] = payload["time"]
        meta["event_count"] = int(meta.get("event_count", 0)) + 1
        turn = payload.get("turn_id")
        if turn:
            turns = list(meta.get("recent_turns", []))
            if turn not in turns:
                turns.append(turn)
                meta["turn_count"] = int(meta.get("turn_count", 0)) + 1
            meta["recent_turns"] = turns[-50:]
            meta["last_turn_id"] = turn
        typ = payload.get("type")
        meta["last_event_type"] = typ
        for source, target in (("summary", "last_summary"), ("chat_source", "chat_source"), ("tool", "last_tool")):
            if payload.get(source):
                meta[target] = str(payload[source])[:500]
        if typ == "command_start":
            meta["last_command"] = str(payload.get("command", ""))[:1000]
        owners = meta.setdefault("active_call_owners", {})
        call = payload.get("call_id")
        if typ == "mcp_start":
            if call:
                owners[call] = payload.get("instance", {})
            else:
                meta["legacy_active_calls"] = meta.get("legacy_active_calls", 0) + 1
        elif typ == "mcp_end":
            if call:
                owners.pop(call, None)
            else:
                meta["legacy_active_calls"] = max(0, meta.get("legacy_active_calls", 0) - 1)
            if payload.get("ok") is not None:
                meta["last_ok"] = payload["ok"]
        tasks = meta.setdefault("active_tasks", [])
        task = payload.get("task_id")
        if typ in ("task_queued", "task_started") and task not in tasks:
            tasks.append(task)
        elif typ == "task_finished":
            if task in tasks:
                tasks.remove(task)
            meta["last_task_status"] = payload.get("status")
        meta["active_calls"] = len(owners) + meta.get("legacy_active_calls", 0)
        meta["status"] = "active" if meta["active_calls"] or tasks else "idle"

    def set_title(self, chat_id: str, title: str) -> dict[str, Any]:
        meta_path = self.chat_dir(chat_id) / "meta.json"
        with self._locked():
            meta = read_json(meta_path) if meta_path.exists() else {"chat_id": safe_id(chat_id)}
            meta["title"] = title.strip()[:200]
            meta["updated_at"] = now_iso()
            write_json_atomic(meta_path, meta)
        return meta

    def list_chats(self, limit: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self.root.iterdir() if self.root.exists() else []:
            if not path.is_dir():
                continue
            meta_path = path / "meta.json"
            try:
                meta = read_json(meta_path) if meta_path.exists() else {"chat_id": path.name}
            except Exception:
                meta = {"chat_id": path.name}
            meta["_mtime"] = meta_path.stat().st_mtime if meta_path.exists() else path.stat().st_mtime
            items.append(meta)
        items.sort(key=lambda item: item.get("_mtime", 0), reverse=True)
        for item in items:
            item.pop("_mtime", None)
        return items[: max(1, min(int(limit), 1000))]

    def tail(self, chat_id: str, limit: int = 200) -> list[dict[str, Any]]:
        path = self.chat_dir(chat_id) / "events.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-max(1, min(int(limit), 5000)) :]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def process_identity(pid: int) -> str | None:
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        stat = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return f'{boot}:{stat[19]}'
    except (OSError, ValueError, IndexError):
        return None


EVENTS = EventStore()
