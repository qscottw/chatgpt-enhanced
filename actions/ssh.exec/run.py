from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def read_text(path: Path, limit: int = 4 * 1024 * 1024) -> tuple[str, bool]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        data = fh.read(limit)
    return data.decode("utf-8", errors="replace"), size > limit


def main() -> None:
    args = json.load(sys.stdin)
    ssh = shutil.which("ssh")
    if ssh is None:
        emit({"ok": False, "error": "OpenSSH client 'ssh' was not found in PATH"})
        return

    host = str(args["host"])
    command = str(args["command"])
    user = args.get("user")
    port = args.get("port")
    connect_timeout = int(args.get("connect_timeout_seconds", 10))
    strict = str(args.get("strict_host_key_checking", "accept-new"))
    reuse = bool(args.get("reuse_connection", True))
    persist = int(args.get("control_persist_seconds", 43200))

    destination = f"{user}@{host}" if user else host
    argv = [
        ssh,
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", f"StrictHostKeyChecking={strict}",
    ]

    if reuse:
        control_dir = Path.home() / ".ssh"
        control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        argv.extend([
            "-o", "ControlMaster=auto",
            "-o", f"ControlPersist={persist}",
            "-o", f"ControlPath={control_dir}/bgc-%C",
        ])
    else:
        argv.extend(["-o", "ControlMaster=no"])

    if port is not None:
        argv.extend(["-p", str(int(port))])

    identity_file = args.get("identity_file")
    if identity_file:
        identity = Path(os.path.expandvars(os.path.expanduser(str(identity_file))))
        argv.extend(["-i", str(identity)])

    argv.extend(["--", destination, command])

    with tempfile.TemporaryDirectory(prefix="ssh-exec-") as tmp:
        stdout_path = Path(tmp) / "stdout"
        stderr_path = Path(tmp) / "stderr"
        with stdout_path.open("wb") as stdout_fh, stderr_path.open("wb") as stderr_fh:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout_fh,
                stderr=stderr_fh,
                check=False,
            )
        stdout, stdout_truncated = read_text(stdout_path)
        stderr, stderr_truncated = read_text(stderr_path)

    emit(
        {
            "ok": proc.returncode == 0,
            "host": host,
            "user": user,
            "port": port,
            "connection_reuse": reuse,
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
    )


if __name__ == "__main__":
    main()
