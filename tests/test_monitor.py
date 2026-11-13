from __future__ import annotations

import curses
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest.mock import patch

import monitor


class FakeScreen:
    def __init__(self, height: int = 24, width: int = 100) -> None:
        self.height, self.width = height, width
        self.writes: list[tuple[int, int, str]] = []

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.writes.clear()

    def addstr(self, y, x, text, attr=0):
        assert 0 <= y < self.height and 0 <= x < self.width
        assert all(unicodedata.category(c) not in {'Cc', 'Cf', 'Cs'} for c in text), repr(text)
        assert x + monitor.display_width(text) < self.width, (x, text, self.width)
        self.writes.append((y, x, text))

    def addch(self, y, x, char):
        assert 0 <= y < self.height and 0 <= x < self.width

    def refresh(self):
        pass

    def clearok(self, flag):
        pass


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='remote-monitor-test-')
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(curses, 'ACS_VLINE', ord('|'), create=True)
        p.start()
        self.addCleanup(p.stop)

    def make_chat(self, name='chat-a', command='hello', output=b''):
        directory = self.root / 'chats' / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'meta.json').write_text(json.dumps({'chat_id': name, 'title': name, 'chat_source': 'openai/session'}))
        log = directory / 'output.log'
        log.write_bytes(output)
        event = {'type': 'command_end', 'time': '2026-09-21T15:00:00+0800', 'turn_id': 'turn',
                 'command': command, 'ok': True, 'exit_code': 0, 'stdout_path': str(log)}
        (directory / 'events.jsonl').write_text(json.dumps(event, ensure_ascii=False) + '\n')
        return {'chat_id': name, 'title': name, 'path': str(directory)}, log

    def test_controls_are_inert_and_nul_is_visible(self):
        text = monitor.display_text('a\x00b\tc\r\nd\re\b\x07')
        self.assertIn(r'a\x00b', text)
        self.assertIn('\nd\ne', text)
        self.assertIn(r'\x08\x07', text)
        self.assertNotIn('\x00', text)
        self.assertNotIn('\t', text)
        self.assertNotIn('\r', text)

    def test_ansi_osc_and_dcs_removed(self):
        text = '\x1b[31mred\x1b[0m\x1b]0;title\x07\x1bPpayload\x1b\\!'
        self.assertEqual(monitor.display_text(text), 'red!')
        self.assertEqual(monitor.display_text('\x1b]8;;https://example.invalid\x1b\\link\x1b]8;;\x1b\\'), 'link')
        self.assertNotIn('\x1b', monitor.display_text('incomplete\x1b['))

    def test_single_line_contains_no_layout_controls(self):
        text = monitor.display_text('one\ntwo\rthree\tfour\x00', single_line=True)
        self.assertIn('↵', text)
        self.assertFalse(any(unicodedata.category(c) == 'Cc' for c in text))

    def test_terminal_width_matches_cjk_and_combining(self):
        self.assertEqual(monitor.display_width('ab中文'), 6)
        self.assertEqual(monitor.display_width('e\u0301'), 1)

    def test_clip_obeys_cell_budget(self):
        for text in ['汉'*80, 'a中文e\u0301🙂'*20, '\u0301汉\t\0\nend']:
            for width in range(0, 81):
                with self.subTest(width=width):
                    result = monitor.clip(text, width)
                    self.assertLessEqual(monitor.display_width(result), width)
                    self.assertNotIn('\0', result)
                    self.assertNotIn('\n', result)

    def test_wrap_does_not_truncate_long_content(self):
        text = 'START-' + 'ab中文e\u0301'*180 + '-COMPLETE-TAIL'
        for width in [7, 49, 88, 200]:
            rows = monitor.wrap_text(text, width, continuation='')
            self.assertGreater(len(rows), 1)
            self.assertEqual(''.join(rows), text)
            self.assertTrue(all(monitor.display_width(r) <= width for r in rows))
            self.assertNotIn('…', ''.join(rows))

    def test_wrap_preserves_newlines_and_indentation(self):
        self.assertEqual(monitor.wrap_text('first\n  second\n', 20), ['first', '  second', ''])
        rows = monitor.wrap_text('abcdefghij', 6)
        self.assertEqual(rows, ['abcdef', '  ghij'])

    def test_combining_marks_stay_with_base(self):
        rows = monitor.wrap_text('ae\u0301b', 2, continuation='')
        self.assertEqual(rows, ['ae\u0301', 'b'])
        self.assertEqual(monitor.wrap_text('\u0301a', 2), [' \u0301a'])

    def test_long_command_summary_not_pretruncated(self):
        text = 'X'*500 + '-END'
        event = {'tool': 'run_command', 'params': {'arguments': {'command': text}}}
        self.assertEqual(monitor.tool_argument_summary(event), text)

    def test_payload_reference_is_readable(self):
        value = {'$payload': {'chars': 5000, 'payload_path': '/tmp/payload.txt'}}
        event = {'tool': 'run_command', 'params': {'arguments': {'command': value}}}
        self.assertEqual(monitor.tool_argument_summary(event), 'payload[5000 chars] /tmp/payload.txt')

    def test_safe_add_handles_nul_and_controls(self):
        screen = FakeScreen()
        m = monitor.Monitor(screen, self.root)
        m._safe_add(3, 29, 'before\0after\t汉\rnext\x1b[31m', 0, 49)
        self.assertEqual(len(screen.writes), 1)
        self.assertIn(r'\x00', screen.writes[0][2])
        self.assertLessEqual(monitor.display_width(screen.writes[0][2]), 49)

    def test_small_terminal_and_offscreen_coordinates(self):
        for height, width in [(1, 1), (2, 10), (8, 40)]:
            m = monitor.Monitor(FakeScreen(height, width), self.root)
            m.draw()
            m._safe_add(99, 99, 'bad\0', 0, 20)

    def test_draw_preserves_original_log_bytes(self):
        raw = b'before\0after\n\x1b[31mred\x1b[0m\n' + ('汉'*200).encode()
        _, log = self.make_chat(command='中文'*200, output=raw)
        m = monitor.Monitor(FakeScreen(), self.root)
        m.draw()
        self.assertEqual(log.read_bytes(), raw)
        self.assertGreater(m._timeline_total, 1)

    def test_pinned_chat_survives_sort_changes(self):
        a, _ = self.make_chat('chat-a')
        b, _ = self.make_chat('chat-b')
        c, _ = self.make_chat('chat-c')
        m = monitor.Monitor(FakeScreen(), self.root)
        with patch.object(monitor, 'list_chats', return_value=[a, b, c]):
            m.draw()
            m._move_chat(1)
            m.draw()
        with patch.object(monitor, 'list_chats', return_value=[c, a, b]):
            m.draw()
        self.assertEqual(m.selected_chat_id, 'chat-b')
        self.assertEqual(m.selected, 2)

    def test_sidebar_scrolls_to_selected_chat(self):
        chats = [self.make_chat(f'chat-{i:02d}')[0] for i in range(35)]
        screen = FakeScreen(12, 90)
        m = monitor.Monitor(screen, self.root)
        with patch.object(monitor, 'list_chats', return_value=chats):
            m.draw()
            m._move_chat(30)
            m.draw()
        self.assertGreater(m.chat_top, 0)
        self.assertTrue(any(x == 0 and '> chat-30' in s for _, x, s in screen.writes))

    def test_output_can_scroll_to_start_and_end(self):
        self.make_chat(output=('OUTSTART\n' + '\n'.join(str(i) for i in range(80)) + '\nOUTEND').encode())
        screen = FakeScreen()
        m = monitor.Monitor(screen, self.root)
        m.draw()
        self.assertTrue(any('OUTEND' in s for _, _, s in screen.writes))
        m.handle_key(9)
        m.handle_key(curses.KEY_HOME)
        m.draw()
        self.assertTrue(any('OUTSTART' in s for _, _, s in screen.writes))
        m.handle_key(curses.KEY_END)
        m.draw()
        self.assertTrue(any('OUTEND' in s for _, _, s in screen.writes))

    def test_timeline_scrolls_wrapped_rows(self):
        self.make_chat(command='COMMAND-START-' + '中a'*2000 + '-COMMAND-END')
        screen = FakeScreen()
        m = monitor.Monitor(screen, self.root)
        m.draw()
        m.handle_key(curses.KEY_HOME)
        m.draw()
        self.assertTrue(any('COMMAND-START-' in s for _, _, s in screen.writes))
        m.handle_key(curses.KEY_END)
        m.draw()
        self.assertTrue(any('COMMAND-END' in s for _, _, s in screen.writes))

    def test_mouse_wheel_routes_by_pane(self):
        if not getattr(curses, 'BUTTON5_PRESSED', 0):
            self.skipTest('ncurses does not expose wheel-down events')
        a, _ = self.make_chat('chat-a', command='x'*1000, output=b'o'*3000)
        b, _ = self.make_chat('chat-b', command='x'*1000, output=b'o'*3000)
        m = monitor.Monitor(FakeScreen(), self.root)
        with patch.object(monitor, 'list_chats', return_value=[a, b]):
            m.draw()
            with patch.object(curses, 'getmouse', return_value=(0, 5, 5, 0, curses.BUTTON5_PRESSED)):
                m.handle_key(curses.KEY_MOUSE)
            self.assertEqual(m.selected_chat_id, 'chat-b')
            m.draw()
            with patch.object(curses, 'getmouse', return_value=(0, 70, 5, 0, curses.BUTTON4_PRESSED)):
                m.handle_key(curses.KEY_MOUSE)
            self.assertEqual(m.scroll, 3)
            with patch.object(curses, 'getmouse', return_value=(0, 70, 22, 0, curses.BUTTON4_PRESSED)):
                m.handle_key(curses.KEY_MOUSE)
            self.assertEqual(m.output_scroll, 3)

    def test_unchanged_frames_use_cache_and_log_append_invalidates_it(self):
        chat, log = self.make_chat(command='x'*1000, output=b'first')
        m = monitor.Monitor(FakeScreen(), self.root)
        with patch.object(monitor, 'tail_events', wraps=monitor.tail_events) as events_read, \
             patch.object(monitor, 'tail_text', wraps=monitor.tail_text) as output_read:
            m.draw()
            m.draw()
            self.assertEqual(events_read.call_count, 1)
            self.assertEqual(output_read.call_count, 1)
            log.write_bytes(b'first second')
            m.draw()
            self.assertEqual(events_read.call_count, 1)
            self.assertEqual(output_read.call_count, 2)
            event_path = Path(chat['path']) / 'events.jsonl'
            with event_path.open('a') as handle:
                handle.write(json.dumps({'type': 'progress', 'message': 'new'}) + '\n')
            m.draw()
            self.assertEqual(events_read.call_count, 2)

    @unittest.skipUnless(os.name == 'posix', 'requires a POSIX PTY and ncurses')
    def test_real_ncurses_pty_nul_wide_text_wrapping_and_resize(self):
        import fcntl
        import pty
        import struct
        import termios

        self.make_chat(command='START-' + '中\t文'*200 + '-END', output=('before\0after\n' + '汉'*400).encode())
        result_path = self.root / 'pty-result.json'
        code = r'''
import curses,json,sys
from pathlib import Path
import monitor
results={}
def probe(screen):
    screen.erase()
    screen.addstr(4,0,'LEFT-ROW-4')
    m=monitor.Monitor(screen,Path(sys.argv[1]))
    m._safe_add(3,29,'汉'*40,0,49)
    results['left_untouched']=screen.instr(4,0,10)==b'LEFT-ROW-4'
    m._safe_add(8,29,'before\x00after'.replace('\\x00','\x00'),0,49)
    results['nul_visible']=b'\\x00' in screen.instr(8,29,45)
    sizes=[(10,70),(24,80),(40,160),(70,320),(3,12),(24,100)]
    for height,width in sizes:
        curses.resizeterm(height,width)
        m.draw()
        if width>=70 and height>=10:
            for row in range(3,height-1):
                assert not screen.instr(row,0,m._left_w-1).strip(), ('sidebar overwritten',height,width,row)
        m.handle_key(curses.KEY_PPAGE)
        m.draw()
        m.handle_key(9)
        m.handle_key(curses.KEY_HOME)
        m.draw()
    results['resize_cases']=len(sizes)
curses.wrapper(probe)
Path(sys.argv[2]).write_text(json.dumps(results))
'''
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 20, 80, 0, 0))
        env = dict(os.environ, TERM='xterm-256color', LC_ALL='C.UTF-8', PYTHONDONTWRITEBYTECODE='1')
        proc = None
        captured = bytearray()
        try:
            proc = subprocess.Popen([sys.executable, '-c', code, str(self.root), str(result_path)],
                                    cwd=str(Path(monitor.__file__).parent), stdin=slave, stdout=slave, stderr=slave, env=env)
            os.close(slave)
            slave = -1
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    captured.extend(data)
                elif proc.poll() is not None:
                    break
            proc.wait(timeout=2)
            self.assertEqual(proc.returncode, 0, repr(bytes(captured)[-3000:]))
            results = json.loads(result_path.read_text())
            self.assertTrue(results['left_untouched'])
            self.assertTrue(results['nul_visible'])
            self.assertEqual(results['resize_cases'], 6)
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=2)
            if slave >= 0:
                os.close(slave)
            os.close(master)


if __name__ == '__main__':
    unittest.main()
