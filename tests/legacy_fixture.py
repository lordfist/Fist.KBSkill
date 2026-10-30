"""A synthetic KB in the layout Fist KB 0.3.x created (Seed v07 data format), for migration tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def legacy_kb(project: Path, *, select: bool = True) -> Path:
    """Write PROJECT/KnowledgeBase as 0.3.x initialization and one reviewed write left it; optionally select it."""
    root = project / "KnowledgeBase"
    (root / "transactions").mkdir(parents=True)
    records = (json.dumps({"id": "FACT-0001", "kind": "observed_fact", "status": "active",
                           "statement": "The sample service starts with SAMPLE_MODE=1.", "source_refs": [],
                           "uncertainty": "Deployment untested.",
                           "resolution_notes": [{"note": "Checked locally.", "observed_at": "2026-10-01"}]},
                          ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    sources = (json.dumps({"catalog_revision": 1, "sources": []}, indent=2, sort_keys=True) + "\n").encode("utf-8")
    core = {"schema_version": 1, "kb_revision": 2, "updated_at": "2026-10-01T00:00:00+00:00",
            "experiment": {"root": str(project), "status": "active", "purpose": None,
                           "root_semantics": "Explicit project data boundary; initialization assigns no agent role."},
            "pinned_record_ids": [], "routes": {"startup_checks": {"record_ids": ["FACT-0001"], "topic_hints": ["startup"]}},
            "owner_decision_ids": [], "open_question_ids": [], "next_decision_order": [],
            "state_files": {"records_jsonl_sha256": hashlib.sha256(records).hexdigest().upper(),
                            "sources_json_sha256": hashlib.sha256(sources).hexdigest().upper()}}
    (root / "records.jsonl").write_bytes(records)
    (root / "sources.json").write_bytes(sources)
    (root / "core.json").write_text(json.dumps(core, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if select:
        (project / ".fist-kb.json").write_text(json.dumps({"format": 1, "project": str(project), "kb": str(root)},
                                                          indent=2) + "\n", encoding="utf-8")
    return root
