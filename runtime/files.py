from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from .core import MAX_READ_BYTES, atomic_write_text, resolve_safe_path


def read_file_range(path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
    target = resolve_safe_path(path)
    if not target.exists():
        raise FileNotFoundError(f"File does not exist: {target}")
    if not target.is_file():
        raise ValueError(f"Not a regular file: {target}")
    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(f"File too large: {size} bytes; maximum is {MAX_READ_BYTES}")
    if start_line < 1:
        raise ValueError("start_line must be >= 1")

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if end_line is None:
        end_line = len(lines)
    if end_line < start_line - 1:
        raise ValueError("end_line must be >= start_line - 1")
    selected = lines[start_line - 1 : end_line]
    numbered = "".join(f"{idx}: {line}" for idx, line in enumerate(selected, start=start_line))
    return {
        "path": str(target),
        "size_bytes": size,
        "start_line": start_line,
        "end_line": min(end_line, len(lines)),
        "total_lines": len(lines),
        "content": numbered,
    }


def replace_text(path: str, old_text: str, new_text: str, expected_replacements: int = 1) -> dict[str, Any]:
    target = resolve_safe_path(path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"File does not exist: {target}")
    content = target.read_text(encoding="utf-8", errors="strict")
    count = content.count(old_text)
    if count != expected_replacements:
        raise ValueError(
            f"Expected {expected_replacements} occurrence(s) of old_text in {target}, found {count}; file unchanged"
        )
    updated = content.replace(old_text, new_text)
    before_lines = content.splitlines(keepends=True)
    after_lines = updated.splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(before_lines, after_lines, fromfile=str(target), tofile=str(target), n=3)
    )
    written = atomic_write_text(target, updated)
    return {
        "path": str(target),
        "replacements": count,
        "bytes_written": written,
        "diff": diff,
    }


def insert_text(path: str, line: int, text: str, *, before: bool = False) -> dict[str, Any]:
    target = resolve_safe_path(path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"File does not exist: {target}")
    if line < 1:
        raise ValueError("line must be >= 1")
    content = target.read_text(encoding="utf-8", errors="strict")
    lines = content.splitlines(keepends=True)
    if line > len(lines) + 1:
        raise ValueError(f"line {line} is beyond file end ({len(lines)} lines)")
    index = line - 1 if before else line
    if line == len(lines) + 1:
        index = len(lines)
    lines.insert(index, text)
    updated = "".join(lines)
    diff = "".join(
        difflib.unified_diff(content.splitlines(keepends=True), updated.splitlines(keepends=True), fromfile=str(target), tofile=str(target), n=3)
    )
    written = atomic_write_text(target, updated)
    return {"path": str(target), "bytes_written": written, "diff": diff}
