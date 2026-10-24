"""Command-line entry point; each engine invocation has its own bound process."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid

from .runtime import configured_root


class WriteLocked(RuntimeError):
    code = "write_locked"


def _run_engine(command: list[str], env: dict[str, str], root, *, mutating: bool) -> int:
    """Serialize public canonical writes; stale locks require explicit inspection."""
    lock = root / ".write.lock"
    payload = json.dumps({"pid": os.getpid(), "token": uuid.uuid4().hex},
                         separators=(",", ":")).encode("utf-8")
    if mutating:
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise WriteLocked(f"Another write owns {lock}. No write was started. Inspect its PID and state; "
                              "never remove an active lock or blindly retry an uncertain write.") from error
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
        except OSError as error:
            raise RuntimeError(f"Could not initialize write lock {lock}; inspect any remaining lock. "
                               "No engine write was started.") from error
    returncode = 2
    cleanup_failed = False
    try:
        returncode = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, check=False).returncode
    finally:
        if mutating:
            try:
                if lock.read_bytes() != payload:
                    raise OSError("Lock contents changed; refusing to remove a lock not owned by this call")
                lock.unlink()
            except OSError as error:
                cleanup_failed = True
                # Preserve the engine's JSON/write_outcome rather than printing a second JSON object.
                print(f"Write-lock cleanup failed: {error}. Inspect KB state and {lock}; "
                      "a write may already have completed. No retry or repair was attempted.", file=sys.stderr)
    return 2 if cleanup_failed else returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fist-kb", allow_abbrev=False,
        description="Local, typed knowledge records and reviewed transactions. No agent role is installed.")
    parser.add_argument("--kb", help="Exact KnowledgeBase data directory (or set FIST_KB_ROOT)")
    parser.add_argument("--project-mode", action="store_true", help="MCP uses saved per-project selections, ignoring FIST_KB_ROOT")
    parser.add_argument("--version", action="store_true", help="Show the installed package version")
    parser.add_argument("command", nargs="?", help="init, mcp, session-context, draft, prepare, checkpoint, guest, inventory, or a native KB command")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.version:
        from . import __version__
        print(__version__)
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "init":
            subparser = argparse.ArgumentParser(prog="fist-kb init", allow_abbrev=False)
            subparser.add_argument("project_dir")
            initial = subparser.parse_args(args.arguments)
            from .initialize import initialize
            result = initialize(initial.project_dir)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0
        if args.command == "mcp":
            if args.arguments:
                raise ValueError("mcp accepts no additional arguments; select the data root with --kb")
            from .mcp_server import main as serve
            if args.project_mode and args.kb:
                raise ValueError("Choose --project-mode or --kb, not both.")
            selected = None if args.project_mode else args.kb or os.environ.get("FIST_KB_ROOT")
            result = serve(configured_root(selected) if selected else None)
            return result if isinstance(result, int) else 0
        if args.command in {"setup", "setup-status"}:
            subparser = argparse.ArgumentParser(prog="fist-kb " + args.command, allow_abbrev=False)
            subparser.add_argument("project")
            if args.command == "setup":
                subparser.add_argument("--mode", choices=["existing", "create"], required=True)
                subparser.add_argument("--folder")
                subparser.add_argument("--replace", action="store_true")
                subparser.add_argument("--allow-separate-store", action="store_true",
                                       help="Accept KB.parent as a separate evidence boundary from this project")
            options = subparser.parse_args(args.arguments)
            from .setup import configure, status
            value = status(options.project) if args.command == "setup-status" else configure(
                options.project, options.mode, options.folder, replace=options.replace,
                allow_separate_store=options.allow_separate_store)
            print(json.dumps(value, ensure_ascii=False))
            return 0 if value.get("ok", False) else 2
        if args.command == "session-context":
            subparser = argparse.ArgumentParser(prog="fist-kb session-context", allow_abbrev=False,
                description="Print the selected project's core as plain text for a host session-start hook.")
            subparser.add_argument("--project", help="Project directory (default: CLAUDE_PROJECT_DIR, then the working directory)")
            options = subparser.parse_args(args.arguments)
            from .session_context import hook_project, session_context
            try:
                text = session_context(hook_project(options.project))
            except (OSError, ValueError, RuntimeError) as error:
                # A session-start hook must not disturb the session: add no context, explain on stderr.
                print(f"Fist KB session context unavailable: {error}", file=sys.stderr)
                return 0
            if text:
                print(text)
            return 0
        root = configured_root(args.kb)
        if not root.is_dir():
            raise ValueError(f"KnowledgeBase directory does not exist: {root}")
        module, rest = "kb", [args.command, *args.arguments]
        if args.command == "draft":
            module = "kb_client"
        elif args.command == "prepare":
            module, rest = "prepare", args.arguments
            if rest and rest[0] not in {"inspect", "prepare", "verify", "-h", "--help"}:
                rest = ["prepare", *rest]
        elif args.command in {"checkpoint", "consolidation"}:
            module, rest = "consolidation_scope", args.arguments
            if rest and rest[0] == "compare":
                rest = rest[1:]
        elif args.command == "guest":
            module, rest = "guest_access", args.arguments
        elif args.command in {"inventory", "verify-tools"}:
            module = "tool_inventory"
            rest = args.arguments if args.command == "inventory" else ["verify", *args.arguments]
        env = {**os.environ, "FIST_KB_ROOT": str(root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        mutating = (not any(flag in rest for flag in ("-h", "--help")) and
                    (module == "kb" and rest[0] == "apply" and "--dry-run" not in rest
                     or module == "prepare" and bool(rest) and rest[0] == "prepare"))
        # -P keeps the caller's working directory off sys.path, so project files cannot shadow imports.
        return _run_engine([sys.executable, "-X", "utf8", "-B", "-P", "-m", f"fist_kb._engine.{module}", *rest],
                           env, root, mutating=mutating)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "error": str(error), "code": getattr(error, "code", None) or
                          ("initialization_failed" if args.command == "init" else "invocation_failed")},
                         ensure_ascii=False, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
