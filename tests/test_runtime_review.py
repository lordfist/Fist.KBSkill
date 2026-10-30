"""Review regressions: root-bound read-only orientation and attributable post-commit cleanup failure."""
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

STATE = ("core.json", "records.jsonl", "journal.jsonl", "sources.json")


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
        return {name: (self.root / name).read_bytes() for name in STATE}

    def test_orientation_is_complete_root_bound_read_only_and_revision_checked(self):
        before = self.canonical()
        value = self.kb.read()
        self.assertTrue(value["ok"], value)
        self.assertTrue(value["complete"])
        core = value["results"][0]["core"]
        stored = json.loads(before["core.json"])
        self.assertEqual(core, {key: item for key, item in stored.items() if key != "state_files"})
        self.assertEqual(core["binding"]["root"], str(self.project))
        self.assertEqual(before, self.canonical())
        self.assertTrue(self.kb.write({"by": "regression", "changes": [
            {"new": {"kind": "observed_fact", "title": f"Fact {n}", "statement": "x" * 900}} for n in range(8)]})["ok"])
        page = self.kb.read({"kinds": ["observed_fact"], "budget": 600})
        self.assertFalse(page["complete"], page)
        self.assertTrue(self.kb.write({"by": "regression", "changes": [
            {"log": {"id": "FACT-0001", "text": "Another writer's note"}}]})["ok"])
        continued = self.kb.read({"continue": page["continue"]})
        self.assertFalse(continued["ok"])
        self.assertEqual(continued["error"]["code"], "revision_changed")

    def test_requests_travel_on_stdin_whole(self):
        quote = "Owner's exact words č " + "q" * 11000
        written = self.kb.write({"by": "regression", "changes": [
            {"new": {"kind": "owner_decision", "title": "Long quote", "statement": "Keep the quote.", "quote": quote}}]})
        self.assertEqual(written["outcome"], "committed", written)
        stored = [json.loads(line) for line in (self.root / "records.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(stored[0]["quote"], quote)

    def test_nonzero_json_keeps_bounded_attributed_stderr_without_rewriting_outcome(self):
        original = {"ok": True, "outcome": "committed", "revision": 2}
        stderr = "x" * 4500 + "remaining lock"
        captured = subprocess.CompletedProcess([], 2, json.dumps(original), stderr)
        with patch("fist_kb.api.run_captured", return_value=captured) as call:
            value = self.kb.write({"by": "synthetic-not-executed", "changes": []})
        call.assert_called_once()
        self.assertFalse(value["ok"])
        self.assertEqual((value["outcome"], value["revision"]), ("committed", 2))
        diagnostic = value["cli_diagnostics"]
        self.assertEqual(diagnostic["source"], "fist-kb CLI stderr")
        self.assertEqual(diagnostic["stderr"], stderr[-4000:])
        self.assertTrue(diagnostic["truncated"])
        self.assertEqual(diagnostic["characters"], len(stderr))

    def test_completed_commit_survives_controlled_cli_lock_cleanup_failure(self):
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
        def execute_faulted(command, *, env, timeout, input=None):
            # Keep the actual engine commit and CLI cleanup path. Only this
            # child's unlink of its disposable write lock is faulted.
            return run_captured([sys.executable, "-X", "utf8", "-B", "-c", wrapper,
                                 str(lock), *command[command.index("fist_kb") + 1:]], env=env, timeout=timeout,
                                input=input)
        with patch("fist_kb.api.run_captured", side_effect=execute_faulted) as call:
            value = self.kb.write({"by": "regression", "note": "Disposable cleanup-fault case", "changes": [
                {"new": {"kind": "observed_fact", "title": "Committed once",
                         "statement": "Committed once before the controlled lock cleanup failure."}}]})
        call.assert_called_once()
        self.assertFalse(value["ok"], value)
        self.assertEqual(value["exit_code"], 2)
        self.assertEqual((value["outcome"], value["revision"]), ("committed", 2))
        self.assertIn("Write-lock cleanup failed", value["cli_diagnostics"]["stderr"])
        self.assertIn("controlled cleanup fault", value["cli_diagnostics"]["stderr"])
        self.assertIn("No retry or repair was attempted", value["cli_diagnostics"]["stderr"])
        self.assertTrue(lock.exists(), "Failed cleanup must not silently remove the lock")
        self.assertEqual(json.loads((self.root / "core.json").read_bytes())["revision"], 2)
        records = (self.root / "records.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(len(records), 1, "The write must not be replayed")
        self.assertEqual(json.loads(records[0])["id"], "FACT-0001")
        self.assertTrue(self.kb.validate()["ok"], "The retained cleanup lock must not prevent reads")
        blocked = self.kb.write({"by": "regression", "changes": [{"log": {"id": "FACT-0001", "text": "x"}}]})
        self.assertEqual(blocked["error"]["code"], "write_locked")
        # Registered fixture cleanup removes this test-owned lock with its KB.


if __name__ == "__main__":
    unittest.main()
