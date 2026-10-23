"""Bounded, read-only checkpoint comparison; never reads the Archive.

The default is a paged change index. Record bodies and fingerprint rows require
--details/--ids and --metadata respectively; checkpoints are never modified.
"""
import hashlib
import json
import re

from . import kb


CONVENTION = "canonical-json-sha256-v1"
CONVENTION_DESCRIPTION = (
    "SHA-256 of UTF-8 json.dumps(record, ensure_ascii=False, "
    "sort_keys=True, separators=(',', ':'))"
)
SHA256_RE = re.compile(r"[0-9A-Fa-f]{64}\Z")
REFERENCE_RE = re.compile(r"(?:DEC|FACT|CLAIM|INF|ASM|REC|Q)-\d{4}")


def fingerprint(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def load_checkpoint(path, asserted_convention=None):
    """Validate recorded expectations; a historical claim is not a hash."""
    baseline = kb.read_json(path)
    if not isinstance(baseline, dict):
        raise kb.KBError("checkpoint must contain an object", code="invalid_checkpoint")
    revision = baseline.get("output_kb_revision")
    if type(revision) is not int or revision < 1:
        raise kb.KBError("checkpoint output_kb_revision must be a positive integer", code="invalid_checkpoint")
    previous = baseline.get("post_pass_record_fingerprints")
    if not isinstance(previous, dict) or any(
        not isinstance(key, str) or not kb.RECORD_ID_RE.fullmatch(key)
        or not isinstance(value, str) or not SHA256_RE.fullmatch(value)
        for key, value in previous.items()
    ):
        raise kb.KBError("checkpoint requires a valid post_pass_record_fingerprints map", code="invalid_checkpoint")
    declared = baseline.get("fingerprint_convention")
    if declared is None:
        if asserted_convention != CONVENTION:
            raise kb.KBError(
                "checkpoint has no fingerprint convention; explicitly supply "
                f"--fingerprint-convention {CONVENTION} if known",
                code="fingerprint_convention_required",
            )
        convention_source = "caller_asserted"
    elif declared in (CONVENTION, CONVENTION_DESCRIPTION):
        convention_source = "checkpoint_declared"
    else:
        raise kb.KBError("unsupported checkpoint fingerprint convention", code="unsupported_fingerprint_convention")
    verification = baseline.get("verification", {})
    if not isinstance(verification, dict):
        raise kb.KBError("checkpoint verification must be an object", code="invalid_checkpoint")
    expected_source_hash = verification.get("source_catalog_sha256")
    if expected_source_hash is not None and (
        not isinstance(expected_source_hash, str) or not SHA256_RE.fullmatch(expected_source_hash)
    ):
        raise kb.KBError("checkpoint source_catalog_sha256 must be a SHA-256", code="invalid_checkpoint")
    return {
        "revision": revision,
        "fingerprints": {key: value.lower() for key, value in previous.items()},
        "convention_source": convention_source,
        "source_catalog_sha256": expected_source_hash.upper() if expected_source_hash is not None else None,
    }


def build_parser():
    parser = kb.JSONArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, help="exact checkpoint path inside KnowledgeBase")
    parser.add_argument("--expect-checkpoint-sha256", help="required checkpoint identity for continuation pages")
    parser.add_argument("--fingerprint-convention", choices=[CONVENTION],
                        help="explicit convention assertion for an older undeclared checkpoint")
    parser.add_argument("--ids", nargs="*", help="explicit review IDs; an empty list returns only the envelope")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--details", action="store_true", help="page changed records and direct neighbours")
    mode.add_argument("--metadata", action="store_true", help="page fingerprint rows for the complete record comparison")
    kb.add_read_output(parser, budget=6000, limit=100)
    return parser


