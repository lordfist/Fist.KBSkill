"""Fist KB migration (Seed KB v08 engine) from a v07 (or evolved earlier) KnowledgeBase into this new, empty KB.

The old KB is read without modification. Every old record keeps its ID. Each old field gets one recorded
destination: a v08 field, log entries (note arrays and anything v08 has no field for), or a history entry
(archived copies). Before publication the check rebuilds every old field value from its destination and
compares it type-exactly, including list order, with the original; any difference leaves this KB
uninitialized. Navigation that v08 bounds (route lead order, pins) is preserved in the migration record's
log and reported as a deliberate navigation change for the adopting agent to review.
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Any

from . import kb_store as store
from .kb_store import KINDS, utc_now

NOTE_ARRAYS = {"resolution_notes", "notes", "history_notes", "correction_notes", "progress_notes"}
HISTORY_NAMES = {"history", "historical_notes", "previous_versions", "audit_trail", "predecessor_statements"}
HISTORY_RE = re.compile(r"(?:pre|before)_.+_(?:statement|resolution|fields|r[0-9]+)")
DIRECT = {"statement", "title", "quote", "action", "evidence", "limits", "claimant", "applicability", "step", "next",
          "stop"}
RENAMED = {"related_record_ids": "related", "source_refs": "sources", "basis_record_ids": "basis"}
MERGED_INTO_LIMITS = ("limitations", "uncertainty")
STATUS_FALLBACK = {
    "active": ["active", "open"],
    "provisional": ["provisional", "active", "open"],
    "historical": ["historical", "superseded", "resolved"],
    "superseded": ["superseded", "historical"],
    "disputed": ["disputed", "active", "open"],
    "open": ["open", "active"],
    "resolved": ["resolved", "historical", "superseded"],
}


def historical(key: str) -> bool:
    name = key.casefold()
    return name.startswith("prior_") or name in HISTORY_NAMES or bool(HISTORY_RE.fullmatch(name))


def origin_type(value: Any) -> str:
    text = str(value or "").casefold()
    if text.startswith("owner"):
        return "owner_message"
    if text.startswith("peer"):
        return "peer_message"
    if "source" in text:
        return "source"
    if "inference" in text:
        return "inference"
    if "observ" in text or text == "experiment":
        return "observation"
    return "import"


def topic_name(tag: str) -> str:
    name = re.sub(r"[^a-z0-9_.-]+", "_", tag.casefold()).strip("_.-")[:store.LIMITS["topic_name"]]
    return name or "untitled"


def canonical(value: Any) -> str:
    """Type-exact comparison: true differs from 1, 1 from 1.0, and list order counts; object key order does not."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Migration:
    def __init__(self, old: Path, actor: str):
        self.old, self.actor, self.at = old, actor, utc_now()
        self.records: list[dict[str, Any]] = []
        self.journal: list[dict[str, Any]] = []
        self.counts: dict[tuple[str, str], int] = {}
        self.mapping: dict[str, dict[str, tuple[Any, ...]]] = collections.defaultdict(dict)
        self.report: dict[str, Any] = {"status_remapped": [], "tags_normalized": 0, "imported_fields": collections.Counter(),
                                       "note_entries": 0, "history_entries": 0, "field_count": 0, "spilled_fields": 0}

    def load_old(self) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        core = store.read_json_file(self.old / "core.json")
        records_bytes = (self.old / "records.jsonl").read_bytes()
        sources_path = self.old / "sources.json"
        sources_bytes = sources_path.read_bytes() if sources_path.exists() else b""
        files = core.get("state_files", {}) if isinstance(core, dict) else {}
        for key, data in (("records_jsonl_sha256", records_bytes), ("sources_json_sha256", sources_bytes)):
            if files.get(key) and str(files[key]).upper() != store.sha256_bytes(data):
                raise store.KBError("old_kb_inconsistent", f"the old KB's {key} does not match its core.json",
                                    "Migrate only a consistent KB; resolve its RECOVERY_REQUIRED state first.")
        sources = store.strict_loads(sources_bytes.decode("utf-8"), "old sources.json") if sources_bytes.strip() else {}
        return core, store.parse_jsonl(records_bytes, "old records.jsonl"), sources

    def entry(self, kind: str, rid: str, at: str, **fields: Any) -> int:
        key = (kind, rid)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.journal.append({"type": kind, "rev": 1, "at": at, "by": self.actor, "id": rid, "n": self.counts[key], **fields})
        return self.counts[key]

    def logged(self, rid: str, key: str, label: str, value: Any, when: str) -> None:
        """Keep a whole old value as one log entry: text as written, anything else as JSON."""
        raw = isinstance(value, str)
        number = self.entry("log", rid, when, text=label + (value if raw else json.dumps(value, ensure_ascii=False)))
        self.mapping[rid][key] = ("log", number, label, raw)

    # Records ---------------------------------------------------------------------------------------

    def convert(self, old: dict[str, Any], known_ids: set[str], source_ids: set[str]) -> dict[str, Any]:
        rid, kind = old["id"], old["kind"]
        when = str(old.get("updated_at") or old.get("created_at") or self.at)
        allowed = set(store.kind_fields(kind))
        row: dict[str, Any] = {"id": rid, "kind": kind}
        status = old.get("status")
        row["status"] = next(candidate for candidate in [*STATUS_FALLBACK.get(status, []), KINDS[kind]["default_status"]]
                             if candidate in KINDS[kind]["statuses"])
        if row["status"] == status:
            self.mapping[rid]["status"] = ("field", "status")
        else:
            self.logged(rid, "status", f"imported status (not a {kind} status in v08; now {row['status']}): ", status, when)
            self.report["status_remapped"].append(f"{rid}: {status} -> {row['status']}")
        pending_limits: list[tuple[str, Any]] = []
        for key, value in old.items():
            if key in {"id", "kind", "status"}:
                continue
            self.report["field_count"] += 1
            target = RENAMED.get(key, key)
            if value == [] and (target in {"related", "sources", "basis", "options"} or key in {"tags", "topics"}):
                self.mapping[rid][key] = ("empty_list",)  # v08 omits an empty list; nothing to keep
            elif key in {"created_at", "updated_at"} and isinstance(value, str) and value.strip():
                row[key] = value
                self.mapping[rid][key] = ("field", key)
            elif key in {"tags", "topics"} and isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                names = [topic_name(tag) for tag in value]
                row["topics"] = list(dict.fromkeys([*row.get("topics", []), *names]))
                if names == value and len(set(value)) == len(value) and \
                        [item for item in row["topics"] if item in set(value)] == value:
                    self.mapping[rid][key] = ("in_order", "topics")
                else:
                    self.report["tags_normalized"] += 1
                    self.logged(rid, key, f"imported field {key}: ", value, when)
            elif target in {"related", "sources", "basis"} and target in allowed and isinstance(value, list) and value \
                    and all(isinstance(v, str) and v in (source_ids if target == "sources" else known_ids) for v in value):
                row[target] = list(dict.fromkeys(value))
                if row[target] == value:
                    self.mapping[rid][key] = ("field", target)
                else:
                    self.logged(rid, key, f"imported field {key}: ", value, when)
            elif key == "current_resolution" and isinstance(value, dict) and set(value) <= {"note", "record_ids"} \
                    and isinstance(value.get("note"), str) and value["note"].strip() \
                    and isinstance(value.get("record_ids", []), list) \
                    and all(v in known_ids for v in value.get("record_ids", [])) \
                    and len(set(value.get("record_ids", []))) == len(value.get("record_ids", [])):
                row["resolution"] = {"note": value["note"]}
                if "record_ids" in value:
                    row["resolution"]["ids"] = value["record_ids"]
                self.mapping[rid][key] = ("resolution",)
            elif key in NOTE_ARRAYS and isinstance(value, list):
                numbers = []
                for item in value:
                    plain = isinstance(item, dict) and set(item) == {"note", "observed_at"} and \
                        isinstance(item["note"], str) and item["note"].strip() and isinstance(item["observed_at"], str)
                    text = item["note"] if plain else "imported note: " + json.dumps(item, ensure_ascii=False)
                    numbers.append(self.entry("log", rid, item["observed_at"] if plain else when, text=text))
                    self.report["note_entries"] += 1
                self.mapping[rid][key] = ("notes", numbers)
            elif historical(key):
                self.mapping[rid][key] = ("history", self.entry("history", rid, when, field=key, value=value))
                self.report["history_entries"] += 1
            elif key in MERGED_INTO_LIMITS and "limits" in allowed and isinstance(value, str) and value.strip():
                pending_limits.append((key, value))
            elif key == "origin" and isinstance(value, dict):
                row["origin"] = self.origin(value)
                if canonical(row["origin"]) == canonical(value):
                    self.mapping[rid][key] = ("field", "origin")
                else:
                    self.logged(rid, key, "imported origin: ", value, when)
            elif key in DIRECT and key in allowed and isinstance(value, str) and value.strip():
                row[key] = value
                self.mapping[rid][key] = ("field", key)
            elif key == "options" and key in allowed and isinstance(value, list) and value \
                    and all(isinstance(v, str) and v.strip() for v in value):
                row[key] = value
                self.mapping[rid][key] = ("field", key)
            else:
                self.report["imported_fields"][key] += 1
                self.report["spilled_fields"] += 1
                self.logged(rid, key, f"imported field {key}: ", value, when)
        for key, value in pending_limits:
            if "limits" not in row:
                row["limits"] = value
                self.mapping[rid][key] = ("field", "limits")
            else:
                self.report["imported_fields"][key] += 1
                self.report["spilled_fields"] += 1
                self.logged(rid, key, f"imported field {key}: ", value, when)
        if "title" not in row:
            statement = str(old.get("statement", "")).strip()
            row["title"] = (statement[:117] + "…") if len(statement) > 120 else (statement or rid)
            self.entry("log", rid, when, text="imported without a title; the title is the start of the statement")
        if "statement" not in row:
            row["statement"] = row["title"]
        # Provenance only when the old KB knew it; the migrator is storage authorship, recorded in the journal.
        row.setdefault("origin", {"type": "import"})
        row.setdefault("created_at", when)
        row.setdefault("updated_at", when)
        row.update(created_rev=1, updated_rev=1)
        return row

    def origin(self, value: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"type": origin_type(value.get("type"))}
        for source_key, target in (("by", "by"), ("actor", "by"), ("at", "at"), ("observed_at", "at"),
                                   ("channel", "channel"), ("reason", "reason")):
            item = value.get(source_key)
            if target not in result and isinstance(item, str) and item.strip() and \
                    len(item.encode("utf-8")) <= store.LIMITS["text" if target == "reason" else "origin_value"]:
                result[target] = item
        return result

    # Core ------------------------------------------------------------------------------------------

    def run(self, purpose_override: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        old_core, old_records, old_sources = self.load_old()
        known_ids = {row.get("id") for row in old_records}
        bad = [row.get("id") for row in old_records if not store.is_record_id(row.get("id")) or row.get("kind") not in KINDS
               or store.PREFIX_KIND[row["id"].split("-")[0]] != row.get("kind")]
        if bad:
            raise store.KBError("old_kb_unsupported", f"records with IDs or kinds v08 cannot map: {', '.join(map(str, bad[:5]))}")
        source_rows = old_sources.get("sources", []) if isinstance(old_sources, dict) else []
        source_ids = {row.get("source_id") for row in source_rows if isinstance(row, dict)}
        by_id = {}
        for old in old_records:
            row = self.convert(old, known_ids, source_ids)
            self.records.append(row)
            by_id[row["id"]] = row
        experiment = old_core.get("experiment", {}) if isinstance(old_core.get("experiment"), dict) else {}
        purpose = purpose_override or str(experiment.get("purpose") or "")  # 0.3.x KBs store a null purpose
        owner = str(experiment.get("owner") or "")
        navigation: dict[str, Any] = {}
        routes: dict[str, Any] = {}
        for topic, route in (old_core.get("routes") or {}).items():
            name = topic_name(topic)
            ids = [rid for rid in (route.get("record_ids", []) if isinstance(route, dict) else []) if rid in by_id]
            for rid in ids:
                by_id[rid]["topics"] = list(dict.fromkeys([*by_id[rid].get("topics", []), name]))
            lead = list(dict.fromkeys(ids))
            routes[name] = {"lead": lead[:store.CORE_LIMITS["lead"]]}
            if len(lead) > store.CORE_LIMITS["lead"]:
                navigation.setdefault("route_order_after_lead", {})[name] = len(lead) - store.CORE_LIMITS["lead"]
            if name != topic:
                navigation.setdefault("route_renamed", {})[topic] = name
            about = route.get("about", route.get("description")) if isinstance(route, dict) else None
            if isinstance(about, str) and about.strip() and len(about.encode("utf-8")) <= store.CORE_LIMITS["about"]:
                routes[name]["about"] = about
        if len(routes) > store.CORE_LIMITS["routes"]:
            navigation["routes_without_core_entry"] = list(routes)[store.CORE_LIMITS["routes"]:]
            routes = dict(list(routes.items())[:store.CORE_LIMITS["routes"]])
        old_pins = [rid for rid in old_core.get("pinned_record_ids", []) if rid in by_id]
        pinned = old_pins[:store.CORE_LIMITS["pinned"]]
        if len(old_pins) > len(pinned):
            navigation["pins_dropped"] = old_pins[len(pinned):]
        if len(purpose.encode("utf-8")) > store.CORE_LIMITS["purpose"]:
            purpose = purpose[:300] + "…"
            navigation["purpose_clipped"] = True
        marker_number = max([int(rid.split("-")[1]) for rid in by_id if rid.startswith("FACT-")], default=0) + 1
        marker = f"FACT-{marker_number:04d}"
        self.records.append({"id": marker, "kind": "observed_fact", "status": "active",
                             "title": "Migrated from a v07 KnowledgeBase",
                             "statement": f"This KB was migrated by {self.actor} on {self.at[:10]} from {self.old} at "
                                          f"v07 revision {old_core.get('kb_revision')}. Its log holds the old core and "
                                          "catalog values verbatim, including any navigation v08 bounds.",
                             "topics": ["migration"], "origin": {"type": "import", "at": self.at},
                             "created_at": self.at, "created_by": self.actor, "updated_at": self.at,
                             "created_rev": 1, "updated_rev": 1})
        for key, value in old_core.items():
            if key not in {"state_files", "kb_revision"}:
                self.logged(marker, f"core.{key}", f"imported core {key}: ", value, self.at)
        for key, value in (old_sources.items() if isinstance(old_sources, dict) else []):
            if key != "sources":
                self.logged(marker, f"catalog.{key}", f"imported catalog {key}: ", value, self.at)
        sources = {"sources": source_rows}
        self.journal.append({"type": "write", "rev": 1, "at": self.at, "by": self.actor,
                             "note": f"migrated from v07 revision {old_core.get('kb_revision')}",
                             "ops": [{"op": "migrate", "id": marker}]})
        records_bytes, journal_bytes = store.jsonl_bytes(self.records), store.jsonl_bytes(self.journal)
        sources_bytes = store.json_bytes(sources)
        core = {"schema": store.CORE_SCHEMA, "revision": 1, "binding": {"root": str(store.BINDING_ROOT)},
                "purpose": purpose, "owner": owner[:store.CORE_LIMITS["owner"]], "current_task": None, "pinned": pinned,
                "routes": routes,
                "state_files": {"records": store.sha256_bytes(records_bytes), "journal": store.sha256_bytes(journal_bytes),
                                "sources": store.sha256_bytes(sources_bytes)},
                "last_write": {"rev": 1, "at": self.at, "by": self.actor, "note": "migrated from v07"}}
        state = store.State(core, self.records, self.journal, sources, core["state_files"])
        validation = store.validate_state(state)
        check = self.verify(old_records, old_core, old_sources, state, marker)
        candidates = [row["id"] for row in self.records if row["kind"] == "observed_fact" and row["id"] != marker and (
            row["id"] in routes.get("current_plan", {}).get("lead", [])[:1]
            or re.search(r"\b(current|state|next)\b", row["title"].casefold()))][:5]
        report = {**self.report, "imported_fields": dict(self.report["imported_fields"].most_common(20)),
                  "records": len(old_records), "sources": len(source_rows), "log_entries": self.counts_of("log"),
                  "history_entries": self.counts_of("history"), "migration_record": marker,
                  "spill_rate": round(self.report["spilled_fields"] / max(1, self.report["field_count"]), 3),
                  "over_limit_warnings": len(validation["warnings"]), "task_candidates": candidates,
                  "navigation_changes": navigation, "lossless": check}
        if not validation["ok"] or check["mismatch_count"]:
            return {"ok": False, "published": False, "errors": validation["errors"][:20], "report": report}
        if dry_run:
            report["prepared_files"] = sum(1 for row in source_rows if isinstance(row, dict) and row.get("preparation"))
            return {"ok": True, "published": False, "dry_run": True, "report": report,
                    "next": "Nothing was written. Run without --dry-run inside a new, empty KnowledgeBase to migrate."}
        report["prepared_copied"], report["prepared_not_copied"] = copy_prepared(self.old, source_rows)
        publish({store.RECORDS_PATH: records_bytes, store.JOURNAL_PATH: journal_bytes, store.SOURCES_PATH: sources_bytes,
                 store.CORE_PATH: store.json_bytes(core)})
        committed = store.load_state()
        store.require_valid(committed)
        report["next"] = ("Review navigation_changes, read kb_read({}) and the task candidates; create your TASK record "
                          "with one kb_write and set core.current_task.")
        return {"ok": True, "published": True, "revision": committed.revision, "report": report}

    def counts_of(self, kind: str) -> int:
        return sum(value for (entry_kind, _), value in self.counts.items() if entry_kind == kind)

    # Verification ----------------------------------------------------------------------------------

    def rebuild(self, state: store.State, rid: str, key: str, old_value: Any) -> Any:
        """The old value of one field, rebuilt only from where the migration put it."""
        spec = self.mapping[rid].get(key)
        if spec is None:
            raise LookupError("no destination recorded")
        logs = {entry["n"]: entry for entry in state.logs.get(rid, [])}
        row = state.by_id.get(rid, {})
        kind = spec[0]
        if kind == "field":
            return row[spec[1]]
        if kind == "empty_list":
            return []
        if kind == "in_order":
            topics = row.get(spec[1], [])
            present = [item for item in topics if item in set(old_value)]
            return present if present == old_value else topics
        if kind == "resolution":
            rebuilt = {"note": row["resolution"]["note"]}
            if "ids" in row["resolution"]:
                rebuilt["record_ids"] = row["resolution"]["ids"]
            return rebuilt
        if kind == "log":
            text = logs[spec[1]]["text"]
            if not text.startswith(spec[2]):
                raise LookupError("log label changed")
            payload = text[len(spec[2]):]
            return payload if spec[3] else json.loads(payload)
        if kind == "notes":
            rebuilt = []
            for number in spec[1]:
                entry = logs[number]
                if entry["text"].startswith("imported note: "):
                    rebuilt.append(json.loads(entry["text"][len("imported note: "):]))
                else:
                    rebuilt.append({"note": entry["text"], "observed_at": entry["at"]})
            return rebuilt
        if kind == "history":
            entries = {entry["n"]: entry for entry in state.history.get(rid, [])}
            if entries[spec[1]]["field"] != key:
                raise LookupError("history field changed")
            return entries[spec[1]]["value"]
        raise LookupError(f"unknown destination {kind}")

    def verify(self, old_records: list[dict[str, Any]], old_core: dict[str, Any], old_sources: Any,
               state: store.State, marker: str) -> dict[str, Any]:
        mismatches = []
        fields = 0
        for old in old_records:
            for key, value in old.items():
                if key in {"id", "kind"}:
                    if state.by_id[old["id"]][key] != value:
                        mismatches.append({"id": old["id"], "field": key, "problem": "changed"})
                    continue
                fields += 1
                try:
                    rebuilt = self.rebuild(state, old["id"], key, value)
                except (LookupError, KeyError, ValueError) as exc:
                    mismatches.append({"id": old["id"], "field": key, "problem": str(exc)})
                    continue
                if canonical(rebuilt) != canonical(value):
                    mismatches.append({"id": old["id"], "field": key, "problem": "value differs"})
        for key, value in old_core.items():
            if key in {"state_files", "kb_revision"}:
                continue
            fields += 1
            try:
                if canonical(self.rebuild(state, marker, f"core.{key}", value)) != canonical(value):
                    mismatches.append({"id": "core", "field": key, "problem": "value differs"})
            except (LookupError, KeyError, ValueError) as exc:
                mismatches.append({"id": "core", "field": key, "problem": str(exc)})
        if isinstance(old_sources, dict):
            if canonical(state.sources["sources"]) != canonical(old_sources.get("sources", [])):
                mismatches.append({"id": "sources", "field": "sources", "problem": "value differs"})
            for key, value in old_sources.items():
                if key != "sources":
                    fields += 1
                    try:
                        if canonical(self.rebuild(state, marker, f"catalog.{key}", value)) != canonical(value):
                            mismatches.append({"id": "catalog", "field": key, "problem": "value differs"})
                    except (LookupError, KeyError, ValueError) as exc:
                        mismatches.append({"id": "catalog", "field": key, "problem": str(exc)})
        return {"checked_records": len(old_records), "checked_fields": fields, "mismatch_count": len(mismatches),
                "mismatches": mismatches[:20], "method": "each old field rebuilt from its recorded destination and "
                "compared type-exactly, including list order"}


def copy_prepared(old: Path, source_rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Copy each prepared file whose bytes still match the catalog; the catalog rows themselves are kept as they are."""
    copied, skipped = [], []
    for row in source_rows:
        prep = row.get("preparation") if isinstance(row, dict) else None
        relative = prep.get("path") if isinstance(prep, dict) else None
        if not isinstance(relative, str) or not relative:
            continue
        source_path, target = (old / relative).resolve(), (store.KB_ROOT / relative).resolve()
        if not store.within(source_path, old) or not store.within(target, store.KB_ROOT) or not source_path.is_file() \
                or store.sha256_file(source_path) != str(prep.get("prepared_sha256", "")).upper():
            skipped.append(row.get("source_id"))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        store.stage_file(target, source_path.read_bytes()).replace(target)
        copied.append(row.get("source_id"))
    return copied, skipped


def publish(payloads: dict[Path, bytes]) -> None:
    staged = {path: store.stage_file(path, data) for path, data in payloads.items()}
    for path in (store.RECORDS_PATH, store.JOURNAL_PATH, store.SOURCES_PATH, store.CORE_PATH):
        staged[path].replace(path)


def migrate(old: str, actor: str, purpose: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Migrate into this empty KB; with dry_run, only build and check the result in memory and write nothing."""
    if store.CORE_PATH.exists() and not dry_run:
        raise store.KBError("already_initialized", "this KB already has core.json",
                            "Migrate into a new, empty KnowledgeBase folder.")
    path = Path(old).resolve()
    if not (path / "core.json").is_file() or not (path / "records.jsonl").is_file():
        raise store.KBError("old_kb_missing", f"no v07 KnowledgeBase at {old}", "Give the folder that holds core.json.")
    if store.within(path, store.KB_ROOT) or store.within(store.KB_ROOT, path):
        raise store.KBError("old_kb_overlap", "the old and new KB folders overlap", "Use a separate new folder.")
    return Migration(path, actor).run(purpose, dry_run)
