"""Small process-isolated adapter over the same CLI used by humans.

Every call fixes one data root. No module is imported from a KB directory and
no uncertain write is retried. This is a convenience API, not a filesystem sandbox.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .process import run_captured

# The core schema this engine reads; Fist KB 0.3.x and earlier wrote the v07 layout.
CORE_SCHEMA = "seed-kb-8"
CORE_PROBE_LIMIT = 16 * 1024 * 1024


class LegacyKnowledgeBase(ValueError):
    """A KB in the Fist KB 0.3.x (Seed v07) layout, which this version reads only through migration."""
    code = "v07_kb_requires_migration"


def kb_format(root: str | Path) -> str:
    """'v08' for this engine's layout, 'v07' for a Fist KB 0.3.x KB, otherwise 'unknown'. Reads core.json only."""
    path = Path(root) / "core.json"
    try:
        if path.stat().st_size > CORE_PROBE_LIMIT:
            return "unknown"
        core = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return "unknown"
    if not isinstance(core, dict):
        return "unknown"
    if core.get("schema") == CORE_SCHEMA:
        return "v08"
    if "kb_revision" in core and "schema_version" in core:
        return "v07"
    return "unknown"


def migration_target(root: str | Path) -> Path:
    """The sibling folder a migration of ROOT creates: same parent, so the evidence boundary stays the same."""
    path = Path(root)
    return path.with_name(path.name + ".v08")


class KnowledgeBase:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir() or not (self.root / "core.json").is_file():
            raise ValueError("Select an initialized KnowledgeBase directory")
        if kb_format(self.root) == "v07":
            raise LegacyKnowledgeBase(
                f"{self.root} uses the Fist KB 0.3.x (v07) layout. Migrate it once into a new folder "
                f"({migration_target(self.root).name}) with kb_setup mode 'migrate' or 'fist-kb admin migrate-v07'; "
                "the old KB is only read.")

    def run(self, arguments: list[str], *, input: str | None = None, timeout: float = 120) -> dict[str, Any]:
        # -P keeps the caller's working directory off sys.path, so project files cannot shadow imports.
        command = [sys.executable, "-X", "utf8", "-B", "-P", "-m", "fist_kb",
                   "--kb", str(self.root), *arguments]
        try:
            result = run_captured(command, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                  timeout=timeout, input=input)
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "outcome_unknown": True, "termination": getattr(exc, "termination", {}),
                    "error": {"code": "execution_timeout", "message": "The call timed out. A write may have "
                              "completed: read orientation (or the request_id) before sending anything again."}}
        except OSError as exc:
            return {"ok": False, "error": {"code": "launch_failed", "message": str(exc)}}
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            return {"ok": False, "outcome_unknown": True, "exit_code": result.returncode,
                    "stderr": result.stderr[-2000:],
                    "error": {"code": "invalid_tool_output", "message": "No complete JSON result was received; "
                              "inspect state before repeating a write."}}
        if not isinstance(value, dict):
            return {"ok": False, "outcome_unknown": True,
                    "error": {"code": "invalid_tool_output", "message": "Tool response was not a JSON object."}}
        if result.returncode:
            value = {**value, "ok": False, "exit_code": result.returncode}
            if result.stderr:
                # CLI stderr can explain a post-commit cleanup error. Keep the
                # engine's outcome intact: a failed exit is not a failed commit.
                value["cli_diagnostics"] = {
                    "source": "fist-kb CLI stderr",
                    "stderr": result.stderr[-4000:],
                    "truncated": len(result.stderr) > 4000,
                    "characters": len(result.stderr),
                }
        return value

    def read(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        """kb_read: {} for orientation; ids, topic, query, kinds/status, changes_since, request or continue."""
        if request is not None and not isinstance(request, dict):
            raise ValueError("A read request is one JSON object")
        return self.run(["read", "-"], input=json.dumps(request or {}, ensure_ascii=False, allow_nan=False))

    def write(self, request: dict[str, Any]) -> dict[str, Any]:
        """kb_write: one call commits and echoes what was stored; never retried here."""
        if not isinstance(request, dict):
            raise ValueError("A write request is one JSON object with changes")
        return self.run(["write", "-"], input=json.dumps(request, ensure_ascii=False, allow_nan=False))

    def validate(self, *, deep: bool = False) -> dict[str, Any]:
        return self.run(["admin", "validate", *(["--deep"] if deep else [])])
