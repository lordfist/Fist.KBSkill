"""Engine tests (operations, history, paging, revisions, faults, request IDs, tool identity, orientation, migration,
sources and guest reads) through the public fist-kb CLI.

Each test initializes a disposable KB with `fist-kb init` and calls `fist-kb --kb KB read|write|admin|prepare|guest`
as a separate process, the way a host does; requests travel on stdin. Cleanup is registered at creation.
Run with `python -m unittest discover -s tests` (set PYTHONPATH to src for a source checkout).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "fist_kb"


def temporary_parent() -> Path:
    return Path(os.environ.get("FIST_KB_TEST_TMP", Path(__file__).resolve().parent)).resolve()


class KB:
    def __init__(self, test: unittest.TestCase, init: bool = True):
        temp = tempfile.TemporaryDirectory(prefix="fist-kb-engine-", dir=temporary_parent())
        test.addCleanup(self._cleanup, temp)
        self.root = Path(temp.name) / "root"
        self.root.mkdir()
        self.kb = self.root / "KnowledgeBase"
        self.env = {**os.environ, "KB_ACTOR": "test", "PYTHONDONTWRITEBYTECODE": "1"}
        self.env.pop("SEED_KB_TEST_FAULT", None)
        self.env.pop("FIST_KB_ROOT", None)
        self.seen: int | None = None
        if init:
            result = self.run("init", self.root, "--purpose", "Synthetic test KB", "--owner", "Owner")
            assert result["ok"], result
            self.seen = 1
        else:
            self.kb.mkdir()

    @staticmethod
    def _cleanup(temp: tempfile.TemporaryDirectory) -> None:
        path = Path(temp.name)
        temp.cleanup()
        assert not path.exists()

    def run(self, *args, env: dict[str, str] | None = None, input: str | None = None) -> dict:
        process = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "fist_kb", *map(str, args)],
                                 capture_output=True, text=True, encoding="utf-8", env=env or self.env,
                                 input=input, timeout=120)
        assert process.returncode in {0, 1}, process.stdout + process.stderr
        return json.loads(process.stdout)

    def cli(self, *args, env: dict[str, str] | None = None) -> dict:
        return self.run("--kb", self.kb, *args, env=env)

    def read(self, request: dict | None = None) -> dict:
        result = self.run("--kb", self.kb, "read", "-", input=json.dumps(request or {}))
        self.seen = result.get("revision", self.seen)
        return result

    def write(self, request: dict, env: dict[str, str] | None = None, host: bool = True) -> dict:
        """Like a host: pass the last revision returned to this caller, never simply the latest."""
        if host and self.seen is not None and "confirm" not in request:
            request = {"expect_revision": self.seen, **request}
        result = self.run("--kb", self.kb, "write", "-", env=env, input=json.dumps(request))
        if result.get("outcome") in {"committed", "preview"}:
            self.seen = result.get("revision", self.seen)
        return result

    def engine_script(self, script: str, *args: str) -> dict:
        """Run Python against this KB's engine modules in a child process (for fault and check substitution)."""
        process = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script, *args], capture_output=True,
                                 text=True, encoding="utf-8", env={**self.env, "FIST_KB_ROOT": str(self.kb)},
                                 timeout=120)
        assert process.returncode == 0, process.stdout + process.stderr
        return json.loads(process.stdout)

    def state_bytes(self) -> dict[str, bytes]:
        return {name: (self.kb / name).read_bytes() for name in ("core.json", "records.jsonl", "journal.jsonl", "sources.json")}

    def all_parts(self, request: dict) -> list[dict]:
        parts, result = [], self.read(request)
        while True:
            assert result["ok"], result
            parts.extend(result["results"])
            if result["complete"]:
                return parts
            result = self.read({"continue": result["continue"]})


def changed_package(test: unittest.TestCase, kb: KB, name: str = "kb_read.py") -> dict[str, str]:
    """A disposable copy of the package with one engine file changed; returns an environment that runs it."""
    temp = tempfile.TemporaryDirectory(prefix="fist-kb-package-", dir=temporary_parent())
    test.addCleanup(KB._cleanup, temp)
    copy = Path(temp.name) / "fist_kb"
    shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    with (copy / "_engine" / name).open("a", encoding="utf-8") as handle:
        handle.write("\n# changed\n")
    return {**kb.env, "PYTHONPATH": os.pathsep.join([temp.name, *filter(None, [kb.env.get("PYTHONPATH")])])}


def new(kind: str, title: str, statement: str, **fields) -> dict:
    return {"new": {"kind": kind, "title": title, "statement": statement, **fields}}


