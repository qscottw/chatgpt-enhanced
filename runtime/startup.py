"""Check local credentials before importing modules that create runtime state."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

DEFAULT_ENV = Path(__file__).resolve().parents[1] / '.env'
PLATFORM_URL = 'https://platform.openai.com/settings/organization/projects'
KEY_NAME = 'CONTROL_PLANE_API_KEY'
TUNNEL_NAME = 'TUNNEL_ID'
CONFIG_ERROR = 78


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        name, separator, raw = line.partition('=')
        name = name.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError(f'.env 第 {number} 行格式错误，应为 NAME=value')
        try:
            parts = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            raise ValueError(f'.env 第 {number} 行引号未正确闭合') from None
        if len(parts) > 1:
            raise ValueError(f'.env 第 {number} 行含空格的值需要使用引号')
        values[name] = parts[0] if parts else ''
    return values


def key_configured(value: str) -> bool:
    value = value.strip()
    marker = value.lower().removeprefix('sk-proj-').removeprefix('sk-')
    placeholders = ('your-', 'your_', 'replace', 'changeme', 'placeholder', 'example', '填入', '替换', '你的')
    return (bool(value) and not any(c.isspace() for c in value)
            and not marker.startswith(placeholders)
            and not any(p in value for p in ('...', '…', '$', '<', '>')))


def tunnel_configured(value: str) -> bool:
    value = value.strip()
    return (bool(re.fullmatch(r'tunnel_[A-Za-z0-9]+', value))
            and key_configured(value.removeprefix('tunnel_')))


def configure(path: Path = DEFAULT_ENV, *, local: bool = False) -> None:
    try:
        values = read_env(path)
    except FileNotFoundError:
        values = {}
    except (OSError, UnicodeError) as exc:
        raise ValueError('无法读取 .env 配置文件') from exc
    missing = []
    if not key_configured(values.get(KEY_NAME, '')):
        missing.append(KEY_NAME)
    if not tunnel_configured(values.get(TUNNEL_NAME, '')):
        missing.append(TUNNEL_NAME)
    if not local and missing:
        raise ValueError(
            f'{path} 缺少配置或配置无效：{", ".join(missing)}。\n'
            '请先将 .env.example 复制为 .env，并填写隧道认证 API key 和 tunnel ID。\n'
            f'前往 OpenAI 平台获取或管理 API key 和 tunnel ID：{PLATFORM_URL}\n'
            '配置示例：\nCONTROL_PLANE_API_KEY=你的实际密钥\nTUNNEL_ID=tunnel_你的实际隧道ID\n'
            '本地离线开发可显式使用 --local；普通启动不会跳过检查。'
        )
    # Only runtime settings belong in MCP/worker environments. Credentials are
    # checked here but are for a separate tunnel client, not arbitrary child tasks.
    for name, value in values.items():
        if name.startswith('REMOTE_'):
            os.environ.setdefault(name, value)
    for name in (KEY_NAME, TUNNEL_NAME, 'OPENAI_API_KEY'):
        os.environ.pop(name, None)


def bootstrap() -> None:
    parser = argparse.ArgumentParser(description='chatgpt-enhanced 启动配置检查')
    parser.add_argument('--env-file', type=Path, default=DEFAULT_ENV, help='配置文件路径，默认项目 .env')
    parser.add_argument('--local', action='store_true', help='仅本地开发：允许未配置隧道 API key 和 tunnel ID')
    args = parser.parse_args()
    try:
        configure(args.env_file, local=args.local)
    except ValueError as exc:
        print(f'chatgpt-enhanced 启动失败：\n{exc}', file=sys.stderr)
        if PLATFORM_URL not in str(exc):
            print(f'请检查 .env 配置；OpenAI 平台：{PLATFORM_URL}', file=sys.stderr)
        raise SystemExit(CONFIG_ERROR) from None


if __name__ == '__main__':
    bootstrap()
    print('chatgpt-enhanced 配置检查通过（仅检查是否填写，不验证远端权限）。')
