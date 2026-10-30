"""Create an empty, explicitly bound KB without installing an agent role."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .process import run_captured

# The files the engine's "admin init" writes, core last.
STATE_FILES = ("records.jsonl", "journal.jsonl", "sources.json", "core.json")


def initialize(project_dir: str | Path, *, purpose: str = "", owner: str = "") -> dict[str, Any]:
    """Create only KB data in PROJECT/KnowledgeBase; refuse existing KBs, never migrate or rebind.

    The engine writes the empty state itself, so a new KB has exactly the format the engine validates.
    """
    project = Path(project_dir).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise ValueError("The project must be an existing directory.")
    root = project / "KnowledgeBase"
    root.mkdir()  # Exclusive creation decides ownership before any bytes are written.
    try:
        command = [sys.executable, "-X", "utf8", "-B", "-P", "-m", "fist_kb._engine.kb", "admin", "init",
                   "--purpose", purpose, "--owner", owner]
        env = {**os.environ, "FIST_KB_ROOT": str(root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        try:
            completed = run_captured(command, env=env, timeout=60)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("KB initialization timed out") from error
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            result = {}
        if completed.returncode or not isinstance(result, dict) or result.get("ok") is not True:
            detail = result.get("error") if isinstance(result, dict) and result.get("error") else completed.stderr[-500:]
            raise RuntimeError(f"The engine did not initialize the KB: {detail}")
        return {"ok": True, "operation": "initialize", "project_root": str(project), "kb_root": str(root),
                "revision": 1, "created_files": [name for name in STATE_FILES if (root / name).is_file()],
                "role_files_created": False, "migration_performed": False}
    except Exception as error:
        cleanup_errors = []
        for name in reversed(STATE_FILES):
            try:
                (root / name).unlink(missing_ok=True)
            except OSError as cleanup_error:
                cleanup_errors.append(f"{name}: {cleanup_error}")
        try:
            root.rmdir()
        except OSError as cleanup_error:
            cleanup_errors.append(f"{root}: {cleanup_error}")
        if cleanup_errors:
            raise RuntimeError(f"KB initialization failed ({error}); cleanup is incomplete: " + "; ".join(cleanup_errors)) from error
        raise
