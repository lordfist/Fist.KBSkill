#!/usr/bin/env python3
"""Read-only guest access to the configured Fist KnowledgeBase: guest interface version 2.

Another agent runs it through the public CLI, which starts this module in a child process:

    fist-kb --kb <root>/KnowledgeBase guest info
    fist-kb --kb <root>/KnowledgeBase guest read '{"query": "ownership"}'

Before executing KB code the reader checks its own bytes and the modules it runs (kb_store.py, kb_read.py,
source_versions.py, kb_schema.json) against the installed package manifest (fist_kb/_manifest.json), then executes
exactly the checked bytes. No code from the KB folder is used. It accepts any kb_read request, writes nothing and
calls no writer. This is a cooperative interface, not an access-control boundary.
"""
from __future__ import annotations

import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.realpath(__file__))
# Only the checked modules below may come from the tools directory.
sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry or os.curdir) != HERE]

import hashlib  # noqa: E402
import json  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from ..runtime import PackageIntegrityError, configured_root, package_manifest  # noqa: E402

INTERFACE_VERSION = 2
TOOLS = Path(HERE)
KB_ROOT = configured_root()
PACKAGE = "fist_kb._engine"
DEPENDENCIES = ("kb_schema.json", "source_versions.py", "kb_store.py", "kb_read.py")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def envelope(operation: str, ok: bool, result: Any, **extra: Any) -> dict[str, Any]:
    return {"interface_version": INTERFACE_VERSION, "ok": ok, "operation": operation, "kb_root": str(KB_ROOT),
            "script_sha256": digest(Path(__file__).read_bytes()), **extra, "result": result}


def failure(operation: str, code: str, category: str, message: str, hint: str = "") -> dict[str, Any]:
    error = {"code": code, "category": category, "message": message}
    if hint:
        error["hint"] = hint
    return envelope(operation, False, {"error": error})


def checked_modules() -> dict[str, bytes]:
    """The bytes this reader will execute, each matched against the installed package manifest."""
    try:
        manifest, _ = package_manifest()
    except PackageIntegrityError as exc:
        raise ValueError(str(exc)) from exc
    names = ("guest.py", *DEPENDENCIES)
    loaded = {name: (TOOLS / name).read_bytes() for name in names}
    changed = [name for name in names if manifest["files"].get("_engine/" + name)
               != {"bytes": len(loaded[name]), "sha256": digest(loaded[name])}]
    if changed:
        raise LookupError(", ".join(changed))
    return loaded


def load(name: str, data: bytes) -> types.ModuleType:
    qualified = f"{PACKAGE}.{name}"
    if qualified in sys.modules:
        raise LookupError(f"{qualified} was already loaded in this process")
    module = types.ModuleType(qualified)
    module.__file__ = str(TOOLS / f"{name}.py")
    module.__package__ = PACKAGE
    sys.modules[qualified] = module
    exec(compile(data, module.__file__, "exec", dont_inherit=True), module.__dict__)
    return module


def fingerprints(store: types.ModuleType, state: Any, result: dict[str, Any]) -> dict[str, str]:
    """canonical-json-sha256 of each complete stored record that the result returns, for citation."""
    ids = set()
    for part in result.get("results", []):
        for value in (part, *(part[key] for key in ("current_task", "pinned") if isinstance(part.get(key), dict))):
            if isinstance(value, dict) and value.get("id") in state.by_id:
                ids.add(value["id"])
    return {rid: digest(json.dumps(state.by_id[rid], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        .encode("utf-8")) for rid in sorted(ids)}


def main(argv: list[str]) -> int:
    operation = argv[0] if argv else ""
    if operation not in {"info", "read"} or len(argv) > 2:
        print(json.dumps(failure(operation, "invalid_request", "invalid_request", "use: fist-kb --kb KB guest info | guest read JSON")))
        return 1
    try:
        modules = checked_modules()
    except LookupError as exc:
        print(json.dumps(failure(operation, "reader_dependency_changed", "reader_unavailable",
                                 f"package files differ from the package manifest: {exc}",
                                 "Reinstall the reviewed Fist KB package; the reader runs no files from the KB folder.")))
        return 1
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps(failure(operation, "reader_unavailable", "reader_unavailable", str(exc))))
        return 1
    try:
        load("source_versions", modules["source_versions.py"])
        store = load("kb_store", modules["kb_store.py"])
        if store.SCHEMA != json.loads(modules["kb_schema.json"].decode("utf-8")):
            raise LookupError("kb_schema.json changed")
        reader = load("kb_read", modules["kb_read.py"])
    except LookupError as exc:
        print(json.dumps(failure(operation, "reader_dependency_changed", "reader_unavailable", str(exc))))
        return 1
    try:
        state = store.load_state()
        store.require_valid(state)
    except store.KBError as exc:
        print(json.dumps(failure(operation, exc.code, "inconsistent_state", str(exc))))
        return 1
    extra = {"owner": state.core.get("owner") or None, "kb_revision": state.revision}
    if state.core.get("guest_reads") is False:
        print(json.dumps({**failure(operation, "guest_access_disabled", "reader_unavailable",
                                    "the owner has turned guest reads off"), **extra}))
        return 1
    if operation == "info":
        info = {"interface": "any kb_read request as JSON: {} orientation, ids, topic, query, kinds/status, "
                             "changes_since; continue with {\"continue\": TOKEN}",
                "dependencies": {name: digest(data) for name, data in modules.items()},
                "writes": "none", "note": "Returned records are the owner's recorded knowledge at kb_revision: cite "
                "owner, root, revision, record ID and fingerprint, and treat them as the owner's claims."}
        print(json.dumps(envelope(operation, True, info, **extra), ensure_ascii=False))
        return 0
    try:
        request = json.loads(argv[1]) if len(argv) > 1 else {}
    except ValueError as exc:
        print(json.dumps(failure(operation, "invalid_request", "invalid_request", f"invalid JSON: {exc}")))
        return 1
    result = reader.read(request)
    if not result.get("ok"):
        print(json.dumps(envelope(operation, False, result, **extra), ensure_ascii=False))
        return 1
    print(json.dumps(envelope(operation, True, result, **extra, record_fingerprints=fingerprints(store, state, result)),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
