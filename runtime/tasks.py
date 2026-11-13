"""Durable submission and replay. Only the standalone worker executes tasks."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .core import STATE_DIR, now_iso

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'unknown'})
MAX_WAIT_SECONDS = 110.0


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def wait_deadline(seconds: float) -> float:
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('wait_timeout_seconds must be finite and nonnegative')
    return time.monotonic() + min(seconds, MAX_WAIT_SECONDS)


class IdempotencyConflict(ValueError):
    pass


class TaskStore:
    def __init__(self, root: Path = STATE_DIR) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'tasks.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, scope TEXT NOT NULL, key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, request_json TEXT NOT NULL,
                    spec_json TEXT NOT NULL, origin_json TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
                    ended_at TEXT, worker_id TEXT, pid INTEGER, result_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(scope, key)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    chat_id TEXT NOT NULL, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS worker (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    worker_id TEXT NOT NULL, heartbeat REAL NOT NULL
                );
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=1.0)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def validate_key(key: str) -> None:
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError('idempotency_key must be a nonempty string of at most 200 characters')

    @staticmethod
    def digest(request: dict) -> str:
        return hashlib.sha256(canonical(request).encode()).hexdigest()

    def lookup(self, scope: str, key: str, request: dict | None = None) -> dict | None:
        self.validate_key(key)
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE scope=? AND key=?', (scope, key)).fetchone()
        if row is None:
            return None
        if request is not None and row['request_hash'] != self.digest(request):
            raise IdempotencyConflict('Same idempotency_key has different execution parameters; no task was started')
        return dict(row)

    def submit(self, scope: str, key: str, request: dict, spec: dict, origin: dict) -> dict:
        self.validate_key(key)
        digest = self.digest(request)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE scope=? AND key=?', (scope, key)).fetchone()
            if row:
                if row['request_hash'] != digest:
                    raise IdempotencyConflict('Same idempotency_key has different execution parameters; no task was started')
                return dict(row)
            task_id = 'task_' + uuid.uuid4().hex
            db.execute('''INSERT INTO tasks
                (task_id,scope,key,request_hash,request_json,spec_json,origin_json,status,created_at)
                VALUES (?,?,?,?,?,?,?,'queued',?)''',
                (task_id, scope, key, digest, canonical(request), canonical(spec), canonical(origin), now_iso()))
            row = dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
            self._event(db, row, 'task_queued')
            return row

    def get(self, scope: str, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT * FROM tasks WHERE scope=? AND task_id=?', (scope, task_id)).fetchone()
        if row is None:
            raise KeyError(f'Unknown task_id: {task_id}')
        return dict(row)

    def _event(self, db, row: dict, typ: str, **extra) -> None:
        origin = json.loads(row['origin_json'])
        chat = origin.get('chat_id', 'local-tasks')
        event_id = f"{row['task_id']}:{typ}"
        payload = {**origin, 'event_id': event_id, 'type': typ, 'task_id': row['task_id'],
                   'time': now_iso(),
                   'status': row['status'], 'summary': f"{row['task_id']} {row['status']}",
                   'stdout_path': str(self.root / 'tasks' / row['task_id'] / 'stdout.log'),
                   'stderr_path': str(self.root / 'tasks' / row['task_id'] / 'stderr.log'), **extra}
        db.execute('INSERT OR IGNORE INTO outbox(event_id,chat_id,payload) VALUES(?,?,?)',
                   (event_id, chat, canonical(payload)))

    def claim(self, worker_id: str) -> dict | None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE tasks SET status='running',worker_id=?,started_at=? WHERE task_id=?",
                       (worker_id, now_iso(), row['task_id']))
            row = dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (row['task_id'],)).fetchone())
            self._event(db, row, 'task_started')
            return row

    def set_pid(self, task_id: str, worker_id: str, pid: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE tasks SET pid=? WHERE task_id=? AND worker_id=? AND status='running'",
                       (pid, task_id, worker_id))

    def finish(self, task_id: str, worker_id: str, status: str, result: dict) -> None:
        if status not in TERMINAL:
            raise ValueError('Invalid terminal status')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("""UPDATE tasks SET status=?,result_json=?,ended_at=?
                WHERE task_id=? AND worker_id=? AND status='running'""",
                (status, canonical(result), now_iso(), task_id, worker_id)).rowcount
            if changed:
                row = dict(db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
                self._event(db, row, 'task_finished', task_ok=result.get('ok'), exit_code=result.get('exit_code'))

    def recover(self, worker_id: str) -> None:
        """Called ONLY after acquiring the exclusive worker lifetime lock. Never replay running work."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for found in db.execute("SELECT * FROM tasks WHERE status='running'").fetchall():
                row = dict(found)
                result = {'ok': None, 'error': 'Worker stopped before durable completion; side effects or child processes may remain. Inspect before starting a new operation.'}
                db.execute("UPDATE tasks SET status='unknown',ended_at=?,result_json=? WHERE task_id=?",
                           (now_iso(), canonical(result), row['task_id']))
                row['status'] = 'unknown'
                self._event(db, row, 'task_finished', task_ok=None)
            db.execute('INSERT OR REPLACE INTO worker VALUES(1,?,?)', (worker_id, time.time()))

    def heartbeat(self, worker_id: str) -> None:
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO worker VALUES(1,?,?)', (worker_id, time.time()))

    def cancel(self, scope: str, task_id: str) -> dict:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT * FROM tasks WHERE scope=? AND task_id=?', (scope, task_id)).fetchone()
            if found is None:
                raise KeyError(task_id)
            row = dict(found)
            if row['status'] == 'queued':
                db.execute("UPDATE tasks SET status='cancelled',cancel_requested=1,ended_at=?,result_json=? WHERE task_id=?",
                           (now_iso(), canonical({'ok': False, 'error': 'Cancelled before execution'}), task_id))
                row['status'] = 'cancelled'
                self._event(db, row, 'task_finished', task_ok=False)
            elif row['status'] == 'running':
                db.execute('UPDATE tasks SET cancel_requested=1 WHERE task_id=?', (task_id,))
        return self.get(scope, task_id)

    def wait(self, scope: str, task_id: str, deadline: float) -> dict:
        while True:
            row = self.get(scope, task_id)
            remaining = deadline - time.monotonic()
            if row['status'] in TERMINAL or remaining <= 0:
                return self.response(row)
            time.sleep(min(0.1, remaining))

    def response(self, row: dict) -> dict:
        status = row['status']
        with self.connect() as db:
            worker = db.execute('SELECT heartbeat FROM worker WHERE singleton=1').fetchone()
        terminal = status in TERMINAL
        return {'task_id': row['task_id'], 'idempotency_key': row['key'], 'status': status,
                'done': terminal, 'task_ok': (status == 'succeeded') if terminal and status != 'unknown' else None,
                'created_at': row['created_at'], 'started_at': row['started_at'], 'ended_at': row['ended_at'],
                'cancel_requested': bool(row['cancel_requested']),
                'result': json.loads(row['result_json']) if row['result_json'] else None,
                'worker_available': bool(worker and time.time() - worker['heartbeat'] < 5),
                'next_action': 'inspect_before_retry' if status == 'unknown' else ('none' if terminal else 'task_get'),
                'message': ('Execution outcome is unknown. Do not automatically resubmit.' if status == 'unknown' else
                            'Task finished; this result can be read repeatedly.' if terminal else
                            'Task is queued or running. Query this task_id; if submission response was lost, retry with the SAME idempotency_key. Do not submit a new key.')}

    def logs(self, scope: str, task_id: str, stream: str, offset: int, limit: int) -> dict:
        row = self.get(scope, task_id)
        if stream not in ('stdout', 'stderr') or offset < 0 or not 1 <= limit <= 200000:
            raise ValueError('stream must be stdout/stderr; offset >= 0; limit between 1 and 200000')
        path = self.root / 'tasks' / row['task_id'] / f'{stream}.log'
        if path.exists():
            with path.open('rb') as f:
                f.seek(offset)
                data = f.read(limit)
                more = bool(f.read(1))
        else:
            data, more = b'', False
        import base64
        return {'task_id': task_id, 'stream': stream, 'offset': offset, 'next_offset': offset + len(data),
                'text': data.decode('utf-8', errors='replace'), 'data_base64': base64.b64encode(data).decode(),
                'more_available': more, 'done': row['status'] in TERMINAL}

    def flush_events(self, events) -> None:
        with self.connect() as db:
            rows = db.execute('SELECT * FROM outbox WHERE delivered=0 ORDER BY seq LIMIT 100').fetchall()
        for row in rows:
            events.append(row['chat_id'], json.loads(row['payload']))
            with self.connect() as db:
                db.execute('UPDATE outbox SET delivered=1 WHERE seq=?', (row['seq'],))


def submit_prepared(store: TaskStore, scope: str, key: str, request: dict,
                    prepare: Callable[[], dict], origin: dict, deadline: float) -> dict:
    # Look up BEFORE loading current manifests: hot reload/deletion must not break replay.
    row = store.lookup(scope, key, request)
    if row is None:
        row = store.submit(scope, key, request, prepare(), origin)
    return store.wait(scope, row['task_id'], deadline)
