from __future__ import annotations

import argparse
import curses
import ctypes
import json
import os
import re
import time
import unicodedata
from collections import deque
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_STATE = Path(os.getenv("REMOTE_STATE_DIR", str(Path.home() / ".local/state/chatgpt-enhanced"))).expanduser()


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def list_chats(chat_root: Path) -> list[dict[str, Any]]:
    if not chat_root.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in chat_root.iterdir():
        if not path.is_dir():
            continue
        meta = load_json(path / "meta.json")
        meta.setdefault("chat_id", path.name)
        meta["path"] = str(path)
        try:
            meta["_mtime"] = (path / "meta.json").stat().st_mtime
        except OSError:
            meta["_mtime"] = path.stat().st_mtime
        items.append(meta)
    # Real OpenAI chats stay above protocol/legacy groups even immediately after
    # a service restart causes fresh initialize/discovery traffic.
    items.sort(
        key=lambda x: (1 if x.get("chat_source") == "openai/session" else 0, x.get("_mtime", 0)),
        reverse=True,
    )
    return items


def tail_events(chat_dir: Path, limit: int = 500) -> list[dict[str, Any]]:
    path = chat_dir / "events.jsonl"
    if not path.exists():
        return []
    lines: deque[str] = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                lines.append(line)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def tail_text(path: Path, max_bytes: int = 16000) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


# Measure cells using the same locale/libc rules as ncurses, not Python len().
# No third-party dependency is required by the standalone monitor launcher.
try:
    _wcwidth = ctypes.CDLL(None).wcwidth
    _wcwidth.argtypes = [ctypes.c_wchar]
    _wcwidth.restype = ctypes.c_int
except (AttributeError, OSError):
    _wcwidth = None


