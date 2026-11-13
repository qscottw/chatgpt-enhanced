"""Describe this runtime without hard-coded private infrastructure references."""
import json
import os
import sys

json.load(sys.stdin)
json.dump({
    'root': os.getenv('REMOTE_ROOT'),
    'state_dir': os.getenv('REMOTE_STATE_DIR'),
    'task_id': os.getenv('REMOTE_TASK_ID'),
    'python': sys.version.split()[0],
    'platform': sys.platform,
}, sys.stdout)
print()