class OperationsTest(unittest.TestCase):
    """Every operation works; every refusal names its code and hint and leaves the state bytes unchanged."""

    def test_operations(self):
        kb = KB(self)
        result = kb.write({"changes": [
            new("task", "Import supplier files", "Import the September supplier CSV files.", ref="t", step="parse",
                next="map columns", stop="report to the owner"),
            new("owner_decision", "Semicolons", "Supplier files use semicolons.", ref="d", quote="They use ;",
                topics=["suppliers"]),
            new("open_question", "Encoding?", "Which encoding do the files use?", ref="q", options=["UTF-8", "CP1250"]),
            {"set": {"id": "$t", "fields": {"rules": ["$d"]}}},
            {"core": {"current_task": "$t"}},
            {"core": {"route": {"topic": "suppliers", "about": "supplier imports", "lead": ["$d"]}}},
            {"core": {"pin": ["$d"]}},
            {"log": {"id": "$t", "text": "Parsed 12 files."}},
            {"add": {"id": "$t", "field": "topics", "items": ["suppliers", "imports"]}},
        ]})
        self.assertEqual(result["outcome"], "committed", result)
        self.assertEqual(result["ids"], {"t": "TASK-0001", "d": "DEC-0001", "q": "Q-0001"})
        result = kb.write({"changes": [
            new("owner_decision", "Comma after all", "Supplier files use commas.", ref="n"),
            {"status": {"id": "DEC-0001", "status": "superseded", "by": "$n", "reason": "Owner corrected it."}},
            {"remove": {"id": "TASK-0001", "field": "topics", "items": ["imports", "absent"]}},
            {"set": {"id": "Q-0001", "fields": {"resolution": {"note": "UTF-8", "ids": ["$n"]}}}},
            {"status": {"id": "Q-0001", "status": "resolved"}},
        ]})
        self.assertEqual(result["outcome"], "committed", result)
        self.assertIn("no quote", " ".join(result["warnings"]))
        removed = [c for c in result["changes"] if c["op"] == "remove"][0]
        self.assertEqual(removed["skipped"], ["absent"])
        records = {row["id"]: row for row in kb.cli("admin", "export")["records"]}
        self.assertEqual(records["DEC-0001"]["superseded_by"], "DEC-0002")
        self.assertEqual(records["DEC-0002"]["supersedes"], ["DEC-0001"])
        self.assertEqual(records["TASK-0001"]["topics"], ["suppliers"])
        source_file = kb.root / "evidence.csv"
        source_file.write_text("a;b\n", encoding="utf-8")
        result = kb.write({"changes": [{"source": {"ref": "s", "locator": {"type": "file", "path": str(source_file)}}},
                                       new("observed_fact", "Header", "The header row is a;b.", sources=["$s"])]})
        self.assertEqual(result["ids"]["s"], "SRC-0001", result)
        self.assertEqual(len(result["changes"][0]["sha256"]), 64)
        self.assertTrue(kb.cli("admin", "validate")["ok"])
        self.assertEqual(kb.cli("admin", "validate", "--deep")["sources"]["unchanged"], ["SRC-0001"])
        source_file.write_text("a;b;c\n", encoding="utf-8")
        deep = kb.cli("admin", "validate", "--deep")
        self.assertEqual((deep["ok"], deep["sources"]["changed"]), (False, ["SRC-0001"]))
        self.assertIn("registration", kb.read({"ids": ["SRC-0001"]})["results"][0]["note"])
        fact = [row for row in kb.cli("admin", "export")["records"] if row["id"] == "FACT-0001"][0]
        self.assertEqual((fact["origin"], fact["created_by"]), ({"type": "observation"}, "test"))

    def test_refusals_leave_state_unchanged(self):
        kb = KB(self)
        kb.write({"changes": [new("observed_fact", "A fact", "Something observed.")]})
        before = kb.state_bytes()
        cases = [
            ({"set": {"id": "FACT-0001", "fields": {"notes_20261029": "x"}}}, "field_not_allowed"),
            ({"set": {"id": "FACT-0001", "fields": {"statement": "x" * 3001}}}, "field_too_large"),
            ({"set": {"id": "FACT-0001", "fields": {"status": "open"}}}, "field_protected"),
            ({"status": {"id": "FACT-0001", "status": "open"}}, "status_not_allowed"),
            ({"set": {"id": "$nope", "fields": {"title": "x"}}}, "unknown_ref"),
            ({"log": {"id": "FACT-0002", "text": "x"}}, "unknown_id"),
            ({"frobnicate": {}}, "operation_invalid"),
            ({"set": {"id": "FACT-0001", "fields": {"related": ["DEC-0009"]}}}, "value_invalid"),
            (new("observed_fact", "No statement", ""), "value_invalid"),
            ({"core": {"schema": "x"}}, "operation_invalid"),
        ]
        for change, code in cases:
            with self.subTest(code=code):
                result = kb.write({"changes": [change]})
                self.assertEqual(result["outcome"], "not_written", result)
                self.assertEqual(result["error"]["code"], code, result)
                self.assertEqual(kb.state_bytes(), before)
        near = kb.write({"changes": [{"log": {"id": "FACT-0002", "text": "x"}}]})["error"]
        self.assertEqual(near["nearest"], ["FACT-0001"])
        hint = kb.write({"changes": [{"set": {"id": "FACT-0001", "fields": {"notes_20261029": "x"}}}]})["error"]["hint"]
        self.assertIn("log entry", hint)
        unchanged = kb.write({"changes": [{"set": {"id": "FACT-0001", "fields": {"title": "A fact"}}}]})
        self.assertEqual(unchanged["error"]["code"], "no_change")
        self.assertEqual(kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "x"}}]}, env={
            k: v for k, v in kb.env.items() if k != "KB_ACTOR"})["error"]["code"], "actor_required")


