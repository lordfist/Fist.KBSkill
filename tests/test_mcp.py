"""Exercise real MCP stdio negotiation, one-call writes with their echo, and new-session recovery."""
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

STATE = ("core.json", "records.jsonl", "journal.jsonl", "sources.json")


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
        return {name: hashlib.sha256((self.kb / name).read_bytes()).hexdigest() for name in STATE}

    async def test_tools_write_with_echo_preview_and_new_session_recovery(self):
        async with self.session() as session:
            tools = (await session.list_tools()).tools
            self.assertEqual({t.name for t in tools}, {"kb_status", "kb_setup", "kb_read", "kb_write"})
            by_name = {t.name: t for t in tools}
            self.assertTrue(by_name["kb_read"].annotations.readOnlyHint)
            self.assertFalse(by_name["kb_write"].annotations.readOnlyHint)
            orientation = await session.call_tool("kb_read", {})
            self.assertFalse(orientation.isError, orientation)
            self.assertEqual(orientation.structuredContent["mode"], "orientation")
            revision = orientation.structuredContent["revision"]
            before = self.canonical()
            request = {"by": "MCP example", "note": "Remember the selected colour", "changes": [
                {"new": {"ref": "d", "kind": "owner_decision", "title": "Chart colour",
                         "statement": "Use blue for the project chart.", "quote": "Make the chart blue."}},
                {"log": {"id": "$d", "text": "Colour chosen in the planning chat."}}]}
            preview = await session.call_tool("kb_write", {"request": {**request, "preview": True}})
            self.assertFalse(preview.isError, preview)
            self.assertEqual(preview.structuredContent["outcome"], "preview")
            self.assertEqual(before, self.canonical())
            written = await session.call_tool("kb_write", {"request": {"confirm": preview.structuredContent["preview_id"],
                                                                       "by": "MCP example"}})
            self.assertFalse(written.isError, written)
            self.assertEqual(written.structuredContent["outcome"], "committed")
            self.assertEqual(written.structuredContent["revision"], revision + 1)
            self.assertEqual(written.structuredContent["ids"], {"d": "DEC-0001"})
            after = self.canonical()
            refused = await session.call_tool("kb_write", {"request": {"by": "MCP example", "changes": [
                {"set": {"id": "DEC-0001", "fields": {"statement": "Use green."}}}]}})
            self.assertTrue(refused.isError)
            self.assertEqual(refused.structuredContent["error"]["code"], "revision_required")
            self.assertEqual(after, self.canonical())
        async with self.session() as recovered:
            found = await recovered.call_tool("kb_read", {"request": {"query": "blue"}})
            self.assertFalse(found.isError, found)
            self.assertEqual(found.structuredContent["results"][0]["id"], "DEC-0001")
            full = await recovered.call_tool("kb_read", {"request": {"ids": ["DEC-0001"], "log": "all"}})
            self.assertFalse(full.isError, full)
            parts = full.structuredContent["results"]
            self.assertEqual(parts[0]["quote"], "Make the chart blue.")
            self.assertEqual([part["text"] for part in parts if "log" in part], ["Colour chosen in the planning chat."])
            self.assertEqual(after, self.canonical())

    async def test_fixed_binding_status_read_and_setup_do_not_touch_project_binding(self):
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
            self.assertIn("fixed --kb", conflicting.structuredContent["error"]["message"])
            refused = await session.call_tool("kb_setup", {"project": str(other), "mode": "existing",
                "folder": str(self.project), "replace": True, "allow_separate_store": True})
            self.assertTrue(refused.isError)
            self.assertIn("fixed --kb", refused.structuredContent["error"]["message"])
            orientation = await session.call_tool("kb_read", {})
            self.assertFalse(orientation.isError, orientation)
            core = orientation.structuredContent["results"][0]["core"]
            stored = json.loads((self.kb / "core.json").read_bytes())
            self.assertEqual(core, {key: value for key, value in stored.items() if key != "state_files"})
            wrong = await session.call_tool("kb_read", {"project": str(other)})
            self.assertTrue(wrong.isError)
        self.assertEqual(before_selection, selection.read_bytes())
        self.assertEqual(before, self.canonical())

    async def test_invalid_requests_are_reported_as_errors_and_change_nothing(self):
        before = self.canonical()
        async with self.session() as session:
            unknown = await session.call_tool("kb_read", {"request": {"frobnicate": True}})
            self.assertTrue(unknown.isError)
            self.assertEqual(unknown.structuredContent["error"]["code"], "request_invalid")
            token = await session.call_tool("kb_read", {"request": {"continue": "not-a-token"}})
            self.assertTrue(token.isError)
            nameless = await session.call_tool("kb_write", {"request": {"changes": [
                {"new": {"kind": "observed_fact", "title": "A", "statement": "a"}}]}})
            self.assertTrue(nameless.isError)
            self.assertEqual(nameless.structuredContent["error"]["code"], "actor_required")
            self.assertEqual(nameless.structuredContent["outcome"], "not_written")
        self.assertEqual(before, self.canonical())


if __name__ == "__main__":
    unittest.main()
