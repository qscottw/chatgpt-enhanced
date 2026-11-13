from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.startup import CONFIG_ERROR, PLATFORM_URL, configure, read_env

PROJECT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env_file = self.root / '.env'

    def test_missing_empty_and_placeholder_key_show_platform_link(self):
        for value in (None, '', '""', 'sk-...', 'your-api-key', '${OPENAI_API_KEY}', '你的实际密钥'):
            with self.subTest(value=value):
                if value is not None:
                    self.env_file.write_text(f'CONTROL_PLANE_API_KEY={value}\n')
                with self.assertRaisesRegex(ValueError, PLATFORM_URL):
                    configure(self.env_file)

    def test_only_runtime_settings_are_loaded_and_keys_not_inherited(self):
        secret = 'sk-configured-test-value'
        self.env_file.write_text(f'export CONTROL_PLANE_API_KEY="{secret}" # comment\nTUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\nREMOTE_PORT=8201\nREMOTE_ROOT="/a path"\nOPENAI_API_KEY=another-test-value\n')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'inherited', 'CONTROL_PLANE_API_KEY': 'inherited', 'TUNNEL_ID': 'tunnel_inherited'}, clear=True):
            configure(self.env_file)
            self.assertEqual(os.environ['REMOTE_PORT'], '8201')
            self.assertEqual(os.environ['REMOTE_ROOT'], '/a path')
            self.assertNotIn('OPENAI_API_KEY', os.environ)
            self.assertNotIn('CONTROL_PLANE_API_KEY', os.environ)
            self.assertNotIn('TUNNEL_ID', os.environ)

    def test_missing_empty_placeholder_and_malformed_tunnel_id(self):
        for value in (None, '', '""', 'tunnel_...', 'tunnel_example', 'your-tunnel-id', '${TUNNEL_ID}', 'wrong_prefix', 'tunnel_', '"tunnel_has spaces"'):
            with self.subTest(value=value):
                self.env_file.write_text('CONTROL_PLANE_API_KEY=sk-configured-test-value\n' +
                                         (f'TUNNEL_ID={value}\n' if value is not None else ''))
                with patch.dict(os.environ, {'TUNNEL_ID': 'tunnel_environmentonly'}):
                    with self.assertRaises(ValueError) as failure:
                        configure(self.env_file)
                message = str(failure.exception)
                self.assertIn('TUNNEL_ID', message)
                self.assertIn(PLATFORM_URL, message)
                self.assertNotIn('sk-configured-test-value', message)

    def test_valid_tunnel_does_not_hide_missing_key(self):
        self.env_file.write_text('TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\n')
        with self.assertRaisesRegex(ValueError, 'CONTROL_PLANE_API_KEY'):
            configure(self.env_file)

    def test_environment_key_does_not_hide_missing_file_configuration(self):
        with patch.dict(os.environ, {'CONTROL_PLANE_API_KEY': 'sk-environment-only'}):
            with self.assertRaises(ValueError):
                configure(self.env_file)

    def test_malformed_configuration_does_not_print_secrets(self):
        self.env_file.write_text('CONTROL_PLANE_API_KEY="sk-secret-not-for-output\n')
        with self.assertRaises(ValueError) as failure:
            read_env(self.env_file)
        self.assertNotIn('sk-secret', str(failure.exception))

    def test_both_entrypoints_fail_before_creating_state(self):
        state = self.root / 'must-not-exist'
        env = dict(os.environ, REMOTE_STATE_DIR=str(state), PYTHONDONTWRITEBYTECODE='1')
        for command in ([str(PROJECT / 'server.py')], ['-m', 'runtime.worker']):
            result = subprocess.run([sys.executable, *command, '--env-file', str(self.env_file)], cwd=PROJECT,
                                    env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, CONFIG_ERROR, result.stderr)
            self.assertIn(PLATFORM_URL, result.stderr)
            self.assertIn('CONTROL_PLANE_API_KEY', result.stderr)
            self.assertIn('TUNNEL_ID', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(state.exists())

    def test_local_mode_and_external_runtime_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            configure(self.env_file, local=True)
        self.env_file.write_text('CONTROL_PLANE_API_KEY=sk-configured-test-value\nTUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef\nREMOTE_PORT=8200\n')
        with patch.dict(os.environ, {'REMOTE_PORT': '9999'}, clear=True):
            configure(self.env_file)
            self.assertEqual(os.environ['REMOTE_PORT'], '9999')
