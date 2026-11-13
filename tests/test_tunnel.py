from __future__ import annotations
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.tunnel import plan, write_config


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.env=self.root/'.env'
        self.key='sk-test-private-value'
        self.env.write_text(f'CONTROL_PLANE_API_KEY={self.key}\nTUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\nREMOTE_STATE_DIR={self.root}/state\nREMOTE_PORT=8202\nTUNNEL_ADMIN_PORT=8282\nTUNNEL_CLIENT_BIN={sys.executable}\nCLOUDFLARED_BIN={sys.executable}\n')

    def test_key_only_in_tunnel_environment_profile_is_private(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'other-secret','TUNNEL_CLIENT_PROFILE':'remote-v2','CONTROL_PLANE_BASE_URL':'https://untrusted.invalid'},clear=True):
            config,env,argv,path=plan(self.env)
        self.assertEqual(env['CONTROL_PLANE_API_KEY'],self.key)
        self.assertNotIn('OPENAI_API_KEY',env)
        self.assertNotIn('TUNNEL_CLIENT_PROFILE',env)
        self.assertNotIn('CONTROL_PLANE_BASE_URL',env)
        self.assertNotIn(self.key,json.dumps(config))
        self.assertNotIn(self.key,' '.join(argv))
        self.assertEqual(config['mcp']['server_urls'][0]['url'],'http://127.0.0.1:8202/mcp')
        self.assertEqual(config['health']['listen_addr'],'127.0.0.1:8282')
        write_config(config,path)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(json.loads(path.read_text()),config)

    def test_proxy_applies_to_tunnel_and_bypasses_local_mcp(self):
        with self.env.open('a') as f:
            f.write('HTTPS_PROXY=http://127.0.0.1:7890\nNO_PROXY=example.internal\n')
        with patch.dict(os.environ,{},clear=True):
            _,env,_,_=plan(self.env)
        self.assertEqual(env['HTTPS_PROXY'],'http://127.0.0.1:7890')
        self.assertIn('127.0.0.1',env['NO_PROXY'])
        self.assertIn('example.internal',env['NO_PROXY'])

    def test_invalid_ports_or_remote_host_rejected(self):
        for setting in ('TUNNEL_ADMIN_PORT=8202','TUNNEL_ADMIN_PORT=70000','REMOTE_HOST=192.168.1.1'):
            with self.subTest(setting=setting),patch.dict(os.environ,{},clear=True):
                test=self.root/'bad.env'
                test.write_text(self.env.read_text()+'\n'+setting+'\n')
                with self.assertRaises(ValueError): plan(test)

    def test_target_owns_all_services_and_installer_renders_interpreter(self):
        project=Path(__file__).resolve().parents[1]
        spec=importlib.util.spec_from_file_location('services',project/'scripts/services.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        target=(project/'systemd/chatgpt-enhanced.target').read_text()
        for kind in ('mcp','worker','tunnel'):
            name=f'chatgpt-enhanced-{kind}.service'
            self.assertIn(name,target)
            source=(project/'systemd'/name).read_text()
            self.assertIn('PartOf=chatgpt-enhanced.target',source)
            rendered=module.render(source,Path('/a path/bin/python'))
            self.assertIn('"/a path/bin/python"',rendered)
            self.assertIn(f'WorkingDirectory={project}\n',rendered)
            self.assertNotIn('%h/chatgpt-enhanced',rendered)
            self.assertNotIn('EnvironmentFile=',rendered)