class NoSilentLossTest(unittest.TestCase):
    """Every replaced or removed value is in history, and restore returns its exact bytes."""

    def test_history_and_restore(self):
        kb = KB(self)
        first = "Long statement ✓ " + "x" * 2500
        kb.write({"changes": [new("recommendation", "Lesson", first, ref="r", action="Do A", topics=["one", "two"])]})
        result = kb.write({"changes": [{"set": {"id": "REC-0001", "fields": {"statement": first.replace("✓", "✗"),
                                                                             "action": None}}}]})
        diff = result["changes"][0]["stored"]["diff"]
        self.assertEqual((diff["removed"], diff["added"]), ("✓", "✗"))
        kb.write({"changes": [{"remove": {"id": "REC-0001", "field": "topics", "items": ["one"]}},
                              {"status": {"id": "REC-0001", "status": "historical"}}]})
        history = kb.all_parts({"ids": ["REC-0001"], "history": True})
        values = {(part["field"], json.dumps(part["value"], ensure_ascii=False)) for part in history if "history" in part}
        self.assertIn(("statement", json.dumps(first, ensure_ascii=False)), values)
        self.assertIn(("action", '"Do A"'), values)
        self.assertIn(("topics", '["one", "two"]'), values)
        self.assertIn(("status", '"active"'), values)
        number = [part["history"] for part in history if part.get("field") == "statement"][0]
        kb.write({"changes": [{"restore": {"id": "REC-0001", "history": number}}]})
        records = {row["id"]: row for row in kb.cli("admin", "export")["records"]}
        self.assertEqual(records["REC-0001"]["statement"], first)


class ReadsNeverFailTest(unittest.TestCase):
    """Every byte of every record is reachable at small budgets, including imported over-limit records."""

    def test_large_and_imported_records(self):
        kb = KB(self)
        kb.write({"changes": [new("observed_fact", "Big", "s" * 2900, ref="f", evidence="e" * 1900, limits="l" * 1900)]
                  + [{"log": {"id": "$f", "text": f"note {n} " + "n" * 3000}} for n in range(30)]})
        # Simulate a migrated record far over the limits: write it directly and recommit the hashes.
        export = kb.cli("admin", "export")
        huge = {**export["records"][0], "id": "FACT-0002", "statement": "H" * 100_000 + "END"}
        rows = export["records"] + [huge]
        records = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows).encode()
        (kb.kb / "records.jsonl").write_bytes(records)
        core = json.loads((kb.kb / "core.json").read_text(encoding="utf-8"))
        core["state_files"]["records"] = hashlib.sha256(records).hexdigest().upper()
        (kb.kb / "core.json").write_text(json.dumps(core, indent=2) + "\n", encoding="utf-8")
        self.assertTrue(kb.cli("admin", "validate")["ok"])
        self.assertTrue(any("over limit" in w for w in kb.cli("admin", "validate")["warnings"]))
        for budget in (600, 4000):
            with self.subTest(budget=budget):
                parts = kb.all_parts({"ids": ["FACT-0002"], "budget": budget})
                text = "".join(part.get("text", "") for part in parts if part.get("field") == "statement")
                whole = [part["statement"] for part in parts if "statement" in part]
                self.assertEqual((text or (whole[0] if whole else "")), "H" * 100_000 + "END")
                logs = kb.all_parts({"ids": ["FACT-0001"], "log": "all", "budget": budget})
                entries = {}
                for part in logs:
                    if "log" in part:
                        entries.setdefault(part["log"], []).append(part.get("text", ""))
                self.assertEqual(len(entries), 30)
                self.assertTrue(all("".join(chunks).endswith("n" * 3000) for chunks in entries.values()))


class RevisionSafetyTest(unittest.TestCase):
    """Stale revisions, continuations and previews refuse instead of writing or mixing revisions."""

    def test_revision_continuation_preview(self):
        kb = KB(self)
        kb.write({"changes": [new("observed_fact", f"Fact {n}", "x" * 900) for n in range(12)]})
        required = kb.write({"changes": [{"set": {"id": "FACT-0001", "fields": {"title": "x"}}}]}, host=False)
        self.assertEqual(required["error"]["code"], "revision_required")
        additive = kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "x"}}]}, host=False)
        self.assertEqual(additive["outcome"], "committed")
        based = kb.seen
        kb.write({"changes": [{"set": {"id": "FACT-0002", "fields": {"title": "changed by another writer"}}}]})
        stale = kb.write({"changes": [{"set": {"id": "FACT-0002", "fields": {"title": "my stale edit"}}}],
                          "expect_revision": based})
        self.assertEqual(stale["error"]["code"], "stale_target")
        self.assertEqual(stale["error"]["written_since"][0]["rev"], based + 1)
        unrelated = kb.write({"changes": [{"set": {"id": "FACT-0003", "fields": {"title": "fine"}}}],
                              "expect_revision": based})
        self.assertEqual(unrelated["outcome"], "committed")
        page = kb.read({"kinds": ["observed_fact"], "budget": 600})
        self.assertFalse(page["complete"])
        preview = kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "previewed"}}], "preview": True})
        self.assertEqual(preview["outcome"], "preview")
        before = kb.state_bytes()
        self.assertEqual(kb.state_bytes(), before)
        other = kb.write({"changes": [{"log": {"id": "FACT-0002", "text": "intervening"}}]})
        self.assertEqual(other["outcome"], "committed")
        self.assertEqual(kb.read({"continue": page["continue"]})["error"]["code"], "revision_changed")
        self.assertEqual(kb.write({"confirm": preview["preview_id"]})["error"]["code"], "preview_unknown")
        preview = kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "previewed"}}], "preview": True})
        confirmed = kb.write({"confirm": preview["preview_id"]})
        self.assertEqual((confirmed["outcome"], confirmed["revision"]), ("committed", preview["would_be_revision"]))
        self.assertEqual(kb.write({"confirm": preview["preview_id"]})["error"]["code"], "preview_unknown")


