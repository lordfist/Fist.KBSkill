"""Checked, UTF-8 subprocess access to the sibling KnowledgeBase tools.

This module does not load KnowledgeBase state when imported.  Callers must name
the required top-level result fields.  No operation is retried automatically.
An exception after a child starts does not establish that a write was rolled
back or that repeating the operation is safe; inspect ``write_outcome`` when
the child reports one, otherwise the outcome remains unknown.
"""

from __future__ import annotations

import json
import math
import copy
import hashlib
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ALLOWED_TOOLS = frozenset({"kb.py", "consolidation_scope.py", "prepare.py", "tool_inventory.py"})
JSON_TYPES = frozenset({dict, list, str, int, float, bool, type(None)})
ExpectedType = type | tuple[type, ...]


class ToolCallError(RuntimeError):
    """Preserve the child's outcome, including output from failed reporting.

    ``stdout`` and ``stderr`` contain text when valid UTF-8, or the original
    bytes when decoding fails. ``parsed`` retains valid JSON even when the
    exit code, error flag, or expected result shape makes the call fail.
    ``write_outcome`` is the child's reported object, never an inferred
    rollback. Its absence means that no write outcome was established here.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        command: Sequence[str],
        returncode: int | None = None,
        stdout: str | bytes = "",
        stderr: str | bytes = "",
        parsed: Any = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.command = tuple(command)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.parsed = parsed
        outcome = parsed.get("write_outcome") if isinstance(parsed, dict) else None
        self.write_outcome = outcome if isinstance(outcome, dict) else None


def _decode_capture(value: bytes | str | None) -> str | bytes:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _parse_capture(value: str | bytes) -> tuple[Any, str | None]:
    if isinstance(value, bytes):
        return None, "stdout is not valid UTF-8"
    try:
        return json.loads(
            value, parse_constant=_reject_constant, object_pairs_hook=_unique_object
        ), None
    except (ValueError, RecursionError) as exc:
        return None, str(exc)


def call_tool(
    tool_name: str,
    arguments: Sequence[str],
    *,
    expected: Mapping[str, ExpectedType],
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run an allowlisted sibling once and return its checked JSON object.

    ``tool_name`` is an exact filename from ``ALLOWED_TOOLS``. ``arguments`` is
    an argument vector, not a shell command. ``expected`` is a nonempty mapping
    from required top-level fields to JSON types, or nonempty tuples of those
    types. Types match exactly: a boolean does not satisfy an integer field.
    Additional result fields are retained. The child inherits the caller's
    working directory; use absolute paths when a path must be unambiguous.

    Invalid invocation configuration raises ``ValueError`` before starting a
    child. Process, output, and result failures raise ``ToolCallError``. UTF-8
    decoding is strict, and malformed output is never silently repaired.
    """
    if not isinstance(tool_name, str) or tool_name not in ALLOWED_TOOLS:
        raise ValueError("tool_name must be an exact allowlisted sibling filename")
    if isinstance(arguments, (str, bytes)) or not isinstance(arguments, Sequence):
        raise ValueError("arguments must be a sequence of strings")
    if any(not isinstance(arg, str) or "\x00" in arg for arg in arguments):
        raise ValueError("arguments must contain only strings without NUL characters")
    if not isinstance(expected, Mapping) or not expected:
        raise ValueError("expected must specify at least one required top-level field")
    expected_types: dict[str, tuple[type, ...]] = {}
    for field, required in expected.items():
        if not isinstance(field, str) or not field:
            raise ValueError("expected field names must be nonempty strings")
        types = required if isinstance(required, tuple) else (required,)
        if not types or any(not isinstance(item, type) or item not in JSON_TYPES for item in types):
            raise ValueError(f"expected type for {field!r} must be a JSON type or tuple of JSON types")
        expected_types[field] = types
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number of seconds")
    tools_directory = Path(__file__).resolve().parent
    try:
        target = (tools_directory / tool_name).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"allowlisted tool is unavailable: {tool_name}") from exc
    if target.parent != tools_directory or target.name != tool_name or not target.is_file():
        raise ValueError("tool must resolve to the named regular file in this module's directory")
    if not sys.executable:
        raise ValueError("the current Python executable is unavailable")
    command = [sys.executable, "-X", "utf8", "-B", "-P", "-m",
               "fist_kb._engine." + target.stem, *arguments]
    try:
        completed = subprocess.run(
            command, capture_output=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_capture(exc.stdout)
        stderr = _decode_capture(exc.stderr)
        parsed, _ = _parse_capture(stdout)
        raise ToolCallError(
            "timeout",
            "Tool timed out; completion and any unreported write outcome are unknown.",
            command=command, stdout=stdout, stderr=stderr, parsed=parsed,
        ) from exc
    except OSError as exc:
        raise ToolCallError(
            "launch_failed", f"Could not start tool: {exc}", command=command
        ) from exc

    stdout = _decode_capture(completed.stdout)
    stderr = _decode_capture(completed.stderr)
    parsed, parse_error = _parse_capture(stdout)
    details = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "parsed": parsed,
    }
    if completed.returncode != 0:
        message = parsed.get("error") if isinstance(parsed, dict) else None
        raise ToolCallError(
            "exit_status",
            f"Tool exited with status {completed.returncode}: {message or 'inspect captured output'}",
            **details,
        )
    if isinstance(stdout, bytes) or isinstance(stderr, bytes):
        raise ToolCallError("encoding", "Tool output is not valid UTF-8.", **details)
    if parse_error is not None:
        raise ToolCallError("invalid_json", f"Tool output is not valid JSON: {parse_error}", **details)
    if not isinstance(parsed, dict):
        raise ToolCallError("result_shape", "Tool output must be a JSON object.", **details)
    if parsed.get("ok") is False:
        raise ToolCallError("reported_error", f"Tool reported failure: {parsed.get('error', 'ok:false')}", **details)
    for field, types in expected_types.items():
        if field not in parsed:
            raise ToolCallError("result_shape", f"Tool result is missing required field {field!r}.", **details)
        if type(parsed[field]) not in types:
            names = " or ".join(item.__name__ for item in types)
            raise ToolCallError("result_shape", f"Tool result field {field!r} must be {names}.", **details)
    return parsed


