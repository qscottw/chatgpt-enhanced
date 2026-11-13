from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

from .core import safe_id
from .events import EVENTS, process_identity


_SECRET_KEY_RE = re.compile(r"authorization|cookie|token|secret|password|api[-_]?key|credential", re.I)
_PRIVATE_META_KEY_RE = re.compile(r"session|subject|organization|userlocation|user_location|latitude|longitude", re.I)
_PRIVATE_HEADER_RE = re.compile(r"x-openai-(session|subject|organization)|forwarded-client-cert|cookie|authorization", re.I)
_CHAT_KEYS = ("chat_id", "chatId", "conversation_id", "conversationId", "thread_id", "threadId")
_TURN_KEYS = ("turn_id", "turnId", "response_id", "responseId", "run_id", "runId")
_DEFAULT_CHAT_HEADERS = (
    "x-openai-conversation-id",
    "x-openai-chat-id",
    "x-openai-thread-id",
    "conversation-id",
    "chat-id",
    "thread-id",
)
_DEFAULT_TURN_HEADERS = (
    "x-openai-turn-id",
    "x-openai-response-id",
    "x-openai-run-id",
    "turn-id",
)
_MAX_INLINE_ARGUMENT_CHARS = int(os.getenv("REMOTE_LOG_INLINE_ARGUMENT_CHARS", "4000"))
_FULL_TRACE_PROBE = os.getenv("REMOTE_TRACE_PROBE_FULL", "0").strip().lower() in {"1", "true", "yes", "on"}


