from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .commands import _BoundedBytes, _drain
from .core import MAX_OUTPUT_CHARS, OUTPUT_DIR, ROOT, STATE_DIR, safe_id
from .events import EVENTS
from .trace import current_trace, emit


ACTIONS_DIR = Path(os.getenv("REMOTE_ACTIONS_DIR", str(Path(__file__).resolve().parents[1] / "actions"))).resolve()


MANIFEST_SCHEMA = {
    "type": "object", "required": ["name", "entrypoint"], "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$"},
        "entrypoint": {"type": "string", "minLength": 1},
        "description": {"type": "string"}, "version": {"type": "string"},
        "runtime": {"enum": ["python", "bash", "executable"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "parameters": {"type": "object"}, "examples": {"type": "array"}
    }
}


class ExecutorError(RuntimeError):
    pass


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ExecutorError(f"Invalid action manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExecutorError(f"Action manifest must be a JSON object: {path}")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ExecutorError(f"Action manifest missing non-empty name: {path}")
    entrypoint = data.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise ExecutorError(f"Action {name} missing non-empty entrypoint")
    try:
        Draft202012Validator(MANIFEST_SCHEMA).validate(data)
        schema = data.get("parameters", {"type": "object"})
        Draft202012Validator.check_schema(schema)
        if schema.get("$schema", "https://json-schema.org/draft/2020-12/schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("Only JSON Schema Draft 2020-12 is supported")
        def check_refs(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("$ref", "$dynamicRef") and not value.startswith("#"):
                        raise ValueError("Only local schema references are supported")
                    check_refs(value)
            elif isinstance(node, list):
                for value in node:
                    check_refs(value)
        check_refs(schema)
    except Exception as exc:
        raise ExecutorError(f"Invalid action manifest {path}: {exc}") from exc
    if name == "__catalog_errors__":
        raise ExecutorError("Reserved action name")
    data = dict(data)
    data["name"] = name.strip()
    data["_manifest_path"] = str(path)
    data["_action_dir"] = str(path.parent)
    _command_for(data)
    return data


def scan_actions() -> dict[str, dict[str, Any]]:
    ACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    actions: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for manifest_path in sorted(ACTIONS_DIR.rglob("manifest.json")):
        try:
            manifest = _load_manifest(manifest_path)
            name = manifest["name"]
            if name in actions:
                raise ExecutorError(f"Duplicate action name {name}: {manifest_path}")
            actions[name] = manifest
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        # Keep valid actions usable while surfacing broken manifests in the catalog.
        actions["__catalog_errors__"] = {"errors": errors}  # sentinel, filtered by callers
    return actions


def catalog(query: str = "", include_schema: bool = True) -> dict[str, Any]:
    scanned = scan_actions()
    errors = scanned.pop("__catalog_errors__", {}).get("errors", [])
    needle = query.strip().lower()
    items: list[dict[str, Any]] = []
    for name, manifest in sorted(scanned.items()):
        description = str(manifest.get("description", ""))
        tags = [str(x) for x in manifest.get("tags", []) if isinstance(x, (str, int, float))]
        if needle and needle not in f"{name} {description} {' '.join(tags)}".lower():
            continue
        item = {
            "name": name,
            "description": description,
            "version": str(manifest.get("version", "1")),
            "tags": tags,
            "runtime": str(manifest.get("runtime", "python")),
        }
        if include_schema:
            item["parameters"] = manifest.get("parameters", {"type": "object", "additionalProperties": True})
            if manifest.get("examples") is not None:
                item["examples"] = manifest.get("examples")
        items.append(item)
    return {
        "actions_dir": str(ACTIONS_DIR),
        "count": len(items),
        "actions": items,
        "errors": errors,
        "hot_reload": "catalog and executor rescan manifests on every call; no MCP schema change or server restart is required",
    }


def describe(action: str) -> dict[str, Any]:
    scanned = scan_actions()
    scanned.pop("__catalog_errors__", None)
    manifest = scanned.get(action)
    if manifest is None:
        raise KeyError(f"Unknown action {action!r}; call executor_catalog first")
    return {k: v for k, v in manifest.items() if not k.startswith("_")}


def _validate_arguments(manifest: dict[str, Any], args: dict[str, Any]) -> None:
    schema = manifest.get("parameters", {"type": "object"})
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(args)


def _command_for(manifest: dict[str, Any]) -> list[str]:
    action_dir = Path(manifest["_action_dir"])
    entrypoint = (action_dir / manifest["entrypoint"]).resolve()
    if entrypoint != action_dir and action_dir not in entrypoint.parents:
        raise ExecutorError(f"Action entrypoint escapes its action directory: {entrypoint}")
    if not entrypoint.is_file():
        raise ExecutorError(f"Action entrypoint does not exist: {entrypoint}")
    runtime = str(manifest.get("runtime", "python")).lower()
    if runtime == "python":
        return [sys.executable, str(entrypoint)]
    if runtime == "bash":
        return ["/bin/bash", str(entrypoint)]
    if runtime == "executable":
        return [str(entrypoint)]
    raise ExecutorError(f"Unsupported action runtime {runtime!r} for {manifest['name']}")


def _log_paths(action: str) -> tuple[Path, Path]:
    trace = current_trace()
    suffix = uuid.uuid4().hex[:8]
    if trace is not None:
        base_dir = EVENTS.artifacts_dir(trace.chat_id, "executor")
        base = f"{safe_id(trace.turn_id)}-{safe_id(trace.call_id)}-{suffix}-{safe_id(action)}"
    else:
        base_dir = OUTPUT_DIR
        base = f"{time.strftime('%Y%m%d-%H%M%S')}-{suffix}-{safe_id(action)}"
    return base_dir / f"{base}.stdout.log", base_dir / f"{base}.stderr.log"


def run(action: str, arguments_json: str = "{}") -> dict[str, Any]:
    scanned = scan_actions()
    catalog_errors = scanned.pop("__catalog_errors__", {}).get("errors", [])
    manifest = scanned.get(action)
    if manifest is None:
        extra = f" Catalog errors: {catalog_errors}" if catalog_errors else ""
        raise KeyError(f"Unknown action {action!r}; call executor_catalog first.{extra}")
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"arguments_json must be a JSON object: {exc}") from exc
    if not isinstance(arguments, dict):
        raise ValueError("arguments_json must decode to a JSON object")
    _validate_arguments(manifest, arguments)

    command = _command_for(manifest)
    action_dir = Path(manifest["_action_dir"])
    stdout_path, stderr_path = _log_paths(action)
    stdout_capture = _BoundedBytes(MAX_OUTPUT_CHARS)
    stderr_capture = _BoundedBytes(MAX_OUTPUT_CHARS)
    started = time.monotonic()

    env = os.environ.copy()
    env.update(
        {
            "REMOTE_ACTION_NAME": action,
            "REMOTE_ROOT": str(ROOT),
            "REMOTE_STATE_DIR": str(STATE_DIR),
        }
    )
    proc = subprocess.Popen(
        command,
        cwd=str(action_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    out_thread = threading.Thread(target=_drain, args=(proc.stdout, stdout_path, stdout_capture), daemon=True)
    err_thread = threading.Thread(target=_drain, args=(proc.stderr, stderr_path, stderr_capture), daemon=True)
    out_thread.start()
    err_thread.start()

    payload = (json.dumps(arguments, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        proc.stdin.write(payload)
    except BrokenPipeError:
        pass
    finally:
        proc.stdin.close()
    emit(
        "executor_start",
        summary=f"{action} {arguments}",
        action=action,
        arguments=arguments,
        pid=proc.pid,
        manifest_path=manifest["_manifest_path"],
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )

    proc.wait()
    out_thread.join(timeout=5)
    err_thread.join(timeout=5)
    try:
        proc.stdout.close()
        proc.stderr.close()
    except Exception:
        pass

    stdout_text, stdout_truncated = stdout_capture.render(stdout_path)
    stderr_text, stderr_truncated = stderr_capture.render(stderr_path)
    duration_ms = (time.monotonic() - started) * 1000
    ok = proc.returncode == 0
    parsed_result: Any = None
    if ok and stdout_capture.total <= 4 * 1024 * 1024:
        try:
            parsed_result = json.loads(stdout_path.read_text(encoding="utf-8", errors="strict"))
        except Exception:
            parsed_result = None

    emit(
        "executor_end",
        summary=f"{action} exit={proc.returncode}",
        action=action,
        pid=proc.pid,
        ok=ok,
        exit_code=proc.returncode,
        duration_ms=round(duration_ms, 3),
        stdout_bytes=stdout_capture.total,
        stderr_bytes=stderr_capture.total,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )
    return {
        "ok": ok,
        "action": action,
        "version": str(manifest.get("version", "1")),
        "exit_code": proc.returncode,
        "duration_ms": round(duration_ms, 3),
        "result": parsed_result,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "stdout_full_path": str(stdout_path),
        "stderr_full_path": str(stderr_path),
    }
