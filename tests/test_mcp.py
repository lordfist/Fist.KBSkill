"""Exercise real MCP stdio negotiation and the reviewed-write workflow."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="fist-mcp-test-", dir=os.environ.get("FIST_KB_TEST_TMP", str(Path(__file__).resolve().parent)))
        def cleanup():
            fixture.cleanup()
            self.assertFalse(Path(fixture.name).exists(), "MCP fixture remained after cleanup")
        self.addCleanup(cleanup)
        self.area = Path(fixture.name)
        self.project = self.area / "A project č"
        self.project.mkdir()
        self.python = os.environ.get("FIST_KB_TEST_PYTHON", sys.executable)
        result = subprocess.run([self.python, "-X", "utf8", "-B", "-m", "fist_kb",
                                 "init", str(self.project)], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.kb = self.project / "KnowledgeBase"

    @asynccontextmanager
    async def session(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        plugin_root = os.environ.get("FIST_KB_TEST_PLUGIN")
        if plugin_root:
            plugin = Path(plugin_root).resolve(strict=True)
            config = json.loads((plugin / ".mcp.json").read_text("utf-8"))["mcpServers"]["fist-kb"]
            env["FIST_KB_ROOT"] = str(self.kb)
            arguments = [item.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin)).replace("${PLUGIN_ROOT}", str(plugin))
                         for item in config["args"]]
            self.assertNotIn("${", " ".join(arguments))
            parameters = StdioServerParameters(command=config["command"], args=arguments, env=env)
        else:
            parameters = StdioServerParameters(command=self.python, args=["-X", "utf8", "-B", "-m", "fist_kb",
                                "--kb", str(self.kb), "mcp"], env=env)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.serverInfo.name, "Fist KB")
                yield session

    def canonical(self):
        return {name: hashlib.sha256((self.kb / name).read_bytes()).hexdigest()
                for name in ("core.json", "records.jsonl", "sources.json")}

    async def test_tools_draft_preview_commit_and_new_session_recovery(self):
        async with self.session() as session:
            tools = (await session.list_tools()).tools
            self.assertEqual({t.name for t in tools}, {"kb_core", "kb_context", "kb_search", "kb_get", "kb_draft",
                                                      "kb_preview", "kb_apply", "kb_validate", "kb_status", "kb_setup"})
            by_name = {t.name: t for t in tools}
            self.assertTrue(by_name["kb_get"].annotations.readOnlyHint)
            self.assertFalse(by_name["kb_apply"].annotations.readOnlyHint)
            context = await session.call_tool("kb_context", {})
            self.assertFalse(context.isError, context)
            revision = context.structuredContent["kb_revision"]
            before = self.canonical()
            request = {"actor": "MCP example", "note": "Remember the selected colour",
                       "new_records": [{"id": "DEC-0001", "kind": "owner_decision", "status": "active",
                                        "statement": "Use blue for the project chart.", "source_refs": [],
                                        "resolution_notes": [{"note": "Colour chosen."}]}]}
            draft = await session.call_tool("kb_draft", {"request": request, "expect_revision": revision})
            self.assertFalse(draft.isError, draft)
            self.assertEqual(before, self.canonical())
            transaction = draft.structuredContent["transaction"]
            preview = await session.call_tool("kb_preview", {"transaction": transaction, "expect_revision": revision})
            self.assertFalse(preview.isError, preview)
            self.assertIsNone(preview.structuredContent["next_offset"])
            self.assertTrue(preview.structuredContent["preview"]["values_included"])
            sha = preview.structuredContent["transaction_sha256"]
            self.assertEqual(before, self.canonical())
            refused = await session.call_tool("kb_apply", {"transaction": transaction, "expect_revision": revision,
                                                           "transaction_sha256": "0" * 64})
            self.assertTrue(refused.isError)
            self.assertEqual(before, self.canonical())
            applied = await session.call_tool("kb_apply", {"transaction": transaction, "expect_revision": revision,
                                                           "transaction_sha256": sha})
            self.assertFalse(applied.isError, applied)
            self.assertTrue(applied.structuredContent["write_outcome"]["verification_completed"])
            after = self.canonical()
            stale = await session.call_tool("kb_apply", {"transaction": transaction, "expect_revision": revision,
                                                         "transaction_sha256": sha})
            self.assertTrue(stale.isError)
            self.assertEqual(after, self.canonical())
        async with self.session() as recovered:
            found = await recovered.call_tool("kb_search", {"query": "blue"})
            self.assertFalse(found.isError, found)
            self.assertIn("DEC-0001", json.dumps(found.structuredContent))
            full = await recovered.call_tool("kb_get", {"identifiers": ["DEC-0001"]})
            self.assertFalse(full.isError, full)
            self.assertEqual(full.structuredContent["value"]["statement"], "Use blue for the project chart.")
            page = await recovered.call_tool("kb_get", {"identifiers": ["DEC-0001"], "array": "resolution_notes"})
            self.assertFalse(page.isError, page)
            checked = await recovered.call_tool("kb_validate", {"deep": True})
            self.assertFalse(checked.isError, checked)
            self.assertEqual(after, self.canonical())

    async def test_fixed_binding_status_core_and_setup_do_not_touch_project_binding(self):
        from fist_kb.initialize import initialize
        from fist_kb.setup import configure
        other = self.area / "Project B"
        other.mkdir()
        initialize(other)
        chosen = configure(str(other), "existing", str(other))
        self.assertTrue(chosen["ok"], chosen)
        selection = other / ".fist-kb.json"
        before_selection = selection.read_bytes()
        before = self.canonical()
        async with self.session() as session:
            status = await session.call_tool("kb_status", {"verify_runtime": True})
            self.assertFalse(status.isError, status)
            state = status.structuredContent
            self.assertEqual(state["binding_mode"], "fixed")
            self.assertEqual(state["kb_root"], str(self.kb.resolve()))
            self.assertEqual(state["evidence_root"], str(self.project.resolve()))
            self.assertTrue(state["runtime_verification"]["ok"])
            self.assertEqual(state["runtime_verification"]["operation"], "verify_package")
            self.assertEqual(state["workflow_activation"], "requires_operating_skill")
            conflicting = await session.call_tool("kb_status", {"project": str(other)})
            self.assertTrue(conflicting.isError)
            self.assertIn("fixed --kb", conflicting.structuredContent["error"])
            refused = await session.call_tool("kb_setup", {"project": str(other), "mode": "existing",
                "folder": str(self.project), "replace": True, "allow_separate_store": True})
            self.assertTrue(refused.isError)
            self.assertIn("fixed --kb", refused.structuredContent["error"])
            core = await session.call_tool("kb_core", {"expect_revision": 1})
            self.assertFalse(core.isError, core)
            self.assertFalse(core.structuredContent["core_is_projection"])
            self.assertEqual(core.structuredContent["core"], json.loads((self.kb / "core.json").read_bytes()))
            wrong_core = await session.call_tool("kb_core", {"project": str(other)})
            self.assertTrue(wrong_core.isError)
        self.assertEqual(before_selection, selection.read_bytes())
        self.assertEqual(before, self.canonical())

    async def test_read_errors_and_outside_transaction_are_reported_as_errors(self):
        before = self.canonical()
        outside = self.project / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        async with self.session() as session:
            missing = await session.call_tool("kb_get", {"identifiers": ["FACT-9999"]})
            self.assertTrue(missing.isError)
            bad = await session.call_tool("kb_preview", {"transaction": str(outside), "expect_revision": 1})
            self.assertTrue(bad.isError)
            continued = await session.call_tool("kb_search", {"query": "blue", "offset": 2})
            self.assertTrue(continued.isError)
        self.assertEqual(before, self.canonical())


if __name__ == "__main__":
    unittest.main()
