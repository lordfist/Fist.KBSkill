"""Real unconfigured startup, setup, concurrent project reads and restart recovery."""
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
            self.assertEqual(len((await session.list_tools()).tools), 10)
            missing = await session.call_tool("kb_context", {})
            self.assertTrue(missing.isError)
            for project, colour in zip(self.projects, ("blue č", "green č")):
                self.assertFalse((await self.call(session, "kb_status", project=str(project)))["ready"])
                setup = await self.call(session, "kb_setup", project=str(project), mode="create", folder=str(project))
                self.assertTrue(setup["ready"])
                draft = await self.call(session, "kb_draft", project=str(project), expect_revision=1,
                    request={"actor": "test", "note": "Chosen colour", "new_records": [
                        {"id": "FACT-0001", "kind": "observed_fact", "status": "active",
                         "statement": colour, "source_refs": []}]})
                preview = await self.call(session, "kb_preview", project=str(project),
                    transaction=draft["transaction"], expect_revision=1)
                await self.call(session, "kb_apply", project=str(project), transaction=draft["transaction"],
                    expect_revision=1, transaction_sha256=preview["transaction_sha256"])
            # A transaction from one project must not apply in the other.
            wrong = await session.call_tool("kb_preview", {"project": str(self.projects[0]),
                "transaction": draft["transaction"], "expect_revision": 2})
            self.assertTrue(wrong.isError)
        async with self.connection() as restarted:
            results = await asyncio.gather(*(self.call(restarted, "kb_get", project=str(project),
                identifiers=["FACT-0001"]) for project in self.projects))
            self.assertEqual([value["value"]["statement"] for value in results], ["blue č", "green č"])
            for project in self.projects:
                self.assertTrue((await self.call(restarted, "kb_status", project=str(project)))["ready"])


if __name__ == "__main__":
    unittest.main()