class CrashOutcomeTest(unittest.TestCase):
    """Injected faults give the documented outcome; nothing retries; a broken state refuses writes."""

    def test_faults(self):
        kb = KB(self)
        kb.write({"changes": [new("observed_fact", "A", "a")]})
        before = kb.state_bytes()
        early = kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "w"}}], "request_id": "early"},
                         env={**kb.env, "SEED_KB_TEST_FAULT": "before:replace"})
        self.assertEqual(early["outcome"], "not_written", early)
        self.assertEqual(kb.state_bytes(), before)
        self.assertFalse(kb.read({"request": "early"})["found"])
        fault = {**kb.env, "SEED_KB_TEST_FAULT": "after:core"}
        result = kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "x"}}]}, env=fault)
        self.assertEqual(result["outcome"], "committed_unverified", result)
        self.assertTrue(kb.read()["ok"])
        fault = {**kb.env, "SEED_KB_TEST_FAULT": "after:records"}
        result = kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "y"}}], "request_id": "r-9"}, env=fault)
        self.assertEqual(result["outcome"], "uncertain", result)
        again = kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "y"}}], "request_id": "r-9"})
        self.assertEqual(again["error"]["code"], "recovery_required")
        self.assertIn("Do not resend", result["next"])
        self.assertEqual(kb.read()["error"]["code"], "recovery_required")
        self.assertEqual(kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "z"}}]})["error"]["code"],
                         "recovery_required")
        self.assertFalse(kb.cli("admin", "recover")["consistent"])


class RequestIdTest(unittest.TestCase):
    """A repeated request_id returns the first outcome and writes nothing."""

    def test_request_id(self):
        kb = KB(self)
        request = {"changes": [{"new": {"kind": "observed_fact", "title": "A", "statement": "a"}}], "request_id": "r-1"}
        first = kb.write(request)
        before = kb.state_bytes()
        again = kb.write(request)
        self.assertTrue(again["repeated"])
        self.assertEqual(again["revision"], first["revision"])
        self.assertEqual(kb.state_bytes(), before)
        self.assertTrue(kb.read({"request": "r-1"})["found"])
        different = kb.write({**request, "changes": [{"new": {"kind": "observed_fact", "title": "B", "statement": "b"}}]})
        self.assertEqual(different["error"]["code"], "request_id_conflict")
        self.assertEqual(kb.state_bytes(), before)
        self.assertFalse(kb.read({"request": "r-2"})["found"])


class ToolIdentityTest(unittest.TestCase):
    """Changed tools refuse writes and warn on reads."""

    def test_changed_tool(self):
        kb = KB(self)
        changed = changed_package(self, kb)
        result = kb.write({"changes": [{"new": {"kind": "observed_fact", "title": "A", "statement": "a"}}]}, env=changed)
        self.assertEqual(result["error"]["code"], "tools_unverified")
        self.assertEqual(result["error"]["files"], ["_engine/kb_read.py"])
        read = kb.run("--kb", kb.kb, "read", env=changed)
        self.assertTrue(read["ok"])
        self.assertEqual(read["tools"]["status"], "mismatch")
        self.assertFalse(kb.cli("admin", "verify-tools", env=changed)["ok"])
        self.assertTrue(kb.cli("admin", "verify-tools")["ok"])


class OrientationAndFindTest(unittest.TestCase):
    """Orientation is small and complete; topic, search, filters and changes work."""

    def test_orientation_and_find(self):
        kb = KB(self)
        words = ["zero", "one", "two", "three", "four", "five", "six", "seven"]
        changes = [new("owner_decision", f"Rule {words[n]}", f"Standing rule {words[n]} about deployments. " + "r" * 400,
                       ref=f"d{n}", topics=["rules"]) for n in range(8)]
        changes += [new("observed_fact", f"Finding {n}", f"Measured result {n} for the parser. " + "f" * 600,
                        topics=["parser"]) for n in range(50)]
        changes += [new("task", "Ship the parser", "Ship the CSV parser to staging.", ref="t", step="review",
                        next="fix the quoting bug", stop="owner approval", rules=["$d0", "$d1"]),
                    {"core": {"current_task": "$t"}}, {"core": {"pin": ["$d0", "$d1"]}},
                    {"core": {"route": {"topic": "rules", "about": "standing rules", "lead": ["$d3"]}}}]
        changes += [{"log": {"id": "$t", "text": f"progress {n}"}} for n in range(6)]
        self.assertEqual(kb.write({"changes": changes})["outcome"], "committed")
        orientation = kb.read()
        self.assertTrue(orientation["complete"])
        self.assertLessEqual(len(json.dumps(orientation, ensure_ascii=False).encode()) / 3, 3500)
        task = [part["current_task"] for part in orientation["results"] if "current_task" in part][0]
        self.assertEqual((task["step"], task["next"], task["stop"]), ("review", "fix the quoting bug", "owner approval"))
        self.assertEqual([rule["title"] for rule in task["rules"]], ["Rule zero", "Rule one"])
        self.assertEqual(len([p for p in orientation["results"] if "log" in p]), 3)
        topic = kb.read({"topic": "rules"})
        self.assertEqual(topic["results"][0]["id"], "DEC-0004")
        self.assertIn("statement", topic["results"][0])
        self.assertTrue(any(part.get("summary") for part in topic["results"]))
        search = kb.read({"query": "deployments rule five"})
        self.assertEqual(search["results"][0]["id"], "DEC-0006")
        within = kb.read({"topic": "parser", "query": "deployments"})
        self.assertEqual(within["matches"], 0)
        self.assertIn("topic parser", within["searched"])
        missing = kb.read({"topic": "parsr"})
        self.assertEqual(missing["nearest_topics"][0], "parser")
        listed = kb.read({"kinds": ["owner_decision"], "limit": 3})
        self.assertEqual((listed["matches"], listed["omitted_matches"]), (8, 5))
        changes = kb.read({"changes_since": 1})
        self.assertEqual(changes["writes"], 1)

    def test_orientation_without_task_says_how_to_set_one(self):
        kb = KB(self)
        first = kb.read()["results"]
        hint = [part for part in first if "current_task" in part][0]
        self.assertIsNone(hint["current_task"])
        self.assertIn("kind", hint["hint"])

    def test_binding_refuses_moved_kb(self):
        kb = KB(self)
        moved = kb.root.parent / "moved"
        shutil.copytree(kb.root, moved)
        self.assertEqual(kb.run("--kb", moved / "KnowledgeBase", "read")["error"]["code"], "invalid_state")