@lru_cache(maxsize=4096)
def cell_width(char: str) -> int:
    if _wcwidth is not None:
        width = _wcwidth(char)
        if width >= 0:
            return width
    if unicodedata.category(char) in {"Mn", "Me"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(cell_width(char) for char in text)


# CSI (including colors/cursor movement), OSC, and DCS/SOS/PM/APC strings.
# Unknown/incomplete escapes are rendered visibly by display_text instead.
_ANSI_RE = re.compile(
    r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
    r"|(?:\x1b[P^_X])[\s\S]*?\x1b\\"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[@-Z\\-_]"
)


def display_text(value: Any, *, single_line: bool = False) -> str:
    """Turn arbitrary log text into inert display text; never change the log file."""
    text = _ANSI_RE.sub("", str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    parts: list[str] = []
    for char in text:
        if char == "\n":
            parts.append(" ↵ " if single_line else "\n")
        elif unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            code = ord(char)
            parts.append(f"\\x{code:02x}" if code < 256 else f"\\u{code:04x}")
        else:
            parts.append(char)
    return "".join(parts)


def _clusters(text: str) -> Iterator[str]:
    """Keep zero-width combining marks attached to their preceding character."""
    pending = ""
    for char in text:
        if cell_width(char) == 0:
            pending = (pending or " ") + char
        else:
            if pending:
                yield pending
            pending = char
    if pending:
        yield pending


def _take_cells(text: str, width: int) -> str:
    parts: list[str] = []
    used = 0
    for cluster in _clusters(text):
        size = display_width(cluster)
        if used + size > width:
            break
        parts.append(cluster)
        used += size
    return "".join(parts)


def clip(text: str, width: int) -> str:
    """Clip a single-line label by terminal cells, not code points or bytes."""
    if width <= 0:
        return ""
    text = display_text(text, single_line=True)
    # _clusters supplies a base cell for any leading combining mark.
    text = "".join(_clusters(text))
    if display_width(text) <= width:
        return text
    return _take_cells(text, width - 1) + "…"


def wrap_text(text: str, width: int, continuation: str = "  ") -> list[str]:
    """Wrap complete text inside a panel, preserving explicit line breaks."""
    if width <= 0:
        return []
    indent = _take_cells(continuation, max(0, width - 2))
    indent_width = display_width(indent)
    rows: list[str] = []
    for logical_line in display_text(text).split("\n"):
        parts: list[str] = []
        used = 0
        for cluster in _clusters(logical_line):
            size = display_width(cluster)
            if used + size > width and parts:
                rows.append("".join(parts))
                parts = [indent]
                used = indent_width
            if size > width - used:
                # A double-width glyph cannot fit in a one-cell viewport.
                cluster, size = "?", 1
            parts.append(cluster)
            used += size
        rows.append("".join(parts))
    return rows


def short_id(value: Any, n: int = 8) -> str:
    text = str(value or "")
    if not text:
        return "-"
    return text[:n]


def _payload_summary(value: Any) -> str:
    if isinstance(value, dict) and "$payload" in value:
        info = value["$payload"] or {}
        return f"payload[{info.get('chars', '?')} chars] {info.get('payload_path', '')}"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def tool_argument_summary(event: dict[str, Any]) -> str:
    params = event.get("params") or {}
    args = params.get("arguments") if isinstance(params, dict) else None
    if not isinstance(args, dict):
        return ""
    tool = str(event.get("tool") or params.get("name") or "")
    if tool in {"run_command", "process_start"}:
        return _payload_summary(args.get("command", ""))
    if tool in {"read_file", "read_file_range", "list_directory", "write_file"}:
        path = args.get("path", "")
        if tool == "write_file" and "content" in args:
            return f"{path}  {_payload_summary(args['content'])}"
        return str(path)
    if tool in {"replace_text", "insert_text"}:
        return f"{args.get('path', '')}"
    if tool == "apply_patch":
        return _payload_summary(args.get("patch", ""))
    if tool in {"executor_run", "executor_describe"}:
        action = args.get("action", "")
        return f"{action} {_payload_summary(args.get('arguments_json', ''))}".strip()
    if not args:
        return ""
    return _payload_summary(args)


def event_line(event: dict[str, Any]) -> str:
    ts = str(event.get("time", ""))
    if "T" in ts:
        date_part, time_part = ts.split("T", 1)
        ts = f"{date_part} {time_part[:8]}"
    typ = str(event.get("type", "?"))
    turn = short_id(event.get("turn_id"), 8)
    tool = str(event.get("tool") or "")

    if typ == "command_start":
        return f"{ts} {turn} CLI ▶   {event.get('command', '')}"
    if typ == "command_end":
        ok = bool(event.get("ok"))
        mark = "✓" if ok else "✗"
        return f"{ts} {turn} CLI {mark}   exit={event.get('exit_code')} {event.get('duration_ms', '?')}ms  {event.get('command', '')}"
    if typ == "process_start":
        return f"{ts} {turn} PROC ▶  pid={event.get('pid')} {event.get('command', '')}"
    if typ == "process_exit":
        return f"{ts} {turn} PROC ■  exit={event.get('exit_code')} {event.get('command', '')}"
    if typ == "executor_start":
        return f"{ts} {turn} EXEC ▶  {event.get('action', '')} {_payload_summary(event.get('arguments', {}))}"
    if typ == "executor_end":
        mark = "✓" if event.get("ok") else "✗"
        return f"{ts} {turn} EXEC {mark}  {event.get('action', '')} exit={event.get('exit_code')} {event.get('duration_ms', '?')}ms"
    if typ == "progress":
        phase = f"[{event.get('phase')}] " if event.get("phase") else ""
        return f"{ts} {turn} GPT …   {phase}{event.get('message') or event.get('summary') or ''}"
    if typ in ("task_queued", "task_started", "task_finished"):
        return f"{ts} TASK {event.get('status', '?')} {event.get('task_id', '')}"
    if typ == "mcp_start":
        if tool:
            detail = tool_argument_summary(event)
            return f"{ts} {turn} CALL    {tool}{('  ' + detail) if detail else ''}"
        return f"{ts} {turn} MCP ▶   {event.get('method', '')}"
    if typ == "mcp_end":
        mark = "…" if event.get("ok") is None else ("✓" if event.get("ok") else "✗")
        return f"{ts} {turn} DONE {mark}  {tool or event.get('method', '')} {event.get('duration_ms', '?')}ms"

    summary = str(event.get("summary") or event.get("message") or event.get("command") or "")
    return f"{ts} {turn} {typ:8} {summary}"


def latest_output_paths(events: list[dict[str, Any]]) -> list[Path]:
    for event in reversed(events):
        paths = [Path(str(event[k])) for k in ("stdout_path", "log_path", "stderr_path") if event.get(k)]
        existing = list(dict.fromkeys(p for p in paths if p.exists()))
        if existing:
            return existing
    return []


def latest_output_path(events: list[dict[str, Any]]) -> Path | None:
    paths = latest_output_paths(events)
    return next((p for p in paths if p.stat().st_size), paths[0] if paths else None)


def chat_display_name(chat: dict[str, Any]) -> str:
    if chat.get("title"):
        return str(chat["title"])
    chat_id = str(chat.get("chat_id") or "unknown")
    if chat_id.startswith("chat-"):
        return f"OpenAI {chat_id[5:13]}"
    if chat_id.startswith("turn-call-"):
        return "Legacy trace"
    return chat_id


class Monitor:
    def __init__(self, stdscr: Any, state_dir: Path) -> None:
        self.stdscr = stdscr
        self.state_dir = state_dir
        self.chat_root = state_dir / "chats"
        self.selected = 0
        self.selected_chat_id: str | None = None
        self.follow_latest = True
        self.scroll = 0
        self.output_scroll = 0
        self.focus = "timeline"
        self.chat_top = 0
        self._chats: list[dict[str, Any]] = []
        self._left_w = 28
        self._output_top: int | None = None
        self._timeline_rows = 1
        self._output_rows = 1
        self._timeline_total = 0
        self._output_total = 0
        self._events_key: tuple[Any, ...] | None = None
        self._cached_events: list[dict[str, Any]] = []
        self._cached_timeline: list[str] = []
        self._output_key: tuple[Any, ...] | None = None
        self._cached_output: list[str] = []
        self._last_output_path: Path | None = None

    @staticmethod
    def _file_key(path: Path) -> tuple[Any, ...]:
        try:
            stat = path.stat()
            return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            return (str(path), None)

    def _timeline_view(self, chat_dir: Path, width: int) -> tuple[list[dict[str, Any]], list[str]]:
        key = (*self._file_key(chat_dir / "events.jsonl"), width)
        if key != self._events_key:
            self._cached_events = tail_events(chat_dir, 700)
            self._cached_timeline = [row for event in self._cached_events for row in wrap_text(event_line(event), width)]
            self._events_key = key
        return self._cached_events, self._cached_timeline

    def _output_view(self, path: Path | None, width: int, paths: list[Path] | None = None) -> list[str]:
        if path != self._last_output_path:
            self.output_scroll = self._output_total = 0
            self._last_output_path = path
        paths = paths if paths is not None else ([path] if path else [])
        key = (tuple(self._file_key(p) for p in paths), width)
        if key != self._output_key:
            text = "\n".join((f"[{p.name}]\n" if len(paths) > 1 else "") + tail_text(p) for p in paths)
            self._cached_output = wrap_text(text, width) if text else []
            self._output_key = key
        return self._cached_output

    def _reset_scroll(self) -> None:
        self.scroll = self.output_scroll = 0
        self._timeline_total = self._output_total = 0

    def _move_chat(self, delta: int) -> None:
        if not self._chats:
            return
        self.follow_latest = False
        self.selected = min(max(0, self.selected + delta), len(self._chats) - 1)
        self.selected_chat_id = str(self._chats[self.selected]["chat_id"])
        self._reset_scroll()

    def _scroll(self, amount: int, pane: str | None = None) -> None:
        if (pane or self.focus) == "output":
            self.output_scroll = max(0, self.output_scroll + amount)
        else:
            self.scroll = max(0, self.scroll + amount)

    def _handle_mouse(self) -> None:
        try:
            _, x, y, _, state = curses.getmouse()
        except curses.error:
            return
        up = bool(state & getattr(curses, "BUTTON4_PRESSED", 0))
        down = bool(state & getattr(curses, "BUTTON5_PRESSED", 0))
        if x < self._left_w:
            if up or down:
                self._move_chat(-1 if up else 1)
            elif state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                index = self.chat_top + y - 2
                if y >= 2 and 0 <= index < len(self._chats):
                    self._move_chat(index - self.selected)
        elif up or down:
            pane = "output" if self._output_top is not None and y >= self._output_top else "timeline"
            self.focus = pane
            self._scroll(3 if up else -3, pane)

    def handle_key(self, key: int) -> bool:
        """Return False on quit. Scroll offsets count wrapped screen rows."""
        if key in (ord("q"), ord("Q")):
            return False
        if key == curses.KEY_UP:
            self._move_chat(-1)
        elif key == curses.KEY_DOWN:
            self._move_chat(1)
        elif key in (ord("f"), ord("F")):
            self.follow_latest = True
            self.selected = 0
            self._reset_scroll()
        elif key == 9:
            self.focus = "output" if self.focus == "timeline" else "timeline"
        elif key == curses.KEY_PPAGE:
            self._scroll(max(1, (self._output_rows if self.focus == "output" else self._timeline_rows) - 1))
        elif key == curses.KEY_NPAGE:
            self._scroll(-max(1, (self._output_rows if self.focus == "output" else self._timeline_rows) - 1))
        elif key == curses.KEY_HOME:
            if self.focus == "output":
                self.output_scroll = max(0, self._output_total - self._output_rows)
            else:
                self.scroll = max(0, self._timeline_total - self._timeline_rows)
        elif key == curses.KEY_END:
            if self.focus == "output":
                self.output_scroll = 0
            else:
                self.scroll = 0
        elif key == curses.KEY_MOUSE:
            self._handle_mouse()
        elif key in (ord("r"), ord("R"), curses.KEY_RESIZE):
            self.stdscr.clearok(True)
        return True

    def run(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.stdscr.keypad(True)
        self.stdscr.timeout(250)
        try:
            curses.use_default_colors()
            mask = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED
            mask |= getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON5_PRESSED", 0)
            curses.mousemask(mask)
            curses.mouseinterval(0)
        except curses.error:
            pass
        while True:
            self.draw()
            if not self.handle_key(self.stdscr.getch()):
                return

    def draw(self) -> None:
        self._chats = chats = list_chats(self.chat_root)
        previous = self.selected_chat_id
        if self.follow_latest:
            self.selected = 0
        elif self.selected_chat_id is not None:
            self.selected = next((i for i, c in enumerate(chats) if c["chat_id"] == self.selected_chat_id), self.selected)
        self.selected = min(max(0, self.selected), max(0, len(chats) - 1))
        self.selected_chat_id = str(chats[self.selected]["chat_id"]) if chats else None
        if self.selected_chat_id != previous:
            self._reset_scroll()

        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        self._output_top = None
        if height < 10 or width < 70:
            self._safe_add(0, 0, "Terminal too small for remote monitor (minimum 70x10)", 0, width - 1)
            self.stdscr.refresh()
            return
        left_w = self._left_w = min(38, max(28, width // 4))
        right_x, right_w = left_w + 1, width - left_w - 2
        self._safe_add(0, 0, f" remote monitor  {time.strftime('%Y-%m-%d %H:%M:%S')}  {'FOLLOW' if self.follow_latest else 'PINNED'} ", curses.A_REVERSE, width - 1)
        self._safe_add(1, 0, f"Chats {self.selected + 1 if chats else 0}/{len(chats)}", curses.A_BOLD, left_w - 1)
        self._safe_add(1, right_x, f"Timeline / live output  [scroll: {self.focus}]", curses.A_BOLD, right_w)

        max_chat_rows = height - 3
        self.chat_top = min(max(0, self.chat_top), max(0, len(chats) - max_chat_rows))
        if self.selected < self.chat_top:
            self.chat_top = self.selected
        elif self.selected >= self.chat_top + max_chat_rows:
            self.chat_top = self.selected - max_chat_rows + 1
        for row, index in enumerate(range(self.chat_top, min(len(chats), self.chat_top + max_chat_rows)), start=2):
            chat = chats[index]
            active = int(chat.get("active_calls", 0) or 0) + len(chat.get("active_tasks", []))
            suffix = f" {'●' if active > 0 else '·'} t{int(chat.get('turn_count', 0) or 0)}"
            prefix = "> " if index == self.selected else "  "
            title = clip(chat_display_name(chat), left_w - display_width(suffix) - 4)
            self._safe_add(row, 0, prefix + title + suffix, curses.A_REVERSE if index == self.selected else 0, left_w - 1)

        for row in range(1, height - 1):
            try:
                self.stdscr.addch(row, left_w - 1, curses.ACS_VLINE)
            except curses.error:
                pass

        if not chats:
            self._safe_add(3, right_x, f"No chat events yet under {self.chat_root}", 0, right_w)
        else:
            chat = chats[self.selected]
            events, timeline = self._timeline_view(Path(chat["path"]), right_w)
            header = (f"{chat_display_name(chat)}  id={chat.get('chat_id')}  "
                      f"turns={chat.get('turn_count', 0)}  status={chat.get('status', 'idle')}")
            self._safe_add(2, right_x, header, curses.A_BOLD, right_w)

            output_path = latest_output_path(events)
            output = self._output_view(output_path, right_w, latest_output_paths(events))
            # Allocate physical rows, not logical log lines, to each pane.
            body_rows = height - 4
            output_budget = min(8, max(1, body_rows // 3), len(output)) if output else 0
            self._output_rows = max(1, output_budget)
            self._timeline_rows = max(1, body_rows - output_budget - (1 if output_budget else 0))
            if self.scroll and len(timeline) > self._timeline_total:
                self.scroll += len(timeline) - self._timeline_total
            if self.output_scroll and len(output) > self._output_total:
                self.output_scroll += len(output) - self._output_total
            self._timeline_total, self._output_total = len(timeline), len(output)
            self.scroll = min(self.scroll, max(0, len(timeline) - self._timeline_rows))
            self.output_scroll = min(self.output_scroll, max(0, len(output) - self._output_rows))
            end = len(timeline) - self.scroll
            for row, text in enumerate(timeline[max(0, end - self._timeline_rows):end], start=3):
                self._safe_add(row, right_x, text, 0, right_w)

            if output_budget:
                self._output_top = 3 + self._timeline_rows
                label = f"LIVE {'[selected]' if self.focus == 'output' else '[Tab to scroll]'}: {output_path}"
                self._safe_add(self._output_top, right_x, label, curses.A_BOLD, right_w)
                end = len(output) - self.output_scroll
                for row, text in enumerate(output[max(0, end - output_budget):end], start=self._output_top + 1):
                    self._safe_add(row, right_x, text, 0, right_w)

        footer = "↑↓ chats | Wheel scroll | PgUp/Dn | Tab pane | Home/End | F follow | Q quit"
        self._safe_add(height - 1, 0, footer, curses.A_REVERSE, width - 1)
        self.stdscr.refresh()

    def _safe_add(self, y: int, x: int, text: str, attr: int, max_width: int) -> None:
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        # Always leave the terminal's rightmost column unused to prevent implicit
        # wraparound into column zero, including at the bottom-right corner.
        budget = min(max_width, width - x - 1)
        if budget <= 0:
            return
        safe = clip(text, budget)
        try:
            self.stdscr.addstr(y, x, safe, attr)
        except (curses.error, ValueError, UnicodeError):
            # Resize/encoding races must not kill a monitor observing a live job.
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive local monitor for remote v2 chat/tool activity")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE))
    args = parser.parse_args()
    curses.wrapper(lambda stdscr: Monitor(stdscr, Path(args.state_dir).expanduser()).run())


if __name__ == "__main__":
    main()
