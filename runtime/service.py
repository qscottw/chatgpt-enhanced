"""Validate submissions and capture immutable execution inputs before enqueueing."""
from __future__ import annotations
import base64
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import executor
from .core import ROOT, STATE_DIR, check_command_safety, resolve_safe_path
from .tasks import TaskStore, submit_prepared
from .trace import current_trace


class TaskService:
    def __init__(self, store: TaskStore, scope: str | None = None):
        self.store = store
        # One installation is one trusted OS-user scope; chat/call IDs must not alter retry identity.
        self.scope = scope or os.getenv('REMOTE_TASK_SCOPE', 'local-owner')

    @staticmethod
    def origin() -> dict:
        trace = current_trace()
        return asdict(trace) if trace else {'chat_id': 'local-tasks'}

    def command(self, command: str, key: str, cwd: str, allow_dangerous: bool, deadline: float) -> dict:
        directory = resolve_safe_path(cwd)
        if not isinstance(command, str) or not command.strip():
            raise ValueError('command must be nonempty')
        request = {'kind': 'command', 'command': command, 'cwd': str(directory),
                   'allow_dangerous': allow_dangerous}
        def prepare():
            check_command_safety(command, allow_dangerous=allow_dangerous)
            if not directory.is_dir():
                raise NotADirectoryError(str(directory))
            return {'argv': ['/bin/bash', '-lc', command], 'cwd': str(directory)}
        return submit_prepared(self.store, self.scope, key, request, prepare, self.origin(), deadline)

    def action(self, action: str, arguments_json: str, key: str, deadline: float) -> dict:
        arguments = json.loads(arguments_json, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f'Invalid JSON value {x}')))
        if not isinstance(arguments, dict):
            raise ValueError('arguments_json must decode to an object')
        request = {'kind': 'action', 'action': action, 'arguments': arguments}
        def prepare():
            manifest = executor.scan_actions().get(action)
            if not manifest or action == '__catalog_errors__':
                raise KeyError(f'Unknown action {action!r}; call executor_catalog')
            executor._validate_arguments(manifest, arguments)
            argv = executor._command_for(manifest)
            entrypoint = Path(manifest['_action_dir']) / manifest['entrypoint']
            source = entrypoint.read_bytes()
            if len(source) > 8 * 1024 * 1024:
                raise ValueError('Action entrypoint exceeds the 8 MiB snapshot limit')
            argv[-1] = '$ENTRYPOINT'
            return {'argv': argv, 'cwd': manifest['_action_dir'], 'stdin': json.dumps(arguments, ensure_ascii=False) + '\n',
                    'action': action, 'version': manifest.get('version', '1'),
                    'source_base64': base64.b64encode(source).decode(), 'source_name': 'action-entry' + entrypoint.suffix,
                    'env': {'REMOTE_ACTION_NAME': action, 'REMOTE_ROOT': str(ROOT), 'REMOTE_STATE_DIR': str(STATE_DIR),
                            'PYTHONPATH': manifest['_action_dir'] + os.pathsep + os.environ.get('PYTHONPATH', '')}}
        return submit_prepared(self.store, self.scope, key, request, prepare, self.origin(), deadline)
