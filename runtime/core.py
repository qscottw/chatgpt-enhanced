from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(os.getenv("REMOTE_ROOT", str(Path.home()))).expanduser().resolve()
STATE_DIR = Path(
    os.getenv("REMOTE_STATE_DIR", str(Path.home() / ".local" / "state" / "chatgpt-enhanced"))
).expanduser().resolve()
MAX_READ_BYTES = int(os.getenv("REMOTE_MAX_READ_BYTES", str(2 * 1024 * 1024)))
MAX_OUTPUT_CHARS = int(os.getenv("REMOTE_MAX_OUTPUT_CHARS", "100000"))
PROCESS_READ_CHARS = int(os.getenv("REMOTE_PROCESS_READ_CHARS", "20000"))
PROCESS_REAP_INTERVAL_SECONDS = float(os.getenv("REMOTE_PROCESS_REAP_INTERVAL_SECONDS", "1"))
PROCESS_RETENTION_SECONDS = int(os.getenv("REMOTE_PROCESS_RETENTION_SECONDS", "3600"))

OUTPUT_DIR = STATE_DIR / "outputs"
PROCESS_DIR = STATE_DIR / "processes"
SESSION_DIR = STATE_DIR / "sessions"
CHAT_DIR = STATE_DIR / "chats"
for _dir in (STATE_DIR, OUTPUT_DIR, PROCESS_DIR, SESSION_DIR, CHAT_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


class CommandSafetyError(ValueError):
    pass


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def now_ns() -> int:
    return time.time_ns()


def safe_id(value: str, *, fallback: str = "unknown") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "")).strip("-.")
    return cleaned[:160] or fallback


def resolve_safe_path(path: str | Path) -> Path:
    """Resolve a path and ensure the resolved location stays under ROOT."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    resolved = candidate.resolve()
    if resolved != ROOT and ROOT not in resolved.parents:
        raise ValueError(f"Access denied: path must be inside {ROOT}; got {resolved}")
    return resolved


def atomic_write_text(target: Path, content: str) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    previous_mode: int | None = None
    if target.exists():
        previous_mode = target.stat().st_mode & 0o777

    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            os.chmod(temp_path, previous_mode)
        os.replace(temp_path, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp_path.unlink(missing_ok=True)
    return len(encoded)


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_full_output(text: str, label: str) -> str:
    safe_label = safe_id(label, fallback="output")
    filename = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:10]}-{safe_label}.log"
    target = OUTPUT_DIR / filename
    atomic_write_text(target, text)
    return str(target)


def preview_text(text: str, *, limit: int = MAX_OUTPUT_CHARS, label: str = "output") -> dict[str, Any]:
    """Preview already-materialized text. Streaming command code avoids materializing large output."""
    if limit < 200:
        limit = 200
    if len(text) <= limit:
        return {"text": text, "truncated": False, "original_chars": len(text), "full_output_path": None}

    full_output_path = _write_full_output(text, label)
    marker_budget = 160
    usable = max(2, limit - marker_budget)
    head_len = int(usable * 0.6)
    tail_len = usable - head_len
    omitted = len(text) - head_len - tail_len
    marker = f"\n\n...[truncated {omitted} characters; full output: {full_output_path}]...\n\n"
    return {
        "text": text[:head_len] + marker + text[-tail_len:],
        "truncated": True,
        "original_chars": len(text),
        "full_output_path": full_output_path,
    }


_DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("privilege escalation", re.compile(r"(^|[;&|]\s*)sudo\b", re.I)),
    ("interactive root shell", re.compile(r"(^|[;&|]\s*)(su|doas)\b", re.I)),
    ("system shutdown", re.compile(r"\b(shutdown|reboot|poweroff|halt)\b", re.I)),
    ("filesystem formatting", re.compile(r"\bmkfs(?:\.[A-Za-z0-9_-]+)?\b", re.I)),
    ("raw device overwrite", re.compile(r"\bdd\b[^\n]*\bof=/dev/", re.I)),
    ("destructive git reset", re.compile(r"\bgit\s+reset\s+--hard\b", re.I)),
    ("destructive git clean", re.compile(r"\bgit\s+clean\s+[^\n]*(?:-[^\s]*f[^\s]*d|-[^\s]*d[^\s]*f)", re.I)),
    ("recursive root deletion", re.compile(r"\brm\s+[^\n]*(?:-rf|-fr|--recursive)[^\n]*(?:\s/\s*$|\s/\*|\s~(?:/|\s|$))", re.I)),
)


def check_command_safety(command: str, *, allow_dangerous: bool = False) -> None:
    """Best-effort accidental-destruction guardrail; deliberately not a sandbox."""
    if allow_dangerous:
        return
    for reason, pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            raise CommandSafetyError(
                f"Command blocked by remote safety guardrail ({reason}). "
                "If this action is explicitly intended, retry with allow_dangerous=true."
            )
