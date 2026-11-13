#!/usr/bin/env python3
"""Install and operate all chatgpt-enhanced services through the user systemd manager."""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
TARGET = 'chatgpt-enhanced.target'
SERVICES = ['chatgpt-enhanced-worker.service', 'chatgpt-enhanced-mcp.service', 'chatgpt-enhanced-tunnel.service']


def systemd_arg(value: str) -> str:
    if any(c in value for c in '\n\r\0'):
        raise ValueError('Unsupported newline in service path')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def render(source: str, python: Path) -> str:
    # WorkingDirectory is a single path directive, not an ExecStart argument list.
    # systemd treats surrounding quotes here as literal filename characters.
    source = source.replace('WorkingDirectory=%h/chatgpt-enhanced', 'WorkingDirectory=' + str(PROJECT).replace('%', '%%'))
    source = source.replace('%h/chatgpt-enhanced/.venv/bin/python', systemd_arg(str(python)))
    source = source.replace('%h/chatgpt-enhanced/server.py', systemd_arg(str(PROJECT / 'server.py')))
    return source


def run(*args):
    subprocess.run(args, check=True)


def install(python: Path, enable_linger: bool):
    python = python.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError('Python executable does not exist')
    run(str(python), '-c', 'import mcp, jsonschema')
    run(str(python), '-m', 'runtime.startup')
    run(str(python), '-m', 'runtime.tunnel', '--check')
    units = Path.home() / '.config/systemd/user'
    # Validate concrete rendered unit files before touching the installed services.
    with tempfile.TemporaryDirectory(prefix='chatgpt-enhanced-units-') as tmp:
        generated=[]
        for name in [*SERVICES, TARGET]:
            path=Path(tmp)/name
            path.write_text(render((PROJECT/'systemd'/name).read_text(), python))
            generated.append(path)
        run('systemd-analyze', '--user', 'verify', *(str(p) for p in generated))
        units.mkdir(parents=True, exist_ok=True)
        for path in generated:
            target = units/path.name
            temp = target.with_suffix(target.suffix+'.tmp')
            temp.write_text(path.read_text())
            temp.chmod(0o644)
            os.replace(temp, target)
    if enable_linger:
        run('loginctl', 'enable-linger', str(os.getuid()))
    run('systemctl', '--user', 'daemon-reload')
    run('systemctl', '--user', 'enable', '--now', TARGET)
    print('Installed and started chatgpt-enhanced.target. Run status to inspect all three services.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install','start','stop','restart','status','logs'])
    parser.add_argument('--python',type=Path,default=Path(sys.executable),help='Python interpreter recorded in installed units')
    parser.add_argument('--enable-linger',action='store_true',help='Keep user services running after logout and start at boot')
    args=parser.parse_args()
    os.chdir(PROJECT)
    try:
        if args.action=='install': install(args.python,args.enable_linger)
        elif args.action=='status': run('systemctl','--user','status','--no-pager',TARGET,*SERVICES)
        elif args.action=='logs': run('journalctl','--user','-f',*(x for s in SERVICES for x in ('-u',s)))
        else: run('systemctl','--user',args.action,TARGET)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f'Service management failed: {exc}',file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__':
    main()
