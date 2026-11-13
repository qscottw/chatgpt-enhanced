#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${REMOTE_PYTHON:-python3}"
VERIFY_ROOT=$(mktemp -d /tmp/chatgpt-enhanced-verify-XXXXXX)
trap 'rm -rf "$VERIFY_ROOT"' EXIT
export REMOTE_ROOT="$VERIFY_ROOT"
export REMOTE_STATE_DIR="$VERIFY_ROOT/state"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
"$PY" -m unittest discover -s tests -t . -v
"$PY" - <<'PY'
import asyncio
import server
async def check():
    tools = await server.mcp.list_tools()
    for name in ('run_command', 'executor_run', 'process_start'):
        tool = next(t for t in tools if t.name == name)
        assert 'idempotency_key' in tool.input_schema['required'], name
        assert 'execution_timeout_seconds' not in tool.input_schema.get('properties', {}), name
    print(f'{server.RUNTIME_VERSION}: {len(tools)} MCP tools; idempotency keys required; execution timeouts absent')
asyncio.run(check())
PY
