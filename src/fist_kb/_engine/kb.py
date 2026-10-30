"""Fist KB engine entry point (Seed KB v08): two model-facing operations and a few admin commands.

    fist-kb --kb KB read  '{"query": "supplier csv"}'
    fist-kb --kb KB write @request.json        (or - for stdin)
    fist-kb --kb KB admin validate|verify-tools|init|migrate-v07|recover|export

The public CLI runs this module in a child process with FIST_KB_ROOT naming the KB folder. The result is always
one JSON object with "ok". Exit status: 0 when ok, 1 otherwise, 2 for a command-line usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import kb_migrate
from . import kb_read
from . import kb_store as store
from . import kb_write
from ..runtime import PackageIntegrityError, verify_package


def read(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """kb_read: orientation with no keys; ids, topic, query, kinds/status, changes_since or request otherwise."""
    return kb_read.read(request)


def write(request: dict[str, Any]) -> dict[str, Any]:
    """kb_write: one call commits and echoes what was stored; preview and confirm are optional."""
    return kb_write.write(request)


def emit(value: Any, pretty: bool = False) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2 if pretty else None,
                                separators=None if pretty else (",", ":")) + "\n")


def load_request(argument: str | None) -> Any:
    if argument is None or argument == "-":
        text = sys.stdin.read()
    elif argument.startswith("@"):
        text = Path(argument[1:]).read_text(encoding="utf-8")
    else:
        text = argument
    return store.strict_loads(text, "request") if text.strip() else {}


def admin(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "verify-tools":
        status = store.tool_status()
        try:
            package = verify_package()
        except PackageIntegrityError as exc:
            package = {"ok": False, "error": str(exc)}
        return {"ok": status["status"] == "ok", **status, "kb_root": str(store.KB_ROOT), "package": package}
    if args.command == "init":
        if store.CORE_PATH.exists():
            raise store.KBError("already_initialized", "core.json already exists", "This KB is initialized.")
        for path, data in store.initial_files(args.purpose or "", args.owner or "").items():
            path.write_bytes(data)
        return {"ok": True, "initialized": str(store.KB_ROOT), "binding_root": str(store.BINDING_ROOT),
                "tools": store.tool_status()["status"]}
    if args.command == "migrate-v07":
        if not args.source or not args.by:
            raise store.KBError("request_invalid", "migrate-v07 needs --from OLD_KNOWLEDGEBASE and --by NAME")
        return kb_migrate.migrate(args.source, args.by, args.purpose, args.dry_run)
    if args.command == "recover":
        report: dict[str, Any] = {"ok": True, "staging_files": sorted(path.name for path in store.KB_ROOT.glob(".*.tmp"))}
        try:
            core = store.read_json_file(store.CORE_PATH)
            report["revision"] = core.get("revision")
            report["files"] = {name: {"recorded": core.get("state_files", {}).get(name),
                                      "actual": store.sha256_file(path) if path.exists() else None}
                               for name, path in store.STATE_FILES.items()}
            report["consistent"] = all(item["recorded"] == item["actual"] for item in report["files"].values())
        except store.KBError as exc:
            report["core_error"] = exc.payload()
        report["next"] = "Inspection only; nothing was changed. Report this to the owner before any write."
        return report
    state = store.load_state()
    if args.command == "validate":
        result = store.validate_state(state)
        report = {"ok": result["ok"], "revision": state.revision, "errors": result["errors"][:50],
                  "error_count": len(result["errors"]), "warnings": result["warnings"][:50],
                  "warning_count": len(result["warnings"]), "tools": store.tool_status()["status"]}
        if args.deep:
            report["sources"] = store.check_sources(state)
            report["ok"] = report["ok"] and not any(report["sources"][key] for key in ("changed", "prepared_changed",
                                                                                        "history_failed"))
        return report
    if args.command == "export":
        return {"ok": True, "core": state.core, "records": state.records, "journal": state.journal,
                "sources": state.sources}
    raise store.KBError("request_invalid", f"unknown admin command {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fist-kb", description="Fist KB (Seed KB v08 engine)")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("read", "write"):
        command = sub.add_parser(name)
        command.add_argument("request", nargs="?", help="JSON text, @file or - for stdin; read defaults to {}")
        command.add_argument("--pretty", action="store_true")
    admin_parser = sub.add_parser("admin")
    admin_parser.add_argument("command", choices=["validate", "verify-tools", "init", "migrate-v07", "recover",
                                                       "export"])
    admin_parser.add_argument("--from", dest="source")
    admin_parser.add_argument("--deep", action="store_true", help="validate: also re-hash registered file sources")
    admin_parser.add_argument("--dry-run", action="store_true", help="migrate-v07: build and check in memory, write nothing")
    admin_parser.add_argument("--by")
    admin_parser.add_argument("--purpose")
    admin_parser.add_argument("--owner")
    admin_parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.operation == "read":
            result = read(load_request(args.request) if args.request is not None else {})
        elif args.operation == "write":
            result = write(load_request(args.request))
        else:
            result = admin(args)
    except store.KBError as exc:
        result = {"ok": False, "error": exc.payload()}
    except OSError as exc:
        result = {"ok": False, "error": {"code": "io_error", "message": str(exc)}}
    emit(result, args.pretty)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
