"""Fist KB writes (Seed KB v08 engine): one call validates, commits, verifies and echoes what was stored.

The model supplies meaning (operations and text); this module allocates IDs, resolves $refs, archives every
replaced value, builds and validates the candidate, commits it with core last and reads it back. A preview
stops before publication and returns a preview_id that commits exactly that candidate later.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import re
import types
from pathlib import Path
from typing import Any

from . import kb_store as store
from . import source_versions
from .kb_store import KBError, KINDS, LIMITS, CORE_LIMITS, FIELD_TYPES, SCHEMA, AUTOMATIC_FIELDS, utf8_len

WRITE_KEYS = {"changes", "note", "by", "expect_revision", "preview", "confirm", "request_id"}
OPS = ("new", "set", "log", "add", "remove", "status", "restore", "core", "source")
ID_FIELDS = {name for name, kind in FIELD_TYPES.items() if kind in {"ids", "source_ids"}}
ECHO_FULL_BYTES = 2000
DIFF_CONTEXT = 80
MAX_CHANGES = 200
NOTE_LIMIT = 1000
REF_RE = re.compile(r"^\$[A-Za-z][A-Za-z0-9_]{0,30}$")
PREVIEW_RE = re.compile(r"^P\d+-[0-9A-F]{16}$")
CORE_TARGET = {"current_task": "current_task", "pin": "pinned", "unpin": "pinned", "route": "routes",
               "remove_route": "routes", "purpose": "purpose", "owner": "owner", "guest_reads": "guest_reads"}
DEFAULT_ORIGIN = {"owner_decision": "owner_message", "source_claim": "source", "inference": "inference"}


def write(request: Any) -> dict[str, Any]:
    """The kb_write operation. Never raises; every result names its outcome."""
    progress = {"replaced": [], "committed": False, "verified": False, "cleanup_errors": []}
    try:
        return _write(request, progress)
    except KBError as exc:
        return failure(progress, exc.payload())
    except Exception as exc:  # An unexpected failure still gets an honest outcome.
        return failure(progress, {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"})


def failure(progress: dict[str, Any], error: dict[str, Any]) -> dict[str, Any]:
    outcome, stage = store.outcome_for(progress)
    advice = {
        "not_written": "Nothing was written. Fix the request and send it again.",
        "committed_unverified": "The write is committed but not verified. Stop writing, run kb_read({}) and report.",
        "uncertain": "Publication cannot be established. Do not resend: check kb_read({\"request\": ID}) or "
                     "kb_read({}) and report.",
        "committed": "The write is committed and verified; only reporting failed.",
    }[outcome]
    result = {"ok": outcome == "committed", "outcome": outcome, "error": error, "next": advice}
    if outcome != "not_written":
        result["detail"] = {"write_outcome": store.write_outcome(progress, stage)}
    return result


def _write(request: Any, progress: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise KBError("request_invalid", "a write request is one JSON object", "Send {\"changes\": [...]}.")
    unknown = sorted(set(request) - WRITE_KEYS)
    if unknown:
        raise KBError("request_invalid", f"unknown request keys: {', '.join(unknown)}",
                      f"Allowed keys: {', '.join(sorted(WRITE_KEYS))}.")
    tools = store.tool_status()
    if tools["status"] != "ok":
        raise KBError("tools_unverified", f"tool identity is {tools['status']}", tools.get("hint", ""),
                      files=tools.get("files", []))
    state = store.load_state()
    store.require_valid(state)
    actor = request.get("by") or os.environ.get("KB_ACTOR", "")
    if not isinstance(actor, str) or not actor.strip() or utf8_len(actor) > LIMITS["origin_value"]:
        raise KBError("actor_required", "the writer is not named",
                      "Pass \"by\" (your agent name) or let the host set KB_ACTOR.")
    if request.get("confirm") is not None:
        return confirm_preview(state, request, actor, progress)
    request_id = request.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
            raise KBError("request_invalid", "request_id must be a string of 1 to 120 characters")
        repeated = repeat_of(state, request_id, request_digest(request.get("changes"), request.get("note"), actor))
        if repeated:
            return repeated
    expected = request.get("expect_revision")
    if expected is not None and (not isinstance(expected, int) or not 1 <= expected <= state.revision):
        raise KBError("request_invalid", f"expect_revision must be a revision from 1 to {state.revision}",
                      "Use the revision of the read or write result your change is based on.")
    note = request.get("note")
    if note is not None and (not isinstance(note, str) or not note.strip() or utf8_len(note) > NOTE_LIMIT):
        raise KBError("request_invalid", f"note must be nonempty text of at most {NOTE_LIMIT} B")
    changes = request.get("changes")
    if not isinstance(changes, list) or not changes or len(changes) > MAX_CHANGES:
        raise KBError("request_invalid", f"changes must be a list of 1 to {MAX_CHANGES} operations",
                      "Example: {\"changes\": [{\"log\": {\"id\": \"TASK-0001\", \"text\": \"...\"}}]}")
    replacing = sorted({next(iter(change)) for change in changes if isinstance(change, dict) and len(change) == 1}
                       & set(SCHEMA["replacing_ops"]))
    if replacing and expected is None:
        raise KBError("revision_required", f"{', '.join(replacing)} replaces stored values and needs expect_revision",
                      "Pass expect_revision: the revision of the read or write result your change is based on (every "
                      "result reports one). A host may fill in the last revision it returned to you, never simply "
                      "the latest.")
    at = store.utc_now()
    builder = Builder(state, actor, at, expected)
    builder.apply_all(changes)
    builder.finish(note, request_id, request_digest(changes, note, actor))
    if request.get("preview"):
        return store_preview(state, builder, changes, actor, at, note, request_id)
    return publish(state, builder, progress)


def request_digest(changes: Any, note: Any, actor: str) -> str:
    """Identity of one request: its operations, note and writer, so a reused request_id cannot hide a different
    payload."""
    return store.sha256_bytes(json.dumps({"changes": changes, "note": note, "by": actor}, ensure_ascii=False,
                                         sort_keys=True, separators=(",", ":")).encode("utf-8"))


def repeat_of(state: store.State, request_id: str, digest: str) -> dict[str, Any] | None:
    earlier = state.requests.get(request_id)
    if earlier is None:
        return None
    if earlier.get("request_sha256") != digest:
        raise KBError("request_id_conflict", f"request_id {request_id!r} was committed at revision {earlier['rev']} "
                      "with different content", "Nothing was written. Use a new request_id for a different request.")
    return {"ok": True, "outcome": "committed", "repeated": True, "revision": earlier["rev"],
            "ids": earlier.get("ids", {}), "next": "This request was already committed; nothing was written again."}


def publish(state: store.State, builder: "Builder", progress: dict[str, Any]) -> dict[str, Any]:
    store.commit(state, builder.core, builder.records, builder.journal, builder.sources, progress)
    clear_pending()
    result = {"ok": True, "outcome": "committed", "revision": builder.rev, "from_revision": state.revision,
              "changes": builder.echo}
    if builder.refs:
        result["ids"] = {name[1:]: rid for name, rid in builder.refs.items()}
    if builder.warnings:
        result["warnings"] = builder.warnings
    return result


# ---------------------------------------------------------------------------------------------- preview


def candidate_hash(builder: "Builder") -> str:
    parts = [store.jsonl_bytes(builder.records), store.jsonl_bytes(builder.journal), store.json_bytes(builder.sources),
             store.json_bytes(builder.core)]
    return store.sha256_bytes(b"\0".join(parts))


def store_preview(state: store.State, builder: "Builder", changes: list[Any], actor: str, at: str, note: Any,
                  request_id: Any) -> dict[str, Any]:
    pending = {"format": "seed-kb-8-pending", "base_revision": state.revision, "at": at, "by": actor, "note": note,
               "request_id": request_id, "changes": changes, "expect_revision": builder.expected,
               "request_sha256": request_digest(changes, note, actor)}
    digest = store.sha256_bytes(json.dumps(pending, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    preview_id = f"P{state.revision}-{digest[:16]}"
    pending["candidate_sha256"] = candidate_hash(builder)
    store.PENDING_DIR.mkdir(exist_ok=True)
    (store.PENDING_DIR / f"{preview_id}.json").write_bytes(store.json_bytes(pending))
    result = {"ok": True, "outcome": "preview", "preview_id": preview_id, "base_revision": state.revision,
              "would_be_revision": builder.rev, "changes": builder.echo,
              "next": f"Nothing is written yet. Commit exactly this with {{\"confirm\": \"{preview_id}\"}}."}
    if builder.refs:
        result["ids"] = {name[1:]: rid for name, rid in builder.refs.items()}
    if builder.warnings:
        result["warnings"] = builder.warnings
    return result


def confirm_preview(state: store.State, request: dict[str, Any], actor: str, progress: dict[str, Any]) -> dict[str, Any]:
    preview_id = request["confirm"]
    extra = sorted(set(request) - {"confirm", "by"})
    if extra:
        raise KBError("request_invalid", f"confirm takes no other keys: {', '.join(extra)}",
                      "Send only {\"confirm\": PREVIEW_ID}; the previewed changes are stored.")
    path = store.PENDING_DIR / f"{preview_id}.json" if isinstance(preview_id, str) and PREVIEW_RE.fullmatch(preview_id) else None
    if path is None or not path.exists():
        raise KBError("preview_unknown", "no such preview", "Preview again; previews end when the KB changes.")
    pending = store.read_json_file(path)
    if pending["base_revision"] != state.revision:
        raise KBError("preview_stale", f"the KB changed since the preview (revision {pending['base_revision']} -> "
                      f"{state.revision})", "Nothing was written. Preview again.")
    if pending.get("request_id") is not None:
        repeated = repeat_of(state, pending["request_id"], pending["request_sha256"])
        if repeated:
            return repeated
    builder = Builder(state, pending["by"], pending["at"], pending.get("expect_revision"))
    builder.apply_all(pending["changes"])
    builder.finish(pending["note"], pending["request_id"], pending["request_sha256"])
    if candidate_hash(builder) != pending["candidate_sha256"]:
        raise KBError("preview_mismatch", "the rebuilt candidate differs from the preview",
                      "Nothing was written. Preview again.")
    result = publish(state, builder, progress)
    result["confirmed"] = preview_id
    return result


def clear_pending() -> None:
    if store.PENDING_DIR.exists():
        for path in store.PENDING_DIR.glob("P*.json"):
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------------------------- builder


class Builder:
    """Applies operations to a copy of one revision; nothing touches disk until publish."""

    def __init__(self, state: store.State, actor: str, at: str, expected: int | None = None):
        self.base = state
        self.expected = expected
        self.actor, self.at = actor, at
        self.rev = state.revision + 1
        self.records = copy.deepcopy(state.records)
        self.by_id = {row["id"]: row for row in self.records}
        self.journal = list(state.journal)
        self.sources = copy.deepcopy(state.sources) or {"sources": []}
        self.sources.setdefault("sources", [])
        self.core = copy.deepcopy(state.core)
        self.counts = {(kind, rid): len(rows) for kind, index in (("log", state.logs), ("history", state.history))
                       for rid, rows in index.items()}
        self.refs: dict[str, str] = {}
        self.echo: list[dict[str, Any]] = []
        self.ops: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.touched: set[str] = set()
        self.created: set[str] = set()

    # Allocation and references ---------------------------------------------------------------------

    def next_id(self, prefix: str, existing: set[str]) -> str:
        numbers = [int(rid.split("-", 1)[1]) for rid in existing if rid.startswith(prefix + "-")]
        return f"{prefix}-{max(numbers, default=0) + 1:04d}"

    def allocate(self, changes: list[Any]) -> None:
        record_ids = set(self.by_id)
        source_ids = {row["source_id"] for row in self.sources["sources"]}
        for index, change in enumerate(changes):
            if not isinstance(change, dict) or len(change) != 1 or next(iter(change)) not in OPS:
                raise KBError("operation_invalid", f"change {index} must be one object with one of: {', '.join(OPS)}",
                              "Example: {\"log\": {\"id\": \"TASK-0001\", \"text\": \"...\"}}", change=index)
            verb, body = next(iter(change.items()))
            if verb not in {"new", "source"} or not isinstance(body, dict):
                continue
            if verb == "new":
                kind = body.get("kind")
                if kind not in KINDS:
                    raise KBError("kind_invalid", f"change {index}: unknown kind {kind!r}",
                                  f"Kinds: {', '.join(KINDS)}.", change=index)
                rid = self.next_id(KINDS[kind]["prefix"], record_ids)
                record_ids.add(rid)
            else:
                rid = self.next_id("SRC", source_ids)
                source_ids.add(rid)
            ref = body.get("ref")
            if ref is not None:
                name = f"${ref}" if isinstance(ref, str) and not ref.startswith("$") else ref
                if not isinstance(name, str) or not REF_RE.fullmatch(name) or name in self.refs:
                    raise KBError("ref_invalid", f"change {index}: ref must be a new short name like \"d\"",
                                  change=index)
                self.refs[name] = rid
            body["_id"] = rid
        self.known = types.SimpleNamespace(by_id=record_ids, source_ids=source_ids)

    def rid(self, value: Any, index: int) -> Any:
        if isinstance(value, str) and value.startswith("$"):
            if value not in self.refs:
                raise KBError("unknown_ref", f"change {index}: {value} is not defined by a new or source operation "
                              "in this call", "Give the new record \"ref\": \"name\" and refer to it as \"$name\".",
                              change=index)
            return self.refs[value]
        return value

    def resolve(self, field: str, value: Any, index: int) -> Any:
        if field in ID_FIELDS and isinstance(value, list):
            return [self.rid(item, index) for item in value]
        if field == "resolution" and isinstance(value, dict) and isinstance(value.get("ids"), list):
            return {**value, "ids": [self.rid(item, index) for item in value["ids"]]}
        return value

    def record(self, value: Any, index: int) -> dict[str, Any]:
        rid = self.rid(value, index)
        if not isinstance(rid, str) or rid not in self.by_id:
            known = [item for item in self.by_id if isinstance(rid, str) and item.split("-")[0] == rid.split("-")[0]]
            raise KBError("unknown_id", f"change {index}: no record {rid!r} in this KB (revision {self.base.revision})",
                          "Use an existing ID, or a $ref to a record created earlier in this call.",
                          change=index, nearest=difflib.get_close_matches(str(rid), known, n=3, cutoff=0.5))
        return self.by_id[rid]

    def fresh(self, row: dict[str, Any], index: int) -> dict[str, Any]:
        """A replacing operation must be based on the target's current revision: refuse a stale view."""
        if row["id"] not in self.created and self.expected is not None and row.get("updated_rev", 0) > self.expected:
            since = [{"rev": entry["rev"], "by": entry.get("by"), "note": entry.get("note")}
                     for entry in self.base.writes if entry["rev"] > self.expected
                     and any(op.get("id") == row["id"] for op in entry.get("ops", []))][-5:]
            raise KBError("stale_target", f"change {index}: {row['id']} changed at revision {row['updated_rev']}, after "
                          f"the revision {self.expected} this change is based on", "Nothing was written. Read the "
                          "record again and resend with the new revision.", change=index, id=row["id"],
                          written_since=since)
        return row

    # Checks ------------------------------------------------------------------------------------------

    def check_field(self, kind: str, field: str, value: Any, index: int) -> None:
        allowed = store.kind_fields(kind)
        if field in AUTOMATIC_FIELDS:
            hint = "Use the status operation." if field in {"status", "superseded_by", "supersedes"} else \
                "This field is set by the tool."
            raise KBError("field_protected", f"change {index}: {field} cannot be written directly", hint, change=index)
        if field not in allowed:
            close = difflib.get_close_matches(field, allowed, n=2, cutoff=0.6)
            hint = (f"Did you mean {' or '.join(close)}? " if close else "") + \
                f"{kind} fields: {', '.join(allowed)}. Put dated or other detail in a log entry " \
                "({\"log\": {\"id\": ..., \"text\": ...}}) or in a new linked record."
            raise KBError("field_not_allowed", f"change {index}: {kind} has no field {field!r}", hint,
                          change=index, field=field)
        errors, over = store.field_problems(field, value, self.known, kind=kind)
        if errors:
            raise KBError("value_invalid", f"change {index}: {errors[0]}", change=index, field=field)
        if over:
            hint = "Keep the current answer here; move background to a log entry, or detail to a new linked record."
            if field in store.SCHEMA["list_fields"]:
                hint = "Split the list, or link a record that groups the items."
            raise KBError("field_too_large", f"change {index}: {over[0]}", hint, change=index, field=field)

    # History -----------------------------------------------------------------------------------------

    def entry(self, kind: str, rid: str, **fields: Any) -> int:
        key = (kind, rid)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.journal.append({"type": kind, "rev": self.rev, "at": self.at, "by": self.actor, "id": rid,
                             "n": self.counts[key], **fields})
        return self.counts[key]

    def archive(self, rid: str, field: str, value: Any) -> dict[str, Any]:
        number = self.entry("history", rid, field=field, value=value)
        return {"history": number, "bytes": utf8_len(json.dumps(value, ensure_ascii=False))}

    def replace_field(self, row: dict[str, Any], field: str, value: Any, index: int, op: str) -> dict[str, Any]:
        old = row.get(field)
        if value is None:
            if field in SCHEMA["required_fields"]:
                raise KBError("value_invalid", f"change {index}: {field} is required and cannot be removed",
                              change=index, field=field)
            if field not in row:
                return {"op": op, "id": row["id"], "field": field, "unchanged": True}
            del row[field]
            self.touched.add(row["id"])
            return {"op": op, "id": row["id"], "field": field, "removed": True, "archived": self.archive(row["id"], field, old)}
        self.check_field(row["kind"], field, value, index)
        if old == value:
            return {"op": op, "id": row["id"], "field": field, "unchanged": True}
        row[field] = value
        self.touched.add(row["id"])
        echo = {"op": op, "id": row["id"], "field": field, "stored": echo_value(value, old)}
        if old is not None:
            echo["archived"] = self.archive(row["id"], field, old)
        return echo

    # Operations -------------------------------------------------------------------------------------

    def apply_all(self, changes: list[Any]) -> None:
        changes = copy.deepcopy(changes)
        self.allocate(changes)
        for index, change in enumerate(changes):
            verb, body = next(iter(change.items()))
            if not isinstance(body, dict):
                raise KBError("operation_invalid", f"change {index}: {verb} takes an object", change=index)
            getattr(self, f"op_{verb}")(body, index)

    def keys(self, body: dict[str, Any], index: int, verb: str, allowed: set[str], required: set[str]) -> None:
        extra = sorted(set(body) - allowed - {"_id"})
        missing = sorted(required - set(body))
        if extra or missing:
            problem = f"unknown keys {', '.join(extra)}" if extra else f"missing {', '.join(missing)}"
            raise KBError("operation_invalid", f"change {index}: {verb} has {problem}",
                          f"{verb} takes: {', '.join(sorted(allowed))}.", change=index)

    def op_new(self, body: dict[str, Any], index: int) -> None:
        kind, rid = body["kind"], body["_id"]
        fields = {key: value for key, value in body.items() if key not in {"kind", "ref", "_id", "status"}}
        status = body.get("status", KINDS[kind]["default_status"])
        if status not in KINDS[kind]["statuses"]:
            raise KBError("status_not_allowed", f"change {index}: {status!r} is not a status of {kind}",
                          f"Use one of {', '.join(KINDS[kind]['statuses'])}.", change=index)
        for field in SCHEMA["required_fields"]:
            if field not in fields:
                raise KBError("value_invalid", f"change {index}: new {kind} needs {field}", change=index, field=field)
        row: dict[str, Any] = {"id": rid, "kind": kind, "status": status}
        origin = fields.pop("origin", None)
        for field, value in fields.items():
            value = self.resolve(field, value, index)
            self.check_field(kind, field, value, index)
            row[field] = value
        # origin is provenance (who said or produced it, and when), never silently the recorder or the write time.
        origin = dict(origin) if isinstance(origin, dict) else {"type": DEFAULT_ORIGIN.get(kind, "observation")}
        self.check_field(kind, "origin", origin, index)
        row["origin"] = origin
        row.update(created_at=self.at, created_by=self.actor, updated_at=self.at, created_rev=self.rev,
                   updated_rev=self.rev)
        self.records.append(row)
        self.by_id[rid] = row
        self.created.add(rid)
        if kind == "owner_decision" and "quote" not in row:
            self.warnings.append(f"{rid}: no quote; add the owner's verbatim words if you have them")
        self.ops.append({"op": "new", "id": rid})
        self.echo.append({"op": "new", "id": rid, "kind": kind, "status": status,
                          "stored": {key: echo_value(value, None) for key, value in row.items()
                                     if key not in {"id", "kind", "status", "created_at", "created_by", "updated_at",
                                                    "created_rev", "updated_rev"}}})

    def op_set(self, body: dict[str, Any], index: int) -> None:
        self.keys(body, index, "set", {"id", "fields"}, {"id", "fields"})
        row = self.fresh(self.record(body["id"], index), index)
        fields = body["fields"]
        if not isinstance(fields, dict) or not fields:
            raise KBError("operation_invalid", f"change {index}: set needs fields: {{name: value}}", change=index)
        for field, value in fields.items():
            echo = self.replace_field(row, field, self.resolve(field, value, index), index, "set")
            self.echo.append(echo)
            if not echo.get("unchanged"):
                self.ops.append({"op": "set", "id": row["id"], "field": field})

    def op_log(self, body: dict[str, Any], index: int) -> None:
        self.keys(body, index, "log", {"id", "text", "origin"}, {"id", "text"})
        row = self.record(body["id"], index)
        text = body["text"]
        if not isinstance(text, str) or not text.strip():
            raise KBError("value_invalid", f"change {index}: log text must be nonempty", change=index)
        if utf8_len(text) > LIMITS["log_text"]:
            raise KBError("field_too_large", f"change {index}: log text is {utf8_len(text):,} B; the limit is "
                          f"{LIMITS['log_text']:,}", "Split it into several log entries, or put the detail in a new "
                          "linked record.", change=index)
        extra = {}
        if "origin" in body:
            self.check_field(row["kind"], "origin", body["origin"], index)
            extra["origin"] = body["origin"]
        number = self.entry("log", row["id"], text=text, **extra)
        self.touched.add(row["id"])
        self.ops.append({"op": "log", "id": row["id"]})
        self.echo.append({"op": "log", "id": row["id"], "entry": number, "stored": echo_value(text, None)})

    def op_add(self, body: dict[str, Any], index: int, remove: bool = False) -> None:
        verb = "remove" if remove else "add"
        self.keys(body, index, verb, {"id", "field", "items"}, {"id", "field", "items"})
        row = self.record(body["id"], index)
        if remove:
            self.fresh(row, index)
        field, items = body["field"], body["items"]
        if field not in SCHEMA["list_fields"] or field not in store.kind_fields(row["kind"]):
            lists = [name for name in SCHEMA["list_fields"] if name in store.kind_fields(row["kind"])]
            raise KBError("field_not_allowed", f"change {index}: {field!r} is not a list field of {row['kind']}",
                          f"List fields: {', '.join(lists)}. Use set for other fields.", change=index)
        if not isinstance(items, list) or not items:
            raise KBError("operation_invalid", f"change {index}: items must be a nonempty list", change=index)
        items = self.resolve(field, items, index)
        current = list(row.get(field, []))
        if remove:
            new = [item for item in current if item not in items]
            skipped = [item for item in items if item not in current]
        else:
            new = current + [item for item in dict.fromkeys(items) if item not in current]
            skipped = [item for item in items if item in current]
        echo = self.replace_field(row, field, new if new else None, index, verb)
        echo.pop("stored", None)
        echo.update({"items": [item for item in items if item not in skipped], "count": len(new)})
        if skipped:
            echo["skipped"] = skipped
        self.echo.append(echo)
        if not echo.get("unchanged"):
            self.ops.append({"op": verb, "id": row["id"], "field": field})

    def op_remove(self, body: dict[str, Any], index: int) -> None:
        self.op_add(body, index, remove=True)

    def op_status(self, body: dict[str, Any], index: int) -> None:
        self.keys(body, index, "status", {"id", "status", "by", "reason"}, {"id", "status"})
        row = self.fresh(self.record(body["id"], index), index)
        status = body["status"]
        if status not in KINDS[row["kind"]]["statuses"]:
            raise KBError("status_not_allowed", f"change {index}: {status!r} is not a status of {row['kind']}",
                          f"Use one of {', '.join(KINDS[row['kind']]['statuses'])}.", change=index)
        newer = None
        if status == "superseded":
            if "by" not in body:
                raise KBError("operation_invalid", f"change {index}: superseded needs \"by\": the newer record",
                              change=index)
            newer = self.record(body["by"], index)
            if newer["id"] == row["id"]:
                raise KBError("operation_invalid", f"change {index}: a record cannot supersede itself", change=index)
        elif "by" in body:
            raise KBError("operation_invalid", f"change {index}: \"by\" applies only to superseded", change=index)
        old = row["status"]
        if old == status and row.get("superseded_by") == (newer or {}).get("id"):
            self.echo.append({"op": "status", "id": row["id"], "unchanged": True})
            return
        archived = self.archive(row["id"], "status", old)
        previous = row.get("superseded_by")
        if previous and (newer is None or previous != newer["id"]):
            self.archive(row["id"], "superseded_by", previous)
            other = self.by_id.get(previous)
            if other and row["id"] in other.get("supersedes", []):
                other["supersedes"] = [item for item in other["supersedes"] if item != row["id"]] or None
                if other["supersedes"] is None:
                    del other["supersedes"]
                self.touched.add(other["id"])
            row.pop("superseded_by", None)
        row["status"] = status
        if newer is not None:
            row["superseded_by"] = newer["id"]
            newer["supersedes"] = list(dict.fromkeys([*newer.get("supersedes", []), row["id"]]))
            self.touched.add(newer["id"])
        self.touched.add(row["id"])
        reason = body.get("reason")
        if reason is not None:
            if not isinstance(reason, str) or not reason.strip() or utf8_len(reason) > LIMITS["text"]:
                raise KBError("value_invalid", f"change {index}: reason must be text of at most {LIMITS['text']} B",
                              change=index)
            target = f" by {newer['id']}" if newer else ""
            self.entry("log", row["id"], text=f"Status {old} -> {status}{target}: {reason}")
        self.ops.append({"op": "status", "id": row["id"]})
        echo = {"op": "status", "id": row["id"], "from": old, "to": status, "archived": archived}
        if newer is not None:
            echo["by"] = newer["id"]
        self.echo.append(echo)

    def op_restore(self, body: dict[str, Any], index: int) -> None:
        self.keys(body, index, "restore", {"id", "field", "history"}, {"id", "history"})
        row = self.fresh(self.record(body["id"], index), index)
        number = body["history"]
        entries = [item for item in self.journal if item.get("type") == "history" and item.get("id") == row["id"]
                   and item.get("n") == number]
        if not entries:
            raise KBError("history_unknown", f"change {index}: {row['id']} has no history entry {number!r}",
                          "Read the record with \"history\": true to see its entries.", change=index)
        entry = entries[0]
        field = body.get("field", entry["field"])
        if field != entry["field"]:
            raise KBError("operation_invalid", f"change {index}: history {number} holds {entry['field']}, not {field}",
                          change=index)
        if field in {"status", "superseded_by"}:
            raise KBError("field_protected", f"change {index}: restore a status with the status operation",
                          change=index)
        echo = self.replace_field(row, field, copy.deepcopy(entry["value"]), index, "restore")
        echo["restored_from"] = number
        self.echo.append(echo)
        if not echo.get("unchanged"):
            self.ops.append({"op": "restore", "id": row["id"], "field": field})

    def op_core(self, body: dict[str, Any], index: int) -> None:
        allowed = set(CORE_TARGET)
        if len(body) != 1 or next(iter(body)) not in allowed:
            raise KBError("operation_invalid", f"change {index}: core takes exactly one of {', '.join(sorted(allowed))}",
                          "Example: {\"core\": {\"current_task\": \"TASK-0001\"}}", change=index)
        key, value = next(iter(body.items()))
        core = self.core
        target = CORE_TARGET[key]
        changed = [entry["rev"] for entry in self.base.writes if self.expected is not None and entry["rev"] > self.expected
                   and any(op.get("op") == "core" and op.get("field", "").split(".")[0] == target
                           for op in entry.get("ops", []))]
        if changed:
            raise KBError("stale_target", f"change {index}: core {key} changed at revision {changed[-1]}, after the "
                          f"revision {self.expected} this change is based on", "Nothing was written. Read again and "
                          "resend with the new revision.", change=index)
        if key == "current_task":
            if value is not None:
                task = self.record(value, index)
                if task["kind"] != "task":
                    raise KBError("value_invalid", f"change {index}: current_task must be a task record", change=index)
                value = task["id"]
            self.core_set("current_task", value, index)
        elif key in {"pin", "unpin"}:
            ids = [self.record(item, index)["id"] for item in (value if isinstance(value, list) else [value])]
            pinned = list(core.get("pinned", []))
            new = pinned + [rid for rid in ids if rid not in pinned] if key == "pin" else \
                [rid for rid in pinned if rid not in ids]
            if len(new) > CORE_LIMITS["pinned"]:
                raise KBError("field_too_large", f"change {index}: at most {CORE_LIMITS['pinned']} pinned records",
                              "Unpin one first, or put the record in a route instead.", change=index)
            self.core_set("pinned", new, index)
        elif key == "route":
            if not isinstance(value, dict) or set(value) - {"topic", "about", "lead"} or "topic" not in value:
                raise KBError("operation_invalid", f"change {index}: route is {{topic, about?, lead?}}", change=index)
            topic = value["topic"]
            if not isinstance(topic, str) or not store.TOPIC_RE.fullmatch(topic) or len(topic) > LIMITS["topic_name"]:
                raise KBError("value_invalid", f"change {index}: topic must be lowercase letters, digits, _ . -",
                              change=index)
            route = dict(core.get("routes", {}).get(topic, {}))
            if "about" in value:
                if not isinstance(value["about"], str) or utf8_len(value["about"]) > CORE_LIMITS["about"]:
                    raise KBError("field_too_large", f"change {index}: about is at most {CORE_LIMITS['about']} B",
                                  change=index)
                route["about"] = value["about"]
            if "lead" in value:
                lead = [self.record(item, index)["id"] for item in value["lead"]] if isinstance(value["lead"], list) else None
                if lead is None or len(lead) > CORE_LIMITS["lead"] or len(set(lead)) != len(lead):
                    raise KBError("value_invalid", f"change {index}: lead is a list of at most {CORE_LIMITS['lead']} "
                                  "distinct IDs", change=index)
                route["lead"] = lead
            routes = dict(core.get("routes", {}))
            if topic not in routes and len(routes) >= CORE_LIMITS["routes"]:
                raise KBError("field_too_large", f"change {index}: at most {CORE_LIMITS['routes']} routes", change=index)
            routes[topic] = route
            self.core_set("routes", routes, index, label=f"routes.{topic}")
        elif key == "guest_reads":
            if not isinstance(value, bool):
                raise KBError("value_invalid", f"change {index}: guest_reads is true or false", change=index)
            self.core_set("guest_reads", value, index)
        elif key == "remove_route":
            routes = dict(core.get("routes", {}))
            if value not in routes:
                raise KBError("value_invalid", f"change {index}: no route {value!r}", change=index)
            del routes[value]
            self.core_set("routes", routes, index, label=f"routes.{value}")
        else:
            if not isinstance(value, str) or utf8_len(value) > CORE_LIMITS[key]:
                raise KBError("field_too_large", f"change {index}: {key} is text of at most {CORE_LIMITS[key]} B",
                              change=index)
            self.core_set(key, value, index)

    def core_set(self, key: str, value: Any, index: int, label: str | None = None) -> None:
        old = self.core.get(key)
        label = label or key
        if old == value:
            self.echo.append({"op": "core", "field": label, "unchanged": True})
            return
        self.core[key] = value
        echo = {"op": "core", "field": label, "stored": value if not label.startswith("routes.") else
                value.get(label.split(".", 1)[1])}
        if old is not None:
            echo["archived"] = self.archive("core", key, old)
        self.echo.append(echo)
        self.ops.append({"op": "core", "field": label})

    def op_source(self, body: dict[str, Any], index: int) -> None:
        if "revises" in body:
            self.revise_source(body, index)
            return
        self.keys(body, index, "source", {"ref", "locator", "title", "sha256", "size_bytes"}, {"locator"})
        locator = body["locator"]
        row: dict[str, Any] = {"source_id": body["_id"]}
        if not isinstance(locator, dict):
            raise KBError("value_invalid", f"change {index}: locator is {{type: file, path}} or {{type: url, url}}",
                          change=index)
        if locator.get("type") == "file" and set(locator) == {"type", "path"}:
            path = Path(str(locator["path"]))
            if not path.is_absolute():
                raise KBError("value_invalid", f"change {index}: a file locator needs an absolute path", change=index)
            resolved = path.resolve()
            if not store.within(resolved, store.BINDING_ROOT) or store.within(resolved, store.KB_ROOT):
                raise KBError("value_invalid", f"change {index}: a source must be inside the bound root and outside "
                              "KnowledgeBase", change=index)
            if not resolved.is_file():
                raise KBError("value_invalid", f"change {index}: no file at {locator['path']}", change=index)
            digest, size = store.sha256_file(resolved), resolved.stat().st_size
            if body.get("sha256") and str(body["sha256"]).upper() != digest:
                raise KBError("value_invalid", f"change {index}: the file's SHA-256 is {digest}, not the one given",
                              change=index)
            row.update(locator={"type": "file", "path": str(path)}, sha256=digest, size_bytes=size)
        elif locator.get("type") == "url" and set(locator) == {"type", "url"} and \
                re.fullmatch(r"https?://\S+", str(locator.get("url", ""))):
            row["locator"] = {"type": "url", "url": locator["url"]}
            if body.get("sha256"):
                row["sha256"] = str(body["sha256"]).upper()
            if body.get("size_bytes") is not None:
                row["size_bytes"] = body["size_bytes"]
        else:
            raise KBError("value_invalid", f"change {index}: locator is {{type: file, path}} or {{type: url, url}}",
                          change=index)
        for existing in self.sources["sources"]:
            if existing.get("locator") == row["locator"]:
                raise KBError("duplicate_source", f"change {index}: already registered as {existing['source_id']}",
                              "Cite the existing source ID.", change=index, source_id=existing["source_id"])
        if body.get("title") is not None:
            if not isinstance(body["title"], str) or utf8_len(body["title"]) > LIMITS["title"]:
                raise KBError("value_invalid", f"change {index}: title is text of at most {LIMITS['title']} B",
                              change=index)
            row["title"] = body["title"]
        row.update(registered_at=self.at, registered_rev=self.rev, by=self.actor)
        self.sources["sources"].append(row)
        self.ops.append({"op": "source", "id": row["source_id"]})
        self.echo.append({"op": "source", "id": row["source_id"], "locator": row["locator"],
                          "sha256": row.get("sha256"), "size_bytes": row.get("size_bytes")})

    def revise_source(self, body: dict[str, Any], index: int) -> None:
        """A changed file gets a new source version at the same path; the old one keeps its identity and hash."""
        self.keys(body, index, "source", {"ref", "revises", "reason", "record_ids", "git_commit",
                                          "history_unavailable_reason"}, {"revises", "reason", "record_ids"})
        rows = {row["source_id"]: row for row in self.sources["sources"]}
        old = rows.get(body["revises"])
        if old is None:
            raise KBError("unknown_id", f"change {index}: no source {body['revises']!r}", change=index)
        if old.get("supersession") is not None or old.get("availability") is not None or old.get("locator", {}).get("type") != "file":
            raise KBError("value_invalid", f"change {index}: only the current version of a file source can be revised",
                          change=index)
        reason, record_ids = body["reason"], [self.rid(item, index) for item in body["record_ids"]] \
            if isinstance(body["record_ids"], list) else None
        if not isinstance(reason, str) or not reason.strip() or not record_ids \
                or any(item not in self.known.by_id for item in record_ids):
            raise KBError("value_invalid", f"change {index}: revising a source needs a reason and record_ids of the "
                          "records that explain the change", change=index)
        if ("git_commit" in body) == ("history_unavailable_reason" in body):
            raise KBError("value_invalid", f"change {index}: give git_commit or history_unavailable_reason",
                          "git_commit is a full commit that holds the old bytes; otherwise say why history is "
                          "unavailable.", change=index)
        path = Path(old["locator"]["path"])
        if not path.is_file():
            raise KBError("value_invalid", f"change {index}: no file at {path}", change=index)
        digest = store.sha256_file(path)
        if digest == old.get("sha256"):
            raise KBError("no_change", f"change {index}: the file still matches {old['source_id']}",
                          "No new version is needed.", change=index)
        try:
            history = source_versions.git_history(store.BINDING_ROOT, old, body["git_commit"]) if "git_commit" in body \
                else {"kind": "unavailable", "reason": str(body["history_unavailable_reason"]).strip()}
        except source_versions.VersionError as exc:
            raise KBError("value_invalid", f"change {index}: {exc}", change=index) from exc
        if history["kind"] == "unavailable" and not history["reason"]:
            raise KBError("value_invalid", f"change {index}: history_unavailable_reason must be nonempty", change=index)
        new_id = body["_id"]
        self.archive("core", f"sources.{old['source_id']}", copy.deepcopy(old))
        old["supersession"] = {"source_id": new_id, "actor": self.actor, "reason": reason, "record_ids": record_ids,
                               "observed_at": self.at, "history": history}
        row = {"source_id": new_id, "locator": copy.deepcopy(old["locator"]), "sha256": digest,
               "size_bytes": path.stat().st_size, "supersedes": old["source_id"], "registered_at": self.at,
               "registered_rev": self.rev, "by": self.actor}
        if old.get("title"):
            row["title"] = old["title"]
        self.sources["sources"].append(row)
        self.ops.append({"op": "source", "id": new_id})
        self.echo.append({"op": "source", "id": new_id, "revises": old["source_id"], "sha256": digest,
                          "history": history["kind"]})

    # Finish ----------------------------------------------------------------------------------------

    def finish(self, note: Any, request_id: Any, digest: str) -> dict[str, Any]:
        if not self.ops:
            raise KBError("no_change", "the operations change nothing", "Nothing was written; the values are already "
                          "stored.", changes=self.echo)
        for rid in self.touched - self.created:
            if rid in self.by_id:
                self.by_id[rid].update(updated_at=self.at, updated_rev=self.rev)
        note = note or "; ".join(" ".join(part for part in (op["op"], op.get("id"), op.get("field")) if part)
                                 for op in self.ops[:6]) + (" ..." if len(self.ops) > 6 else "")
        entry = {"type": "write", "rev": self.rev, "at": self.at, "by": self.actor, "note": note, "ops": self.ops}
        if self.refs:
            entry["ids"] = {name[1:]: rid for name, rid in self.refs.items()}
        if request_id is not None:
            entry["request_id"] = request_id
            entry["request_sha256"] = digest
        self.journal.append(entry)
        self.core["revision"] = self.rev
        self.core["last_write"] = {"rev": self.rev, "at": self.at, "by": self.actor, "note": note}
        return entry


def echo_value(value: Any, old: Any) -> Any:
    """Echo short values whole; for long text show the changed span against the old value."""
    if not isinstance(value, str) or utf8_len(value) <= ECHO_FULL_BYTES:
        return value
    if isinstance(old, str):
        return {"bytes": utf8_len(value), "diff": span_diff(old, value)}
    return {"bytes": utf8_len(value), "start": value[:600] + "…"}


def span_diff(old: str, new: str) -> dict[str, Any]:
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]:
        suffix += 1
    removed, added = old[prefix:len(old) - suffix], new[prefix:len(new) - suffix]

    def clip(text: str) -> str:
        return text if len(text) <= 1500 else text[:700] + f" …[{len(text) - 1400} characters]… " + text[-700:]

    return {"at": prefix, "before": old[max(0, prefix - DIFF_CONTEXT):prefix], "removed": clip(removed),
            "added": clip(added), "after": new[len(new) - suffix:len(new) - suffix + DIFF_CONTEXT]}