def v07_kb(folder: Path, records: list[dict], core: dict, sources: dict) -> Path:
    """Write a synthetic v07-shaped KnowledgeBase with consistent hashes."""
    folder.mkdir(parents=True)
    records_bytes = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in records).encode()
    sources_bytes = (json.dumps(sources, ensure_ascii=False, indent=2) + "\n").encode()
    (folder / "records.jsonl").write_bytes(records_bytes)
    (folder / "sources.json").write_bytes(sources_bytes)
    core = {**core, "state_files": {"records_jsonl_sha256": hashlib.sha256(records_bytes).hexdigest().upper(),
                                    "sources_json_sha256": hashlib.sha256(sources_bytes).hexdigest().upper()}}
    (folder / "core.json").write_text(json.dumps(core, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return folder


class MigrationTest(unittest.TestCase):
    """Migration is lossless and puts each kind of 0.3.x (v07) content where USAGE.md says."""

    def test_synthetic_v07(self):
        kb = KB(self, init=False)
        records = [
            {"id": "FACT-0001", "kind": "observed_fact", "status": "open", "title": "Current work state",
             "statement": "S" * 12000, "tags": ["Current Plan", "ops"], "uncertainty": "unclear",
             "resolution_notes": [{"note": "first", "observed_at": "2026-10-01"}, {"note": "second", "observed_at": "2026-10-02", "by": "x"}],
             "prior_statement_20261014": "the old statement", "phase_log_20261020": {"step": 3, "ok": True},
             "origin": {"type": "field_observation", "observed_at": "2026-10-01", "extra": "kept"},
             "related_record_ids": ["DEC-0001", "DEC-0001"], "created_at": "2026-10-01T00:00:00+00:00"},
            {"id": "DEC-0001", "kind": "owner_decision", "status": "active", "statement": "Never migrate in place.",
             "quote": "Never in place", "action": ["a", "b"], "no_compat_ever_2026_10_14": "verbatim owner rule",
             "current_resolution": {"note": "still applies", "record_ids": ["FACT-0001"]}},
            {"id": "REC-0001", "kind": "recommendation", "status": "resolved", "title": "Lesson",
             "statement": "Check the base branch.", "limitations": "Linux only", "applicability": "always"},
        ]
        core = {"schema_version": 1, "kb_revision": 7, "experiment": {"root": "/old", "purpose": "Old purpose", "owner": "Owner"},
                "routes": {"current_plan": {"record_ids": ["FACT-0001", "REC-0001"]}}, "pinned_record_ids": ["DEC-0001"],
                "owner_decision_ids": ["DEC-0001"], "next_decision_order": ["Q-0009?"]}
        old = v07_kb(kb.root.parent / "old" / "KnowledgeBase", records, core, {"catalog_revision": 3, "sources": []})
        before = {path.name: path.read_bytes() for path in old.iterdir()}
        result = kb.cli("admin", "migrate-v07", "--from", str(old), "--by", "test")
        self.assertTrue(result["ok"], result)
        report = result["report"]
        self.assertEqual(report["lossless"]["mismatch_count"], 0, report)
        self.assertGreater(report["lossless"]["checked_fields"], 20)
        self.assertEqual({path.name: path.read_bytes() for path in old.iterdir()}, before)
        self.assertIn("FACT-0001: open -> active", report["status_remapped"])
        self.assertIn("REC-0001: resolved -> historical", report["status_remapped"])
        self.assertEqual(report["imported_fields"].get("phase_log_20261020"), 1)
        self.assertEqual(report["task_candidates"], ["FACT-0001"])
        export = kb.cli("admin", "export")
        rows = {row["id"]: row for row in export["records"]}
        self.assertEqual(rows["FACT-0001"]["statement"], "S" * 12000)
        self.assertEqual(rows["FACT-0001"]["limits"], "unclear")
        self.assertEqual(rows["FACT-0001"]["topics"], ["current_plan", "ops"])
        self.assertEqual(rows["FACT-0001"]["related"], ["DEC-0001"])
        self.assertEqual(rows["DEC-0001"]["resolution"], {"note": "still applies", "ids": ["FACT-0001"]})
        self.assertEqual(rows["REC-0001"]["limits"], "Linux only")
        self.assertEqual(export["core"]["routes"]["current_plan"]["lead"], ["FACT-0001", "REC-0001"])
        self.assertEqual(export["core"]["pinned"], ["DEC-0001"])
        logs = [entry for entry in export["journal"] if entry["type"] == "log" and entry["id"] == "FACT-0001"]
        self.assertEqual([entry["text"] for entry in logs if entry["text"] in {"first", "second"}], ["first"])
        history = [entry for entry in export["journal"] if entry["type"] == "history"]
        self.assertEqual(history[0]["field"], "prior_statement_20261014")
        self.assertTrue(kb.cli("admin", "validate")["ok"])
        self.assertTrue(kb.read({"ids": ["FACT-0001"]})["ok"])
        self.assertEqual(kb.cli("admin", "migrate-v07", "--from", str(old), "--by", "test")["error"]["code"],
                         "already_initialized")
        state = kb.state_bytes()
        dry = kb.cli("admin", "migrate-v07", "--from", str(old), "--by", "test", "--dry-run")
        self.assertEqual((dry["ok"], dry["published"], dry["report"]["lossless"]["mismatch_count"]), (True, False, 0))
        self.assertEqual(kb.state_bytes(), state)
        self.assertEqual({path.name: path.read_bytes() for path in old.iterdir()}, before)
        # The migrated KB is writable at once: create the task record and point core at it.
        result = kb.write({"changes": [new("task", "Resume", "Continue the migrated work.", ref="t", rules=["DEC-0001"]),
                                       {"core": {"current_task": "$t"}}]})
        self.assertEqual(result["outcome"], "committed", result)

    def test_lossless_check_blocks_publication(self):
        kb = KB(self, init=False)
        records = [{"id": "Q-0001", "kind": "open_question", "status": "open", "statement": "Which one?",
                    "odd_field": "must survive"}]
        old = v07_kb(kb.root.parent / "old2" / "KnowledgeBase", records,
                     {"kb_revision": 1, "experiment": {"purpose": "p"}}, {"sources": []})
        # Drop every imported-field log entry before the check runs: the check must refuse and publish nothing.
        script = (
            "import sys, json\nfrom fist_kb._engine import kb_migrate as m\n"
            "m.Migration.logged = lambda self, rid, key, label, value, when: None\n"
            "print(json.dumps(m.migrate(sys.argv[1], 'test')))\n")
        result = kb.engine_script(script, str(old))
        self.assertFalse(result["published"])
        self.assertEqual(result["report"]["lossless"]["mismatches"][0]["field"], "odd_field")
        self.assertFalse((kb.kb / "core.json").exists())

    def test_type_and_order_exactness(self):
        kb = KB(self, init=False)
        records = [{"id": "FACT-0001", "kind": "observed_fact", "status": "active", "statement": "x", "count": 1,
                    "flag": True, "ratio": 1.0, "nothing": None, "order": ["b", "a"], "nested": {"k": [1, {"z": 0}]}}]
        old = v07_kb(kb.root.parent / "old3" / "KnowledgeBase", records, {"kb_revision": 1}, {"sources": []})
        script = (
            "import sys, json\nfrom fist_kb._engine import kb_migrate as m\n"
            "real = m.Migration.rebuild\n"
            "def bent(self, state, rid, key, value):\n"
            "    got = real(self, state, rid, key, value)\n"
            "    return 1 if got is True else got\n"
            "if sys.argv[2] == 'bent': m.Migration.rebuild = bent\n"
            "print(json.dumps(m.migrate(sys.argv[1], 'test')))\n")
        run = lambda mode: kb.engine_script(script, str(old), mode)
        bent = run("bent")
        self.assertFalse(bent["published"])
        self.assertEqual(bent["report"]["lossless"]["mismatches"], [{"id": "FACT-0001", "field": "flag",
                                                                     "problem": "value differs"}])
        exact = run("exact")
        self.assertTrue(exact["published"], exact)

    def test_prepared_evidence_is_copied(self):
        kb = KB(self, init=False)
        original = kb.root / "evidence.txt"
        original.write_text("The heron count was 41.\n", encoding="utf-8")
        source_sha = hashlib.sha256(original.read_bytes()).hexdigest().upper()
        prepared = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in (
            {"type": "meta", "source_id": "SRC-0001", "source_sha256": source_sha, "coverage": {"text": "complete"}},
            {"type": "text", "locator": "line:1", "text": "The heron count was 41."})).encode()
        old = kb.root.parent / "old4" / "KnowledgeBase"
        sources = {"sources": [{"source_id": "SRC-0001", "locator": {"type": "file", "path": str(original)},
                                "sha256": source_sha, "preparation": {"status": "prepared", "path": "prepared/SRC-0001.jsonl",
                                "prepared_sha256": hashlib.sha256(prepared).hexdigest().upper()}},
                               {"source_id": "SRC-0002", "locator": {"type": "url", "url": "https://example.invalid/x"},
                                "preparation": {"status": "prepared", "path": "prepared/SRC-0002.jsonl",
                                                "prepared_sha256": "0" * 64}}]}
        v07_kb(old, [], {"kb_revision": 1}, sources)
        (old / "prepared").mkdir()
        (old / "prepared" / "SRC-0001.jsonl").write_bytes(prepared)
        result = kb.cli("admin", "migrate-v07", "--from", str(old), "--by", "test")
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["report"]["prepared_copied"], result["report"]["prepared_not_copied"]),
                         (["SRC-0001"], ["SRC-0002"]))
        evidence = [part for part in kb.read({"query": "heron count"})["results"] if "evidence" in part]
        self.assertEqual(evidence[0]["locator"], "line:1")


