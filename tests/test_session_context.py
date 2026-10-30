"""Session-start hook context: the orientation page that fits, nothing for unrelated projects, read-only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from fist_kb.api import KnowledgeBase
from fist_kb.session_context import OUTPUT_LIMIT
from fist_kb.setup import CONFIG_NAME, configure

sys.path.insert(0, str(Path(__file__).resolve().parent))
from legacy_fixture import legacy_kb  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[1]


def snapshot(root):
    return {str(path.relative_to(root)): (None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest())
            for path in sorted(root.rglob("*"))}


class SessionContextTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="session-context-", dir=os.environ.get("FIST_KB_TEST_TMP"))
        self.area = Path(fixture.name).resolve()

        def cleanup():
            fixture.cleanup()
            self.assertFalse(self.area.exists(), "Session-context fixture remained")

        self.addCleanup(cleanup)
        self.project = self.area / "Project č"
        self.project.mkdir()
        self.assertTrue(configure(self.project, "create", str(self.project))["ready"])
        self.kb = self.project / "KnowledgeBase"
        self.elsewhere = self.area / "unrelated"
        self.elsewhere.mkdir()
        self.env = dict(os.environ)
        for name in ("CLAUDE_PROJECT_DIR", "FIST_KB_ROOT"):
            self.env.pop(name, None)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def core(self):
        """The stored core as orientation shows it: everything but the state-file hashes."""
        stored = json.loads((self.kb / "core.json").read_bytes())
        return {key: value for key, value in stored.items() if key != "state_files"}

    @staticmethod
    def shown_core(text):
        return json.loads(text.splitlines()[-1])["results"][0]["core"]

    def hook(self, *args, cwd=None, env=None):
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-P", "-m", "fist_kb", "session-context",
                                 *map(str, args)], cwd=cwd or self.elsewhere, env=env or self.env,
                                capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        return result.stdout

    def test_selected_project_receives_complete_orientation_within_hook_limit(self):
        before = snapshot(self.area)
        text = self.hook("--project", self.project)
        lines = text.splitlines()
        self.assertIn(f"Project: {self.project};", lines[0])
        self.assertIn("revision 1", lines[0])
        self.assertIn("The complete orientation", lines[1])
        orientation = json.loads(lines[-1])
        self.assertEqual((orientation["mode"], orientation["complete"]), ("orientation", True))
        self.assertEqual(self.shown_core(text), self.core())
        self.assertLessEqual(len(text.encode("utf-8")), OUTPUT_LIMIT)
        self.assertEqual(snapshot(self.area), before, "The session-start read changed files")

    def test_project_comes_from_argument_then_claude_project_dir_then_working_directory(self):
        expected = self.core()
        self.assertEqual(self.shown_core(self.hook(env={**self.env, "CLAUDE_PROJECT_DIR": str(self.project)})),
                         expected)
        self.assertEqual(self.shown_core(self.hook(cwd=self.project)), expected)
        self.assertEqual(self.hook("--project", self.elsewhere, cwd=self.project), "")

    def test_unconfigured_project_adds_nothing(self):
        before = snapshot(self.elsewhere)
        self.assertEqual(self.hook(), "")
        self.assertEqual(snapshot(self.elsewhere), before)

    def test_unusable_selection_gets_one_line_and_no_core(self):
        (self.elsewhere / CONFIG_NAME).write_text(json.dumps(
            {"format": 1, "project": str(self.project), "kb": str(self.kb)}), encoding="utf-8")
        foreign = self.hook()
        self.assertEqual(len(foreign.splitlines()), 1, foreign)
        self.assertIn("Run Fist KB setup", foreign)
        shutil.rmtree(self.kb)
        missing = self.hook("--project", self.project)
        self.assertEqual(len(missing.splitlines()), 1, missing)
        self.assertIn("unavailable", missing)

    def test_a_0_3_kb_gets_one_line_asking_for_migration(self):
        old = self.area / "Older project"
        old.mkdir()
        root = legacy_kb(old)
        before = snapshot(old)
        text = self.hook("--project", old)
        self.assertEqual(len(text.splitlines()), 1, text)
        self.assertIn("migrate", text)
        self.assertEqual(snapshot(old), before)
        self.assertTrue(root.is_dir())

    def test_large_orientation_gets_a_first_page_within_the_limit(self):
        kb = KnowledgeBase(self.kb)
        changes = [{"new": {"ref": "t", "kind": "task", "title": "Ship the parser", "statement": "Ship it. " * 100,
                            "step": "review " * 60, "next": "fix the quoting bug " * 40, "stop": "owner approval"}},
                   {"core": {"current_task": "$t"}}, {"core": {"purpose": "Synthetic purpose. " * 30}}]
        changes += [{"new": {"ref": f"d{n}", "kind": "owner_decision", "title": f"Rule {n}",
                             "statement": "Standing rule. " * 60}} for n in range(10)]
        changes += [{"core": {"pin": [f"$d{n}" for n in range(10)]}}]
        changes += [{"log": {"id": "$t", "text": f"progress {n} " * 80}} for n in range(5)]
        self.assertTrue(kb.write({"by": "Synthetic regression", "changes": changes, "expect_revision": 1})["ok"])
        self.assertGreater(len(json.dumps(kb.read()).encode("utf-8")), OUTPUT_LIMIT)
        text = self.hook("--project", self.project)
        self.assertLessEqual(len(text.encode("utf-8")), OUTPUT_LIMIT)
        self.assertIn("revision 2", text.splitlines()[0])
        self.assertIn("first orientation page", text.splitlines()[1])
        page = json.loads(text.splitlines()[-1])
        self.assertFalse(page["complete"])
        self.assertEqual(page["results"][0]["core"], self.core())
        continued = kb.read({"continue": page["continue"]})
        self.assertTrue(continued["ok"], continued)

    def test_a_smaller_page_is_used_when_the_first_does_not_fit(self):
        # Long project and KB paths enlarge the text around the page; a lower limit stands in for them here.
        from unittest.mock import patch
        from fist_kb import session_context as hook
        kb = KnowledgeBase(self.kb)
        changes = [{"new": {"ref": f"d{n}", "kind": "owner_decision", "title": f"Rule {n}",
                            "statement": "Standing rule. " * 60}} for n in range(10)]
        changes += [{"core": {"pin": [f"$d{n}" for n in range(10)]}}]
        self.assertTrue(kb.write({"by": "Synthetic regression", "changes": changes, "expect_revision": 1})["ok"])
        limit = 3500
        self.assertGreater(len(json.dumps(kb.read({"budget": hook.BUDGETS[0]}), ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8")), limit)
        with patch.object(hook, "OUTPUT_LIMIT", limit):
            text = hook.session_context(self.project)
        self.assertLessEqual(len(text.encode("utf-8")), limit)
        page = json.loads(text.splitlines()[-1])
        self.assertEqual((page["mode"], page["complete"]), ("orientation", False))
        self.assertEqual(page["results"][0]["core"], self.core())

    def test_both_hosts_run_the_hook_at_start_resume_clear_and_compaction(self):
        def session_start(path):
            (group,) = json.loads((REPOSITORY / path).read_text(encoding="utf-8"))["hooks"]["SessionStart"]
            (hook,) = group["hooks"]
            return group["matcher"], hook

        for path in ("hooks/hooks.json", "hooks/codex-hooks.json"):
            matcher, _ = session_start(path)
            for source in ("startup", "resume", "clear", "compact"):
                self.assertRegex(source, f"^(?:{matcher})$")
            self.assertNotRegex("fork", f"^(?:{matcher})$")
        _, claude = session_start("hooks/hooks.json")
        self.assertEqual((claude["command"], claude["args"][-1]),
                         ("powershell.exe", "${CLAUDE_PLUGIN_ROOT}/scripts/session-context.ps1"))
        _, codex = session_start("hooks/codex-hooks.json")
        # Codex does not document the Windows shell for hook commands; keep shell syntax out of it.
        self.assertNotRegex(codex["commandWindows"], r"[$%`]")
        self.assertIn("'scripts\\session-context.ps1'", codex["commandWindows"])
        for manifest in ("plugin.json", ".codex-plugin/plugin.json"):
            data = json.loads((REPOSITORY / manifest).read_text(encoding="utf-8"))
            declared = data.get("extensions", {}).get("com.openai", {}).get("hooks", data.get("hooks"))
            self.assertEqual(declared, "./hooks/codex-hooks.json", manifest)

    @unittest.skipUnless(os.name == "posix" and shutil.which("pwsh"), "needs PowerShell 7 (pwsh) on a POSIX host")
    def test_powershell_hook_reads_only_the_runtime_the_launcher_prepared(self):
        runtime = self.area / "runtime"
        runtime.mkdir()
        fake_bin = self.area / "bin"
        fake_bin.mkdir()
        record = self.area / "uv-environment.txt"
        uv = fake_bin / "uv"
        uv.write_text('#!/bin/sh\nprintf "%s" "$UV_PROJECT_ENVIRONMENT" > "$FIST_KB_TEST_UV_RECORD"\n', encoding="utf-8")
        uv.chmod(0o755)
        env = {**self.env, "PATH": f"{fake_bin}{os.pathsep}{self.env['PATH']}",
               "FIST_KB_RUNTIME_DIR": str(runtime), "FIST_KB_TEST_UV_RECORD": str(record)}

        def script(name, cwd):
            return subprocess.run([shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
                                   str(REPOSITORY / "scripts" / name)], cwd=cwd, env=env,
                                  capture_output=True, encoding="utf-8", timeout=120)

        quiet = script("session-context.ps1", self.project)
        self.assertEqual((quiet.returncode, quiet.stdout), (0, ""), quiet.stderr)
        self.assertEqual(list(runtime.iterdir()), [], "The hook must not prepare a runtime")
        launched = script("launch.ps1", self.elsewhere)
        self.assertEqual(launched.returncode, 0, launched.stderr)
        # The scripts name the Windows layout; PowerShell on POSIX reads its backslashes as separators.
        python = Path(record.read_text(encoding="utf-8").replace("\\", "/")) / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        python.chmod(0o755)
        loaded = script("session-context.ps1", self.project)
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertEqual(self.shown_core(loaded.stdout), self.core())


if __name__ == "__main__":
    unittest.main()
