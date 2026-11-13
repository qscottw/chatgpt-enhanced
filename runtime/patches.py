from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .core import preview_text, resolve_safe_path


_HEADER_RE = re.compile(r"^(?:---|\+\+\+)\s+([^\t\n]+)", re.MULTILINE)


def _validate_patch_paths(patch: str, cwd: Path) -> None:
    for raw in _HEADER_RE.findall(patch):
        value = raw.strip()
        if value == "/dev/null":
            continue
        if value.startswith(("a/", "b/")):
            value = value[2:]
        candidate = Path(value)
        if candidate.is_absolute():
            raise ValueError(f"Absolute patch path is not allowed: {raw}")
        resolved = (cwd / candidate).resolve()
        if resolved != cwd and cwd not in resolved.parents:
            raise ValueError(f"Patch path escapes cwd: {raw}")


def apply_patch(patch: str, cwd: str = ".") -> dict[str, Any]:
    working = resolve_safe_path(cwd)
    if not working.is_dir():
        raise NotADirectoryError(str(working))
    if not patch.strip():
        raise ValueError("patch must not be empty")
    _validate_patch_paths(patch, working)

    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", "-"],
        cwd=str(working),
        input=patch,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check.returncode != 0:
        return {
            "ok": False,
            "kind": "patch_check_failed",
            "cwd": str(working),
            "stdout": check.stdout,
            "stderr": check.stderr,
        }

    applied = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=str(working),
        input=patch,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    out = preview_text(applied.stdout, label="patch-stdout")
    err = preview_text(applied.stderr, label="patch-stderr")
    return {
        "ok": applied.returncode == 0,
        "kind": "success" if applied.returncode == 0 else "patch_failed",
        "cwd": str(working),
        "exit_code": applied.returncode,
        "stdout": out["text"],
        "stderr": err["text"],
    }
