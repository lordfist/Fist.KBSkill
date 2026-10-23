"""Create an empty, explicitly bound KB without installing an agent role."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def initialize(project_dir: str | Path) -> dict[str, Any]:
    """Create only KB data; refuse existing KBs, never migrate or rebind."""
    project = Path(project_dir).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise ValueError("The project must be an existing directory.")
    root = project / "KnowledgeBase"
    root.mkdir()  # Exclusive creation decides ownership before any bytes are written.
    created_files: list[Path] = []
    created_dirs: list[Path] = []
    try:
        records = b""
        sources = _json_bytes({"catalog_revision": 1, "sources": []})
        core = {
            "schema_version": 1, "kb_revision": 1,
            "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "experiment": {"root": str(project), "status": "active", "purpose": None,
                           "root_semantics": "Explicit project data boundary; initialization assigns no agent role."},
            "pinned_record_ids": [], "routes": {}, "owner_decision_ids": [], "open_question_ids": [],
            "next_decision_order": [],
            "state_files": {"records_jsonl_sha256": hashlib.sha256(records).hexdigest().upper(),
                            "sources_json_sha256": hashlib.sha256(sources).hexdigest().upper()},
        }
        transactions = root / "transactions"
        transactions.mkdir()
        created_dirs.append(transactions)
        # Publish the commit marker last, just as ordinary KB writes do.
        for name, content in (("records.jsonl", records), ("sources.json", sources), ("core.json", _json_bytes(core))):
            path = root / name
            with path.open("xb") as handle:
                created_files.append(path)
                handle.write(content)
        return {"ok": True, "operation": "initialize", "project_root": str(project),
                "kb_root": str(root), "kb_revision": 1, "created_files": [path.name for path in created_files],
                "role_files_created": False, "migration_performed": False}
    except Exception as error:
        cleanup_errors = []
        for path in reversed(created_files):
            try:
                path.unlink()
            except OSError as cleanup_error:
                cleanup_errors.append(f"{path}: {cleanup_error}")
        for path in [*reversed(created_dirs), root]:
            try:
                path.rmdir()
            except OSError as cleanup_error:
                cleanup_errors.append(f"{path}: {cleanup_error}")
        if cleanup_errors:
            raise RuntimeError(f"KB initialization failed ({error}); cleanup is incomplete: " + "; ".join(cleanup_errors)) from error
        raise
