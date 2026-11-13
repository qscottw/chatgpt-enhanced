from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .core import preview_text, resolve_safe_path


def _git(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--no-pager", *args],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _repo_root(cwd: str) -> Path:
    working = resolve_safe_path(cwd)
    result = _git(["rev-parse", "--show-toplevel"], working)
    if result.returncode != 0:
        raise ValueError(f"Not inside a git repository: {working}\n{result.stderr.strip()}")
    return resolve_safe_path(result.stdout.strip())


def git_status(cwd: str = ".") -> dict[str, Any]:
    root = _repo_root(cwd)
    branch = _git(["branch", "--show-current"], root).stdout.strip()
    result = _git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    changes: list[dict[str, str]] = []
    records = iter(result.stdout.split("\0"))
    for record in records:
        if not record:
            continue
        change = {"status": record[:2], "path": record[3:]}
        if "R" in change["status"] or "C" in change["status"]:
            change["original_path"] = next(records)
        changes.append(change)
    return {"repo_root": str(root), "branch": branch, "clean": not changes, "changes": changes}


def git_diff(cwd: str = ".", staged: bool = False, path: str | None = None) -> dict[str, Any]:
    root = _repo_root(cwd)
    args = ["diff"]
    if staged:
        args.append("--cached")
    args.extend(["--no-ext-diff", "--"])
    if path:
        target = resolve_safe_path(root / path)
        try:
            rel = target.relative_to(root)
        except ValueError as exc:
            raise ValueError("path must remain inside the git repository") from exc
        args.append(str(rel))
    result = _git(args, root)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    preview = preview_text(result.stdout, label="git-diff")
    return {
        "repo_root": str(root),
        "staged": staged,
        "diff": preview["text"],
        "truncated": preview["truncated"],
        "full_output_path": preview["full_output_path"],
    }
