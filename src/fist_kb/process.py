"""Bounded capture of one owned command and timeout termination of its children.

This does not retry a command, remove write locks, or establish write rollback.
TimeoutExpired.termination reports what termination and output collection achieved.
Windows tree termination depends on the root process still being discoverable by
taskkill; escaped or already orphaned descendants are not claimed as terminated.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import signal
import subprocess
from typing import Any


TERMINATION_TIMEOUT = 5.0
DRAIN_TIMEOUT = 2.0


def _terminate_tree(process: subprocess.Popen) -> dict[str, Any]:
    info: dict[str, Any] = {"pid": process.pid, "tree_termination_reported": False, "errors": []}
    if os.name == "nt":
        info["method"] = "taskkill_owned_pid_tree"
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            killer = subprocess.Popen([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                info["termination_exit_code"] = killer.wait(timeout=TERMINATION_TIMEOUT)
                info["tree_termination_reported"] = info["termination_exit_code"] == 0
            except subprocess.TimeoutExpired:
                killer.kill()
                try:
                    killer.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    info["errors"].append("The termination helper did not exit after being killed")
                info["errors"].append("The termination helper timed out")
        except OSError as error:
            info["errors"].append(str(error))
    else:
        info["method"] = "kill_owned_process_group"
        try:
            os.killpg(process.pid, signal.SIGKILL)
            info["tree_termination_reported"] = True
        except ProcessLookupError:
            info["errors"].append("The owned process group was no longer present")
        except OSError as error:
            info["errors"].append(str(error))
    # Reap/stop the direct child even when a whole-tree operation was unavailable.
    if process.poll() is None:
        try:
            process.kill()
        except OSError as error:
            info["errors"].append(str(error))
    return info


def _text(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def _close_finished_streams(process: subprocess.Popen) -> None:
    # Closing a Windows buffered stream while its reader thread is blocked can
    # itself block. A failed tree kill must not turn cleanup into an endless wait.
    for name in ("stdout", "stderr"):
        reader = getattr(process, name + "_thread", None)
        stream = getattr(process, name, None)
        if stream is not None and (reader is None or not reader.is_alive()):
            stream.close()


def run_captured(command: list[str], *, env: dict[str, str] | None,
                 timeout: float) -> subprocess.CompletedProcess[str]:
    """Capture UTF-8 output with closed input; terminate the owned tree on timeout.

The command timeout is followed by bounded termination/output-drain attempts.
Failure diagnostics are attached to TimeoutExpired as the JSON-compatible
``termination`` attribute. They do not prove anything about committed writes.
"""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    options: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                               if os.name == "nt" else {"start_new_session": True})
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, encoding="utf-8", env=env, **options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as original:
        termination = _terminate_tree(process)
        stdout, stderr = _text(original.output), _text(original.stderr)
        try:
            stdout, stderr = process.communicate(timeout=DRAIN_TIMEOUT)
            termination["output_drained"] = True
        except subprocess.TimeoutExpired as drain:
            stdout = _text(drain.output) or stdout
            stderr = _text(drain.stderr) or stderr
            termination["output_drained"] = False
            termination["errors"].append("Output pipes did not close within the bounded drain interval")
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            termination["errors"].append("The direct child did not exit within the cleanup interval")
        termination["process_exited"] = process.poll() is not None
        _close_finished_streams(process)
        expired = subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
        expired.termination = termination
        raise expired from None
    _close_finished_streams(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
