"""Prepare a private tunnel-client config and replace this process with its daemon."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

from .startup import CONFIG_ERROR, DEFAULT_ENV, KEY_NAME, PLATFORM_URL, TUNNEL_NAME, configure, read_env


def executable(value: str | None, name: str) -> str:
    candidate = value or shutil.which(name)
    if not candidate:
        bundled = Path.home() / 'topc' / 'bin' / name
        candidate = str(bundled) if bundled.exists() else None
    if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
        raise ValueError(f'找不到可执行的 {name}；请在 .env 配置 ' + ('TUNNEL_CLIENT_BIN' if name == 'tunnel-client' else 'CLOUDFLARED_BIN'))
    return str(Path(candidate).resolve())


def port(value: str, name: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f'{name} 必须是 1–65535 的整数') from None
    if not 1 <= number <= 65535:
        raise ValueError(f'{name} 必须是 1–65535 的整数')
    return number


def plan(env_file: Path = DEFAULT_ENV) -> tuple[dict, dict, list[str], Path]:
    values = read_env(env_file)
    configure(env_file)
    host = os.environ.get('REMOTE_HOST', '127.0.0.1')
    if host in ('0.0.0.0', '::'):
        host = '127.0.0.1' if host == '0.0.0.0' else '::1'
    if host != 'localhost':
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError('MCP 地址必须为本机 loopback 地址')
        except ValueError:
            raise ValueError('隧道的 REMOTE_HOST 必须是本机 IP 或 localhost') from None
    mcp_port = port(os.environ.get('REMOTE_PORT', '8200'), 'REMOTE_PORT')
    admin_port = port(values.get('TUNNEL_ADMIN_PORT', '8280'), 'TUNNEL_ADMIN_PORT')
    if mcp_port == admin_port:
        raise ValueError('TUNNEL_ADMIN_PORT 不能与 REMOTE_PORT 相同')
    authority = f'[{host}]' if ':' in host else host
    config = {
        'config_version': 1,
        'control_plane': {'base_url': 'https://api.openai.com', 'tunnel_id': values[TUNNEL_NAME],
                          'api_key': 'env:CONTROL_PLANE_API_KEY'},
        'health': {'listen_addr': f'127.0.0.1:{admin_port}'},
        'admin_ui': {'open_browser': False},
        'log': {'level': 'info', 'format': 'json'},
        'mcp': {'server_urls': [{'channel': 'main', 'url': f'http://{authority}:{mcp_port}/mcp'}]},
    }
    # Do not inherit unrelated profile overrides or keys from an interactive shell.
    allowed = ('HOME', 'PATH', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TZ',
               'SSL_CERT_FILE', 'SSL_CERT_DIR', 'XDG_RUNTIME_DIR')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env[KEY_NAME] = values[KEY_NAME]
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'):
        if values.get(key):
            env[key] = values[key]
    # Always bypass outbound proxies for the local MCP hop.
    env['NO_PROXY'] = ','.join(filter(None, [env.get('NO_PROXY'), 'localhost', '127.0.0.1', '::1']))
    if values.get('TUNNEL_HTTP_PROXY'):
        env['TUNNEL_HTTP_PROXY'] = values['TUNNEL_HTTP_PROXY']
        config['control_plane']['http_proxy'] = 'env:TUNNEL_HTTP_PROXY'
    binary = executable(values.get('TUNNEL_CLIENT_BIN'), 'tunnel-client')
    cloudflared = executable(values.get('CLOUDFLARED_BIN'), 'cloudflared')
    state = Path(os.environ.get('REMOTE_STATE_DIR', str(Path.home() / '.local/state/chatgpt-enhanced'))).expanduser().resolve()
    target = state / 'tunnel' / 'profile.json'
    argv = [binary, 'run', '--config', str(target), '--cloudflared.path', cloudflared]
    return config, env, argv, target


def write_config(config: dict, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.profile-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(config, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def wait_for_mcp(config: dict, timeout: float = 45) -> None:
    from urllib.parse import urlsplit
    endpoint = urlsplit(config['mcp']['server_urls'][0]['url'])
    until = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port), timeout=1):
                return
        except OSError:
            if time.monotonic() >= until:
                raise TimeoutError('本地 MCP 未就绪；请检查 chatgpt-enhanced-mcp.service') from None
            time.sleep(.25)


def main() -> None:
    parser = argparse.ArgumentParser(description='chatgpt-enhanced 隧道启动器（由 systemd 管理）')
    parser.add_argument('--env-file', type=Path, default=DEFAULT_ENV)
    parser.add_argument('--check', action='store_true', help='仅验证配置和二进制，不联网、不写配置')
    args = parser.parse_args()
    try:
        config, env, argv, target = plan(args.env_file)
        if args.check:
            print('隧道配置检查通过；API key 不写入 profile 或命令行。')
            return
        write_config(config, target)
    except (ValueError, OSError):
        # Do not print third-party exception strings that might embed credential values.
        print(f'隧道配置无效：请检查 .env 的 API key、TUNNEL_ID、端口及客户端路径。\n{PLATFORM_URL}', file=sys.stderr)
        raise SystemExit(CONFIG_ERROR) from None
    try:
        wait_for_mcp(config)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    os.execve(argv[0], argv, env)


if __name__ == '__main__':
    main()