class PrepareTest(unittest.TestCase):
    """Source preparation publishes prepared evidence as one revision; search and deep validation use it."""

    def prepare(self, kb: KB, *args: str) -> dict:
        return kb.cli("prepare", *args, "--compact")

    def test_prepare_search_verify(self):
        kb = KB(self)
        data = kb.root / "suppliers.csv"
        data.write_text("supplier;delimiter\nAcme;semicolon\nBeta;comma\n", encoding="utf-8")
        page = kb.root / "notes.html"
        page.write_text("<html><body><p>The quarterly flamingo audit passed.</p><script>alert(1)</script></body></html>",
                        encoding="utf-8")
        result = kb.write({"changes": [{"source": {"locator": {"type": "file", "path": str(data)}}},
                                       {"source": {"locator": {"type": "file", "path": str(page)}}}]})
        self.assertEqual(result["outcome"], "committed", result)
        prepared = self.prepare(kb, "prepare", "SRC-0001", "SRC-0002", "--actor", "test")
        self.assertEqual(prepared["outcome"], "committed", prepared)
        self.assertEqual({row["source_id"] for row in prepared["prepared"]}, {"SRC-0001", "SRC-0002"})
        self.assertEqual(self.prepare(kb, "prepare", "SRC-0001", "--actor", "test")["status"], "NO-OP")
        found = kb.read({"query": "flamingo audit"})
        evidence = [part for part in found["results"] if "evidence" in part]
        self.assertEqual(evidence[0]["evidence"], "SRC-0002", found)
        self.assertNotIn("alert", json.dumps(found))
        self.assertTrue(self.prepare(kb, "verify", "SRC-0001")["ok"])
        self.assertTrue(kb.cli("admin", "validate", "--deep")["ok"])
        changes = kb.read({"changes_since": 2})
        self.assertEqual(changes["results"][0]["ops"][0]["op"], "prepare")
        prepared_file = kb.kb / "prepared" / "SRC-0002.jsonl"
        prepared_file.write_bytes(prepared_file.read_bytes() + b"\n")
        self.assertEqual([part for part in kb.read({"query": "flamingo"})["results"] if "evidence" in part], [])
        deep = kb.cli("admin", "validate", "--deep")
        self.assertEqual((deep["ok"], deep["sources"]["prepared_changed"]), (False, ["SRC-0002"]))
        self.assertFalse(self.prepare(kb, "verify", "SRC-0002")["ok"])
        data.write_text("changed\n", encoding="utf-8")
        refused = self.prepare(kb, "prepare", "SRC-0001", "--actor", "test", "--upgrade")
        self.assertEqual((refused["outcome"], refused["error"]["code"]), ("not_written", "prepare_failed"))


