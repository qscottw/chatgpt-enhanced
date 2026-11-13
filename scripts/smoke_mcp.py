"""Real local HTTP MCP + independent-worker smoke test, always isolated in /tmp.

Use --long to test the actual 110-second response boundary (takes ~115 seconds).
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PROJECT = Path(__file__).resolve().parents[1]


async def call(url: str, name: str, args: dict) -> dict:
    async with httpx2.AsyncClient(trust_env=False, timeout=150) as http, streamable_http_client(url, http_client=http) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=150) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            assert not result.is_error, result
            return result.structured_content or json.loads(result.content[0].text)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--long',action='store_true')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='chatgpt-enhanced-http-') as tmp:
        root=Path(tmp)
        with socket.socket() as port_socket:
            port_socket.bind(('127.0.0.1',0))
            port=port_socket.getsockname()[1]
        env=dict(os.environ,REMOTE_ROOT=tmp,REMOTE_STATE_DIR=str(root/'state'),REMOTE_PORT=str(port),
                 REMOTE_HOST='127.0.0.1',PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=str(PROJECT))
        server=None
        with (root/'worker.log').open('w+') as worker_log, (root/'server.log').open('w+') as server_log:
            worker=subprocess.Popen([sys.executable,'-m','runtime.worker','--local'],cwd=PROJECT,env=env,stdout=worker_log,stderr=worker_log)
            def start_server():
                proc=subprocess.Popen([sys.executable,str(PROJECT/'server.py'),'--local'],cwd=PROJECT,env=env,stdout=server_log,stderr=server_log)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if proc.poll() is not None:
                        raise RuntimeError((root/'server.log').read_text())
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=.2):
                            return proc
                    except OSError:
                        time.sleep(.05)
                proc.kill();proc.wait()
                raise RuntimeError('Local MCP server did not start')
            url=f'http://127.0.0.1:{port}/mcp'
            try:
                server=start_server()
                submit={'command':'sleep 2; printf x >> counter; printf result','idempotency_key':'restart-probe',
                        'cwd':tmp,'wait_timeout_seconds':0}
                task=asyncio.run(call(url,'run_command',submit))
                server.terminate();server.wait(timeout=5)
                server=start_server()
                replay=asyncio.run(call(url,'run_command',submit))
                assert replay['task_id']==task['task_id']
                final=asyncio.run(call(url,'task_get',{'task_id':task['task_id'],'wait_timeout_seconds':10}))
                assert final['status']=='succeeded',final
                assert (root/'counter').read_text()=='x'
                assert final['result']['stdout']=='result'
                print('PASS: HTTP MCP restart, same-key replay, durable result, one side effect',flush=True)
                if args.long:
                    long_submit={'command':'sleep 112; printf x >> long-counter; printf long-result',
                                 'idempotency_key':'long-probe','cwd':tmp,'wait_timeout_seconds':110}
                    started=time.monotonic()
                    pending=asyncio.run(call(url,'run_command',long_submit))
                    elapsed=time.monotonic()-started
                    assert pending['status']=='running',pending
                    assert 109 <= elapsed < 120,elapsed
                    print(f'PASS: response returned after {elapsed:.2f}s with running/task_id',flush=True)
                    again=asyncio.run(call(url,'run_command',{**long_submit,'wait_timeout_seconds':0}))
                    assert again['task_id']==pending['task_id']
                    final=asyncio.run(call(url,'task_get',{'task_id':pending['task_id'],'wait_timeout_seconds':10}))
                    assert final['status']=='succeeded',final
                    assert final['result']['stdout']=='long-result'
                    assert (root/'long-counter').read_text()=='x'
                    print('PASS: >110s task completed, logs/result retained, retry did not duplicate execution',flush=True)
            except BaseException:
                print((root/'server.log').read_text(), file=sys.stderr)
                print((root/'worker.log').read_text(), file=sys.stderr)
                raise
            finally:
                for proc in (server,worker):
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
                        try: proc.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            proc.kill();proc.wait()


if __name__=='__main__':
    main()
