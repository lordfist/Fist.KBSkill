"""A disposable KB example using the installed public package. No live data changes."""
from pathlib import Path
import tempfile

from fist_kb.api import KnowledgeBase
from fist_kb.initialize import initialize


def main():
    with tempfile.TemporaryDirectory(prefix="fist-kb-demo-") as folder:
        project = Path(folder)
        initialize(project, purpose="Demo project memory", owner="Demo owner")
        kb = KnowledgeBase(project / "KnowledgeBase")
        request = {"by": "Demo", "note": "Record a synthetic project decision", "changes": [
            {"new": {"ref": "d", "kind": "owner_decision", "title": "Chart colour",
                     "statement": "The example project uses blue charts.", "quote": "Use blue for the charts."}}]}
        preview = kb.write({**request, "preview": True})
        assert preview.get("outcome") == "preview", preview
        result = kb.write({"by": "Demo", "confirm": preview["preview_id"]})
        assert result.get("outcome") == "committed" and result["ids"] == {"d": "DEC-0001"}, result
        found = kb.read({"query": "blue charts"})
        assert found["results"][0]["id"] == "DEC-0001", found
        orientation = kb.read()
        assert orientation["complete"] and orientation["revision"] == 2, orientation
        print("Previewed, committed, searched and oriented on a synthetic decision.")
    assert not project.exists(), "Demo directory remained after cleanup"
    print("Temporary knowledge removed; no live KB was used.")


if __name__ == "__main__":
    main()