def _configured_headers(env_name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return defaults
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


CHAT_HEADERS = _configured_headers("REMOTE_CHAT_ID_HEADERS", _DEFAULT_CHAT_HEADERS)
TURN_HEADERS = _configured_headers("REMOTE_TURN_ID_HEADERS", _DEFAULT_TURN_HEADERS)


@dataclass(frozen=True)
class TraceContext:
    chat_id: str
    turn_id: str
    call_id: str
    method: str
    tool: str | None = None
    chat_source: str = "fallback"
    turn_source: str = "fallback"
    call_source: str = "fallback"
    session_hash: str | None = None
    upstream_request_id: str | None = None
    upstream_trace_id: str | None = None


CURRENT_TRACE: ContextVar[TraceContext | None] = ContextVar("remote_current_trace", default=None)


def current_trace() -> TraceContext | None:
    return CURRENT_TRACE.get()


def _model_to_plain(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            return value.model_dump()
    if isinstance(value, Mapping):
        return dict(value)
    return value


def _find_key(value: Any, keys: tuple[str, ...], depth: int = 0) -> str | None:
    if depth > 4:
        return None
    value = _model_to_plain(value)
    if isinstance(value, Mapping):
        for key in keys:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return str(candidate)
        for child in value.values():
            found = _find_key(child, keys, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for child in value[:20]:
            found = _find_key(child, keys, depth + 1)
            if found:
                return found
    return None


def _headers_from_ctx(ctx: ServerRequestContext[Any, Any]) -> dict[str, str]:
    request = ctx.request
    if request is None or not hasattr(request, "headers"):
        return {}
    try:
        return {str(k).lower(): str(v) for k, v in request.headers.items()}
    except Exception:
        return {}


def _session_from(meta: Any, params: Any, headers: Mapping[str, str]) -> tuple[str | None, str]:
    if headers.get("x-openai-session"):
        return headers["x-openai-session"], "header:x-openai-session"
    for source_name, value in (("meta", meta), ("params", params)):
        plain = _model_to_plain(value)
        if isinstance(plain, Mapping):
            candidate = plain.get("openai/session")
            if candidate:
                return str(candidate), f"{source_name}:openai/session"
            embedded_meta = _model_to_plain(plain.get("_meta"))
            if isinstance(embedded_meta, Mapping) and embedded_meta.get("openai/session"):
                return str(embedded_meta["openai/session"]), f"{source_name}._meta:openai/session"
    return None, "fallback"


def _hash_session(session: str) -> str:
    return hashlib.sha256(session.encode("utf-8")).hexdigest()


def _trace_id_from_traceparent(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split("-")
    if len(parts) >= 4 and len(parts[1]) == 32:
        return parts[1]
    return None


def safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        key = str(key).lower()
        if _SECRET_KEY_RE.search(key) or _PRIVATE_HEADER_RE.search(key):
            out[key] = "<redacted>"
        else:
            out[key] = str(value)[:1000]
    return out


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Generic secret-aware sanitizer for tool arguments. Identity metadata is handled separately."""
    if depth > 6:
        return "<depth-limit>"
    value = _model_to_plain(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, child in value.items():
            skey = str(key)
            out[skey] = "<redacted>" if _SECRET_KEY_RE.search(skey) else sanitize(child, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [sanitize(x, depth=depth + 1) for x in value[:200]]
    if isinstance(value, tuple):
        return [sanitize(x, depth=depth + 1) for x in value[:200]]
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:1200] + f"...[{len(value)-1600} chars omitted]..." + value[-400:]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:2000]


def sanitize_meta(meta: Any) -> Any:
    """Keep protocol/debug metadata while removing user/conversation identity values."""
    meta = _model_to_plain(meta)
    if not isinstance(meta, Mapping):
        return None if meta is None else str(meta)[:1000]
    out: dict[str, Any] = {}
    for key, value in meta.items():
        skey = str(key)
        if _SECRET_KEY_RE.search(skey) or _PRIVATE_META_KEY_RE.search(skey):
            out[skey] = "<redacted>"
        elif skey in {"openai/userAgent", "openai/locale", "timezone"}:
            # These are not needed to understand what the tool did.
            continue
        else:
            out[skey] = sanitize(value)
    return out


def _audit_value(trace: TraceContext, key_path: str, value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "<depth-limit>"
    value = _model_to_plain(value)
    leaf = key_path.rsplit(".", 1)[-1]
    if _SECRET_KEY_RE.search(leaf):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): _audit_value(trace, f"{key_path}.{k}" if key_path else str(k), v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_audit_value(trace, f"{key_path}[{i}]", v, depth + 1) for i, v in enumerate(value[:200])]
    if isinstance(value, tuple):
        return [_audit_value(trace, f"{key_path}[{i}]", v, depth + 1) for i, v in enumerate(value[:200])]
    if isinstance(value, str) and len(value) > _MAX_INLINE_ARGUMENT_CHARS:
        return {
            "$payload": EVENTS.store_payload(
                trace.chat_id,
                turn_id=trace.turn_id,
                call_id=trace.call_id,
                label=leaf or "argument",
                text=value,
            )
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:2000]


def audit_params(trace: TraceContext, params: Any) -> Any:
    """Record actual tool arguments while excluding OpenAI transport metadata noise."""
    plain = _model_to_plain(params)
    if not isinstance(plain, Mapping):
        return sanitize(plain)
    if trace.method == "tools/call":
        return {
            "name": str(plain.get("name") or trace.tool or ""),
            "arguments": _audit_value(trace, "arguments", plain.get("arguments") or {}),
        }
    return {
        str(k): _audit_value(trace, str(k), v)
        for k, v in plain.items()
        if str(k) != "_meta"
    }


def resolve_trace(ctx: ServerRequestContext[Any, Any]) -> tuple[TraceContext, dict[str, str], Any]:
    headers = _headers_from_ctx(ctx)
    meta = _model_to_plain(ctx.meta)
    params = _model_to_plain(ctx.params)

    session, session_source = _session_from(meta, params, headers)
    session_hash = _hash_session(session) if session else None

    # Primary Chat identity: OpenAI's anonymized conversation/session id. We assume it
    # remains stable for the lifetime of one Chat; raw values are never persisted.
    chat_id: str | None = None
    chat_source = "fallback"
    if session_hash:
        chat_id = f"chat-{session_hash[:24]}"
        chat_source = "openai/session"
    else:
        for name in CHAT_HEADERS:
            if headers.get(name):
                chat_id = headers[name]
                chat_source = f"header:{name}"
                break
        if not chat_id:
            chat_id = _find_key(meta, _CHAT_KEYS) or _find_key(params, _CHAT_KEYS)
            if chat_id:
                chat_source = "mcp-meta-or-params"

    upstream_request_id = headers.get("x-request-id") or headers.get("x-client-request-id")
    upstream_trace_id = _trace_id_from_traceparent(headers.get("traceparent"))

    # Observed OpenAI tunnel convention: x-request-id = <turn UUID>/<request suffix>.
    turn_id: str | None = None
    turn_source = "fallback"
    if upstream_request_id:
        turn_id = upstream_request_id.split("/", 1)[0]
        turn_source = "header:x-request-id-prefix"
    else:
        for name in TURN_HEADERS:
            if headers.get(name):
                turn_id = headers[name]
                turn_source = f"header:{name}"
                break
        if not turn_id:
            turn_id = _find_key(meta, _TURN_KEYS) or _find_key(params, _TURN_KEYS)
            if turn_id:
                turn_source = "mcp-meta-or-params"

    raw_rpc_id = str(ctx.request_id) if ctx.request_id is not None else ""
    call_source = "fallback"
    if upstream_request_id:
        call_id = safe_id(upstream_request_id.replace("/", "."), fallback=uuid.uuid4().hex[:16])
        call_source = "header:x-request-id"
    elif raw_rpc_id and raw_rpc_id not in {"0", "None"}:
        call_id = safe_id(raw_rpc_id, fallback=uuid.uuid4().hex[:16])
        call_source = "mcp-request-id"
    else:
        call_id = f"local-{uuid.uuid4().hex[:16]}"

    if not turn_id:
        turn_id = f"call-{call_id}"
    turn_id = safe_id(turn_id, fallback=f"call-{call_id}")
    if not chat_id:
        # Transport/discovery traffic is operational noise, not a user Chat. Keep it
        # in one system timeline so it cannot crowd the monitor's Chat list.
        if ctx.method in {"initialize", "notifications/initialized", "server/discover", "tools/list"}:
            chat_id = "system-protocol"
            chat_source = "system"
        else:
            # Never guess cross-turn identity without a true conversation key.
            chat_id = f"turn-{turn_id}"
    chat_id = safe_id(chat_id, fallback=f"turn-{turn_id}")

    tool = None
    if isinstance(params, Mapping) and ctx.method == "tools/call":
        raw_tool = params.get("name")
        if raw_tool:
            tool = str(raw_tool)

    return TraceContext(
        chat_id=chat_id,
        turn_id=turn_id,
        call_id=call_id,
        method=ctx.method,
        tool=tool,
        chat_source=chat_source if session_source == "fallback" else "openai/session",
        turn_source=turn_source,
        call_source=call_source,
        session_hash=session_hash[:24] if session_hash else None,
        upstream_request_id=upstream_request_id,
        upstream_trace_id=upstream_trace_id,
    ), headers, meta


def emit(event_type: str, **fields: Any) -> dict[str, Any] | None:
    trace = current_trace()
    if trace is None:
        return None
    payload = {
        "type": event_type,
        "turn_id": trace.turn_id,
        "call_id": trace.call_id,
        "method": trace.method,
        "tool": trace.tool,
        "chat_source": trace.chat_source,
        "turn_source": trace.turn_source,
        "call_source": trace.call_source,
        **fields,
    }
    return EVENTS.append(trace.chat_id, payload)


def result_status(result: Any) -> dict[str, Any]:
    plain = result.model_dump(by_alias=True) if hasattr(result, "model_dump") else result
    if not isinstance(plain, dict):
        return {"request_ok": True, "tool_ok": True, "task_ok": None, "ok": True}
    tool_ok = plain.get("isError") is not True
    body = plain.get("structuredContent") or plain
    task_ok = body.get("task_ok", body.get("ok")) if isinstance(body, dict) else None
    pending = isinstance(body, dict) and body.get("status") in ("queued", "running", "unknown")
    return {"request_ok": True, "tool_ok": tool_ok, "task_ok": task_ok,
            "task_status": body.get("status") if isinstance(body, dict) else None,
            "ok": False if not tool_ok or task_ok is False else (None if pending else True)}


class TraceMiddleware:
    """Correlate inbound MCP traffic into Chat -> Turn -> Call and write an audit timeline."""

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        trace, headers, meta = resolve_trace(ctx)
        token = CURRENT_TRACE.set(trace)
        started_ns = time.monotonic_ns()
        params = _model_to_plain(ctx.params)
        base = {
            "turn_id": trace.turn_id,
            "call_id": trace.call_id,
            "method": trace.method,
            "tool": trace.tool,
            "chat_source": trace.chat_source,
            "turn_source": trace.turn_source,
            "call_source": trace.call_source,
        }
        trace_info = {
            "session_hash": trace.session_hash,
            "upstream_request_id": trace.upstream_request_id,
            "upstream_trace_id": trace.upstream_trace_id,
        }
        try:
            start_event: dict[str, Any] = {
                **base,
                "type": "mcp_start",
                "summary": trace.tool or trace.method,
                "params": audit_params(trace, params),
                "trace": trace_info,
                "instance": {"pid": os.getpid(), "identity": process_identity(os.getpid())},
            }
            if _FULL_TRACE_PROBE:
                start_event["transport_headers"] = safe_headers(headers)
                start_event["meta"] = sanitize_meta(meta)
            EVENTS.append(trace.chat_id, start_event)

            result = await call_next(ctx)
            duration_ms = (time.monotonic_ns() - started_ns) / 1_000_000
            EVENTS.append(
                trace.chat_id,
                {
                    **base,
                    "type": "mcp_end",
                    "summary": f"{trace.tool or trace.method} completed",
                    **result_status(result),
                    "duration_ms": round(duration_ms, 3),
                },
            )
            return result
        except BaseException as exc:
            duration_ms = (time.monotonic_ns() - started_ns) / 1_000_000
            EVENTS.append(
                trace.chat_id,
                {
                    **base,
                    "type": "mcp_end",
                    "summary": f"{trace.tool or trace.method} failed: {type(exc).__name__}",
                    "ok": False,
                    "request_ok": False,
                    "tool_ok": None,
                    "task_ok": None,
                    "duration_ms": round(duration_ms, 3),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:4000],
                },
            )
            raise
        finally:
            CURRENT_TRACE.reset(token)
