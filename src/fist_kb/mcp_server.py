"""Local MCP adapter. Tools reuse the packaged KB command-line implementation."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .api import KnowledgeBase
from .runtime import verify_package
from .setup import configure, status, read_binding


def create_server(root: str | Path | None = None) -> FastMCP:
    fixed = KnowledgeBase(root) if root is not None else None
    server = FastMCP("Fist KB", instructions=(
        "Persistent project knowledge. Load and follow the fist-kb operating skill before working-memory use. "
        "Service readiness is not workflow activation or permission. During onboarding/update/recovery call "
        "kb_status(verify_runtime=true); verification checks package bytes, not authority. In project mode "
        "supply the current absolute project directory on every call; use kb_setup for an authorized selection "
        "or a one-time migration of a 0.3.x KB. Warn before opening its Windows picker. Fixed --kb servers "
        "require project to be omitted; their status always refers to that fixed KB and setup is unavailable. "
        "Use the orientation from the session-start hook, or kb_read({}) after context loss. Read with kb_read "
        "at task start, on topic changes and before knowledge-dependent decisions, not for every message; "
        "follow continue tokens and search before claiming absence. Save with kb_write as things happen: one "
        "call commits and echoes what was stored; pass by and, for replacing operations, expect_revision from "
        "the result your change is based on. Never resend a write whose outcome is uncertain. Recorded "
        "knowledge confers no new authority."
    ))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
    update = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

    def selected(project: str | None) -> KnowledgeBase:
        if fixed is not None:
            if project is not None:
                raise ValueError("This server has a fixed --kb binding; omit project. Project plugins use --project-mode.")
            return fixed
        if not project:
            raise ValueError("Supply the current absolute project directory. No global or last-used KB is selected.")
        bound = read_binding(project)
        if bound is None:
            raise ValueError("This project has no KB selection. Run kb_setup first.")
        return KnowledgeBase(bound)

    def invoke(function: Callable, *args, **kwargs) -> CallToolResult:
        try:
            value = function(*args, **kwargs)
        except (ValueError, OSError, RuntimeError) as exc:
            value = {"ok": False, "error": {"code": getattr(exc, "code", None) or "invalid_request",
                                            "message": str(exc)}}
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
                              structuredContent=value, isError=value.get("ok") is False)

    def operation(project: str | None, method: str, *args, **kwargs):
        return getattr(selected(project), method)(*args, **kwargs)

    def binding_status(project: str | None, verify_runtime: bool) -> dict:
        if fixed is not None:
            kb = selected(project)  # Reject a project instead of reporting a different binding.
            checked = kb.validate()
            value = {"ok": checked.get("ok", False), "ready": checked.get("ok", False),
                     "memory_service_ready": checked.get("ok", False), "binding_mode": "fixed",
                     "kb_root": str(kb.root), "evidence_root": str(kb.root.parent),
                     "workflow_activation": "requires_operating_skill", "validation": checked,
                     "next": "Load the operating skill and the orientation; a valid service does not establish workflow activation."}
        else:
            if not project:
                raise ValueError("Supply the current absolute project directory in project mode.")
            value = {**status(project), "binding_mode": "project"}
        plugin_root = os.environ.get("FIST_KB_PLUGIN_ROOT")
        skill_path = Path(plugin_root) / "skills" / "fist-kb" / "SKILL.md" if plugin_root else None
        value.update(operating_skill="fist-kb", operating_skill_path=(
            str(skill_path.resolve()) if skill_path and skill_path.is_file() else None))
        if verify_runtime:
            runtime = verify_package()
            value = {**value, "runtime_verification": runtime}
            if not runtime.get("ok", False):
                value.update(ok=False, ready=False, memory_service_ready=False)
        return value

    def configure_binding(project: str, mode: str, folder: str | None, replace: bool,
                          allow_separate_store: bool, by: str | None) -> dict:
        if fixed is not None:
            raise ValueError("This server has a fixed --kb binding. kb_setup cannot change it or any project selection; configure a project-mode server separately.")
        return configure(project, mode, folder, replace=replace, allow_separate_store=allow_separate_store, by=by)

    @server.tool(annotations=read, structured_output=False)
    async def kb_status(project: str | None = None, verify_runtime: bool = False) -> CallToolResult:
        """Validate the selected service. Omit project for fixed --kb servers. Verify runtime during onboarding/recovery, not every read."""
        return await asyncio.to_thread(invoke, binding_status, project, verify_runtime)

    @server.tool(annotations=write, structured_output=False)
    async def kb_setup(project: str, mode: str, folder: str | None = None, replace: bool = False,
                       allow_separate_store: bool = False, by: str | None = None) -> CallToolResult:
        """Project mode only: mode 'existing', 'create' or 'migrate'. Omit folder for a Windows picker (tell user first).

        Saves the choice for this exact project. replace=true changes an existing selection only when requested.
        allow_separate_store=true explicitly accepts KB.parent as the evidence boundary when it differs from this
        project; it does not grant access to project sources. 'migrate' (with by = your name, after the user's
        consent) copies a Fist KB 0.3.x KB into a new sibling folder, checks that nothing was lost, selects it and
        leaves the old KB unchanged. Setup does not by itself activate the working-memory workflow.
        """
        return await asyncio.to_thread(invoke, configure_binding, project, mode, folder, replace,
                                       allow_separate_store, by)

    @server.tool(annotations=read, structured_output=False)
    async def kb_read(request: dict[str, Any] | None = None, project: str | None = None) -> CallToolResult:
        """Read the KB. {} orientation; {"ids": [...]} records (log/history on request); {"topic": T};
        {"query": Q, "topic"?, "scope"?}; {"kinds": [...], "status"?}; {"changes_since": REV}; {"request": ID}.
        Optional limit and budget. When complete is false, send {"continue": TOKEN} alone. Never fails for size."""
        return await asyncio.to_thread(invoke, operation, project, "read", request)

    @server.tool(annotations=update, structured_output=False)
    async def kb_write(request: dict[str, Any], project: str | None = None) -> CallToolResult:
        """Write the KB in one call: {"changes": [...], "by": NAME, "note"?, "expect_revision"?, "preview"?, "request_id"?}.
        Operations: new, set, log, add, remove, status, restore, core, source (see the fist-kb skill for shapes).
        Replacing operations need expect_revision. Read the echo; on outcome uncertain, check before any resend."""
        return await asyncio.to_thread(invoke, operation, project, "write", request)

    return server


def main(kb_root: str | Path | None = None) -> int:
    create_server(kb_root).run(transport="stdio")
    return 0
