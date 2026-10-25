"""Search ranking: match strength, coverage and the full-query route bonus order matches without dropping any.

test_match_sets_are_unchanged pins the search and brief match sets of 0.2.3 on this fixture, so an update can
change the order of results but never which records are found. Synthetic data only; owned temporary directories.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fist_kb.initialize import initialize

FULL_HASH = "3A7F0B12C4D5E6F7A8B9C0D1E2F3A4B5C6D7E8F9A0B1C2D3E4F5A6B7C8D9E0F1"
# Search and brief match sets of Fist KB 0.2.3 on this fixture, records only.
MATCHES_0_2_3 = {
    "guard": ["FACT-0004", "FACT-0005"],
    "safeguards": ["FACT-0004"],
    "plan": ["FACT-0007", "FACT-0008"],
    "plans": ["FACT-0007", "FACT-0008"],
    "planning": ["FACT-0007"],
    "tool": ["FACT-0001", "FACT-0002", "FACT-0003", "FACT-0011"],
    "tools": ["FACT-0001", "FACT-0002", "FACT-0003", "FACT-0011"],
    "kb tools": ["FACT-0001", "FACT-0002", "FACT-0003", "FACT-0011"],
    "kb tool": ["FACT-0001", "FACT-0002", "FACT-0003", "FACT-0011"],
    "kb tools status": ["FACT-0001", "FACT-0002", "FACT-0003", "FACT-0011"],
    "malformed tool input": ["FACT-0001", "FACT-0002", "FACT-0003", "FACT-0011"],
    "parser error input": ["FACT-0001"],
    "item": ["Q-0001"],
    "open items": ["Q-0001"],
    "open items status": ["Q-0001"],
    "release": ["FACT-0006", "Q-0001"],
    "3A7F0B12": ["FACT-0006"],
    "DEC-0006": ["DEC-0006"],
    "KnowledgeBase/tools": ["FACT-0001", "FACT-0002", "FACT-0003", "FACT-0011"],
    "is it allowed to push": ["DEC-0006", "FACT-0001", "FACT-0002", "FACT-0003", "FACT-0006", "FACT-0009"],
    "art": ["FACT-0007"],
    "the": ["FACT-0001", "FACT-0002", "FACT-0004", "FACT-0005", "FACT-0006", "FACT-0007", "FACT-0008", "FACT-0010",
            "Q-0001"],
    "questions": ["Q-0001"],
    "verification": ["FACT-0003"],
    "cache eviction policy": ["DEC-0006", "FACT-0009", "FACT-0010"],
    "policy": ["DEC-0006", "FACT-0010"],
}
BRIEF_COUNTS_0_2_3 = {"malformed tool input": 5, "kb tools status": 5, "guard": 3, "plan": 3,
                      "open items status": 2, "is it allowed to push": 6, "cache eviction policy": 3}


def row(record_id: str, statement: str, **fields) -> dict:
    kind = ("owner_decision" if record_id.startswith("DEC") else
            "open_question" if record_id.startswith("Q") else "observed_fact")
    return {"id": record_id, "kind": kind, "status": "open" if record_id.startswith("Q") else "active",
            "statement": statement, "tags": fields.pop("tags", []), "source_refs": [], **fields}


RECORDS = [
    row("FACT-0001", "Malformed tool input is rejected by the parser with a clear error.",
        title="Parser rejects bad input"),
    row("FACT-0002", "The kb tools are installed under KnowledgeBase/tools and verified after context loss.",
        title="KB tools"),
    row("FACT-0003", "Tool inventory verification records local successors.", title="Tool inventory"),
    row("FACT-0004", "Safeguards protect the owner endpoint from unauthenticated calls.",
        title="Owner endpoint safeguards"),
    row("FACT-0005", "Guard clauses return early instead of nesting the happy path.", title="Guard clauses"),
    row("FACT-0006", f"Release identity: SHA-256 {FULL_HASH} for the shipped capsule.", title="Release identity"),
    row("FACT-0007", "Planning for the next phase starts after the review.", title="Phase planning"),
    row("FACT-0008", "The plan has three steps and one checkpoint.", title="Plan steps"),
    row("Q-0001", "Which questions remain open before the release?", title="Open questions"),
    row("DEC-0006", "Owner decision: do not push to main without an explicit request.", title="Push policy"),
    row("FACT-0009", "Cache eviction runs nightly.", title="Cache eviction", tags=["cache", "eviction"],
        topics=["cache eviction"], action="Check cache eviction weekly.",
        applicability="Applies to cache eviction jobs.", current_resolution={"note": "Cache eviction is enabled."}),
    row("FACT-0010", "The cache eviction policy keeps recent entries.", title="Retention rule"),
    row("FACT-0011", "Installer logs are kept for seven days.", title="Installer logs"),
]
ROUTES = {"kb-tools": {"record_ids": ["FACT-0002", "FACT-0003", "FACT-0011"]},
          "open_items": {"record_ids": ["Q-0001"]}}


class MatchingTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="fist-kb-matching-", dir=os.environ.get("FIST_KB_TEST_TMP"))
        self.area = Path(fixture.name).resolve()

        def cleanup():
            fixture.cleanup()
            self.assertFalse(self.area.exists(), "Matching fixture remained after cleanup")

        self.addCleanup(cleanup)
        initialize(self.area)
        self.kb = self.area / "KnowledgeBase"
        self.env = {key: value for key, value in os.environ.items() if key != "FIST_KB_ROOT"}
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        transaction = self.kb / "transactions" / "fixture.json"
        transaction.write_text(json.dumps({"actor": "Synthetic matching test", "upsert_records": RECORDS,
                                           "core_patch": {"routes": ROUTES, "pinned_record_ids": ["DEC-0006"]}}),
                               encoding="utf-8")
        preview = self.cli("apply", transaction, "--expect-revision", 1, "--dry-run", "--preview-values")
        self.cli("apply", transaction, "--expect-revision", 1, "--expect-transaction-sha256",
                 preview["transaction_sha256"])

    def cli(self, command: str, *args) -> dict:
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-P", "-m", "fist_kb", "--kb", str(self.kb),
                                 command, "--compact", *map(str, args)], cwd=self.area, env=self.env,
                                capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def hits(self, query: str) -> list[tuple[str, int]]:
        found = self.cli("search", "--records-only", "--limit", "50", "--budget", "20000", "--", query)["hits"]
        return [(hit["id"], hit["score"]) for hit in found]

    def ids(self, query: str) -> list[str]:
        return [record_id for record_id, _ in self.hits(query)]

    def brief(self, topic: str) -> dict:
        return self.cli("context", "--brief", "--topic", topic)

    def test_one_route_word_in_a_longer_query_does_not_lift_the_route(self):
        hits = self.hits("malformed tool input")
        self.assertEqual(hits[0][0], "FACT-0001", hits)
        route = {record_id: score for record_id, score in hits if record_id in {"FACT-0002", "FACT-0003", "FACT-0011"}}
        self.assertEqual(set(route), {"FACT-0002", "FACT-0003", "FACT-0011"}, "Route records must stay matches")
        self.assertTrue(all(score < 300 for score in route.values()), hits)
        brief = [item["id"] for item in self.brief("malformed tool input")["records"]]
        self.assertEqual(brief[0], "FACT-0001", brief)
        # FACT-0011 is reached only through the route word "tool"; it is not a strong match and follows pinned work.
        self.assertLess(brief.index("DEC-0006"), brief.index("FACT-0011"), brief)

    def test_filler_words_keep_only_the_small_route_bonus(self):
        hits = self.hits("kb tools status")
        self.assertEqual(hits[0][0], "FACT-0002", hits)
        self.assertTrue(all(score < 300 for _, score in hits), hits)

    def test_a_query_naming_the_route_keeps_the_full_bonus(self):
        for query in ("kb tools", "kb tool"):
            hits = self.hits(query)
            self.assertEqual([record_id for record_id, _ in hits[:2]], ["FACT-0002", "FACT-0003"], query)
            self.assertTrue(all(score >= 300 for _, score in hits[:2]), hits)
        self.assertEqual(self.ids("item")[:1], ["Q-0001"])

    def test_whole_words_rank_before_matches_inside_words_and_nothing_is_dropped(self):
        self.assertEqual(self.ids("guard"), ["FACT-0005", "FACT-0004"])
        self.assertEqual(self.ids("safeguards"), ["FACT-0004"])
        self.assertEqual(self.ids("plan"), ["FACT-0008", "FACT-0007"])
        self.assertEqual(self.ids("planning")[:1], ["FACT-0007"])
        self.assertEqual(sorted(self.ids("plans")), ["FACT-0007", "FACT-0008"], "Word forms are kept")

    def test_hash_prefixes_and_identifiers_still_match(self):
        self.assertEqual(self.ids(FULL_HASH[:8]), ["FACT-0006"])
        self.assertEqual(self.ids(FULL_HASH), ["FACT-0006"])
        self.assertEqual(self.ids("DEC-0006")[0], "DEC-0006")
        self.assertEqual(self.ids("KnowledgeBase/tools")[0], "FACT-0002")

    def test_full_coverage_outranks_one_common_word(self):
        self.assertEqual(self.ids("parser error input")[0], "FACT-0001")

    def test_all_query_words_outrank_fewer_words_in_heavier_fields(self):
        # FACT-0009 has two of the three words in seven fields; FACT-0010 has all three in its statement only.
        # Summed field weights (even with the full-coverage bonus) put FACT-0009 first; coverage puts FACT-0010 first.
        self.assertEqual(self.ids("cache eviction policy")[:2], ["FACT-0010", "FACT-0009"])

    def test_snippets_start_at_the_strongest_occurrence(self):
        statement = "Safeguards " + "filler " * 80 + "and the guard rule."
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-P", "-c",
                                 "import sys\nfrom fist_kb._engine import kb\n"
                                 "print(kb.snippet(sys.argv[1], ['guard'], width=60))", statement],
                                cwd=self.area, env={**self.env, "FIST_KB_ROOT": str(self.kb)},
                                capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guard rule", result.stdout)

    def test_match_sets_are_unchanged(self):
        for query, expected in MATCHES_0_2_3.items():
            self.assertEqual(sorted(self.ids(query)), expected, query)
        for query, expected in BRIEF_COUNTS_0_2_3.items():
            self.assertEqual(self.brief(query)["available_count"], expected, query)


if __name__ == "__main__":
    unittest.main()
