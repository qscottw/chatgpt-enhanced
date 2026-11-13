from __future__ import annotations

import hashlib
import json
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mcp.server.context import ServerRequestContext

import runtime.executor as executor
import runtime.trace as trace_module
from runtime.commands import run_command
from runtime.core import ROOT
from runtime.events import EventStore
from runtime.processes import ProcessManager
from runtime.trace import audit_params, resolve_trace, safe_headers


class RemoteV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-v2-test-", dir=ROOT))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx(self, *, headers: dict[str, str], request_id: str = "0", params=None, meta=None):
        class Request:
            pass
        Request.headers = headers
        return ServerRequestContext(
            session=None,
            lifespan_context={},
            protocol_version="2026-07-28",
            method="tools/call",
            params=params or {"name": "ping", "arguments": {}},
            request_id=request_id,
            meta=meta,
            request=Request(),
        )

    def test_per_chat_event_store(self) -> None:
        store = EventStore(self.tmp / "chats")
        store.append("chat-a", {"type": "mcp_start", "turn_id": "t1", "tool": "ping", "summary": "A"})
        store.append("chat-a", {"type": "mcp_end", "turn_id": "t1", "tool": "ping", "summary": "A done", "ok": True})
        time.sleep(0.01)
        store.append("chat-b", {"type": "progress", "turn_id": "t2", "summary": "B"})
        self.assertEqual(store.tail("chat-a")[-1]["summary"], "A done")
        chats = store.list_chats()
        self.assertEqual(chats[0]["chat_id"], "chat-b")
        chat_a = next(x for x in chats if x["chat_id"] == "chat-a")
        self.assertEqual(chat_a["turn_count"], 1)
        self.assertEqual(chat_a["active_calls"], 0)
        self.assertEqual(chat_a["status"], "idle")

    def test_sensitive_and_identity_headers_are_redacted(self) -> None:
        result = safe_headers({
            "authorization": "Bearer secret",
            "cookie": "x=y",
            "x-openai-session": "v1/private-session",
            "x-openai-subject": "private-user",
            "x-request-id": "turn-uuid/abcd",
        })
        self.assertEqual(result["authorization"], "<redacted>")
        self.assertEqual(result["cookie"], "<redacted>")
        self.assertEqual(result["x-openai-session"], "<redacted>")
        self.assertEqual(result["x-openai-subject"], "<redacted>")
        self.assertEqual(result["x-request-id"], "turn-uuid/abcd")

    def test_trace_uses_observed_openai_session_and_request_id(self) -> None:
        session = "v1/example-anonymized-chat-session"
        turn = "3191bf92-45df-41b0-81f0-278db0e54fd5"
        ctx = self._ctx(
            headers={
                "x-openai-session": session,
                "x-request-id": f"{turn}/gsbx",
                "traceparent": "00-6aa7b5ad00000000de47729fb1a03e48-7b4643b56e96ac9c-00",
            },
            meta={"openai/session": session},
        )
        trace, _, _ = resolve_trace(ctx)
        digest = hashlib.sha256(session.encode()).hexdigest()
        self.assertEqual(trace.chat_id, f"chat-{digest[:24]}")
        self.assertEqual(trace.session_hash, digest[:24])
        self.assertEqual(trace.chat_source, "openai/session")
        self.assertEqual(trace.turn_id, turn)
        self.assertEqual(trace.turn_source, "header:x-request-id-prefix")
        self.assertEqual(trace.call_id, f"{turn}.gsbx")
        self.assertEqual(trace.call_source, "header:x-request-id")
        self.assertEqual(trace.upstream_request_id, f"{turn}/gsbx")
        self.assertEqual(trace.upstream_trace_id, "6aa7b5ad00000000de47729fb1a03e48")

    def test_same_session_groups_cross_turn_and_different_session_splits(self) -> None:
        a1, _, _ = resolve_trace(self._ctx(headers={"x-openai-session": "session-A", "x-request-id": "turn-A1/a"}))
        a2, _, _ = resolve_trace(self._ctx(headers={"x-openai-session": "session-A", "x-request-id": "turn-A2/b"}))
        b1, _, _ = resolve_trace(self._ctx(headers={"x-openai-session": "session-B", "x-request-id": "turn-B1/c"}))
        self.assertEqual(a1.chat_id, a2.chat_id)
        self.assertNotEqual(a1.turn_id, a2.turn_id)
        self.assertNotEqual(a1.chat_id, b1.chat_id)

    def test_trace_falls_back_to_explicit_conversation_and_turn_headers(self) -> None:
        ctx = self._ctx(
            headers={
                "x-openai-conversation-id": "chat-real-123",
                "x-openai-turn-id": "turn-real-456",
                "authorization": "Bearer hidden",
            },
            request_id="call-1",
        )
        trace, headers, _ = resolve_trace(ctx)
        self.assertEqual(trace.chat_id, "chat-real-123")
        self.assertEqual(trace.turn_id, "turn-real-456")
        self.assertEqual(trace.call_id, "call-1")
        self.assertEqual(trace.chat_source, "header:x-openai-conversation-id")
        self.assertEqual(trace.turn_source, "header:x-openai-turn-id")
        self.assertEqual(safe_headers(headers)["authorization"], "<redacted>")

    def test_audit_params_keeps_cli_but_externalizes_large_payload(self) -> None:
        store = EventStore(self.tmp / "audit-chats")
        original = trace_module.EVENTS
        trace_module.EVENTS = store
        try:
            ctx = self._ctx(
                headers={"x-openai-session": "session-A", "x-request-id": "turn-A/abcd"},
                params={
                    "name": "write_file",
                    "arguments": {"path": "./x.txt", "content": "z" * 5000},
                    "_meta": {"openai/session": "session-A", "openai/subject": "private"},
                },
            )
            trace, _, _ = resolve_trace(ctx)
            audited = audit_params(trace, ctx.params)
            self.assertEqual(audited["name"], "write_file")
            self.assertNotIn("_meta", audited)
            payload = audited["arguments"]["content"]["$payload"]
            self.assertEqual(payload["chars"], 5000)
            self.assertEqual(Path(payload["payload_path"]).read_text(), "z" * 5000)
            self.assertEqual(payload["sha256"], hashlib.sha256(("z" * 5000).encode()).hexdigest())
        finally:
            trace_module.EVENTS = original

    def test_streaming_command_bounds_memory_preview_and_keeps_full_log(self) -> None:
        command = f"{shlex.quote(sys.executable)} -c \"import sys;sys.stdout.write('x'*300000)\""
        result = run_command(command, str(self.tmp))
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["stdout_truncated"])
        self.assertEqual(result["stdout_bytes"], 300000)
        self.assertEqual(Path(result["stdout_full_path"]).stat().st_size, 300000)
        self.assertLess(len(result["stdout"]), 120000)

    def test_process_reaper_marks_exit_without_explicit_read(self) -> None:
        manager = ProcessManager()
        started = manager.start("exit 0", str(self.tmp))
        process_id = started["process_id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            item = manager._items[process_id]
            if item.completed_at is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(manager._items[process_id].completed_at)
        self.assertEqual(manager._items[process_id].proc.returncode, 0)

    def test_executor_catalog_hot_add_and_run(self) -> None:
        original = executor.ACTIONS_DIR
        actions = self.tmp / "actions"
        actions.mkdir()
        executor.ACTIONS_DIR = actions
        try:
            self.assertEqual(executor.catalog()["count"], 0)
            action_dir = actions / "sample.action"
            action_dir.mkdir()
            (action_dir / "manifest.json").write_text(json.dumps({
                "name": "sample.action",
                "description": "created after first catalog call",
                "version": "1",
                "runtime": "python",
                "entrypoint": "run.py",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"]
                }
            }), encoding="utf-8")
            (action_dir / "run.py").write_text(
                "import json,sys\na=json.load(sys.stdin)\njson.dump({'seen':a['value']},sys.stdout)\n",
                encoding="utf-8",
            )
            names = [item["name"] for item in executor.catalog()["actions"]]
            self.assertEqual(names, ["sample.action"])
            result = executor.run("sample.action", '{"value":"hot"}')
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["result"], {"seen": "hot"})

            manifest = json.loads((action_dir / "manifest.json").read_text())
            manifest["description"] = "changed live"
            manifest["version"] = "2"
            (action_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            described = executor.describe("sample.action")
            self.assertEqual(described["description"], "changed live")
            self.assertEqual(described["version"], "2")
        finally:
            executor.ACTIONS_DIR = original


if __name__ == "__main__":
    unittest.main()
