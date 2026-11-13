from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from runtime.commands import run_command
from runtime.core import CommandSafetyError, ROOT, preview_text
from runtime.files import insert_text, read_file_range, replace_text
from runtime.git import git_diff, git_status
from runtime.patches import apply_patch
from runtime.processes import ProcessManager
from runtime.sessions import create_session, get_session, update_session


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="topc-test-", dir=ROOT))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_precise_file_editing(self) -> None:
        p = self.tmp / "a.py"
        p.write_text("one\ntwo\nthree\n", encoding="utf-8")
        result = read_file_range(str(p), 2, 3)
        self.assertIn("2: two", result["content"])
        changed = replace_text(str(p), "two", "TWO")
        self.assertIn("-two", changed["diff"])
        self.assertIn("+TWO", changed["diff"])
        insert_text(str(p), 2, "after-two\n")
        self.assertEqual(p.read_text(), "one\nTWO\nafter-two\nthree\n")

    def test_replace_rejects_ambiguous_match(self) -> None:
        p = self.tmp / "a.txt"
        p.write_text("x x", encoding="utf-8")
        with self.assertRaises(ValueError):
            replace_text(str(p), "x", "y")
        self.assertEqual(p.read_text(), "x x")

    def test_command_and_guardrail(self) -> None:
        result = run_command("printf hello", str(self.tmp))
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "hello")
        with self.assertRaises(CommandSafetyError):
            run_command("sudo true", str(self.tmp))

    def test_persistent_process(self) -> None:
        manager = ProcessManager()
        started = manager.start("read line; echo got:$line", str(self.tmp))
        pid = started["process_id"]
        manager.write(pid, "hello\n")
        deadline = time.time() + 3
        output = ""
        while time.time() < deadline:
            polled = manager.read(pid)
            output += polled["output"]
            if not polled["running"]:
                break
            time.sleep(0.05)
        self.assertIn("got:hello", output)

    def test_git_helpers_and_patch(self) -> None:
        run_command("git init -q && git config user.email test@example.com && git config user.name Test", str(self.tmp))
        p = self.tmp / "a.txt"
        p.write_text("old\n", encoding="utf-8")
        run_command("git add a.txt && git commit -qm init", str(self.tmp))
        patch = """diff --git a/a.txt b/a.txt\nindex 3367afd..3e75765 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"""
        applied = apply_patch(patch, str(self.tmp))
        self.assertTrue(applied["ok"], applied)
        status = git_status(str(self.tmp))
        self.assertFalse(status["clean"])
        diff = git_diff(str(self.tmp))
        self.assertIn("+new", diff["diff"])

    def test_session_metadata(self) -> None:
        session = create_session(str(self.tmp), "test task")
        updated = update_session(session["session_id"], note="did thing")
        loaded = get_session(session["session_id"])
        self.assertEqual(updated["task"], "test task")
        self.assertEqual(loaded["notes"][-1]["text"], "did thing")

    def test_output_preview_keeps_head_and_tail(self) -> None:
        text = "HEAD-" + ("x" * 1000) + "-TAIL"
        preview = preview_text(text, limit=300, label="test")
        self.assertTrue(preview["truncated"])
        self.assertTrue(preview["text"].startswith("HEAD-"))
        self.assertTrue(preview["text"].endswith("-TAIL"))
        self.assertTrue(Path(preview["full_output_path"]).exists())


if __name__ == "__main__":
    unittest.main()
