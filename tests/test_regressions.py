from __future__ import annotations
import asyncio
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from jsonschema.exceptions import ValidationError
from runtime.core import ROOT
from runtime.events import EventStore, process_identity
from runtime.git import git_status
from runtime.commands import run_command
from runtime import executor, processes, trace
from tests import test_v2
import monitor


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def test_event_store_init_preserves_live_call_and_explicit_recovery(self):
        store=EventStore(self.root/'chats')
        store.append('chat',{'type':'mcp_start','call_id':'live','instance':{'pid':os.getpid(),'identity':process_identity(os.getpid())}})
        another=EventStore(self.root/'chats')
        self.assertEqual(another.list_chats()[0]['active_calls'],1)
        another.recover_stale_calls()
        self.assertEqual(another.list_chats()[0]['active_calls'],1)
        store.append('chat',{'type':'mcp_start','call_id':'dead','instance':{'pid':99999999,'identity':'old'}})
        another.recover_stale_calls()
        self.assertEqual(another.list_chats()[0]['active_calls'],1)

    def test_schema_constraints_and_nullable(self):
        schema={'type':'object','properties':{'n':{'type':'integer','minimum':1},'v':{'type':['string','null']}},'additionalProperties':False}
        for args in ({'n':True},{'n':0},{'extra':1}):
            with self.assertRaises(ValidationError): executor._validate_arguments({'parameters':schema},args)
        executor._validate_arguments({'parameters':schema},{'n':1,'v':None})

    def test_bad_manifest_isolated(self):
        for name,tags in [('good',[]),('bad',None)]:
            d=self.root/name;d.mkdir();(d/'run.py').write_text('pass')
            (d/'manifest.json').write_text(json.dumps({'name':name,'entrypoint':'run.py','tags':tags}))
        with patch.object(executor,'ACTIONS_DIR',self.root):
            result=executor.catalog()
        self.assertEqual([x['name'] for x in result['actions']],['good'])
        self.assertEqual(len(result['errors']),1)

    def test_sdk_error_wrapper_and_business_failure_not_success(self):
        import server
        from mcp.types import CallToolRequestParams
        store=EventStore(self.root/'events')
        ctx=test_v2.RemoteV2Tests()._ctx(headers={'x-openai-chat-id':'probe','x-request-id':'probe-1'},params={'name':'missing','arguments':{}})
        async def call_next(ctx):
            return await server.mcp._handle_call_tool(ctx,CallToolRequestParams(name='missing',arguments={}))
        with patch.object(trace,'EVENTS',store):
            result=asyncio.run(trace.TraceMiddleware()(ctx,call_next))
        self.assertTrue(result.is_error)
        self.assertFalse(store.tail('probe')[-1]['ok'])
        self.assertTrue(store.tail('probe')[-1]['request_ok'])
        self.assertFalse(trace.result_status({'structuredContent':{'ok':False}})['ok'])
        self.assertIsNone(trace.result_status({'structuredContent':{'status':'running','task_ok':None}})['ok'])

    def test_stderr_visible_even_when_stdout_exists(self):
        out=self.root/'out';err=self.root/'err';out.write_text('');err.write_text('error')
        events=[{'stdout_path':str(out),'stderr_path':str(err)}]
        self.assertEqual(monitor.latest_output_path(events),err)
        self.assertEqual(monitor.latest_output_paths(events),[out,err])

    def test_git_unicode_newline_and_rename(self):
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        subprocess.run(['git','-C',str(self.root),'config','user.name','Test'],check=True)
        subprocess.run(['git','-C',str(self.root),'config','user.email','test@example.invalid'],check=True)
        old='旧文件.txt';new='新 文件\n.txt'
        (self.root/old).write_text('hello')
        subprocess.run(['git','-C',str(self.root),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.root),'commit','-qm','base'],check=True)
        subprocess.run(['git','-C',str(self.root),'mv',old,new],check=True)
        changes=git_status(str(self.root))['changes']
        self.assertEqual(changes[0]['path'],new)
        self.assertEqual(changes[0]['original_path'],old)

    def test_started_at_recorded_before_ended_at(self):
        with patch('runtime.commands.now_iso',side_effect=['START','END']):
            result=run_command('true',str(self.root))
        self.assertEqual(result['started_at'],'START')
        self.assertEqual(result['ended_at'],'END')

    def test_snapshot_pure_and_failed_exit_event_retries(self):
        proc=Mock(pid=123,returncode=None,stdin=None)
        item=processes.ManagedProcess('fake','true',self.root,proc,self.root/'log',{'chat_id':'chat'})
        manager=processes.ProcessManager.__new__(processes.ProcessManager)
        item.snapshot()
        proc.poll.assert_not_called()
        self.assertIsNone(item.completed_at)
        proc.returncode=0;proc.poll.return_value=0
        store=EventStore(self.root/'events')
        original=store.append; attempts=[]
        def append(*args):
            attempts.append(1)
            if len(attempts)==1: raise OSError('disk failure')
            return original(*args)
        with patch.object(processes,'PROCESS_DIR',self.root),patch.object(processes,'EVENTS',store),patch.object(store,'append',side_effect=append):
            with self.assertRaises(OSError): manager._refresh_item(item)
            self.assertFalse(item.exit_event_written)
            manager._refresh_item(item)
            manager._refresh_item(item)
        self.assertFalse(json.loads((self.root/'fake.json').read_text())['running'])
        self.assertEqual(len(store.tail('chat')),1)
