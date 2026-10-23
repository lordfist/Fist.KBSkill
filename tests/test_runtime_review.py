"""SI review regressions: full core and attributable post-commit cleanup failure."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fist_kb.api import KnowledgeBase
from fist_kb.initialize import initialize
from fist_kb.process import run_captured


class RuntimeReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="runtime-review-", dir=os.environ.get("FIST_KB_TEST_TMP"))
        self.area = Path(fixture.name).resolve()
        def cleanup():
            fixture.cleanup()
            self.assertFalse(self.area.exists(), "Runtime review fixture remained")
        self.addCleanup(cleanup)
        self.project = self.area / "Project č"
        self.project.mkdir()
        initialize(self.project)
        self.root = self.project / "KnowledgeBase"
        self.kb = KnowledgeBase(self.root)

    def canonical(self):
        return {name: (self.root / name).read_bytes() for name in ("core.json", "records.jsonl", "sources.json")}

    def test_full_core_is_complete_root_bound_read_only_and_revision_checked(self):
        before = self.canonical()
        value = self.kb.core(expect_revision=1)
        self.assertNotEqual(value.get("ok"), False, value)
        self.assertEqual(value["core"], json.loads(before["core.json"]))
        self.assertFalse(value["core_is_projection"])
        self.assertEqual(value["kb_root"], str(self.root))
        self.assertEqual(value["evidence_root"], str(self.project))
        self.assertLessEqual(value["estimated_tokens"], 8000)
        refused = self.kb.core(expect_revision=2)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["code"], "revision_conflict")
        self.assertNotIn("core", refused)
        too_small = self.kb.core(budget=300)
        self.assertFalse(too_small["ok"])
        self.assertNotIn("core", too_small)
        self.assertGreater(too_small["required_budget"], 300)
        self.assertEqual(before, self.canonical())

    def test_core_wrapper_budget_counts_utf8_metadata(self):
        core = {"text": "字" * 250}
        native = {"core": core, "core_is_projection": False, "kb_revision": 1, "estimated_tokens": 280}
        with patch.object(self.kb, "run", return_value=native):
            value = self.kb.core(budget=300)
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "budget_too_small")
        self.assertNotIn("core", value)
        self.assertGreater(value["required_budget"], 300)

    def test_nonzero_json_keeps_bounded_attributed_stderr_without_rewriting_outcome(self):
        original = {"ok": True, "to_revision": 2,
                    "write_outcome": {"commit_completed": True, "verification_completed": True}}
        stderr = "x" * 4500 + "remaining lock"
        captured = subprocess.CompletedProcess([], 2, json.dumps(original), stderr)
        with patch("fist_kb.api.run_captured", return_value=captured) as call:
            value = self.kb.run(["apply", "synthetic-not-executed"])
        call.assert_called_once()
        self.assertFalse(value["ok"])
        self.assertEqual(value["write_outcome"], original["write_outcome"])
        self.assertEqual(value["to_revision"], 2)
        diagnostic = value["cli_diagnostics"]
        self.assertEqual(diagnostic["source"], "fist-kb CLI stderr")
        self.assertEqual(diagnostic["stderr"], stderr[-4000:])
        self.assertTrue(diagnostic["truncated"])
        self.assertEqual(diagnostic["characters"], len(stderr))

    def test_completed_commit_survives_controlled_cli_lock_cleanup_failure(self):
        draft = self.kb.draft({"actor": "regression", "note": "Disposable cleanup-fault case",
            "new_records": [{"id": "FACT-0001", "kind": "observed_fact", "status": "active",
                "statement": "Committed once before the controlled lock cleanup failure.", "source_refs": []}]}, 1)
        self.assertTrue(draft["ok"], draft)
        preview = self.kb.preview(draft["transaction"], 1)
        self.assertTrue(preview["ok"], preview)
        wrapper = """import sys
from pathlib import Path
from unittest.mock import patch
from fist_kb.cli import main
lock = Path(sys.argv[1]).resolve()
original = Path.unlink
def failed_cleanup(path, *args, **kwargs):
    if path.resolve() == lock:
        raise PermissionError('controlled cleanup fault')
    return original(path, *args, **kwargs)
with patch.object(Path, 'unlink', failed_cleanup):
    raise SystemExit(main(sys.argv[2:]))
"""
        lock = self.root / ".write.lock"
        def execute_faulted(command, *, env, timeout):
            # Keep the actual engine commit and CLI cleanup path. Only this
            # child's unlink of its disposable write lock is faulted.
            return run_captured([sys.executable, "-X", "utf8", "-B", "-c", wrapper,
                                 str(lock), *command[6:]], env=env, timeout=timeout)
        with patch("fist_kb.api.run_captured", side_effect=execute_faulted) as call:
            value = self.kb.apply(draft["transaction"], 1, preview["transaction_sha256"])
        call.assert_called_once()
        self.assertFalse(value["ok"], value)
        self.assertEqual(value["exit_code"], 2)
        self.assertEqual(value["to_revision"], 2)
        self.assertTrue(value["write_outcome"]["commit_completed"])
        self.assertTrue(value["write_outcome"]["verification_completed"])
        self.assertIn("Write-lock cleanup failed", value["cli_diagnostics"]["stderr"])
        self.assertIn("controlled cleanup fault", value["cli_diagnostics"]["stderr"])
        self.assertIn("No retry or repair was attempted", value["cli_diagnostics"]["stderr"])
        self.assertTrue(lock.exists(), "Failed cleanup must not silently remove the lock")
        self.assertEqual(json.loads((self.root / "core.json").read_bytes())["kb_revision"], 2)
        records = (self.root / "records.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(len(records), 1, "The write must not be replayed")
        self.assertEqual(json.loads(records[0])["id"], "FACT-0001")
        self.assertTrue(self.kb.validate()["ok"], "The retained cleanup lock must not prevent reads")
        # Registered fixture cleanup removes this test-owned lock with its KB.


if __name__ == "__main__":
    unittest.main()
