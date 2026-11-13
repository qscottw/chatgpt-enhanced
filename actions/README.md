# Dynamic action protocol

Each action lives in its own directory and contains a `manifest.json` plus an entrypoint.

Example:

```text
actions/demo.echo/
  manifest.json
  run.py
```

## Manifest

```json
{
  "name": "demo.echo",
  "description": "Echo input",
  "version": "1",
  "runtime": "python",
  "entrypoint": "run.py",
  "tags": ["demo"],
  "parameters": {
    "type": "object",
    "properties": {
      "message": {"type": "string"}
    },
    "required": ["message"]
  }
}
```

Supported runtimes:

- `python` — current remote server Python interpreter runs the entrypoint.
- `bash` — `/bin/bash <entrypoint>`.
- `executable` — execute the entrypoint directly.

The entrypoint must stay inside its action directory.

## Input

`executor_run` sends exactly one UTF-8 JSON object followed by a newline to the action's stdin.

```python
args = json.load(sys.stdin)
```

## Output

Stdout/stderr are streamed to per-call log files with bounded in-memory previews.

For structured results, emit one JSON value on stdout. If the action exits with code 0 and stdout is valid JSON, `executor_run.result` contains the decoded value.

Diagnostics should normally go to stderr so stdout remains machine-readable.

## Environment

Actions receive the parent process environment plus:

```text
REMOTE_ACTION_NAME
REMOTE_ROOT
REMOTE_STATE_DIR
```

Do not assume an action is sandboxed. It runs with the same OS-user permissions as the remote MCP process.

## Hot reload

There is intentionally no registry cache. `executor_catalog`, `executor_describe`, and `executor_run` rescan `actions/**/manifest.json` every time.

Therefore all of these are live without a server restart:

- adding an action
- deleting an action
- changing its description/schema/version
- replacing its entrypoint implementation

This is the compatibility layer that prevents future capability growth from requiring a new OpenAI plugin schema.

## v3 durable execution

Public `executor_run` requires `idempotency_key`. Tasks have no execution-time
limit. `wait_timeout_seconds` (maximum 110 seconds) only controls how long one
MCP request waits before returning a running task to the caller.
Task results are available in the response's `result` field and replayable through
`task_get`. Refer to the project README for failure, retry and snapshot semantics.
Schemas use Draft 2020-12 with local references only. A JSON result object with
`ok: false` or `isError: true` marks the task as failed even when the exit code is zero.
The entrypoint snapshot runs from the task directory; use cwd or explicit paths
for data files instead of deriving the original action directory from `__file__`.