class LineageAndRankingTest(unittest.TestCase):
    """Source revisions keep the old identity and are checked against history; superseded records rank lower."""

    def test_revise_source_with_and_without_git(self):
        kb = KB(self)
        data = kb.root / "measure.txt"
        data.write_text("value 1\n", encoding="utf-8")
        git = shutil.which("git")
        commit = None
        if git:
            run = lambda *args: subprocess.run([git, *args], cwd=kb.root, capture_output=True, text=True, timeout=60)
            run("init", "-q")
            run("add", "measure.txt")
            run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "fixture")
            commit = run("rev-parse", "HEAD").stdout.strip()
        kb.write({"changes": [{"source": {"locator": {"type": "file", "path": str(data)}}},
                              new("owner_decision", "Remeasure", "The owner asked for a remeasurement.", ref="d")]})
        same = kb.write({"changes": [{"source": {"revises": "SRC-0001", "reason": "remeasured", "record_ids": ["DEC-0001"],
                                                 "history_unavailable_reason": "no copy"}}]})
        self.assertEqual(same["error"]["code"], "no_change")
        data.write_text("value 2\n", encoding="utf-8")
        history = {"git_commit": commit} if commit else {"history_unavailable_reason": "git not installed"}
        revised = kb.write({"changes": [{"source": {"ref": "s", "revises": "SRC-0001", "reason": "remeasured",
                                                    "record_ids": ["DEC-0001"], **history}}]})
        self.assertEqual(revised["ids"], {"s": "SRC-0002"}, revised)
        deep = kb.cli("admin", "validate", "--deep")
        self.assertTrue(deep["ok"], deep)
        self.assertEqual(deep["sources"]["unchanged"], ["SRC-0002"])
        self.assertEqual(deep["sources"]["history_verified" if commit else "history_unavailable"], ["SRC-0001"])
        again = kb.write({"changes": [{"source": {"revises": "SRC-0001", "reason": "x", "record_ids": ["DEC-0001"],
                                                  "history_unavailable_reason": "x"}}]})
        self.assertEqual(again["error"]["code"], "value_invalid")
        guest = kb.cli("guest", "read", json.dumps({"ids": ["SRC-0001"]}))
        self.assertEqual(guest["result"]["results"][0]["source"]["supersession"]["source_id"], "SRC-0002")

    def test_equal_scores_follow_route_lead_order(self):
        kb = KB(self)
        kb.write({"changes": [new("observed_fact", "Alpha", "Unrelated text.", ref="a", topics=["build_method"]),
                              new("recommendation", "Beta", "Other unrelated text.", ref="b", topics=["build_method"]),
                              {"core": {"route": {"topic": "build_method", "lead": ["$b", "$a"]}}}]})
        results = kb.read({"query": "build method"})["results"]
        self.assertEqual([part["id"] for part in results], ["REC-0001", "FACT-0001"])

    def test_many_matching_log_entries_do_not_swamp(self):
        kb = KB(self)
        kb.write({"changes": [new("observed_fact", "Phase log", "Daily notes.", ref="p"),
                              new("observed_fact", "Flamingo count", "The flamingo count is 41.")]})
        for _ in range(2):
            kb.write({"changes": [{"log": {"id": "FACT-0001", "text": "checked the flamingo pen"}}] * 150})
        results = kb.read({"query": "flamingo"})["results"]
        self.assertEqual([part["id"] for part in results][:2], ["FACT-0002", "FACT-0001"])
        self.assertEqual(results[1]["more_places"], 297)

    def test_superseded_records_rank_lower(self):
        kb = KB(self)
        kb.write({"changes": [new("recommendation", "Old lesson", "Retry the import with semicolons."),
                              new("recommendation", "New lesson", "Retry the import with commas, not semicolons.", ref="n")]})
        kb.write({"changes": [{"status": {"id": "REC-0001", "status": "superseded", "by": "REC-0002"}}]})
        results = kb.read({"query": "retry import semicolons"})["results"]
        self.assertEqual([part["id"] for part in results], ["REC-0002", "REC-0001"])
        self.assertEqual(results[1]["superseded_by"], "REC-0002")


