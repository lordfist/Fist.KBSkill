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


def new(kind="observed_fact", title="Observation", statement="A synthetic observation.", **fields):
    return {"new": {"kind": kind, "title": title, "statement": statement, **fields}}


STATE = ("core.json", "records.jsonl", "journal.jsonl", "sources.json")


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
        for name in ("FIST_KB_ROOT", "KB_ACTOR", "SEED_KB_TEST_FAULT"):
            self.env.pop(name, None)
        self.env.update(PYTHONDONTWRITEBYTECODE="1", TMP=str(self.host_temp), TEMP=str(self.host_temp),
                        TMPDIR=str(self.host_temp))
        package = importlib.util.find_spec("fist_kb")
        self.assertIsNotNone(package, "Install the package or set PYTHONPATH to src")
        self.package_root = Path(next(iter(package.submodule_search_locations))).resolve()
        self.package_before = snapshot(self.package_root)
        self.command("init", self.project, "--purpose", "Synthetic regression KB", "--owner", "Tester")

    def tearDown(self):
        self.assertEqual(snapshot(self.package_root), self.package_before,
                         "A CLI operation changed installed package files or created bytecode")

    def process(self, *args, env=None, input=None):
        return subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "fist_kb", *map(str, args)],
                              cwd=self.host, env=env or self.env, capture_output=True,
                              encoding="utf-8", timeout=60, input=input)

    def command(self, *args, ok=True, env=None, input=None):
        result = self.process(*args, env=env, input=input)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        try:
            return json.loads(result.stdout)
        except ValueError:
            self.fail(f"CLI did not return JSON: {result.stdout!r}; stderr={result.stderr!r}")

    def native(self, *args, root=None, ok=True, env=None):
        return self.command("--kb", root or self.kb, *args, ok=ok, env=env)

    def read(self, request=None, root=None, ok=True):
        return self.command("--kb", root or self.kb, "read", "-", ok=ok, input=json.dumps(request or {}))

    def write(self, request, root=None, ok=True):
        return self.command("--kb", root or self.kb, "write", "-", ok=ok,
                            input=json.dumps({"by": "Synthetic regression", **request}))

    def revision(self, root=None):
        return json.loads(((root or self.kb) / "core.json").read_bytes())["revision"]

    def state(self, root=None):
        return {name: ((root or self.kb) / name).read_bytes() for name in STATE}

    def test_empty_init_is_project_data_only_and_valid(self):
        core = json.loads((self.kb / "core.json").read_bytes())
        self.assertEqual((core["schema"], core["revision"]), ("seed-kb-8", 1))
        self.assertEqual(core["binding"]["root"], str(self.project.resolve()))
        self.assertEqual((core["purpose"], core["owner"]), ("Synthetic regression KB", "Tester"))
        self.assertEqual({p.name for p in self.project.iterdir()}, {"KnowledgeBase"})
        self.assertEqual({p.name for p in self.kb.iterdir()}, set(STATE))
        self.assertTrue(self.native("admin", "validate")["ok"])
        self.assertEqual(self.read({"query": "absent"})["matches"], 0)

    def test_explicit_selection_or_environment_is_required(self):
        before = snapshot(self.project)
        refused = self.command("read", ok=False)
        self.assertEqual(refused["error"]["code"], "invocation_failed")
        orientation = self.command("read", env={**self.env, "FIST_KB_ROOT": str(self.kb)})
        self.assertTrue(orientation["ok"])
        self.assertEqual(orientation["mode"], "orientation")
        self.assertEqual(snapshot(self.project), before)

    def test_package_tool_identity_does_not_require_seed_provenance(self):
        before = snapshot(self.project)
        result = self.native("admin", "verify-tools")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(result["package"]["files_checked"], 10)
        self.assertEqual(result["kb_root"], str(self.kb.resolve()))
        self.assertEqual(snapshot(self.project), before)

    def test_init_refuses_existing_kb_without_replacing_any_bytes(self):
        self.write({"changes": [new(statement="Already owned knowledge")]})
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

    def test_preview_is_read_only_until_confirmed_and_a_changed_kb_refuses_it(self):
        before = self.state()
        preview = self.write({"changes": [new(statement="Exact prospective value")], "preview": True})
        self.assertEqual(preview["outcome"], "preview")
        self.assertIn("Exact prospective value", json.dumps(preview))
        self.assertEqual(self.state(), before)
        self.write({"changes": [new(title="Intervening", statement="Another writer")]})
        stale = self.write({"confirm": preview["preview_id"]}, ok=False)
        self.assertEqual(stale["outcome"], "not_written")
        preview = self.write({"changes": [new(statement="Exact prospective value")], "preview": True})
        confirmed = self.write({"confirm": preview["preview_id"]})
        self.assertEqual((confirmed["outcome"], confirmed["revision"]), ("committed", 3))
        found = self.read({"ids": ["FACT-0002"]})["results"][0]
        self.assertEqual(found["statement"], "Exact prospective value")

    def test_held_write_lock_blocks_writes_and_prepare_but_not_reads(self):
        source = self.project / "source.md"
        source.write_bytes(b"# Synthetic source\nA small observation for lock testing.\n")
        self.write({"changes": [{"source": {"locator": {"type": "file", "path": str(source)}}}, new()]})
        lock = self.kb / ".write.lock"
        held = json_bytes({"pid": os.getpid(), "token": "synthetic-test-owner"})
        lock.write_bytes(held)
        before = self.state()
        self.assertTrue(self.read()["ok"])
        self.assertTrue(self.native("admin", "validate")["ok"])
        refused = self.write({"changes": [{"log": {"id": "FACT-0001", "text": "Blocked note"}}]}, ok=False)
        self.assertEqual((refused["outcome"], refused["error"]["code"]), ("not_written", "write_locked"))
        refused_preview = self.write({"changes": [new()], "preview": True}, ok=False)
        self.assertEqual(refused_preview["error"]["code"], "write_locked")
        refused_prepare = self.native("prepare", "SRC-0001", "--actor", "Synthetic regression", "--compact", ok=False)
        self.assertEqual(refused_prepare["error"]["code"], "write_locked")
        self.assertEqual(lock.read_bytes(), held)
        self.assertEqual(self.state(), before)
        # This test owns this synthetic lock; removing it is not stale-lock recovery.
        lock.unlink()
        self.write({"changes": [{"log": {"id": "FACT-0001", "text": "Allowed note"}}]})
        self.assertFalse(lock.exists(), "A successful write left its own write lock")
        self.native("prepare", "SRC-0001", "--actor", "Synthetic regression", "--compact")
        self.assertFalse(lock.exists(), "Successful source preparation left its own write lock")

    def test_replaced_values_go_to_history_and_long_logs_page_back_whole(self):
        notes = [f"Note {number} " + "detail " * 300 for number in range(7)]
        self.write({"changes": [new(statement="First answer")]
                    + [{"log": {"id": "FACT-0001", "text": note}} for note in notes]})
        revision = self.revision()
        self.write({"changes": [{"set": {"id": "FACT-0001", "fields": {"statement": "Corrected answer"}}}],
                    "expect_revision": revision})
        stale = self.write({"changes": [{"set": {"id": "FACT-0001", "fields": {"statement": "Stale edit"}}}],
                            "expect_revision": revision}, ok=False)
        self.assertEqual(stale["error"]["code"], "stale_target")
        result, parts = self.read({"ids": ["FACT-0001"], "log": "all", "history": True, "budget": 600}), []
        while True:
            self.assertTrue(result["ok"], result)
            parts.extend(result["results"])
            if result["complete"]:
                break
            result = self.read({"continue": result["continue"]})
        entries = {}
        for part in parts:
            if "log" in part:
                entries.setdefault(part["log"], []).append(part.get("text", ""))
        self.assertEqual(["".join(chunks) for _, chunks in sorted(entries.items())], notes)
        history = [part for part in parts if "history" in part]
        self.assertEqual([(part["field"], part["value"]) for part in history], [("statement", "First answer")])
        current = [part for part in parts if part.get("id") == "FACT-0001" and "statement" in part]
        self.assertEqual(current[0]["statement"], "Corrected answer")

    def test_route_lead_keeps_authored_order(self):
        self.write({"changes": [new(title="First record", ref="a", topics=["guide"]),
                                new(title="Second record", ref="b", topics=["guide"]),
                                {"core": {"route": {"topic": "guide", "about": "Synthetic guide", "lead": ["$b", "$a"]}}}],
                    "expect_revision": 1})
        view = self.read({"topic": "guide"})
        self.assertEqual([part["id"] for part in view["results"] if "id" in part][:2], ["FACT-0002", "FACT-0001"])

    def test_two_roots_and_unrelated_working_directory_stay_isolated(self):
        other = self.area / "Second project"
        other.mkdir()
        self.command("init", other)
        other_kb = other / "KnowledgeBase"
        self.write({"changes": [new(statement="Project alpha only")]})
        self.write({"changes": [new(statement="Project beta only")]}, root=other_kb)
        beta_before = self.state(other_kb)
        self.assertEqual(self.read({"ids": ["FACT-0001"]})["results"][0]["statement"], "Project alpha only")
        self.assertEqual(self.read({"ids": ["FACT-0001"]}, root=other_kb)["results"][0]["statement"],
                         "Project beta only")
        self.write({"changes": [{"set": {"id": "FACT-0001", "fields": {"statement": "Project alpha changed"}}}],
                    "expect_revision": 2})
        self.assertEqual(self.state(other_kb), beta_before)
        self.assertEqual(list(self.host.iterdir()), [])

    def test_working_directory_modules_do_not_shadow_engine_imports(self):
        # A console script or MCP server keeps its working directory off sys.path, but a
        # `python -m` child would prepend it. A project folder holding json.py must not
        # replace the standard library in any CLI, API or guest child process.
        (self.host / "json.py").write_text('raise SystemExit("planted json.py was imported")\n', encoding="utf-8")

        def run(*args, env=None, input=None):
            result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-P", *map(str, args)],
                                    cwd=self.host, env=env or self.env, capture_output=True,
                                    encoding="utf-8", timeout=60, input=input)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("planted", result.stdout + result.stderr)
            return json.loads(result.stdout)

        self.assertTrue(run("-m", "fist_kb", "--kb", self.kb, "admin", "validate")["ok"])
        written = run("-m", "fist_kb", "--kb", self.kb, "write", "-",
                      input=json.dumps({"by": "Synthetic regression", "changes": [new()]}))
        self.assertEqual(written["outcome"], "committed")
        api = run("-c", "import json, sys\nfrom fist_kb.api import KnowledgeBase\nkb = KnowledgeBase(sys.argv[1])\n"
                  "w = kb.write({'by': 'Synthetic regression', 'changes': [{'log': {'id': 'FACT-0001', 'text': 'x'}}]})\n"
                  "print(json.dumps({'write': w, 'read': kb.read({'ids': ['FACT-0001']})}))", self.kb)
        self.assertEqual(api["write"]["outcome"], "committed", api)
        self.assertEqual(api["read"]["revision"], 3)
        self.assertTrue(run("-m", "fist_kb", "--kb", self.kb, "guest", "info")["ok"])

    def test_data_tools_directory_is_never_used_as_runtime_code(self):
        self.write({"changes": [new(statement="Package implementation is selected")]})
        data_tools = self.kb / "tools"
        data_tools.mkdir()
        sentinel = data_tools / "kb_store.py"
        sentinel.write_text("This is synthetic data, not valid Python source.\n", encoding="utf-8")
        before = sentinel.read_bytes()
        self.assertEqual(self.read({"ids": ["FACT-0001"]})["results"][0]["statement"],
                         "Package implementation is selected")
        self.assertEqual(self.write({"changes": [{"log": {"id": "FACT-0001", "text": "Still the package"}}]})["outcome"],
                         "committed")
        self.assertTrue(self.native("guest", "info")["ok"])
        self.assertEqual(sentinel.read_bytes(), before)

    def test_copy_does_not_silently_rebind_an_existing_kb(self):
        target = self.area / "Copied project"
        target.mkdir()
        copy_kb = target / "KnowledgeBase"
        shutil.copytree(self.kb, copy_kb)
        before = snapshot(copy_kb)
        refused = self.native("admin", "validate", root=copy_kb, ok=False)
        self.assertTrue(any("binding.root" in error for error in refused["errors"]), refused)
        self.assertEqual(self.read(root=copy_kb, ok=False)["error"]["code"], "invalid_state")
        written = self.command("--kb", copy_kb, "write", "-", ok=False,
                               input=json.dumps({"by": "Synthetic regression", "changes": [new()]}))
        self.assertEqual(written["outcome"], "not_written")
        self.assertEqual(snapshot(copy_kb), before)

    def test_guest_reads_are_fingerprinted_and_write_nothing(self):
        self.write({"changes": [new(statement="Guest-visible observation", limits="Still part of the fingerprint")]})
        stored = self.native("admin", "export")["records"][0]
        before = snapshot(self.project)
        info = self.native("guest", "info")
        found = self.native("guest", "read", json.dumps({"query": "Guest-visible"}))
        selected = self.native("guest", "read", json.dumps({"ids": ["FACT-0001"], "fields": ["statement"]}))
        self.assertTrue(info["ok"])
        self.assertEqual((info["interface_version"], info["owner"], info["kb_revision"]), (2, "Tester", 2))
        self.assertEqual(info["kb_root"], str(self.kb.resolve()))
        self.assertEqual(selected["result"]["results"][0]["statement"], "Guest-visible observation")
        expected = digest(json_bytes(stored))
        self.assertEqual(selected["record_fingerprints"]["FACT-0001"], expected)
        self.assertEqual(found["record_fingerprints"]["FACT-0001"], expected)
        self.assertEqual(snapshot(self.project), before)

    def test_guest_refusals_are_distinct_and_change_nothing(self):
        before = self.state()
        self.native("guest", "write", "{}", ok=False)
        invalid = self.native("guest", "read", "not json", ok=False)
        self.assertEqual(invalid["result"]["error"]["category"], "invalid_request")
        unknown = self.native("guest", "read", json.dumps({"frobnicate": True}), ok=False)
        self.assertEqual(unknown["result"]["error"]["code"], "request_invalid")
        self.assertEqual(self.state(), before)
        self.write({"changes": [{"core": {"guest_reads": False}}], "expect_revision": 1})
        disabled = self.native("guest", "info", ok=False)
        self.assertEqual(disabled["result"]["error"]["code"], "guest_access_disabled")
        path = self.kb / "records.jsonl"
        path.write_bytes(path.read_bytes() + b"\n")
        inconsistent = self.state()
        result = self.native("guest", "info", ok=False)
        self.assertEqual(result["result"]["error"]["category"], "inconsistent_state")
        self.assertEqual(self.state(), inconsistent)

    def test_source_preparation_is_explicit_and_bound_to_registered_bytes(self):
        original = self.project / "observation.md"
        data = b"# Synthetic source\n\nThe sample component uses a turquoise label.\n"
        original.write_bytes(data)
        self.write({"changes": [{"source": {"ref": "s", "locator": {"type": "file", "path": str(original)},
                                            "title": "Synthetic observation"}},
                                new(statement="The label colour was observed.", sources=["$s"])]})
        before = self.state()
        self.native("prepare", "inspect", original, "--compact")
        self.assertEqual(self.state(), before)
        prepared = self.native("prepare", "prepare", "SRC-0001", "--actor", "Synthetic regression", "--compact")
        self.assertEqual(prepared["outcome"], "committed", prepared)
        self.assertEqual(original.read_bytes(), data)
        self.assertTrue(self.native("prepare", "verify", "SRC-0001", "--compact")["ok"])
        evidence = [part for part in self.read({"query": "turquoise"})["results"] if "evidence" in part]
        self.assertEqual(evidence[0]["evidence"], "SRC-0001")
        original.write_bytes(b"Changed synthetic source, requiring a new source version.\n")
        self.native("prepare", "verify", "SRC-0001", "--compact", ok=False)
        deep = self.native("admin", "validate", "--deep", ok=False)
        self.assertEqual(deep["sources"]["changed"], ["SRC-0001"])

    def test_sources_outside_the_binding_root_are_refused(self):
        outside = self.area / "outside.txt"
        outside.write_text("Not inside the project.\n", encoding="utf-8")
        before = self.state()
        refused = self.write({"changes": [{"source": {"locator": {"type": "file", "path": str(outside)}}}]}, ok=False)
        self.assertEqual(refused["outcome"], "not_written")
        self.assertEqual(self.state(), before)
        inspection = self.native("prepare", "inspect", outside, "--compact", ok=False)
        self.assertFalse(inspection["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