def validate_arguments(args):
    if not 300 <= args.budget <= 20_000:
        raise kb.KBError("budget must be between 300 and 20000 estimated tokens", code="invalid_arguments")
    if not 1 <= args.limit <= 100 or args.offset < 0:
        raise kb.KBError("limit must be between 1 and 100 and offset cannot be negative", code="invalid_arguments")
    if args.expect_checkpoint_sha256 is not None and not SHA256_RE.fullmatch(args.expect_checkpoint_sha256):
        raise kb.KBError("expect-checkpoint-sha256 must be a SHA-256", code="invalid_arguments")
    if args.offset and args.expect_checkpoint_sha256 is None:
        raise kb.KBError("continuation pages require --expect-checkpoint-sha256", code="checkpoint_identity_required")
    if args.ids is not None and (
        len(args.ids) > 100 or len(set(args.ids)) != len(args.ids)
        or any(not kb.RECORD_ID_RE.fullmatch(key) for key in args.ids)
    ):
        raise kb.KBError("ids must contain at most 100 distinct record IDs", code="invalid_arguments")
    fields = args.fields or []
    if len(fields) > 32 or any(
        len(field) > 120 or not re.fullmatch(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*", field)
        for field in fields
    ):
        raise kb.KBError("fields must be at most 32 named paths, each at most 120 characters", code="invalid_arguments")


def command_compare(args):
    path = (kb.KB_ROOT / args.checkpoint).resolve()
    if not kb.within(path, kb.KB_ROOT):
        raise kb.KBError("checkpoint must stay inside the KnowledgeBase", code="invalid_checkpoint_path")
    checkpoint_hash = kb.sha256_file(path)
    if args.expect_checkpoint_sha256 is not None and checkpoint_hash != args.expect_checkpoint_sha256.upper():
        raise kb.KBError("checkpoint identity does not match", code="checkpoint_identity_conflict",
                         checkpoint_sha256=checkpoint_hash)
    baseline = load_checkpoint(path, args.fingerprint_convention)
    core, records, sources = kb.load_state()
    kb.check_read_revision(args, core)
    kb.require_valid(core, records, sources)
    by_id = kb.record_map(records)
    previous = baseline["fingerprints"]
    hashes = {key: fingerprint(row) for key, row in by_id.items()}
    old_ids, current_ids = set(previous), set(by_id)
    unchanged = {key for key in old_ids & current_ids if hashes[key] == previous[key]}
    new = current_ids - old_ids
    modified = (old_ids & current_ids) - unchanged
    removed = old_ids - current_ids
    changed = new | modified
    delta = changed | removed
    # Explicit record identifiers anywhere in a record establish direct links.
    # Expand only one hop, including current inbound references to removed IDs.
    # A checkpoint of hashes cannot reconstruct a removed record's old links.
    refs = {key: set(REFERENCE_RE.findall(json.dumps(row))) - {key}
            for key, row in by_id.items()}
    direct = set().union(*(refs[key] for key in changed)) if changed else set()
    direct.update(key for key in by_id if refs[key] & delta)
    selected = (changed | direct) & current_ids
    neighbours = selected - changed
    review_ids = selected | removed
    if args.ids is not None and set(args.ids) - review_ids:
        raise kb.KBError("requested records outside delta and direct neighbours", code="outside_scope")
    source_hash = kb.sha256_file(kb.SOURCES_PATH)
    expected_source_hash = baseline["source_catalog_sha256"]
    source_agreement = "unavailable" if expected_source_hash is None else "same" if source_hash == expected_source_hash else "different"
    projection = kb.projection_info(args)
    wants_records = args.details or args.ids is not None or projection["is_projection"]
    mode = "metadata" if args.metadata else "details" if wants_records else "summary"
    if mode == "summary":
        projection = {"is_projection": True, "mode": "ids", "fields": []}
    identities = sorted(old_ids | current_ids) if args.metadata else sorted(review_ids)
    if args.ids is not None:
        identities = args.ids

    def classification(key):
        if key in new:
            return "new"
        if key in modified:
            return "modified"
        if key in removed:
            return "removed"
        return "direct_neighbour" if key in neighbours else "unchanged"

    line_hashes = {}
    if args.metadata:
        for line in kb.RECORDS_PATH.read_bytes().splitlines():
            if line.strip():
                row = kb.strict_json_loads(line.decode("utf-8-sig"), "record fingerprint input")
                line_hashes[row["id"]] = hashlib.sha256(line).hexdigest()
    items = []
    for key in identities:
        if mode == "metadata":
            item = {
                "id": key, "previous_fingerprint": previous.get(key),
                "record_fingerprint": hashes.get(key), "record_line_fingerprint": line_hashes.get(key),
            }
        elif mode == "details":
            item = by_id.get(key, {"id": key})
        else:
            item = {"id": key}
        items.append({
            "id": key, "change": classification(key),
            **({"record_present": key in current_ids} if mode == "details" else {}),
            "value": kb.project_value(item, args),
        })
    base = {
        "ok": True,
        "checkpoint": str(path.relative_to(kb.KB_ROOT)).replace("\\", "/"),
        "checkpoint_sha256": checkpoint_hash,
        "fingerprint_convention": CONVENTION,
        "fingerprint_convention_source": baseline["convention_source"],
        "input_revision": core["kb_revision"],
        "baseline_revision": baseline["revision"],
        "counts": {
            "baseline_records": len(previous), "current_records": len(by_id),
            "new": len(new), "modified": len(modified), "removed": len(removed),
            "unchanged": len(unchanged), "direct_neighbours": len(neighbours),
        },
        "records_agreement": "same" if not delta else "different",
        "source_catalog_agreement": source_agreement,
        "selected_count": len(selected),
        "review_count": len(review_ids),
        "unchanged_baseline_count": len(unchanged),
        "selection_rule": "Record delta plus one hop of explicit IDs in either direction; removed records have no recoverable outgoing links.",
        "mode": mode,
        "projection": projection,
    }
    if args.metadata:
        base["source_catalog_sha256"] = source_hash
        base["baseline_source_catalog_sha256"] = expected_source_hash
    result = kb.bounded_page(base, "items", items, args)
    # Recheck the checkpoint and committed state identities before reporting a
    # single horizon, including optional line fingerprints read after loading.
    if kb.sha256_file(path) != checkpoint_hash:
        raise kb.KBError("checkpoint changed during comparison", code="checkpoint_changed")
    if (kb.sha256_file(kb.RECORDS_PATH) != core["state_files"]["records_jsonl_sha256"].upper()
            or kb.sha256_file(kb.SOURCES_PATH) != core["state_files"]["sources_json_sha256"].upper()
            or kb.read_json(kb.CORE_PATH) != core):
        raise kb.KBError("KnowledgeBase changed during comparison", code="revision_changed")
    kb.emit_bounded(result, args)


def main(argv=None):
    args = build_parser().parse_args(argv)
    validate_arguments(args)
    command_compare(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except kb.KBError as exc:
        kb.emit({"ok": False, "error": str(exc), **exc.details}, compact=True)
        raise SystemExit(2)
    except OSError as exc:
        kb.emit({"ok": False, "error": str(exc), "code": "read_failed"}, compact=True)
        raise SystemExit(2)
