"""Portable public CLI regressions using only disposable, synthetic knowledge.

Run with `python -m unittest discover -s tests`. The package must be importable
(for source development set PYTHONPATH to src). No installer, agent role files,
network, model, optional PDF parser, or private corpus is needed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest().upper()


def snapshot(root):
    return {str(path.relative_to(root)): (None if path.is_dir() else digest(path.read_bytes()))
            for path in sorted(root.rglob("*"))}


def fact(identifier="FACT-0001", statement="A synthetic observation.", **fields):
    return {"id": identifier, "kind": "observed_fact", "status": "active",
            "statement": statement, "source_refs": [], **fields}


class PublicCliTests(unittest.TestCase):
    def setUp(self):
        temporary_parent = Path(os.environ.get("FIST_KB_TEST_TMP", Path(__file__).resolve().parent)).resolve()
        area = tempfile.TemporaryDirectory(prefix="fist-kb-test-", dir=temporary_parent)
        self.area = Path(area.name).resolve()

        def cleanup():
            area.cleanup()
            self.assertFalse(self.area.exists(), "Disposable test directory was not removed")

        self.addCleanup(cleanup)
        self.project = self.area / "Project with spaces č"
        self.project.mkdir()
        self.kb = self.project / "KnowledgeBase"
        self.host = self.area / "unrelated working directory"
        self.host.mkdir()
        self.host_temp = self.area / "host-temp"
        self.host_temp.mkdir()
        self.env = dict(os.environ)
        self.env.pop("FIST_KB_ROOT", None)
        self.env.update(PYTHONDONTWRITEBYTECODE="1", TMP=str(self.host_temp), TEMP=str(self.host_temp),
                        TMPDIR=str(self.host_temp))
        package = importlib.util.find_spec("fist_kb")
        self.assertIsNotNone(package, "Install the package or set PYTHONPATH to src")
        self.package_root = Path(next(iter(package.submodule_search_locations))).resolve()
        self.package_before = snapshot(self.package_root)
        self.command("init", self.project)

    def tearDown(self):
        self.assertEqual(snapshot(self.package_root), self.package_before,
                         "A CLI operation changed installed package files or created bytecode")

    def process(self, *args, env=None):
        return subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "fist_kb", *map(str, args)],
                              cwd=self.host, env=env or self.env, capture_output=True,
                              encoding="utf-8", timeout=45)

    def command(self, *args, ok=True, env=None):
        result = self.process(*args, env=env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.fail(f"CLI did not return JSON: {result.stdout!r}; stderr={result.stderr!r}")

    def native(self, *args, root=None, ok=True):
        return self.command("--kb", root or self.kb, *args, ok=ok)

    def core(self, root=None):
        return json.loads(((root or self.kb) / "core.json").read_bytes())

    def canonical(self, root=None):
        return {name: ((root or self.kb) / name).read_bytes()
                for name in ("core.json", "records.jsonl", "sources.json")}

    def transaction(self, body, name="transaction", root=None):
        root = root or self.kb
        folder = root / "transactions"
        path = folder / (name + ".json")
        path.write_bytes(json_bytes({"actor": "Synthetic regression", **body}))
        return path

    def apply(self, body, root=None):
        root = root or self.kb
        revision = self.core(root)["kb_revision"]
        path = self.transaction(body, f"transaction-{revision}", root)
        preview = self.native("apply", path, "--expect-revision", revision,
                              "--dry-run", "--preview-values", "--compact", root=root)
        return self.native("apply", path, "--expect-revision", revision,
                           "--expect-transaction-sha256", preview["transaction_sha256"], "--compact", root=root)

    def test_empty_init_is_project_data_only_and_valid(self):
        self.assertEqual(self.core()["kb_revision"], 1)
        self.assertEqual(self.core()["experiment"]["root"], str(self.project.resolve()))
        self.assertEqual({p.name for p in self.project.iterdir()}, {"KnowledgeBase"})
        self.assertEqual({p.name for p in self.kb.iterdir()},
                         {"core.json", "records.jsonl", "sources.json", "transactions"})
        self.assertTrue(self.native("validate", "--compact")["ok"])
        self.assertEqual(self.native("search", "absent", "--records-only", "--compact")["hits"], [])

    def test_explicit_selection_or_environment_is_required(self):
        before = snapshot(self.project)
        self.command("status", "--compact", ok=False)
        status = self.command("status", "--compact", env={**self.env, "FIST_KB_ROOT": str(self.kb)})
        self.assertTrue(status["ok"])
        self.assertEqual(snapshot(self.project), before)

    def test_package_inventory_does_not_require_seed_provenance(self):
        before = snapshot(self.project)
        result = self.native("inventory", "verify", "--compact")
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["files_checked"], 8)
        self.assertEqual(result["kb_root"], str(self.kb.resolve()))
        self.assertEqual(snapshot(self.project), before)

    def test_init_refuses_existing_kb_without_replacing_any_bytes(self):
        self.apply({"upsert_records": [fact(statement="Already owned knowledge")]})
        before = snapshot(self.project)
        self.command("init", self.project, ok=False)
        self.assertEqual(snapshot(self.project), before)

    def test_init_preserves_existing_project_and_role_files(self):
        other = self.area / "Existing project"
        other.mkdir()
        (other / "AGENTS.md").write_text("Existing project instructions.\n", encoding="utf-8")
        (other / "notes.txt").write_bytes(b"Original source bytes\r\n")
        before = {p.name: p.read_bytes() for p in other.iterdir()}
        self.command("init", other)
        for name, data in before.items():
            self.assertEqual((other / name).read_bytes(), data)
        self.assertFalse((other / ".SeedAnalyzer").exists())
        self.assertFalse((other / "CLAUDE.md").exists())

    def test_full_preview_is_read_only_and_sha_or_revision_mismatch_refuses(self):
        path = self.transaction({"upsert_records": [fact(statement="Exact prospective value")]})
        before = self.canonical()
        preview = self.native("apply", path, "--expect-revision", 1, "--dry-run", "--preview-values", "--compact")
        self.assertEqual(self.canonical(), before)
        self.assertIn("Exact prospective value", json.dumps(preview))
        self.native("apply", path, "--expect-revision", 1, "--expect-transaction-sha256", "0" * 64,
                    "--compact", ok=False)
        self.native("apply", path, "--expect-revision", 2, "--expect-transaction-sha256",
                    preview["transaction_sha256"], "--compact", ok=False)
        self.assertEqual(self.canonical(), before)
        applied = self.native("apply", path, "--expect-revision", 1, "--expect-transaction-sha256",
                              preview["transaction_sha256"], "--compact")
        self.assertEqual((applied["from_revision"], applied["to_revision"]), (1, 2))
        self.assertEqual(self.native("get", "FACT-0001", "--compact")["value"]["statement"],
                         "Exact prospective value")

    def test_held_write_lock_blocks_commit_and_prepare_but_not_reads_or_draft(self):
        source = self.project / "source.md"
        content = b"# Synthetic source\nA small observation for lock testing.\n"
        source.write_bytes(content)
        self.apply({"upsert_records": [fact(resolution_notes=[])], "upsert_sources": [{
            "source_id": "SRC-0001", "title": "Lock fixture source",
            "locator": {"type": "file", "path": str(source)},
            "sha256": digest(content), "size_bytes": len(content)}]})
        revision = self.core()["kb_revision"]
        transaction = self.transaction({"upsert_records": [fact("FACT-0002", "Prospective finding")]}, "locked-write")
        request = self.transaction({"records": [{"id": "FACT-0001",
                     "append": {"/resolution_notes": [{"note": "Prospective note"}]}}]}, "locked-request")
        lock = self.kb / ".write.lock"
        held = json_bytes({"pid": os.getpid(), "token": "synthetic-test-owner"})
        lock.write_bytes(held)
        before = self.canonical()
        self.assertTrue(self.native("status", "--compact")["ok"])
        preview = self.native("apply", transaction, "--expect-revision", revision,
                              "--dry-run", "--preview-values", "--compact")
        self.native("draft", request, "--output", self.kb / "transactions" / "locked-draft.json",
                    "--expect-revision", revision, "--compact")
        refused = self.native("apply", transaction, "--expect-revision", revision,
                              "--expect-transaction-sha256", preview["transaction_sha256"], "--compact", ok=False)
        self.assertEqual(refused["code"], "write_locked")
        refused_prepare = self.native("prepare", "SRC-0001", "--actor", "Synthetic regression", "--compact", ok=False)
        self.assertEqual(refused_prepare["code"], "write_locked")
        self.assertEqual(lock.read_bytes(), held)
        self.assertEqual(self.canonical(), before)
        # This test owns this synthetic lock; removing it is not stale-lock recovery.
        lock.unlink()
        self.native("apply", transaction, "--expect-revision", revision,
                    "--expect-transaction-sha256", preview["transaction_sha256"], "--compact")
        self.assertFalse(lock.exists(), "Successful commit left its own write lock")
        self.native("prepare", "SRC-0001", "--actor", "Synthetic regression", "--compact")
        self.assertFalse(lock.exists(), "Successful source preparation left its own write lock")

    def test_append_draft_preserves_prior_fields_and_array_pages_reconstruct(self):
        original = [{"text": f"Note {number}"} for number in range(7)]
        self.apply({"upsert_records": [fact(resolution_notes=original, custom={"retained": True})]})
        revision = self.core()["kb_revision"]
        request = self.transaction({"records": [{"id": "FACT-0001",
                     "append": {"/resolution_notes": [{"text": "Newest correction"}]}}]}, "request")
        draft = self.kb / "transactions" / "draft.json"
        before = self.canonical()
        response = self.native("draft", request, "--output", draft, "--expect-revision", revision, "--compact")
        self.assertTrue(response["draft_only"])
        self.assertEqual(self.canonical(), before)
        preview = self.native("apply", draft, "--expect-revision", revision,
                              "--dry-run", "--preview-values", "--compact")
        self.assertIn('"appended"', json.dumps(preview))
        self.native("apply", draft, "--expect-revision", revision, "--expect-transaction-sha256",
                    preview["transaction_sha256"], "--compact")
        current = self.native("get", "FACT-0001", "--compact")["value"]
        self.assertEqual(current["custom"], {"retained": True})
        self.assertEqual(current["resolution_notes"], original + [{"text": "Newest correction"}])
        revision, offset, found = self.core()["kb_revision"], 0, []
        while offset is not None:
            page = self.native("get", "FACT-0001", "--array", "resolution_notes", "--limit", 3,
                               "--offset", offset, "--expect-revision", revision, "--compact")
            self.assertTrue(page["projection"]["is_projection"])
            found.extend(page["value"]["resolution_notes"])
            offset = page["array_page"]["next_offset"]
        self.assertEqual(found, current["resolution_notes"])

    def test_explicit_core_routes_keep_authored_order(self):
        self.apply({"upsert_records": [fact("FACT-0001", "First record"), fact("FACT-0002", "Second record")],
                    "core_patch": {"routes": {"guide": {"record_ids": ["FACT-0002", "FACT-0001"]}}}})
        view = self.native("context", "--topic", "guide", "--records-only", "--no-core", "--compact")
        self.assertEqual([row["id"] for row in view["records"]], ["FACT-0002", "FACT-0001"])

    def test_two_roots_and_unrelated_working_directory_stay_isolated(self):
        other = self.area / "Second project"
        other.mkdir()
        self.command("init", other)
        other_kb = other / "KnowledgeBase"
        self.apply({"upsert_records": [fact(statement="Project alpha only")]})
        self.apply({"upsert_records": [fact(statement="Project beta only")]}, root=other_kb)
        beta_before = self.canonical(other_kb)
        self.assertEqual(self.native("get", "FACT-0001", "--compact")["value"]["statement"], "Project alpha only")
        self.assertEqual(self.native("get", "FACT-0001", "--compact", root=other_kb)["value"]["statement"],
                         "Project beta only")
        self.apply({"upsert_records": [fact(statement="Project alpha changed")]})
        self.assertEqual(self.canonical(other_kb), beta_before)
        self.assertEqual(list(self.host.iterdir()), [])

    def test_working_directory_modules_do_not_shadow_engine_imports(self):
        # A console script or MCP server keeps its working directory off sys.path, but a
        # `python -m` child would prepend it. A project folder holding json.py must not
        # replace the standard library in any CLI, draft or API child process.
        (self.host / "json.py").write_text('raise SystemExit("planted json.py was imported")\n', encoding="utf-8")

        def run(*args, env=None):
            result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-P", *map(str, args)],
                                    cwd=self.host, env=env or self.env, capture_output=True,
                                    encoding="utf-8", timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("planted", result.stdout + result.stderr)
            return json.loads(result.stdout)

        self.assertTrue(run("-m", "fist_kb", "--kb", self.kb, "validate", "--compact")["ok"])
        request = self.transaction({"note": "Working-directory isolation", "new_records": [fact()]}, "request")
        draft = self.kb / "transactions" / "draft.json"
        self.assertTrue(run("-m", "fist_kb", "--kb", self.kb, "draft", request, "--output", draft,
                            "--expect-revision", 1, "--compact")["draft_only"])
        preview = run("-m", "fist_kb", "--kb", self.kb, "apply", draft, "--expect-revision", 1,
                      "--dry-run", "--preview-values", "--compact")
        run("-m", "fist_kb", "--kb", self.kb, "apply", draft, "--expect-revision", 1,
            "--expect-transaction-sha256", preview["transaction_sha256"], "--compact")
        core = run("-c", "import json, sys\nfrom fist_kb.api import KnowledgeBase\n"
                   "print(json.dumps(KnowledgeBase(sys.argv[1]).core()))", self.kb)
        self.assertIs(core["core_is_projection"], False, core)
        self.assertEqual(core["kb_revision"], 2)
        checked = run("-c", "import json\nfrom fist_kb._engine.kb_client import call_tool\n"
                      "print(json.dumps(call_tool('kb.py', ['validate', '--compact'], expected={'ok': bool})))",
                      env={**self.env, "FIST_KB_ROOT": str(self.kb)})
        self.assertTrue(checked["ok"], checked)

    def test_data_tools_directory_is_never_used_as_runtime_code(self):
        self.apply({"upsert_records": [fact(statement="Package implementation is selected")]})
        data_tools = self.kb / "tools"
        data_tools.mkdir()
        sentinel = data_tools / "kb.py"
        sentinel.write_text("This is synthetic data, not valid Python source.\n", encoding="utf-8")
        before = sentinel.read_bytes()
        self.assertEqual(self.native("get", "FACT-0001", "--compact")["value"]["statement"],
                         "Package implementation is selected")
        self.assertEqual(sentinel.read_bytes(), before)

    def test_copy_does_not_silently_rebind_an_existing_kb(self):
        target = self.area / "Copied project"
        target.mkdir()
        copy_kb = target / "KnowledgeBase"
        shutil.copytree(self.kb, copy_kb)
        before = snapshot(copy_kb)
        self.native("validate", "--compact", root=copy_kb, ok=False)
        self.assertEqual(snapshot(copy_kb), before)

    def test_guest_reads_are_projected_fingerprinted_and_write_nothing(self):
        self.apply({"upsert_records": [fact(statement="Guest-visible observation", private_detail="Still part of fingerprint")]})
        complete = self.native("get", "FACT-0001", "--compact")["value"]
        before = snapshot(self.project)
        info = self.native("guest", "info")
        search = self.native("guest", "search", "--query", "Guest-visible")
        selected = self.native("guest", "get", "--id", "FACT-0001", "--fields", "statement",
                               "--expect-revision", info["kb_revision"])
        self.assertTrue(info["ok"])
        self.assertEqual(info["kb_root"], str(self.kb.resolve()))
        self.assertEqual(selected["result"]["value"], {"id": "FACT-0001", "statement": "Guest-visible observation"})
        expected = digest(json_bytes(complete))
        self.assertEqual(selected["record_fingerprints"]["FACT-0001"], expected)
        self.assertEqual(search["record_fingerprints"]["FACT-0001"], expected)
        self.assertEqual(snapshot(self.project), before)

    def test_guest_missing_invalid_and_inconsistent_results_are_distinct(self):
        before = self.canonical()
        missing = self.native("guest", "get", "--id", "FACT-9999", ok=False)
        self.assertEqual(missing["result"]["error"]["code"], "unknown_id")
        self.assertEqual(self.native("guest", "search", "--query", "notfound")["result"]["hits"], [])
        self.native("guest", "apply", "anything", ok=False)
        self.native("guest", "get", "--id", "../core.json", ok=False)
        self.assertEqual(self.canonical(), before)
        path = self.kb / "records.jsonl"
        path.write_bytes(path.read_bytes() + b"\n")
        inconsistent = self.canonical()
        result = self.native("guest", "info", ok=False)
        self.assertEqual(result["result"]["error"]["category"], "inconsistent_state")
        self.assertEqual(self.canonical(), inconsistent)

    def test_guest_continuation_refuses_a_later_revision(self):
        self.apply({"upsert_records": [fact(f"FACT-{n:04d}", f"Shared search token {n}") for n in range(1, 5)]})
        first = self.native("guest", "search", "--query", "Shared", "--limit", 2)
        self.assertEqual(first["result"]["next_offset"], 2)
        self.apply({"upsert_records": [fact("FACT-0005", "Shared later observation")]})
        result = self.native("guest", "search", "--query", "Shared", "--limit", 2, "--offset", 2,
                             "--expect-revision", first["kb_revision"], ok=False)
        self.assertEqual(result["result"]["error"]["category"], "changed_revision")

    def test_brief_keeps_current_qualification_and_recent_correction(self):
        self.apply({"upsert_records": [fact(statement="Synthetic measurement remains provisional.",
                    uncertainty="No measured savings have been established.",
                    current_resolution={"statement": "Use the latest correction; it is not production approval."},
                    resolution_notes=[{"note": f"Routine historical step {n}. " + "detail " * 30} for n in range(8)]
                                     + [{"note": "Latest correction: the measurement was synthetic."}])]})
        view = self.native("get", "FACT-0001", "--summary", "--compact")["value"]
        self.assertIn("No measured savings", view["uncertainty"])
        self.assertIn("not production approval", view["current_resolution"]["statement"])
        self.assertIn("Latest correction", view["resolution_notes"][-1]["note"])
        self.assertTrue(view["detail_required"])

    def test_brief_optional_reserve_keeps_the_original_fitting_page(self):
        self.apply({"upsert_records": [fact(f"FACT-{n:04d}", "needle " + "x" * 353,
                                            uncertainty="u" * 180) for n in (1, 2)]})
        before = self.canonical()
        # Inspect the real initial page to derive the boundary; do not hard-code
        # a budget that depends on serialized metadata or package revisions.
        probe = r'''
import copy, io, json
from contextlib import redirect_stdout
from fist_kb._engine import kb
original = kb.bounded_page
events = []
def observed(base, key, items, args, **kwargs):
    event = {"budget": args.budget}
    events.append(event)
    try:
        result = original(base, key, items, args, **kwargs)
    except kb.KBError as error:
        event["error"] = error.details.get("code")
        raise
    event["page"] = copy.deepcopy(result)
    return result
kb.bounded_page = observed
common = ["context", "--brief", "--no-core", "--topic", "needle", "--compact"]
with redirect_stdout(io.StringIO()):
    kb.main([*common, "--limit", "1", "--budget", "20000"])
first = events[0]["page"]
assert len(first["records"]) == 1 and first["available_count"] == 2
budget = kb.estimate_tokens(first) + 80
events.clear()
output = io.StringIO()
with redirect_stdout(output):
    kb.main([*common, "--limit", "10", "--budget", str(budget)])
result = json.loads(output.getvalue())
assert result["records"] == first["records"], "Optional hints displaced the fitting first record"
assert result["returned_count"] == 1 and result["available_count"] == 2
assert result["next_offset"] == 1 and kb.estimate_tokens(result) <= budget
assert any(event.get("error") == "item_too_large" and event["budget"] < budget for event in events), events
try:
    with redirect_stdout(io.StringIO()):
        kb.main([*common, "--limit", "10", "--budget", str(budget - 1)])
except kb.KBError as error:
    assert error.details.get("code") == "item_too_large", error.details
else:
    raise AssertionError("A page below the actual first-item budget unexpectedly succeeded")
print(json.dumps({"ok": True, "boundary_budget": budget}))
'''
        env = {**self.env, "FIST_KB_ROOT": str(self.kb)}
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", probe], cwd=self.host,
                                env=env, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(self.canonical(), before)

    def test_overlong_current_statement_warns_without_changing_canonical_value(self):
        statement = "The current answer remains qualified. " * 15
        self.apply({"upsert_records": [fact(current_resolution={"statement": statement})]})
        warnings = self.native("validate", "--compact")["warnings"]
        self.assertTrue(any("current_resolution" in warning and "FACT-0001" in warning for warning in warnings))
        self.assertEqual(self.native("get", "FACT-0001", "--compact")["value"]["current_resolution"]["statement"], statement)

    def test_source_preparation_is_explicit_metadata_reads_do_not_reopen_original(self):
        original = self.project / "observation.md"
        data = b"# Synthetic source\n\nThe sample component uses a turquoise label.\n"
        original.write_bytes(data)
        self.apply({"upsert_sources": [{"source_id": "SRC-0001", "title": "Synthetic observation",
                    "locator": {"type": "file", "path": str(original)}, "sha256": digest(data), "size_bytes": len(data)}],
                    "upsert_records": [fact(source_refs=["SRC-0001"])]})
        before = self.canonical()
        self.native("prepare", "inspect", original, "--compact")
        self.assertEqual(self.canonical(), before)
        self.native("prepare", "prepare", "SRC-0001", "--actor", "Synthetic regression", "--compact")
        self.assertEqual(original.read_bytes(), data)
        self.assertTrue(self.native("prepare", "verify", "SRC-0001", "--compact")["ok"])
        original.write_bytes(b"Changed synthetic source, requiring a new source identity.\n")
        before = snapshot(self.project)
        metadata = self.native("guest", "get", "--id", "SRC-0001")["result"]
        self.assertTrue(metadata["metadata_only"])
        self.assertFalse(metadata["prepared_content_accessed"])
        self.assertEqual(snapshot(self.project), before)
        self.native("prepare", "verify", "SRC-0001", "--compact", ok=False)

    def test_checkpoint_compares_exact_record_fingerprints_without_writes(self):
        self.apply({"upsert_records": [fact()]})
        record = self.native("get", "FACT-0001", "--compact")["value"]
        checkpoint = self.kb / "checkpoint.json"
        checkpoint.write_bytes(json_bytes({"output_kb_revision": self.core()["kb_revision"],
            "fingerprint_convention": "canonical-json-sha256-v1",
            "post_pass_record_fingerprints": {"FACT-0001": digest(json_bytes(record))},
            "verification": {"source_catalog_sha256": digest((self.kb / "sources.json").read_bytes())}}))
        same = self.native("checkpoint", "--checkpoint", checkpoint, "--compact")
        self.assertEqual(same["records_agreement"], "same")
        self.apply({"upsert_records": [fact(statement="Changed current finding")]})
        before = snapshot(self.project)
        changed = self.native("checkpoint", "--checkpoint", checkpoint, "--metadata", "--compact")
        self.assertEqual(changed["counts"]["modified"], 1)
        self.assertEqual(changed["source_catalog_agreement"], "same")
        self.assertEqual(snapshot(self.project), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
