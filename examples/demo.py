"""A disposable KB example using the installed public package. No live data changes."""
from pathlib import Path
import tempfile

from fist_kb.api import KnowledgeBase
from fist_kb.initialize import initialize


def main():
    with tempfile.TemporaryDirectory(prefix="fist-kb-demo-") as folder:
        project = Path(folder)
        initialize(project)
        kb = KnowledgeBase(project / "KnowledgeBase")
        request = {"actor": "Demo", "note": "Record a synthetic project decision", "new_records": [
            {"id": "DEC-0001", "kind": "owner_decision", "status": "active", "source_refs": [],
             "statement": "The example project uses blue charts."}]}
        draft = kb.draft(request, 1)
        assert draft.get("ok") is not False, draft
        preview = kb.preview(draft["transaction"], 1)
        assert preview.get("ok") is not False and preview["next_offset"] is None, preview
        result = kb.apply(draft["transaction"], 1, preview["transaction_sha256"])
        assert result["write_outcome"]["verification_completed"], result
        found = kb.get(["DEC-0001"])
        assert found["value"]["statement"] == "The example project uses blue charts.", found
        print("Saved, previewed, committed and retrieved a synthetic decision.")
    assert not project.exists(), "Demo directory remained after cleanup"
    print("Temporary knowledge removed; no live KB was used.")


if __name__ == "__main__":
    main()
