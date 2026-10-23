#!/usr/bin/env python3
"""Read-only guest access to the configured Fist KnowledgeBase, interface version 1.

Checks the packaged runtime identities and executes exactly the checked backend
bytes. No Seed role, installation receipt or data-directory executable is used.
"""
from __future__ import annotations

import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.realpath(__file__))
# Only the checked modules below may come from the tools directory.
sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry or os.curdir) != HERE]

import argparse
import contextlib
import hashlib
import io
import json
import re
import types
from pathlib import Path
from typing import Any

INTERFACE_VERSION = 1
TOOLS = Path(HERE)
from ..runtime import configured_root, package_manifest, PackageIntegrityError

KB_ROOT = configured_root()
ROOT = KB_ROOT.parent
# Execution order: kb.py imports the two helpers.
MODULES = ("source_versions", "memory_view", "kb")
CHECKED_FILES = ("guest_access.py", *(name + ".py" for name in MODULES))
DEFAULT_LIMIT = 10
DEFAULT_BUDGET = 6000
# The longest identifier (CLAIM-0001) plus a full digest as one compact map entry.
FINGERPRINT_ENTRY_BYTES = len('"CLAIM-0001":"' + "0" * 64 + '",')
FIELD_RE = re.compile(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*")
SLICE_RE = re.compile(r"(?P<field>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*):(?P<start>[0-9]{1,6}):(?P<count>[0-9]{1,3})")
CATEGORIES = {
    "invalid_arguments": "invalid_request", "revision_required": "invalid_request",
    "budget_too_small": "invalid_request", "item_too_large": "invalid_request",
    "invalid_array_path": "invalid_request", "unsupported_array_page": "invalid_request",
    "unknown_id": "missing_record",
    "revision_changed": "changed_revision",
    "inconsistent_state": "inconsistent_state",
    "reader_dependency_changed": "reader_unavailable", "reader_dependency_conflict": "reader_unavailable",
    "reader_identity_unavailable": "reader_unavailable", "binding_mismatch": "reader_unavailable",
    "guest_access_disabled": "reader_unavailable",
}
# Native codes for a state that does not match its commit marker or fails ordinary validation.
STATE_ERROR_CODES = {"state_hash_mismatch", "validation_failed", "invalid_encoding"}
OWNER_ACTION = ("Reinstall or rebuild the reviewed Fist KB package and verify its package manifest. "
                "The reader does not execute tools from the data directory.")


class GuestError(Exception):
    """A reported read failure; the reader repairs and retries nothing."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


class GuestParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.update(allow_abbrev=False, add_help=False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise GuestError("invalid_arguments", message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def fingerprint(value: Any) -> str:
    """canonical-json-sha256-v1 of a complete original object, before any projection."""
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8"))


def parser() -> argparse.ArgumentParser:
    result = GuestParser(prog="guest_access.py")
    commands = result.add_subparsers(dest="operation", required=True)
    info = commands.add_parser("info")
    info.add_argument("--expect-revision", type=int)
    for name in ("search", "get"):
        command = commands.add_parser(name)
        command.add_argument("--expect-revision", type=int)
        command.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        command.add_argument("--offset", type=int, default=0)
        command.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
        if name == "search":
            command.add_argument("--query", required=True)
        else:
            command.add_argument("--id", required=True)
            command.add_argument("--fields")
            command.add_argument("--slice")
    return result


def validate_request(args: argparse.Namespace) -> None:
    """Request checks needing no KB code; native validation still applies afterwards."""
    if args.expect_revision is not None and args.expect_revision < 1:
        raise GuestError("invalid_arguments", "Expected revision must be positive.")
    if args.operation == "info":
        return
    if not 1 <= args.limit <= 100 or args.offset < 0 or not 300 <= args.budget <= 20000:
        raise GuestError("invalid_arguments", "Use --limit 1..100, a nonnegative --offset and --budget 300..20000.")
    if args.operation == "search":
        if not args.query.strip() or len(args.query) > 500:
            raise GuestError("invalid_arguments", "Search requires 1..500 query characters.")
        if args.offset and args.expect_revision is None:
            raise GuestError("revision_required", "Continuation requires --expect-revision from the first page.")
        return
    if args.offset:
        raise GuestError("invalid_arguments",
                         "Single-record get has no --offset continuation; narrow --fields or page an array with --slice.")
    if args.fields is not None:
        args.fields = args.fields.split(",")
        if len(args.fields) > 32 or any(len(field) > 120 or not FIELD_RE.fullmatch(field) for field in args.fields):
            raise GuestError("invalid_arguments", "Use at most 32 comma-separated dotted field names.")
    if args.slice is not None:
        match = SLICE_RE.fullmatch(args.slice)
        if not match or len(match["field"]) > 120 or not 1 <= int(match["count"]) <= 100:
            raise GuestError("invalid_arguments", "Slice is FIELD:START:COUNT with COUNT 1..100 on one record's array field.")
        args.slice = (match["field"], int(match["start"]), int(match["count"]))
        # The paged array is part of the selection, as a guest naming it would expect.
        if args.fields is not None and not any(match["field"] == field or match["field"].startswith(field + ".")
                                               for field in args.fields):
            if len(args.fields) == 32:
                raise GuestError("invalid_arguments", "Select at most 31 --fields besides the sliced array.")
            args.fields.append(match["field"])
        if args.slice[1] and args.expect_revision is None:
            raise GuestError("revision_required", "A slice continuation requires --expect-revision from the first page.")


def parse_json(data: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate key")
        return dict(pairs)

    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique)


def installed_identities() -> tuple[dict[str, Any], dict[str, Any]]:
    """Read package identities, never data-directory code or a Seed installation."""
    try:
        manifest, manifest_sha256 = package_manifest()
    except PackageIntegrityError as error:
        raise GuestError("reader_identity_unavailable", str(error)) from error
    return {"source": "python_package_manifest", "file": "fist_kb/_manifest.json",
            "package_id": manifest["package"], "version": manifest["version"],
            "manifest_sha256": manifest_sha256}, manifest["files"]


def load_backend() -> tuple[Any, dict[str, Any], str]:
    """Check every executed file against the inventory, then execute exactly those bytes."""
    identity, files = installed_identities()
    checked: dict[str, bytes] = {}
    for name in CHECKED_FILES:
        path = "_engine/" + name
        try:
            data = (TOOLS / name).read_bytes()
        except OSError as error:
            raise GuestError("reader_dependency_changed", f"{path} is unreadable. " + OWNER_ACTION, path=path) from error
        expected = files.get(path) if isinstance(files, dict) else None
        if expected != {"bytes": len(data), "sha256": digest(data)}:
            raise GuestError("reader_dependency_changed", f"{path} does not match {identity['file']}. " + OWNER_ACTION,
                             path=path, identity_source=identity["source"])
        checked[name] = data
    for name in MODULES:
        qualified = "fist_kb._engine." + name
        if qualified in sys.modules:
            raise GuestError("reader_dependency_conflict", f"Module {qualified} is already loaded in this process.")
        module = types.ModuleType(qualified)
        module.__file__ = str(TOOLS / (name + ".py"))
        module.__package__ = "fist_kb._engine"
        sys.modules[qualified] = module
        exec(compile(checked[name + ".py"], module.__file__, "exec", dont_inherit=True), module.__dict__)
    kb = sys.modules["fist_kb._engine.kb"]
    if kb.KB_ROOT != KB_ROOT.resolve():
        raise GuestError("binding_mismatch", "The KB tools are bound to a different KnowledgeBase.")
    identity["files"] = {"_engine/" + name: digest(data) for name, data in checked.items()}
    return kb, identity, digest(checked["guest_access.py"])


def read_state(kb: Any) -> tuple[bytes, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """One validated committed state, with the core commit marker bytes used to detect a concurrent write."""
    try:
        before = kb.CORE_PATH.read_bytes()
        core, records, sources = kb.load_state()
        kb.require_valid(core, records, sources)
    except (kb.KBError, OSError) as error:
        details = {key: value for key, value in getattr(error, "details", {}).items() if key != "code"}
        raise GuestError("inconsistent_state", f"{error}. No repair was attempted; if the owner is writing, "
                         "read again later.", **details) from error
    if kb.CORE_PATH.read_bytes() != before:
        raise GuestError("revision_changed", "The KB changed during the read; read again from the first page.")
    return before, core, records, sources


def guest_settings(core: dict[str, Any]) -> dict[str, Any]:
    settings = core.get("guest_access", {})
    if not isinstance(settings, dict) or not isinstance(settings.get("owner", ""), str) \
            or not isinstance(settings.get("enabled", True), bool):
        raise GuestError("inconsistent_state", "core.json guest_access has an invalid owner label or enabled flag.")
    if settings.get("enabled") is False:
        raise GuestError("guest_access_disabled", "The KB owner has disabled guest access to this KnowledgeBase.")
    return settings


def large_records(kb: Any, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Records a default-budget get cannot return whole, with the arrays a slice can page."""
    sizes = sorted(((kb.estimate_tokens(row), row) for row in records), key=lambda pair: (-pair[0], pair[1]["id"]))
    large = [(size, row) for size, row in sizes if size > DEFAULT_BUDGET]
    entries = []
    for size, row in large[:10]:
        arrays = sorted(((len(value), key) for key, value in row.items() if isinstance(value, list)), reverse=True)
        entries.append({"id": row["id"], "estimated_tokens": size,
                        "array_fields": {key: count for count, key in arrays[:3]}})
    return {"count": len(large), "records": entries,
            "hint": "read selected --fields first; page an array with --slice FIELD:0:COUNT"}


def info_result(kb: Any, core: dict[str, Any], records: list[dict[str, Any]], sources: dict[str, Any],
                identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "read_only": True,
        "operations": ["info", "search", "get"],
        "extensions": {"get --slice FIELD:START:COUNT": "one page of one array field of one record; the other "
                       "selected fields stay complete. Combine with --fields; START > 0 needs --expect-revision."},
        "reader": "fist-kb --kb PATH guest",
        "owner_label": "core.json guest_access.owner, set by the KB owner; null when unset",
        "record_count": len(records),
        "source_count": len(kb.source_rows(sources)),
        "state_files": core["state_files"],
        "dependency_identity": identity,
        "defaults": {"limit": DEFAULT_LIMIT, "budget": DEFAULT_BUDGET},
        "bounds": {"limit": [1, 100], "budget": [300, 20000], "query_characters": 500, "fields": 32,
                   "slice_count": [1, 100]},
        "search_scope": "records_only_current_fields",
        "get_scope": "record_complete_fields_or_slice; source_catalog_metadata_only",
        "budget_scope": "complete_envelope_estimated_compact_JSON_tokens (UTF-8 bytes / 3, rounded up)",
        "continuation": "search: repeat the request with --offset result.next_offset and --expect-revision "
                        "kb_revision; slice: START = result.array_page.next_offset with --expect-revision; a changed "
                        "revision fails with revision_changed and the read restarts from the first page",
        "large_records": large_records(kb, records),
        "fingerprint_convention": "canonical-json-sha256-v1: uppercase SHA-256 of the complete original record or "
                                  "source object as UTF-8 JSON with sorted keys and compact separators",
        "runtime": "Python 3.11+ standard library; run with -X utf8 -B",
        "effects": "read_only; no KB state, recent-anchor, bytecode, log, cache or temporary-file writes; "
                   "no network or child processes; source locators are returned, never followed",
        "error_categories": sorted({*CATEGORIES.values(), "read_failed"}),
    }


def native_read(kb: Any, argv: list[str]) -> dict[str, Any]:
    """Run one ordinary read command of the checked kb.py and return its JSON result."""
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        kb.main(argv)
    return json.loads(captured.getvalue())


def native_budget(kb: Any, envelope: dict[str, Any], budget: int, slots: int) -> tuple[int, int]:
    """Reserve the envelope and fingerprint map, so the complete response stays within the guest budget."""
    skeleton = {**envelope, "ok": True, "result": {}, "record_fingerprints": {}}
    reserve = kb.estimate_tokens(skeleton) + (slots * FINGERPRINT_ENTRY_BYTES + 2) // 3 + 2
    if budget - reserve < 300:
        raise GuestError("budget_too_small", "The budget cannot hold the response envelope and one native page; "
                         "raise --budget or lower --limit.", required_budget=reserve + 300)
    return budget - reserve, reserve


def perform_read(args: argparse.Namespace, kb: Any, envelope: dict[str, Any], core: dict[str, Any],
                 records: list[dict[str, Any]], sources: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    revision = core["kb_revision"]
    if args.operation == "search":
        if not kb.token_terms(args.query):
            raise GuestError("invalid_arguments", "Search query contains no usable terms.")
        budget, reserve = native_budget(kb, envelope, args.budget, args.limit)
        argv = ["search", "--records-only", "--compact", "--limit", str(args.limit), "--offset", str(args.offset),
                "--budget", str(budget), "--expect-revision", str(revision), "--", args.query]
    else:
        if not (kb.RECORD_ID_RE.fullmatch(args.id) or kb.SOURCE_ID_RE.fullmatch(args.id)):
            raise GuestError("invalid_arguments", "Use an exact record or source ID, not a path.")
        budget, reserve = native_budget(kb, envelope, args.budget, 1)
        argv = ["get", args.id, "--compact", "--budget", str(budget), "--expect-revision", str(revision)]
        if args.slice is not None:
            field, start, count = args.slice
            argv += ["--array", field, "--offset", str(start), "--limit", str(count)]
        else:
            argv.append("--metadata-only")
        if args.fields is not None:
            argv += ["--fields", *args.fields]
    try:
        result = native_read(kb, argv)
    except kb.KBError as error:
        details = dict(error.details)
        code = details.pop("code", None) or "read_failed"
        if code == "revision_conflict":
            raise GuestError("revision_changed", "The KB changed during the read; read again from the first page.",
                             **details) from error
        if code in STATE_ERROR_CODES:
            # The second, native read met an uncommitted or invalid state: the same class as the first read's failure.
            raise GuestError("inconsistent_state", f"{error}. No repair was attempted; if the owner is writing, "
                             "read again later.", native_code=code, **details) from error
        if code in {"budget_too_small", "item_too_large"} and "required_budget" in details:
            details["required_budget"] += reserve
            message = "The response exceeds the budget; raise --budget"
            if args.operation == "get":
                message += ", select --fields a,b or page one array with --slice FIELD:START:COUNT"
            if details["required_budget"] > 20000:
                message += ". The selected item exceeds the 20000 maximum; narrow the selection or ask the owner"
            raise GuestError(code, message + ".", **details) from error
        raise GuestError(code, str(error), **details) from error
    if result.get("kb_revision") != revision:
        raise GuestError("revision_changed", "The KB changed during the read; read again from the first page.")
    originals = {row["id"]: row for row in records}
    originals.update({row["source_id"]: row for row in kb.source_rows(sources)})
    ids = [hit["id"] for hit in result["hits"]] if args.operation == "search" else [args.id]
    return result, {identifier: fingerprint(originals[identifier]) for identifier in ids}


def main(argv: list[str] | None = None) -> int:
    envelope: dict[str, Any] = {
        "interface_version": INTERFACE_VERSION, "ok": False, "owner": None,
        "kb_root": str(KB_ROOT.resolve()), "kb_revision": None, "operation": None, "script_sha256": None,
    }
    try:
        envelope["script_sha256"] = digest(Path(__file__).read_bytes())
        args = parser().parse_args(argv)
        envelope["operation"] = args.operation
        validate_request(args)
        kb, identity, own_sha256 = load_backend()
        envelope["script_sha256"] = own_sha256
        core_bytes, core, records, sources = read_state(kb)
        envelope["owner"] = guest_settings(core).get("owner")
        revision = core["kb_revision"]
        envelope["kb_revision"] = revision
        if args.expect_revision is not None and args.expect_revision != revision:
            raise GuestError("revision_changed", f"Expected revision {args.expect_revision}; current revision is "
                             f"{revision}. Read again from the first page.",
                             expected_revision=args.expect_revision, kb_revision=revision)
        if args.operation == "info":
            result, fingerprints = info_result(kb, core, records, sources, identity), {}
        else:
            result, fingerprints = perform_read(args, kb, envelope, core, records, sources)
        if kb.CORE_PATH.read_bytes() != core_bytes:
            raise GuestError("revision_changed", "The KB changed during the read; read again from the first page.")
        envelope.update(ok=True, result=result, record_fingerprints=fingerprints)
        if args.operation != "info" and kb.estimate_tokens(envelope) > args.budget:
            raise GuestError("budget_too_small", "The response exceeds the budget; raise --budget.",
                             required_budget=kb.estimate_tokens(envelope))
    except Exception as error:  # Every failure is reported in the envelope, never as a traceback.
        code = getattr(error, "code", None) or "read_failed"
        problem: dict[str, Any] = {"code": code, "category": CATEGORIES.get(code, "read_failed"), "message": str(error)}
        details = getattr(error, "details", None)
        if isinstance(details, dict) and details:
            problem["details"] = {key: value for key, value in details.items() if key != "code"}
        envelope.update(ok=False, result={"error": problem})
        envelope.pop("record_fingerprints", None)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str) + "\n")
    return 0 if envelope["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
