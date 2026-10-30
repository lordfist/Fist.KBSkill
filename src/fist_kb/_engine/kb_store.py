"""Fist KB storage (Seed KB v08 engine): schema, state files, validation, tool identity and the commit protocol.

State is four files in the KnowledgeBase folder. records.jsonl holds current records only (closed schema,
small by construction); journal.jsonl holds log entries, archived values and one entry per committed write;
sources.json is the source catalog; core.json is orientation plus the hashes of the other three. core.json is
written last and is the commit marker. One writer at a time; optimistic checks detect, but do not lock
against, a second writer. Nothing here retries a write or repairs state automatically.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import source_versions
from ..runtime import PackageIntegrityError, configured_root, verify_package

TOOLS_DIR = Path(__file__).resolve().parent
KB_ROOT = configured_root()
BINDING_ROOT = KB_ROOT.parent.resolve()
CORE_PATH = KB_ROOT / "core.json"
RECORDS_PATH = KB_ROOT / "records.jsonl"
JOURNAL_PATH = KB_ROOT / "journal.jsonl"
SOURCES_PATH = KB_ROOT / "sources.json"
PENDING_DIR = KB_ROOT / ".pending"
SCHEMA_PATH = TOOLS_DIR / "kb_schema.json"
STATE_FILES = {"records": RECORDS_PATH, "journal": JOURNAL_PATH, "sources": SOURCES_PATH}
CORE_SCHEMA = "seed-kb-8"
CORE_KEYS = {"schema", "revision", "binding", "purpose", "owner", "current_task", "pinned", "routes",
             "state_files", "last_write", "guest_reads"}
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
SOURCE_ID_RE = re.compile(r"^SRC-\d{4,}$")
SHA_RE = re.compile(r"^[0-9A-F]{64}$")


class KBError(RuntimeError):
    """A refusal or failure with a stable code, a one-line hint and structured details for the caller."""

    def __init__(self, code: str, message: str, hint: str = "", **details: Any):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.details = details

    def payload(self) -> dict[str, Any]:
        result = {"code": self.code, "message": str(self)}
        if self.hint:
            result["hint"] = self.hint
        result.update(self.details)
        return result


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def estimate_tokens(value: Any) -> int:
    # The v07 estimate: compact UTF-8 JSON bytes / 3, rounded up; not a model tokenizer.
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return max(1, (len(payload) + 2) // 3)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KBError("invalid_json", f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise KBError("invalid_json", f"non-finite JSON number is not allowed: {value}")


def strict_loads(text: str, label: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_pairs, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise KBError("invalid_json", f"invalid JSON in {label}: {exc}",
                      "Send one JSON object; check quoting and trailing commas.") from exc


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
                   for row in rows).encode("utf-8")


def read_json_file(path: Path) -> Any:
    try:
        return strict_loads(path.read_bytes().decode("utf-8"), path.name)
    except FileNotFoundError as exc:
        raise KBError("missing_state", f"required file is missing: {path.name}",
                      "Initialize the KB with 'fist-kb init' or check the KB folder.") from exc
    except UnicodeDecodeError as exc:
        raise KBError("invalid_encoding", f"{path.name} is not UTF-8") from exc


def parse_jsonl(data: bytes, label: str) -> list[dict[str, Any]]:
    rows = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KBError("invalid_encoding", f"{label} is not UTF-8") from exc
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        row = strict_loads(line, f"{label} line {number}")
        if not isinstance(row, dict):
            raise KBError("invalid_state", f"{label} line {number} is not an object")
        rows.append(row)
    return rows


def load_schema() -> dict[str, Any]:
    schema = read_json_file(SCHEMA_PATH)
    if not isinstance(schema, dict) or schema.get("format") != "seed-kb-8-schema":
        raise KBError("invalid_schema", "kb_schema.json is not a seed-kb-8 schema")
    return schema


SCHEMA = load_schema()
LIMITS = SCHEMA["limits"]
CORE_LIMITS = SCHEMA["core_limits"]
KINDS = SCHEMA["kinds"]
FIELD_TYPES = SCHEMA["field_types"]
AUTOMATIC_FIELDS = set(SCHEMA["automatic_fields"])
PREFIX_KIND = {spec["prefix"]: kind for kind, spec in KINDS.items()}
RECORD_ID_RE = re.compile(r"^(" + "|".join(sorted(PREFIX_KIND)) + r")-(\d{4,})$")


def kind_fields(kind: str) -> list[str]:
    return [*SCHEMA["common_fields"], *KINDS[kind]["fields"]]


def is_record_id(value: Any) -> bool:
    return isinstance(value, str) and bool(RECORD_ID_RE.fullmatch(value))


# ---------------------------------------------------------------------------------------------- state


@dataclasses.dataclass
class State:
    """One loaded, hash-checked revision. Derived indexes are built here, never stored."""

    core: dict[str, Any]
    records: list[dict[str, Any]]
    journal: list[dict[str, Any]]
    sources: dict[str, Any]
    file_hashes: dict[str, str]

    def __post_init__(self) -> None:
        self.by_id = {row["id"]: row for row in self.records if isinstance(row.get("id"), str)}
        self.source_ids = {row.get("source_id") for row in self.sources.get("sources", []) if isinstance(row, dict)}
        self.logs: dict[str, list[dict[str, Any]]] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.writes: list[dict[str, Any]] = []
        for entry in self.journal:
            kind = entry.get("type")
            if kind == "log":
                self.logs.setdefault(entry.get("id"), []).append(entry)
            elif kind == "history":
                self.history.setdefault(entry.get("id"), []).append(entry)
            elif kind == "write":
                self.writes.append(entry)
        self.requests = {entry["request_id"]: entry for entry in self.writes if entry.get("request_id")}

    @property
    def revision(self) -> int:
        return self.core["revision"]

    def members(self, topic: str) -> list[str]:
        """Route membership: the lead order first, then records carrying the topic by kind priority and recency."""
        route = self.core.get("routes", {}).get(topic, {})
        lead = [rid for rid in route.get("lead", []) if rid in self.by_id]
        priority = {kind: index for index, kind in enumerate(SCHEMA["kind_priority"])}
        tagged = [row for row in self.records if topic in row.get("topics", []) and row["id"] not in lead]
        tagged.sort(key=lambda row: (priority.get(row["kind"], 99), -row.get("updated_rev", 0), row["id"]))
        return [*lead, *(row["id"] for row in tagged)]

    def topics(self) -> dict[str, int]:
        names = set(self.core.get("routes", {}))
        for row in self.records:
            names.update(row.get("topics", []))
        return {name: len(self.members(name)) for name in sorted(names)}


def load_state(*, check_hashes: bool = True) -> State:
    try:
        core_bytes = CORE_PATH.read_bytes()
    except FileNotFoundError as exc:
        raise KBError("missing_state", "required file is missing: core.json",
                      "Initialize the KB with 'fist-kb init' or check the KB folder.") from exc
    try:
        core = strict_loads(core_bytes.decode("utf-8"), "core.json")
    except UnicodeDecodeError as exc:
        raise KBError("invalid_encoding", "core.json is not UTF-8") from exc
    if not isinstance(core, dict):
        raise KBError("invalid_state", "core.json must be an object")
    raw = {}
    for name, path in STATE_FILES.items():
        try:
            raw[name] = path.read_bytes()
        except FileNotFoundError as exc:
            raise KBError("missing_state", f"required file is missing: {path.name}") from exc
    hashes = {name: sha256_bytes(data) for name, data in raw.items()}
    expected = core.get("state_files") if isinstance(core.get("state_files"), dict) else {}
    if check_hashes:
        for name, digest in hashes.items():
            if expected.get(name) != digest:
                raise KBError("recovery_required", f"RECOVERY_REQUIRED: {STATE_FILES[name].name} does not match "
                              "the state committed by core.json",
                              "Do not write. Inspect with 'fist-kb admin recover' and report to the owner.", file=name)
    sources = strict_loads(raw["sources"].decode("utf-8"), "sources.json") if raw["sources"].strip() else {}
    if not isinstance(sources, dict):
        raise KBError("invalid_state", "sources.json must be an object")
    hashes["core"] = sha256_bytes(core_bytes)
    return State(core, parse_jsonl(raw["records"], "records.jsonl"), parse_jsonl(raw["journal"], "journal.jsonl"),
                 sources, hashes)


# ----------------------------------------------------------------------------------------- validation


def field_problems(field: str, value: Any, state: State, *, kind: str) -> tuple[list[str], list[str]]:
    """Check one field value. Returns (errors, over_limit); size limits are reported separately so imported
    records may hold over-limit text while new writes may not."""
    ftype = FIELD_TYPES.get(field)
    errors: list[str] = []
    over: list[str] = []

    def size(text: str, limit_name: str) -> None:
        limit = LIMITS[limit_name]
        if utf8_len(text) > limit:
            over.append(f"{field} is {utf8_len(text):,} B; the limit is {limit:,}")

    if ftype in {"title", "statement", "text", "quote"}:
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be a nonempty string")
        else:
            size(value, ftype if ftype != "text" else "text")
    elif ftype == "topics":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append("topics must be a list of strings")
        else:
            for item in value:
                if not TOPIC_RE.fullmatch(item) or len(item) > LIMITS["topic_name"]:
                    errors.append(f"topic {item!r} must be lowercase letters, digits, _ . - (at most "
                                  f"{LIMITS['topic_name']} characters)")
            if len(set(value)) != len(value):
                errors.append("topics contains duplicates")
            if len(value) > LIMITS["topics"]:
                over.append(f"topics has {len(value)} items; the limit is {LIMITS['topics']}")
    elif ftype in {"ids", "source_ids"}:
        known = state.source_ids if ftype == "source_ids" else state.by_id
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(f"{field} must be a list of IDs")
        else:
            missing = [item for item in value if item not in known]
            if missing:
                errors.append(f"{field} names unknown IDs: {', '.join(missing[:5])}")
            if len(set(value)) != len(value):
                errors.append(f"{field} contains duplicates")
            limit = LIMITS["rules" if field == "rules" else "ids"]
            if len(value) > limit:
                over.append(f"{field} has {len(value)} items; the limit is {limit}")
    elif ftype == "text_list":
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"{field} must be a list of nonempty strings")
        else:
            if len(value) > LIMITS["options"]:
                over.append(f"{field} has {len(value)} items; the limit is {LIMITS['options']}")
            if any(utf8_len(item) > LIMITS["option"] for item in value):
                over.append(f"an item of {field} exceeds {LIMITS['option']} B")
    elif ftype == "origin":
        allowed = {"type", "by", "at", "channel", "reason"}
        if not isinstance(value, dict) or set(value) - allowed:
            errors.append("origin is an object with type, by, at and optional channel and reason")
        elif value.get("type") not in SCHEMA["origin_types"]:
            errors.append(f"origin.type must be one of {', '.join(SCHEMA['origin_types'])}")
        else:
            for key, item in value.items():
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"origin.{key} must be a nonempty string")
                elif utf8_len(item) > LIMITS["text" if key == "reason" else "origin_value"]:
                    over.append(f"origin.{key} is too long")
    elif ftype == "resolution":
        if not isinstance(value, dict) or set(value) - {"note", "ids"} or not isinstance(value.get("note"), str) \
                or not value["note"].strip():
            errors.append("resolution is {\"note\": text, \"ids\": [record IDs]}")
        else:
            if utf8_len(value["note"]) > LIMITS["resolution_note"]:
                over.append(f"resolution.note is {utf8_len(value['note']):,} B; the limit is "
                            f"{LIMITS['resolution_note']:,}")
            ids = value.get("ids", [])
            if not isinstance(ids, list) or any(item not in state.by_id for item in ids):
                errors.append("resolution.ids must list existing record IDs")
    else:
        errors.append(f"{field} is not a field of {kind}")
    return errors, over


def validate_record(row: dict[str, Any], state: State) -> tuple[list[str], list[str]]:
    rid = row.get("id")
    label = str(rid)
    match = RECORD_ID_RE.fullmatch(rid) if isinstance(rid, str) else None
    errors: list[str] = []
    warnings: list[str] = []
    kind = row.get("kind")
    if kind not in KINDS:
        return [f"{label}: unknown kind {kind!r}"], []
    if not match or PREFIX_KIND[match.group(1)] != kind:
        errors.append(f"{label}: ID does not match kind {kind}")
    if row.get("status") not in KINDS[kind]["statuses"]:
        errors.append(f"{label}: status {row.get('status')!r} is not a status of {kind}")
    allowed = set(kind_fields(kind)) | AUTOMATIC_FIELDS
    for field in sorted(set(row) - allowed):
        errors.append(f"{label}: {field} is not a field of {kind}")
    for field in SCHEMA["required_fields"]:
        if field not in row:
            errors.append(f"{label}: {field} is required")
    for field, value in row.items():
        if field in AUTOMATIC_FIELDS or field not in allowed:
            continue
        problems, over = field_problems(field, value, state, kind=kind)
        errors.extend(f"{label}: {item}" for item in problems)
        warnings.extend(f"{label}: over limit: {item}" for item in over)
    for field in ("created_rev", "updated_rev"):
        if not isinstance(row.get(field), int):
            errors.append(f"{label}: {field} must be an integer")
    if "created_by" in row and (not isinstance(row["created_by"], str) or not row["created_by"].strip()):
        errors.append(f"{label}: created_by must be nonempty text")
    if "superseded_by" in row and row["superseded_by"] not in state.by_id:
        errors.append(f"{label}: superseded_by names an unknown record")
    if any(item not in state.by_id for item in row.get("supersedes", [])):
        errors.append(f"{label}: supersedes names an unknown record")
    return errors, warnings


def validate_core(state: State) -> tuple[list[str], list[str]]:
    core = state.core
    errors: list[str] = []
    for key in sorted(set(core) - CORE_KEYS):
        errors.append(f"core: unknown key {key}")
    if core.get("schema") != CORE_SCHEMA:
        errors.append(f"core: schema must be {CORE_SCHEMA}")
    if not isinstance(core.get("revision"), int) or core["revision"] < 1:
        errors.append("core: revision must be a positive integer")
    binding = core.get("binding")
    if not isinstance(binding, dict) or binding.get("root") != str(BINDING_ROOT):
        errors.append("core: binding.root does not match this KB's exact parent folder; rebinding needs an assignment")
    for key in ("purpose", "owner"):
        value = core.get(key, "")
        if not isinstance(value, str) or utf8_len(value) > CORE_LIMITS[key]:
            errors.append(f"core: {key} must be text of at most {CORE_LIMITS[key]} B")
    if "guest_reads" in core and not isinstance(core["guest_reads"], bool):
        errors.append("core: guest_reads must be true or false")
    task = core.get("current_task")
    if task is not None and (task not in state.by_id or state.by_id[task]["kind"] != "task"):
        errors.append("core: current_task must name an existing task record or be null")
    pinned = core.get("pinned", [])
    if not isinstance(pinned, list) or any(item not in state.by_id for item in pinned) \
            or len(pinned) > CORE_LIMITS["pinned"] or len(set(pinned)) != len(pinned):
        errors.append(f"core: pinned must list at most {CORE_LIMITS['pinned']} distinct existing record IDs")
    routes = core.get("routes", {})
    if not isinstance(routes, dict) or len(routes) > CORE_LIMITS["routes"]:
        errors.append(f"core: routes must be an object of at most {CORE_LIMITS['routes']} topics")
    else:
        for topic, route in routes.items():
            if not TOPIC_RE.fullmatch(topic) or not isinstance(route, dict) or set(route) - {"about", "lead"}:
                errors.append(f"core: route {topic!r} must be a topic name with optional about and lead")
                continue
            about = route.get("about", "")
            if not isinstance(about, str) or utf8_len(about) > CORE_LIMITS["about"]:
                errors.append(f"core: route {topic} about must be text of at most {CORE_LIMITS['about']} B")
            lead = route.get("lead", [])
            if not isinstance(lead, list) or any(item not in state.by_id for item in lead) \
                    or len(lead) > CORE_LIMITS["lead"] or len(set(lead)) != len(lead):
                errors.append(f"core: route {topic} lead must list at most {CORE_LIMITS['lead']} existing IDs")
    files = core.get("state_files")
    if not isinstance(files, dict) or set(files) != set(STATE_FILES) \
            or any(not isinstance(value, str) or not SHA_RE.fullmatch(value) for value in files.values()):
        errors.append("core: state_files must hold SHA-256 values for records, journal and sources")
    return errors, []


def validate_journal(state: State) -> list[str]:
    errors: list[str] = []
    counters: dict[tuple[str, str], int] = {}
    revisions = [entry.get("rev") for entry in state.writes]
    if revisions != sorted(revisions) or len(set(revisions)) != len(revisions):
        errors.append("journal: write entries must have increasing, distinct revisions")
    for number, entry in enumerate(state.journal, 1):
        kind = entry.get("type")
        if kind not in {"write", "log", "history"}:
            errors.append(f"journal line {number}: unknown type {kind!r}")
            continue
        if not isinstance(entry.get("rev"), int) or entry["rev"] > state.revision:
            errors.append(f"journal line {number}: rev must be an integer not above the KB revision")
        if kind == "write":
            continue
        rid = entry.get("id")
        if rid != "core" and rid not in state.by_id:
            errors.append(f"journal line {number}: {kind} entry for unknown record {rid!r}")
        key = (kind, rid)
        counters[key] = counters.get(key, 0) + 1
        if entry.get("n") != counters[key]:
            errors.append(f"journal line {number}: {kind} entries of {rid} must be numbered 1, 2, 3, ...")
        if kind == "log" and (not isinstance(entry.get("text"), str) or not entry["text"].strip()):
            errors.append(f"journal line {number}: log text must be nonempty")
        if kind == "log" and "origin" in entry:
            problems, _ = field_problems("origin", entry["origin"], state, kind="log")
            errors.extend(f"journal line {number}: {item}" for item in problems)
        if kind == "history" and "field" not in entry:
            errors.append(f"journal line {number}: history entry needs field")
    return errors


def validate_sources(state: State) -> list[str]:
    errors: list[str] = []
    rows = state.sources.get("sources")
    if set(state.sources) - {"sources"} or not isinstance(rows, list):
        errors.append("sources.json must be {\"sources\": [...]}")
        return errors
    seen = set()
    for row in rows:
        sid = row.get("source_id") if isinstance(row, dict) else None
        if not isinstance(sid, str) or not SOURCE_ID_RE.fullmatch(sid) or sid in seen:
            errors.append(f"source {sid!r}: invalid or duplicate source_id")
            continue
        seen.add(sid)
        locator = row.get("locator")
        if not isinstance(locator, dict) or locator.get("type") not in {"file", "url"}:
            errors.append(f"{sid}: locator must be a file or url locator")
        if "sha256" in row and not (isinstance(row["sha256"], str) and SHA_RE.fullmatch(row["sha256"])):
            errors.append(f"{sid}: sha256 must be an uppercase SHA-256")
    lookup = {row["source_id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("source_id"), str)}
    errors.extend(source_versions.validate_lineage(lookup, set(state.by_id), BINDING_ROOT))
    return errors


def validate_state(state: State) -> dict[str, Any]:
    errors, warnings = validate_core(state)
    ids = [row.get("id") for row in state.records]
    for duplicate in sorted({rid for rid in ids if ids.count(rid) > 1}, key=str):
        errors.append(f"duplicate record id {duplicate}")
    for row in state.records:
        problems, over = validate_record(row, state)
        errors.extend(problems)
        warnings.extend(over)
    errors.extend(validate_journal(state))
    errors.extend(validate_sources(state))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def prepared_path(source: dict[str, Any]) -> Path | None:
    prep = source.get("preparation")
    value = prep.get("path") if isinstance(prep, dict) else None
    if not isinstance(value, str) or not value:
        return None
    candidate = (KB_ROOT / value).resolve()
    return candidate if within(candidate, KB_ROOT) else None


def prepared_check(source: dict[str, Any]) -> dict[str, Any]:
    """Prepared bytes against the recorded hash, and their meta row against the source; never reads the original."""
    path = prepared_path(source)
    if path is None:
        return {"status": "none"}
    if not path.is_file():
        return {"status": "missing", "path": source["preparation"]["path"]}
    data = path.read_bytes()
    prep = source["preparation"]
    first = next((line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()), "")
    try:
        meta = json.loads(first)
    except ValueError:
        meta = {}
    bound = (sha256_bytes(data) == str(prep.get("prepared_sha256", "")).upper() and meta.get("type") == "meta"
             and meta.get("source_id") == source.get("source_id")
             and str(meta.get("source_sha256", "")).upper() == str(source.get("sha256", "")).upper())
    return {"status": "ok" if bound else "changed", "path": prep["path"]}


def prepared_rows(state: State) -> Iterable[tuple[str, dict[str, Any]]]:
    """Content rows of every prepared source whose bytes still match their record; others are skipped."""
    for source in state.sources.get("sources", []):
        if prepared_check(source)["status"] != "ok":
            continue
        for row in parse_jsonl(prepared_path(source).read_bytes(), source["source_id"]):
            if row.get("type") != "meta":
                yield source["source_id"], row


def check_sources(state: State) -> dict[str, Any]:
    """Re-hash registered file sources. A record-only write checks only that cited source IDs exist; this is the
    check of current source bytes against the hash recorded at registration."""
    report: dict[str, Any] = {"checked": 0, "unchanged": [], "changed": [], "unavailable": [], "not_checkable": [],
                              "prepared_changed": [], "history_verified": [], "history_unavailable": [],
                              "history_failed": []}
    for row in state.sources.get("sources", []):
        locator = row.get("locator", {})
        if row.get("supersession") is not None:
            # A historical version: its file has moved on by design; check it against recorded history instead.
            try:
                checked = source_versions.verify_history(BINDING_ROOT, row)
                report["history_verified" if checked["history_verified"] else "history_unavailable"].append(row["source_id"])
            except source_versions.VersionError:
                report["history_failed"].append(row["source_id"])
            continue
        if locator.get("type") != "file" or not row.get("sha256"):
            report["not_checkable"].append(row.get("source_id"))
            continue
        report["checked"] += 1
        path = Path(locator.get("path", ""))
        if not path.is_file():
            report["unavailable"].append(row["source_id"])
        elif sha256_file(path) != row["sha256"]:
            report["changed"].append(row["source_id"])
        else:
            report["unchanged"].append(row["source_id"])
        if prepared_check(row)["status"] in {"missing", "changed"}:
            report["prepared_changed"].append(row["source_id"])
    return report


def require_valid(state: State) -> None:
    result = validate_state(state)
    if not result["ok"]:
        raise KBError("invalid_state", "KB state is invalid: " + "; ".join(result["errors"][:5]),
                      "Do not write. Run 'fist-kb admin validate' and report to the owner.",
                      error_count=len(result["errors"]))


# ------------------------------------------------------------------------------------------ tool identity


def tool_status() -> dict[str, Any]:
    """Compare the installed package files with the package manifest. Identity, not correctness or permission.

    The tools are the installed Python package, not files in the KB folder, so their recorded inventory is the
    manifest shipped with the package (fist_kb/_manifest.json); nothing in the KB folder is executed or recorded.
    """
    try:
        result = verify_package()
    except PackageIntegrityError as exc:
        return {"status": "mismatch", "files": ["_manifest.json"], "hint": f"The package manifest is unusable: {exc}"}
    if not result["ok"]:
        return {"status": "mismatch", "files": sorted(item["path"] for item in result["errors"]),
                "hint": "Installed package files differ from the package manifest. Reads work; writes are refused "
                        "until the reviewed package is reinstalled."}
    return {"status": "ok"}


# -------------------------------------------------------------------------------------- commit protocol


def stage_file(path: Path, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def write_outcome(progress: dict[str, Any], stage: str) -> dict[str, Any]:
    return {"stage": stage, "replaced_files": list(progress["replaced"]),
            "commit_completed": progress["committed"], "verification_completed": progress["verified"],
            "rollback_performed": False, "cleanup_errors": list(progress["cleanup_errors"])}


def outcome_for(progress: dict[str, Any]) -> tuple[str, str]:
    """Map commit progress to the model-facing outcome and the v07-compatible stage name."""
    if progress["verified"]:
        return "committed", "commit_completed"
    if progress["committed"]:
        return "committed_unverified", "commit_completed_verification_failed"
    if progress["replaced"] or progress.get("uncertain"):
        return "uncertain", "partial_replacement" if progress["replaced"] else "replacement_uncertain"
    return "not_written", "before_replacement"


# Fault injection for tests only: name the step after which to raise, e.g. "after:records".
FAULT_ENV = "SEED_KB_TEST_FAULT"


def _fault(step: str) -> None:
    if os.environ.get(FAULT_ENV) == step:
        raise OSError(f"injected fault {step}")


def commit(base: State, core: dict[str, Any], records: list[dict[str, Any]], journal: list[dict[str, Any]],
           sources: dict[str, Any], progress: dict[str, Any]) -> State:
    """Publish one revision: stage all files, check nobody changed the base, replace the state files, then core
    last. Verification reloads and validates the committed state. Progress is updated in place so the caller can
    report an honest outcome whatever fails."""
    payloads = {"records": jsonl_bytes(records), "journal": jsonl_bytes(journal), "sources": json_bytes(sources)}
    core = {**core, "state_files": {name: sha256_bytes(data) for name, data in payloads.items()}}
    candidate = State(core, records, journal, sources, core["state_files"])
    require_valid(candidate)
    paths = {**STATE_FILES, "core": CORE_PATH}
    payloads["core"] = json_bytes(core)
    staged: dict[str, Path] = {}
    try:
        for name, data in payloads.items():
            staged[name] = stage_file(paths[name], data)
        if sha256_file(CORE_PATH) != base.file_hashes["core"] or any(
                sha256_file(STATE_FILES[name]) != base.file_hashes[name] for name in STATE_FILES):
            raise KBError("concurrent_write", "the KB changed while this write was prepared",
                          "Nothing was written. Re-read and resend.")
        _fault("before:replace")
        for name in ("records", "journal", "sources", "core"):
            if payloads[name] == (paths[name].read_bytes() if name != "core" else None):
                staged.pop(name).unlink(missing_ok=True)
                continue
            progress["uncertain"] = True
            os.replace(staged.pop(name), paths[name])
            progress["uncertain"] = False
            progress["replaced"].append(paths[name].name)
            if name == "core":
                progress["committed"] = True
            _fault(f"after:{name}")
    finally:
        for temp in staged.values():
            try:
                temp.unlink(missing_ok=True)
            except OSError as exc:
                progress["cleanup_errors"].append({"path": temp.name, "error": str(exc)})
    committed = load_state()
    result = validate_state(committed)
    if not result["ok"] or committed.revision != core["revision"]:
        raise KBError("verification_failed", "post-commit verification failed",
                      "The write is committed but not verified. Do not write; report to the owner.")
    progress["verified"] = True
    return committed


def initial_files(purpose: str, owner: str) -> dict[Path, bytes]:
    records, journal, sources = b"", b"", json_bytes({"sources": []})
    core = {"schema": CORE_SCHEMA, "revision": 1, "binding": {"root": str(BINDING_ROOT)}, "purpose": purpose,
            "owner": owner, "current_task": None, "pinned": [], "routes": {},
            "state_files": {"records": sha256_bytes(records), "journal": sha256_bytes(journal),
                            "sources": sha256_bytes(sources)},
            "last_write": None}
    return {RECORDS_PATH: records, JOURNAL_PATH: journal, SOURCES_PATH: sources, CORE_PATH: json_bytes(core)}

