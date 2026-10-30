"""Owned, harmless child processes: output capture and nested timeout cleanup."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from fist_kb.process import run_captured


def running(pid):
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: no process has this PID.
                return False
            raise ctypes.WinError(error)  # Access denied is not proof of termination.
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed orphan can briefly remain as a non-running zombie on Linux.
    stat = Path(f"/proc/{pid}/stat")
    try:
        if stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z "):
            return False
    except FileNotFoundError:
        return False
    return True


class ProcessTests(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get("FIST_KB_TEST_TMP", str(Path(__file__).resolve().parent))
        fixture = tempfile.TemporaryDirectory(prefix="fist-process-test-", dir=parent)
        self.root = Path(fixture.name)

        def cleanup():
            fixture.cleanup()
            self.assertFalse(self.root.exists(), "Process fixture remained")

        self.addCleanup(cleanup)
        self.addCleanup(self.stop_remaining_fixture_processes)
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def stop_remaining_fixture_processes(self):
        # Only exact PIDs written by this test's own children are considered.
        for name in ("parent.json", "grandchild.json"):
            path = self.root / name
            if not path.exists():
                continue
            pid = json.loads(path.read_text())["pid"]
            if not running(pid):
                continue
            if os.name == "nt":
                executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
                subprocess.run([str(executable), "/PID", str(pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            else:
                os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 2
            while running(pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(running(pid), f"Owned fixture process {pid} remained")

    def test_normal_capture_is_utf8_and_stdin_is_closed(self):
        command = [sys.executable, "-X", "utf8", "-B", "-c",
                   'import sys; print("č字", sys.stdin.read() == ""); print("stderr č", file=sys.stderr)']
        result = run_captured(command, env=self.env, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "č字 True")
        self.assertEqual(result.stderr.strip(), "stderr č")

    def test_input_is_sent_as_utf8_and_then_closed(self):
        text = '{"query": "č字"}' + "x" * 100000
        command = [sys.executable, "-X", "utf8", "-B", "-c",
                   'import sys; data = sys.stdin.read(); print(len(data), data[:15], sys.stdin.read() == "")']
        result = run_captured(command, env=self.env, timeout=10, input=text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'{len(text)} {text[:15]} True')

    def test_nonzero_exit_preserves_output_and_return_code(self):
        result = run_captured([sys.executable, "-X", "utf8", "-B", "-c",
                               'import sys; print("reported failure"); sys.exit(7)'], env=self.env, timeout=5)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), "reported failure")

    def test_timeout_stops_child_and_grandchild_without_removing_write_lock(self):
        grandchild = self.root / "grandchild.py"
        grandchild.write_text('''import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / "grandchild.json").write_text(json.dumps({"pid": os.getpid()}))
print("grandchild ready", file=sys.stderr, flush=True)
time.sleep(10)
(root / "late-marker.txt").write_text("This must not be written after the timeout")
''', encoding="utf-8")
        parent = self.root / "parent.py"
        parent.write_text('''import json, os, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
(root / "parent.json").write_text(json.dumps({"pid": os.getpid()}))
child = subprocess.Popen([sys.executable, "-X", "utf8", "-B", str(root / "grandchild.py"), str(root)])
print("parent ready", flush=True)
child.wait()
''', encoding="utf-8")
        lock = self.root / ".write.lock"
        lock.write_bytes(b"Synthetic retained write-outcome marker")
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            run_captured([sys.executable, "-X", "utf8", "-B", str(parent), str(self.root)],
                         env=self.env, timeout=1.5)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 9.5, "Timeout cleanup was not bounded")
        metadata = json.loads((self.root / "grandchild.json").read_text())
        parent_pid = json.loads((self.root / "parent.json").read_text())["pid"]
        self.assertFalse(running(parent_pid))
        self.assertFalse(running(metadata["pid"]), "Grandchild survived the timeout")
        self.assertTrue(caught.exception.termination["tree_termination_reported"], caught.exception.termination)
        self.assertTrue(caught.exception.termination["process_exited"])
        self.assertTrue(caught.exception.termination["output_drained"])
        self.assertIn("parent ready", caught.exception.stdout)
        self.assertIn("grandchild ready", caught.exception.stderr)
        self.assertFalse((self.root / "late-marker.txt").exists())
        self.assertEqual(lock.read_bytes(), b"Synthetic retained write-outcome marker")


if __name__ == "__main__":
    unittest.main(verbosity=2)
