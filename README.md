# ChatGPT Enhanced

> A durable, observable local development runtime for ChatGPT, connected through MCP and OpenAI Tunnel.

**ChatGPT Enhanced** connects ChatGPT to a controlled local Linux/WSL workspace for coding tasks. It supports safe-path file operations, command execution, Git workflows, persistent jobs, dynamic actions and a local trace monitor. It is **not** a model inference API or a model server.

## Features

- **MCP tools** to read/write files, apply patches, run commands and inspect Git repositories.
- **Durable jobs** with idempotency keys, progress/status, stored output and retry-safe lookup.
- **Worker isolation**, asynchronous process management and local task state.
- **Dynamic actions** discovered from `actions/` manifests, plus a local terminal monitor.
- **OpenAI Tunnel bridge** that keeps the MCP server on the local loopback interface.

## Architecture

```text
ChatGPT (MCP client)
        │
        ▼
OpenAI Tunnel / cloudflared
        │ local loopback
        ▼
MCP server (server.py) ──► Durable task queue/state
        │                            │
        │                      Worker service
        │                            │
        └──────────────► Files / Shell / Git / Dynamic actions
                                     │
                              Restricted REMOTE_ROOT
```

## Requirements

- Linux or WSL2, Python **3.12**, and a working `systemd --user` service manager.
- `tunnel-client` and `cloudflared` executables obtained through their applicable official distributions. **Executables and credentials are intentionally not committed to this repository.**
- OpenAI Tunnel credentials and a configured tunnel ID. The tunnel is separate from the OpenAI model API; `OPENAI_API_KEY` is not required by the runtime.

Python dependencies are pinned in `requirements.txt`.

## Quick start

```bash
git clone git@github.com:qscottw/chatgpt-enhanced.git
cd chatgpt-enhanced
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with values for `CONTROL_PLANE_API_KEY`, `TUNNEL_ID`, `REMOTE_ROOT`, `REMOTE_STATE_DIR`, and the executable paths (`TUNNEL_CLIENT_BIN` and `CLOUDFLARED_BIN`) if not on `PATH`. `REMOTE_ROOT` must point to the workspace you intend to expose; use the **least-privileged narrowest directory** you can. Do not publish `.env`, private keys, state, traces, or logs.

Check configuration without starting the services:

```bash
python -m runtime.tunnel --check
```

Install and start the *chatgpt-enhanced* user services:

```bash
python scripts/services.py install
python scripts/services.py status
python monitor.py
```

To enable service persistence after logout, use `python scripts/services.py install --enable-linger`. Other service commands: `start`, `stop`, `restart`, `logs`.

## Development and tests

```bash
./scripts/verify.sh
```

For local-only development without credentials, `python -m runtime.startup --local` performs a limited startup check. This does not configure the remote tunnel.

## Security notes

- MCP listens on loopback and access to files is constrained to `REMOTE_ROOT`. This is **not** a hardened OS sandbox: shell commands and custom actions can have the permissions of the Linux user.
- Use a separate, limited-permission OS user and workspace, protect OpenAI Tunnel credentials, and review any custom action manifests before enabling them.
- Back up any files before using write/patch/command features on important projects.
- This repository is source-only. Bundled tunnel clients, local convenience tools, runtime history, credentials, environment files and executable binaries are intentionally excluded.

## License and attribution

MIT License; see [LICENSE.txt](LICENSE.txt).

Based on the MIT-licensed **better-gptchat** project by **Zhicheng Huang**. The upstream MIT copyright notice is retained; this repository renames and packages the runtime as *ChatGPT Enhanced* for personal use and further development. This is a community project and is not affiliated with or endorsed by OpenAI.