def _draft_fields(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unsupported {label} fields: {', '.join(sorted(unknown))}")
    return value


def _merge_existing(target: dict[str, Any], patch: Any, label: str) -> None:
    if not isinstance(patch, dict):
        raise ValueError(f"{label}.merge must be an object")
    for key, value in patch.items():
        if key not in target:
            raise ValueError(f"unknown merge field {label}.{key}; use explicit add")
        if isinstance(value, dict):
            if not isinstance(target[key], dict):
                raise ValueError(f"cannot merge an object into non-object {label}.{key}")
            _merge_existing(target[key], value, f"{label}.{key}")
        else:
            target[key] = copy.deepcopy(value)


def _pointer_parts(pointer: str, label: str, operation: str, noun: str, protected: set[str]) -> list[str]:
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError(f"invalid JSON Pointer in {label}.{operation}: {pointer!r}")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    if not all(parts) or parts[0] in protected:
        raise ValueError(f"empty or protected {noun} path in {label}: {pointer!r}")
    return parts


def _edit_object(original: dict[str, Any], edit: dict[str, Any], *, label: str,
                 protected: set[str]) -> dict[str, Any]:
    result = copy.deepcopy(original)
    merge = edit.get("merge", {})
    if not isinstance(merge, dict) or protected.intersection(merge):
        raise ValueError(f"{label}.merge must be an object without protected fields: {sorted(protected)}")
    _merge_existing(result, merge, label)
    additions = edit.get("add", {})
    if not isinstance(additions, dict):
        raise ValueError(f"{label}.add must be an object of JSON Pointer paths to new values")
    paths: list[list[str]] = []
    for pointer in additions:
        parts = _pointer_parts(pointer, label, "add", "addition", protected)
        if any(parts[:len(other)] == other or other[:len(parts)] == parts for other in paths):
            raise ValueError(f"overlapping addition paths in {label}")
        paths.append(parts)
        parent = result
        for part in parts[:-1]:
            if part not in parent or not isinstance(parent[part], dict):
                raise ValueError(f"addition parent must already be an object in {label}: {pointer!r}")
            parent = parent[part]
        if parts[-1] in parent:
            raise ValueError(f"addition already exists in {label}: {pointer!r}")
        parent[parts[-1]] = copy.deepcopy(additions[pointer])
    appends = edit.get("append", {})
    if not isinstance(appends, dict):
        raise ValueError(f"{label}.append must be an object of JSON Pointer paths to nonempty arrays of new items")
    for pointer, items in appends.items():
        parts = _pointer_parts(pointer, label, "append", "append", protected)
        if any(parts[:len(other)] == other or other[:len(parts)] == parts for other in paths):
            raise ValueError(f"append path overlaps an addition or another append in {label}: {pointer!r}")
        paths.append(parts)
        merged: Any = merge
        for part in parts:
            if not isinstance(merged, dict) or part not in merged:
                break
            merged = merged[part]
            if not isinstance(merged, dict):
                raise ValueError(f"append path is also replaced by merge in {label}: {pointer!r}")
        if not isinstance(items, list) or not items:
            raise ValueError(f"{label}.append values must be nonempty arrays of new items: {pointer!r}")
        parent = result
        for part in parts[:-1]:
            if part not in parent or not isinstance(parent[part], dict):
                raise ValueError(f"append parent must already be an object in {label}: {pointer!r}")
            parent = parent[part]
        if not isinstance(parent.get(parts[-1]), list):
            raise ValueError(f"append target must be an existing array in {label}: {pointer!r}")
        parent[parts[-1]] = [*parent[parts[-1]], *copy.deepcopy(items)]
    return result


def _draft_path(path: str, root: Path, *, must_exist: bool) -> Path:
    candidate = Path(os.path.abspath(path))
    resolved = candidate.resolve(strict=must_exist)
    if not candidate.is_relative_to(root) or not resolved.is_relative_to(root):
        raise ValueError("draft request and output must be inside the exact KnowledgeBase root")
    for part in (candidate, *candidate.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if part != candidate or must_exist:
                raise
        else:
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"draft paths cannot contain a symlink or reparse point: {part}")
        if part == root:
            break
    return candidate


def draft_update(request_path: str, output_path: str, *, expect_revision: int) -> dict[str, Any]:
    """Draft only, with complete originals and explicit, lossless field selection.

    Request keys: actor (required), note (optional), records (edits with id, merge,
    add, append), new_records (complete new objects), core (merge/add/append).
    ``merge`` walks existing objects recursively; omitted fields survive and
    arrays/scalars replace whole values. Unknown merge fields fail. ``add`` maps
    JSON Pointers to absent object keys with existing parents; array indexing is
    unsupported. ``append`` maps JSON Pointers to existing arrays and appends a
    nonempty array of items after the current ones; it cannot overlap an add,
    another append or a merge that replaces the same array or its parent.
    Record IDs/timestamps and automatic core fields cannot be edited here.
    New-record payloads use the existing extensible KB schema and validation.
    No deletion, source editing, application, retry, or semantic inference occurs.

    Both files must be inside this tool's KB. The output must not exist. Normal
    KB integrity/shape validation runs without opening original source files;
    the subsequent kb.py apply --dry-run supplies exact, deeply validated deltas.
    """
    from . import kb

    if type(expect_revision) is not int or expect_revision < 1:
        raise ValueError("expect_revision must be a positive integer")
    request_file = _draft_path(request_path, kb.KB_ROOT, must_exist=True)
    output_file = _draft_path(output_path, kb.KB_ROOT, must_exist=False)
    if output_file.exists() or not output_file.parent.is_dir():
        raise ValueError("draft output must be a new file in an existing directory")
    request = _draft_fields(kb.read_json(request_file), {"actor", "note", "records", "new_records", "core"}, "request")
    if "actor" not in request:
        raise ValueError("actor is required for explicit maintenance attribution")
    for field in ("actor", "note"):
        if field in request and (not isinstance(request[field], str) or not request[field].strip()):
            raise ValueError(f"{field} must be a nonempty string")
    state_paths = (kb.CORE_PATH, kb.RECORDS_PATH, kb.SOURCES_PATH)
    before_hashes = {path: kb.sha256_file(path) for path in state_paths}
    core, records, sources = kb.load_state()
    kb.require_valid(core, records, sources)
    if core["kb_revision"] != expect_revision:
        raise ValueError(f"revision conflict: expected {expect_revision}, current {core['kb_revision']}")
    existing = kb.record_map(records)
    edits, new_records = request.get("records", []), request.get("new_records", [])
    if not isinstance(edits, list) or not isinstance(new_records, list):
        raise ValueError("records and new_records must be arrays")
    if len(edits) + len(new_records) > 100:
        raise ValueError("draft accepts at most 100 record selections")
    selected: set[str] = set()
    upserts: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for edit in edits:
        edit = _draft_fields(edit, {"id", "merge", "add", "append"}, "record edit")
        identifier = edit.get("id")
        if not isinstance(identifier, str) or identifier not in existing or identifier in selected:
            raise ValueError(f"record edit must select one distinct existing ID: {identifier!r}")
        selected.add(identifier)
        changed = _edit_object(existing[identifier], edit, label=identifier, protected={"id", "created_at", "updated_at"})
        deltas = list(kb.field_changes(existing[identifier], changed))
        if not deltas:
            raise ValueError(f"record edit has no field changes: {identifier}")
        upserts.append(changed)
        summary.append({"id": identifier, "operation": "update", "changed_fields": len(deltas)})
    for row in new_records:
        if not isinstance(row, dict):
            raise ValueError("each new_records entry must be a complete object")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not kb.RECORD_ID_RE.fullmatch(identifier) or identifier in existing or identifier in selected:
            raise ValueError(f"new record must select one distinct unused ID: {identifier!r}")
        selected.add(identifier)
        upserts.append(copy.deepcopy(row))
        summary.append({"id": identifier, "operation": "create", "supplied_fields": len(row)})
    core_edit = _draft_fields(request.get("core", {}), {"merge", "add", "append"}, "core edit")
    changed_core = _edit_object(core, core_edit, label="core", protected={"schema_version", "kb_revision", "updated_at", "state_files", "last_maintenance"})
    core_patch = {key: value for key, value in changed_core.items() if key not in core or not kb.json_equal(core[key], value)}
    if not upserts and not core_patch:
        raise ValueError("draft request has no record or core changes")
    transaction = {key: request[key] for key in ("actor", "note") if key in request}
    if upserts:
        transaction["upsert_records"] = upserts
    if core_patch:
        transaction["core_patch"] = core_patch
    next_core, next_records, next_sources, _ = kb.build_candidate(core, records, sources, transaction)
    kb.require_valid(next_core, next_records, next_sources)
    payload = kb.canonical_json_bytes(transaction)
    for path, digest in before_hashes.items():
        if kb.sha256_file(path) != digest:
            raise ValueError(f"KB changed during drafting: {path.name}; inspect state before retrying")
    created = False
    try:
        with output_file.open("xb") as handle:
            created = True
            handle.write(payload)
    except OSError as exc:
        cleanup = "no new file was created"
        if created:
            try:
                output_file.unlink()
                cleanup = "partial draft removed"
            except OSError as cleanup_error:
                cleanup = f"partial draft may remain; cleanup failed: {cleanup_error}"
        raise ValueError(f"could not publish draft {output_file}: {exc}; {cleanup}. No KB transaction was applied.") from exc
    digest = hashlib.sha256(payload).hexdigest().upper()
    return {
        "ok": True, "draft_only": True, "applied": False,
        "kb_revision": expect_revision, "transaction": str(output_file),
        "transaction_sha256": digest, "transaction_bytes": len(payload),
        "record_count": len(summary), "records": summary[:20],
        "records_truncated": len(summary) > 20,
        "core_sections_changed": len(core_patch),
        "next": "Review exact deltas using kb.py apply TRANSACTION --expect-revision N --expect-transaction-sha256 SHA --dry-run --preview-values; then commit the reviewed SHA with the same revision. This draft summary is not the full review.",
    }


def main(argv: list[str] | None = None) -> int:
    from . import kb

    parser = kb.JSONArgumentParser(description="Checked KB caller and explicit update drafting", allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    draft = subparsers.add_parser("draft", allow_abbrev=False, help="draft a transaction without applying it")
    draft.add_argument("request")
    draft.add_argument("--output", required=True)
    draft.add_argument("--expect-revision", type=int, required=True)
    draft.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    kb.emit(draft_update(args.request, args.output, expect_revision=args.expect_revision), compact=args.compact)
    return 0


if __name__ == "__main__":
    from . import kb

    try:
        raise SystemExit(main())
    except (kb.KBError, ValueError, OSError) as exc:
        kb.emit({"ok": False, "error": str(exc), "code": "draft_failed", "applied": False}, compact=True)
        raise SystemExit(2)
