"""Standalone task supervisor. Its lifetime is independent of any MCP request/server."""
from __future__ import annotations

if __name__ == '__main__':
    from .startup import bootstrap
    bootstrap()

import base64
import concurrent.futures
import fcntl
import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .commands import _BoundedBytes
from .core import MAX_OUTPUT_CHARS, STATE_DIR, now_iso
from .events import EVENTS
from .tasks import TaskStore

log = logging.getLogger(__name__)


def stop_group(proc: subprocess.Popen) -> None:
    # The shell may have exited while descendants still own the group.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


def preview(path: Path) -> tuple[str, bool, int]:
    size = path.stat().st_size
    capture = _BoundedBytes(MAX_OUTPUT_CHARS)
    with path.open('rb') as f:
        if size <= capture.limit:
            capture.add(f.read(capture.limit))
        else:
            capture.head.extend(f.read(capture.head_limit))
            f.seek(-capture.tail_limit, 2)
            capture.tail.extend(f.read(capture.tail_limit))
            capture.total = size
    text, truncated = capture.render(path)
    return text, truncated, size


class Worker:
    def __init__(self, store: TaskStore, concurrency: int = 4, events=EVENTS):
        self.store = store
        if not 1 <= concurrency <= 64:
            raise ValueError('worker concurrency must be between 1 and 64')
        self.concurrency = concurrency
        self.events = events
        self.worker_id = uuid.uuid4().hex
        self.stop = threading.Event()

    def execute(self, row: dict) -> None:
        task_id = row['task_id']
        spec = json.loads(row['spec_json'])
        directory = self.store.root / 'tasks' / task_id
        proc = None
        started = time.monotonic()
        started_at = now_iso()
        status = 'failed'
        result = {}
        try:
            directory.mkdir(parents=True, exist_ok=True)
            argv = spec['argv']
            if 'source_base64' in spec:
                entry = directory / spec['source_name']
                entry.write_bytes(base64.b64decode(spec['source_base64']))
                entry.chmod(0o700)
                argv = [str(entry) if x == '$ENTRYPOINT' else x for x in argv]
            stdin = directory / 'stdin.json'
            stdin.write_text(spec.get('stdin', ''), encoding='utf-8')
            out, err = directory / 'stdout.log', directory / 'stderr.log'
            env = dict(os.environ, **spec.get('env', {}), REMOTE_TASK_ID=task_id,
                       REMOTE_IDEMPOTENCY_KEY=row['key'])
            with stdin.open('rb') as inp, out.open('wb', buffering=0) as stdout, err.open('wb', buffering=0) as stderr:
                current = self.store.get(row['scope'], task_id)
                if current['cancel_requested'] or self.stop.is_set():
                    status = 'cancelled'
                    result = {'ok': False, 'error': 'Cancelled before process launch', 'exit_code': None}
                else:
                    proc = subprocess.Popen(argv, cwd=spec['cwd'], stdin=inp, stdout=stdout, stderr=stderr,
                                            env=env, start_new_session=True)
                    self.store.set_pid(task_id, self.worker_id, proc.pid)
                    while proc.poll() is None:
                        if self.stop.is_set() or self.store.get(row['scope'], task_id)['cancel_requested']:
                            status = 'cancelled'
                            break
                        self.stop.wait(0.05)
                    else:
                        status = 'succeeded' if proc.returncode == 0 else 'failed'
                    # Tasks own their process group. No detached background descendants are retained.
                    stop_group(proc)
                    result = {'ok': status == 'succeeded', 'exit_code': proc.returncode, 'pid': proc.pid}
                os.fsync(stdout.fileno())
                os.fsync(stderr.fileno())
            for stream, path in [('stdout', out), ('stderr', err)]:
                text, truncated, count = preview(path)
                result.update({stream: text, stream + '_truncated': truncated,
                               stream + '_bytes': count, stream + '_full_path': str(path)})
            if spec.get('action'):
                result.update(action=spec['action'], version=spec['version'], result=None)
                if status == 'succeeded' and out.stat().st_size <= 4 * 1024 * 1024:
                    try:
                        result['result'] = json.loads(out.read_text(encoding='utf-8'))
                        # Explicit action-level failure has semantic meaning even with exit code zero.
                        if isinstance(result['result'], dict) and (result['result'].get('ok') is False or result['result'].get('isError') is True):
                            status = 'failed'
                            result['ok'] = False
                    except (ValueError, UnicodeError):
                        pass
        except Exception as exc:
            log.exception('Task execution failed: %s', task_id)
            if proc is not None:
                try:
                    stop_group(proc)
                except Exception:
                    log.exception('Process cleanup failed')
            # After launch an infrastructure error does not prove that side effects failed.
            status = 'unknown' if proc is not None else 'failed'
            result = {'ok': None if status == 'unknown' else False, 'error': str(exc), 'error_type': type(exc).__name__}
        result.update(started_at=started_at, ended_at=now_iso(), duration_ms=round((time.monotonic()-started)*1000, 3))
        # Retry completion persistence; never execute again because saving the result failed.
        while True:
            try:
                self.store.finish(task_id, self.worker_id, status, result)
                return
            except Exception:
                log.exception('Completion persistence failed for %s; retrying', task_id)
                time.sleep(0.5)

    def run(self) -> None:
        lock_path = self.store.root / 'worker.lock'
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another worker owns this state directory') from None
            self.store.recover(self.worker_id)
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = set()
                while not self.stop.is_set():
                    try:
                        self.store.heartbeat(self.worker_id)
                        try:
                            self.store.flush_events(self.events)
                        except Exception:
                            # Observability is a retryable projection, not a prerequisite
                            # for claiming work or persisting task completion.
                            log.exception('Event projection failed; retained in outbox')
                        for future in list(futures):
                            if future.done():
                                future.result()
                                futures.remove(future)
                        while len(futures) < self.concurrency and not self.stop.is_set():
                            row = self.store.claim(self.worker_id)
                            if row is None:
                                break
                            futures.add(pool.submit(self.execute, row))
                    except Exception:
                        log.exception('Worker supervisor iteration failed')
                    self.stop.wait(0.1)
            self.store.flush_events(self.events)
            with self.store.connect() as db:
                db.execute('DELETE FROM worker WHERE worker_id=?', (self.worker_id,))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    worker = Worker(TaskStore(STATE_DIR), int(os.getenv('REMOTE_WORKER_CONCURRENCY', '4')))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop.set())
    worker.run()


if __name__ == '__main__':
    main()
