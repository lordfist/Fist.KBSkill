"""Real unconfigured startup, setup, concurrent project reads, restart recovery and 0.3.x migration."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from legacy_fixture import legacy_kb  # noqa: E402


class ProjectMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="project-mcp-", dir=os.environ.get("FIST_KB_TEST_TMP"))
        self.addCleanup(fixture.cleanup)
        self.area = Path(fixture.name).resolve()
        self.projects = [self.area / "First č", self.area / "Second č"]
        for project in self.projects:
            project.mkdir()

    @asynccontextmanager
    async def connection(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
               "FIST_KB_ROOT": str(self.area / "must-not-use-global-kb")}
        plugin = os.environ.get("FIST_KB_TEST_PROJECT_PLUGIN")
        if plugin:
            root = Path(plugin)
            config = json.loads((root / "mcp.json").read_text())["mcpServers"]["fist-kb"]
            arguments = [part.replace("${PLUGIN_ROOT}", str(root)) for part in config["args"]]
            params = StdioServerParameters(command=config["command"], args=arguments, env=env)
        else:
            params = StdioServerParameters(command=sys.executable,
                args=["-X", "utf8", "-B", "-m", "fist_kb", "--project-mode", "mcp"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                await session.initialize()
                yield session

    async def call(self, session, name, **arguments):
        result = await session.call_tool(name, arguments)
        self.assertFalse(result.isError, result)
        return result.structuredContent

    async def test_setup_write_restart_and_two_projects(self):
        async with self.connection() as session:
            self.assertEqual(len((await session.list_tools()).tools), 4)
            missing = await session.call_tool("kb_read", {})
            self.assertTrue(missing.isError)
            for project, colour in zip(self.projects, ("blue č", "green č")):
                self.assertFalse((await self.call(session, "kb_status", project=str(project)))["ready"])
                setup = await self.call(session, "kb_setup", project=str(project), mode="create", folder=str(project))
                self.assertTrue(setup["ready"])
                written = await self.call(session, "kb_write", project=str(project), request={
                    "by": "test", "note": "Chosen colour",
                    "changes": [{"new": {"kind": "observed_fact", "title": "Colour", "statement": colour}}]})
                self.assertEqual((written["outcome"], written["revision"]), ("committed", 2))
        async with self.connection() as restarted:
            results = await asyncio.gather(*(self.call(restarted, "kb_read", project=str(project),
                request={"ids": ["FACT-0001"]}) for project in self.projects))
            self.assertEqual([value["results"][0]["statement"] for value in results], ["blue č", "green č"])
            for project in self.projects:
                self.assertTrue((await self.call(restarted, "kb_status", project=str(project)))["ready"])

    async def test_a_0_3_kb_is_reported_then_migrated_once_into_a_sibling_folder(self):
        project = self.projects[0]
        old = legacy_kb(project)
        before = {path.name: path.read_bytes() for path in old.iterdir() if path.is_file()}
        async with self.connection() as session:
            status = await session.call_tool("kb_status", {"project": str(project)})
            self.assertTrue(status.isError)
            self.assertEqual(status.structuredContent["code"], "v07_kb_requires_migration")
            refused = await session.call_tool("kb_read", {"project": str(project)})
            self.assertEqual(refused.structuredContent["error"]["code"], "v07_kb_requires_migration")
            nameless = await session.call_tool("kb_setup", {"project": str(project), "mode": "migrate"})
            self.assertTrue(nameless.isError)
            migrated = await self.call(session, "kb_setup", project=str(project), mode="migrate", by="test")
            self.assertTrue(migrated["ready"])
            self.assertFalse(migrated["old_kb_changed"])
            self.assertEqual(migrated["kb_root"], str(project / "KnowledgeBase.v08"))
            self.assertEqual(migrated["migration"]["report"]["lossless"]["mismatch_count"], 0)
            orientation = await self.call(session, "kb_read", project=str(project))
            core = orientation["results"][0]["core"]
            self.assertEqual((core["purpose"], core["binding"]["root"]), ("", str(project)))
            self.assertEqual(core["routes"]["startup_checks"]["lead"], ["FACT-0001"])
            record = await self.call(session, "kb_read", project=str(project), request={"ids": ["FACT-0001"], "log": "all"})
            parts = record["results"]
            self.assertEqual((parts[0]["statement"], parts[0]["limits"]),
                             ("The sample service starts with SAMPLE_MODE=1.", "Deployment untested."))
            self.assertIn("Checked locally.", [part.get("text") for part in parts])
            again = await session.call_tool("kb_setup", {"project": str(project), "mode": "migrate", "by": "test",
                                                         "folder": str(old)})
            self.assertTrue(again.isError)
        self.assertEqual({path.name: path.read_bytes() for path in old.iterdir() if path.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
