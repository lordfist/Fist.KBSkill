"""Folder setup, cancellation, preserved existing data and project isolation."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fist_kb.api import KnowledgeBase
from fist_kb.initialize import initialize
from fist_kb.setup import configure, status, read_binding


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="setup-", dir=os.environ.get("FIST_KB_TEST_TMP"))
        self.addCleanup(self.fixture.cleanup)
        self.area = Path(self.fixture.name).resolve()
        self.project = self.area / "project č"
        self.project.mkdir()
        self.storage = self.area / "data č"
        self.storage.mkdir()

    def test_create_and_recover_binding_without_environment(self):
        self.assertFalse(status(self.project)["ready"])
        result = configure(self.project, "create", str(self.storage), allow_separate_store=True)
        self.assertTrue(result["ready"])
        self.assertEqual(read_binding(self.project), self.storage / "KnowledgeBase")
        self.assertTrue(status(self.project)["ready"])
        self.assertFalse(result["migration_performed"])
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_existing_selection_does_not_change_knowledge(self):
        initialize(self.storage)
        kb = self.storage / "KnowledgeBase"
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in kb.iterdir() if p.is_file()}
        result = configure(self.project, "existing", str(kb), allow_separate_store=True)
        self.assertTrue(result["ready"])
        self.assertFalse(result["created"])
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in kb.iterdir() if p.is_file()})

    def test_cancel_is_a_noop(self):
        with patch("fist_kb.setup.choose_directory", return_value=None):
            result = configure(self.project, "create")
        self.assertTrue(result["cancelled"])
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_two_projects_keep_distinct_selections_and_switch_is_explicit(self):
        second = self.area / "second"
        second.mkdir()
        configure(self.project, "create", str(self.storage), allow_separate_store=True)
        configure(second, "create", str(second))
        first_binding = read_binding(self.project)
        with self.assertRaisesRegex(ValueError, "already has a KB"):
            configure(self.project, "existing", str(second))
        self.assertEqual(read_binding(self.project), first_binding)
        self.assertNotEqual(read_binding(second), first_binding)
        configure(self.project, "existing", str(second), replace=True, allow_separate_store=True)
        self.assertEqual(read_binding(self.project), read_binding(second))
        self.assertTrue(first_binding.is_dir())

    def test_create_never_replaces_existing_data(self):
        initialize(self.storage)
        with self.assertRaisesRegex(ValueError, "already exists"):
            configure(self.project, "create", str(self.storage), allow_separate_store=True)
        self.assertFalse((self.project / ".fist-kb.json").exists())

    def test_legacy_tools_are_not_silently_adopted(self):
        initialize(self.storage)
        (self.storage / "KnowledgeBase" / "tools").mkdir()
        with self.assertRaisesRegex(ValueError, "evolved Seed"):
            configure(self.project, "existing", str(self.storage), allow_separate_store=True)
        self.assertFalse((self.project / ".fist-kb.json").exists())

    def test_moved_or_damaged_binding_does_not_fall_back(self):
        configure(self.project, "create", str(self.storage), allow_separate_store=True)
        path = self.project / ".fist-kb.json"
        data = json.loads(path.read_text())
        data["project"] = str(self.storage)
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "another project"):
            status(self.project)

    def test_missing_target_can_be_replaced_explicitly(self):
        configure(self.project, "create", str(self.storage), allow_separate_store=True)
        original = self.storage / "KnowledgeBase"
        retained = self.storage / "retained"
        original.rename(retained)
        alternate = self.area / "alternate"
        alternate.mkdir()
        with self.assertRaises(OSError):
            status(self.project)
        value = configure(self.project, "create", str(alternate), replace=True, allow_separate_store=True)
        self.assertTrue(value["ready"])
        self.assertTrue(retained.is_dir())


    def test_separate_store_requires_explicit_acknowledgement(self):
        result = configure(self.project, "create", str(self.storage))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "storage_scope_confirmation_required")
        self.assertEqual(result["evidence_root"], str(self.storage))
        self.assertEqual(list(self.storage.iterdir()), [])
        self.assertFalse((self.project / ".fist-kb.json").exists())
        result = configure(self.project, "create", str(self.storage), allow_separate_store=True)
        self.assertTrue(result["memory_service_ready"])
        self.assertEqual(result["storage_mode"], "separate_store")
        self.assertEqual(result["workflow_activation"], "requires_operating_skill")
        self.assertEqual(status(self.project)["evidence_root"], str(self.storage))

    def test_project_store_reports_its_real_boundary(self):
        result = configure(self.project, "create", str(self.project))
        self.assertTrue(result["ready"])
        self.assertEqual(result["storage_mode"], "project")
        self.assertTrue(result["project_evidence_root_matches"])
        self.assertEqual(result["evidence_root"], str(self.project))

    def test_separate_store_enforces_source_registration_and_preparation_boundary(self):
        configure(self.project, "create", str(self.storage), allow_separate_store=True)
        root = read_binding(self.project)
        api = KnowledgeBase(root)
        external = self.project / "source.cs"
        inside = self.storage / "evidence.cs"
        external.write_text("// project source\n", encoding="utf-8")
        inside.write_text("// explicitly in the store evidence root\n", encoding="utf-8")
        canonical = {name: (root / name).read_bytes() for name in ("core.json", "records.jsonl", "sources.json")}
        def source_request(path, name):
            raw = path.read_bytes()
            value = {"actor": "fixture", "note": "Synthetic source boundary check",
                     "upsert_sources": [{"source_id": "SRC-0001", "locator": {"type": "file", "path": str(path)},
                                         "sha256": hashlib.sha256(raw).hexdigest().upper(), "size_bytes": len(raw)}],
                     "upsert_records": [], "core_patch": {}}
            target = root / "transactions" / name
            target.write_text(json.dumps(value), encoding="utf-8")
            return target
        rejected = source_request(external, "outside.json")
        preview = api.run(["apply", str(rejected), "--expect-revision", "1", "--dry-run", "--preview-values", "--compact"])
        self.assertFalse(preview.get("ok", True), preview)
        self.assertEqual(canonical, {name: (root / name).read_bytes() for name in canonical})
        inspection = api.run(["prepare", "inspect", str(external), "--compact"])
        self.assertFalse(inspection.get("ok", True), inspection)
        accepted = source_request(inside, "inside.json")
        preview = api.run(["apply", str(accepted), "--expect-revision", "1", "--dry-run", "--preview-values", "--compact"])
        self.assertTrue(preview.get("ok"), preview)
        result = api.apply(str(accepted), 1, preview["transaction_sha256"])
        self.assertTrue(result.get("ok"), result)
        prepared = api.run(["prepare", "prepare", "SRC-0001", "--actor", "fixture", "--compact"])
        self.assertTrue(prepared.get("ok"), prepared)
        verified = api.run(["prepare", "verify", "SRC-0001", "--compact"])
        self.assertTrue(verified.get("ok"), verified)


if __name__ == "__main__":
    unittest.main()
