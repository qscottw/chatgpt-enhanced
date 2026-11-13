from __future__ import annotations

import json
import sys


def main() -> None:
    args = json.load(sys.stdin)
    message = str(args.get("message", ""))
    repeat = max(1, min(int(args.get("repeat", 1)), 1000))
    json.dump(
        {
            "ok": True,
            "message": message,
            "repeat": repeat,
            "items": [message] * repeat,
        },
        sys.stdout,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