class GuestTest(unittest.TestCase):
    """The guest reader checks its dependencies, returns fingerprints, writes nothing and honours guest_reads."""

    def guest(self, kb: KB, *args: str, env: dict[str, str] | None = None) -> dict:
        return kb.cli("guest", *args, env=env)

    def test_guest_reads(self):
        kb = KB(self)
        kb.write({"changes": [new("owner_decision", "Ownership", "Each agent owns its KB.", topics=["rules"])]})
        before = kb.state_bytes()
        listing = sorted(str(path) for path in kb.kb.rglob("*"))
        info = self.guest(kb, "info")
        self.assertTrue(info["ok"], info)
        self.assertEqual((info["interface_version"], info["owner"], info["kb_revision"]), (2, "Owner", 2))
        read = self.guest(kb, "read", json.dumps({"query": "owns"}))
        self.assertEqual(read["result"]["results"][0]["id"], "DEC-0001")
        self.assertEqual(len(read["record_fingerprints"]["DEC-0001"]), 64)
        self.assertEqual(kb.state_bytes(), before)
        self.assertEqual(sorted(str(path) for path in kb.kb.rglob("*")), listing)
        self.assertFalse(self.guest(kb, "write", "{}")["ok"])
        kb.write({"changes": [{"core": {"guest_reads": False}}]})
        self.assertEqual(self.guest(kb, "read", "{}")["result"]["error"]["code"], "guest_access_disabled")
        changed = changed_package(self, kb)
        self.assertEqual(self.guest(kb, "info", env=changed)["result"]["error"]["code"], "reader_dependency_changed")
        self.assertEqual(sorted(str(path) for path in kb.kb.rglob("*")), listing)


class ContractTest(unittest.TestCase):
    """docs/USAGE.md must match the schema file and name every error code the package can return."""

    def test_usage_matches_schema_and_codes(self):
        text = (ROOT / "docs" / "USAGE.md").read_text(encoding="utf-8")
        schema = json.loads((PACKAGE / "_engine" / "kb_schema.json").read_text(encoding="utf-8"))
        rows = {match.group(1): [cell.strip() for cell in match.group(2).split("|")]
                for match in re.finditer(r"^\| `(\w+)` \| (.+) \|$", text, re.M)}
        for kind, spec in schema["kinds"].items():
            with self.subTest(kind=kind):
                self.assertIn(kind, rows)
                prefix, statuses, fields = rows[kind][:3]
                self.assertEqual(prefix, spec["prefix"])
                self.assertEqual([item.strip() for item in statuses.split(",")], spec["statuses"])
                self.assertEqual([item.strip() for item in fields.split(",")], spec["fields"])
        codes = set()
        for path in [*PACKAGE.glob("*.py"), *(PACKAGE / "_engine").glob("*.py")]:
            source = path.read_text(encoding="utf-8")
            codes.update(re.findall(r'KBError\(\s*"(\w+)"', source))
            codes.update(re.findall(r'failure\(operation, "(\w+)"', source))
            codes.update(re.findall(r'"code": "(\w+)"', source))
            codes.update(re.findall(r'^\s+code = "(\w+)"', source, re.M))
        undocumented = sorted(code for code in codes if f"`{code}`" not in text)
        self.assertEqual(undocumented, [])
        self.assertIn(f"at most {schema['limits']['statement']:,} B", text)
        self.assertIn(f"at most {schema['limits']['log_text']:,} B", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
