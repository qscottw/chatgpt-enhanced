from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.core import ROOT
from runtime.events import EventStore
from runtime.service import TaskService
from runtime.tasks import IdempotencyConflict, TaskStore, wait_deadline
from runtime.worker import Worker
import runtime.executor as executor


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT, prefix='durable-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / 'state')
        self.service = TaskService(self.store, 'test')
        self.events = EventStore(self.root / 'chats')

    def start_worker(self):
        worker = Worker(self.store, 2, self.events)
        thread = threading.Thread(target=worker.run)
        thread.start()
        def cleanup():
            worker.stop.set()
            thread.join(8)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        return worker, thread

    def submit(self, command, key='key', wait=0):
        return self.service.command(command, key, str(self.root), False, wait_deadline(wait))

    def done(self, task):
        return self.store.wait('test', task['task_id'], wait_deadline(8))

    def test_concurrent_submissions_use_one_task_and_one_side_effect(self):
        command = 'printf x >> counter'
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            responses = list(pool.map(lambda _: self.submit(command), range(24)))
        self.assertEqual(len({r['task_id'] for r in responses}), 1)
        self.start_worker()
        self.assertEqual(self.done(responses[0])['status'], 'succeeded')
        self.assertEqual((self.root/'counter').read_text(), 'x')

    def test_wait_expiry_does_not_kill_task_and_lost_response_replays(self):
        self.start_worker()
        started = time.monotonic()
        first = self.submit('sleep 0.4; printf x >> counter; printf result', wait=.02)
        self.assertLess(time.monotonic() - started, .3)
        self.assertIn(first['status'], ('queued', 'running'))
        again = self.submit('sleep 0.4; printf x >> counter; printf result')
        self.assertEqual(first['task_id'], again['task_id'])
        final = self.done(first)
        self.assertEqual(final['result']['stdout'], 'result')
        reopened = TaskStore(self.store.root)
        self.assertEqual(reopened.response(reopened.lookup('test', 'key'))['result'], final['result'])
        self.assertEqual((self.root/'counter').read_text(), 'x')
        self.assertLess(final['started_at'], final['ended_at'])

    def test_separate_processes_share_atomic_idempotency(self):
        code = """import sys
from pathlib import Path
from runtime.tasks import TaskStore
s=TaskStore(Path(sys.argv[1]))
r=s.submit('test','cross-process',{'operation':'one'}, {'argv':['true'],'cwd':sys.argv[2]},{})
print(r['task_id'])
"""
        children = [subprocess.Popen([sys.executable, '-c', code, str(self.store.root), str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        ids=[]
        for child in children:
            out,err=child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,err)
            ids.append(out.strip())
        self.assertEqual(len(set(ids)),1)

    def test_event_projection_failure_does_not_block_execution(self):
        with patch.object(self.store, 'flush_events', side_effect=OSError('projection offline')), self.assertLogs('runtime.worker', level='ERROR'):
            self.start_worker()
            result=self.done(self.submit('printf still-runs'))
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['result']['stdout'],'still-runs')

    def test_parameter_conflict_and_scope(self):
        first = self.submit('true')
        with self.assertRaises(IdempotencyConflict):
            self.submit('false')
        other = TaskService(self.store, 'other').command('false','key',str(self.root),False,wait_deadline(0))
        self.assertNotEqual(first['task_id'], other['task_id'])
        with self.assertRaises(KeyError):
            self.store.get('other', first['task_id'])

    def test_running_cancellation_works(self):
        self.start_worker()
        task = self.submit('sleep 20', key='cancel')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.store.get('test',task['task_id'])['pid']:
            time.sleep(.02)
        self.store.cancel('test',task['task_id'])
        self.assertEqual(self.done(task)['status'], 'cancelled')
        self.assertEqual(self.submit('sleep 20', key='cancel')['status'], 'cancelled')

    def test_queued_cancellation_never_executes(self):
        task = self.submit('touch should-not-exist')
        self.store.cancel('test', task['task_id'])
        self.start_worker()
        self.assertEqual(self.done(task)['status'], 'cancelled')
        self.assertFalse((self.root/'should-not-exist').exists())

    def test_failed_task_not_reexecuted(self):
        self.start_worker()
        task = self.submit('echo x >> counter; echo error >&2; exit 7')
        final = self.done(task)
        self.assertEqual(final['status'], 'failed')
        again = self.submit('echo x >> counter; echo error >&2; exit 7')
        self.assertEqual(again['result'], final['result'])
        self.assertEqual((self.root/'counter').read_text(), 'x\n')

    def test_logs_replay_offsets_and_split_unicode(self):
        self.start_worker()
        task = self.submit("printf '中文'; printf 'ERROR' >&2")
        self.done(task)
        a = self.store.logs('test', task['task_id'], 'stdout', 0, 2)
        b = self.store.logs('test', task['task_id'], 'stdout', 0, 2)
        self.assertEqual(a, b)
        import base64
        tail = self.store.logs('test', task['task_id'], 'stdout', a['next_offset'], 100)
        self.assertEqual((base64.b64decode(a['data_base64'])+base64.b64decode(tail['data_base64'])).decode(), '中文')
        self.assertEqual(self.store.logs('test', task['task_id'], 'stderr', 0, 100)['text'], 'ERROR')

    def test_outbox_retries_after_projection_failure(self):
        task = self.submit('true')
        with patch.object(self.events, 'append', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.store.flush_events(self.events)
        self.store.flush_events(self.events)
        # Simulate crash after append but before marking delivered.
        with self.store.connect() as db:
            db.execute('UPDATE outbox SET delivered=0')
        self.store.flush_events(self.events)
        events = self.events.tail('local-tasks')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['task_id'], task['task_id'])
        self.assertEqual(self.events.list_chats()[0]['active_tasks'], [task['task_id']])

    def test_action_snapshot_replay_survives_manifest_deletion(self):
        actions=self.root/'actions'; d=actions/'hello';d.mkdir(parents=True)
        manifest={'name':'hello','entrypoint':'run.py','parameters':{'type':'object','properties':{'v':{'type':'integer'}},'additionalProperties':False}}
        (d/'manifest.json').write_text(json.dumps(manifest))
        (d/'run.py').write_text('import json,sys; print(json.dumps({"version":"old","args":json.load(sys.stdin)}))')
        with patch.object(executor, 'ACTIONS_DIR', actions):
            task=self.service.action('hello','{"v":1}','action',wait_deadline(0))
            (d/'run.py').write_text('raise RuntimeError("new source")')
            (d/'manifest.json').unlink()
            again=self.service.action('hello','{ "v": 1 }','action',wait_deadline(0))
            self.assertEqual(task['task_id'],again['task_id'])
            self.start_worker()
            final=self.done(task)
            self.assertEqual(final['result']['result']['version'],'old')

    def test_action_failure_payload_is_failed(self):
        spec={'argv':[sys.executable,'-c','print(\'{"ok":false}\')'],'cwd':str(self.root),
              'action':'failure','version':'1'}
        row=self.store.submit('test','business-fail',{'kind':'test'},spec,{})
        self.start_worker()
        self.assertEqual(self.done(row)['status'],'failed')

    def test_worker_crash_marks_unknown_without_repeating_side_effect(self):
        task=self.submit('echo x >> counter; sleep 20')
        env=dict(os.environ,REMOTE_STATE_DIR=str(self.store.root),REMOTE_ROOT=str(ROOT),PYTHONDONTWRITEBYTECODE='1')
        child=subprocess.Popen([sys.executable,'-m','runtime.worker','--local'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        pid=None
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                row=self.store.get('test',task['task_id'])
                if row['pid'] and (self.root/'counter').exists():
                    pid=row['pid'];break
                time.sleep(.03)
            self.assertIsNotNone(pid)
            child.kill();child.wait(timeout=3)
            self.start_worker()
            final=self.done(task)
            self.assertEqual(final['status'],'unknown')
            self.assertIsNone(final['task_ok'])
            self.assertEqual(self.submit('echo x >> counter; sleep 20')['task_id'],task['task_id'])
            self.assertEqual((self.root/'counter').read_text(),'x\n')
        finally:
            if child.poll() is None:
                child.kill();child.wait()
            if pid:
                try: os.killpg(pid,signal.SIGKILL)
                except ProcessLookupError: pass

    def test_second_worker_cannot_recover_live_tasks(self):
        worker,thread=self.start_worker()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with self.store.connect() as db:
                if db.execute('SELECT * FROM worker').fetchone(): break
            time.sleep(.02)
        other=Worker(self.store,events=self.events)
        with self.assertRaises(RuntimeError): other.run()

    def test_finished_state_retries_without_reexecution(self):
        original=self.store.finish
        calls=[]
        def finish(*args):
            calls.append(args)
            if len(calls)==1: raise OSError('transient persistence error')
            return original(*args)
        with patch.object(self.store,'finish',side_effect=finish), self.assertLogs('runtime.worker',level='ERROR'):
            self.start_worker()
            final=self.done(self.submit('echo x >> counter'))
        self.assertEqual(final['status'],'succeeded')
        self.assertEqual((self.root/'counter').read_text(),'x\n')
        self.assertGreaterEqual(len(calls),2)

    def test_request_cancellation_does_not_cancel_accepted_task(self):
        import server
        self.start_worker()
        async def scenario():
            with patch.object(server,'TASKS',self.store),patch.object(server,'SERVICE',self.service):
                pending=asyncio.create_task(server.run_command('sleep .3; echo x >> counter','disconnect',str(self.root),10))
                until = time.monotonic() + 3
                while self.store.lookup('test','disconnect') is None:
                    if pending.done():
                        pending.result()
                        self.fail('Submission finished without a task')
                    self.assertLess(time.monotonic(), until, 'Submission did not become durable')
                    await asyncio.sleep(.01)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError): await pending
                row=self.store.lookup('test','disconnect')
                return await server.task_get(row['task_id'],5)
        result=asyncio.run(scenario())
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual((self.root/'counter').read_text(),'x\n')
