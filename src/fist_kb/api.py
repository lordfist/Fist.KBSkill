"""Small process-isolated adapter over the same CLI used by humans.

Every call fixes one data root. No module is imported from a KB directory and
no uncertain write is retried. This is a convenience API, not a filesystem sandbox.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import uuid

from .process import run_captured


class KnowledgeBase:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir() or not (self.root / "core.json").is_file():
            raise ValueError("Select an initialized KnowledgeBase directory")

    def run(self, arguments: list[str], *, timeout: float = 120) -> dict[str, Any]:
        command = [sys.executable, "-X", "utf8", "-B", "-m", "fist_kb",
                   "--kb", str(self.root), *arguments]
        try:
            result = run_captured(command,
                                  env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "code": "execution_timeout", "outcome_unknown": True,
                    "termination": getattr(exc, "termination", {}),
                    "error": "The call timed out. Inspect KB state and any transaction before retrying; a write may have completed."}
        except OSError as exc:
            return {"ok": False, "code": "launch_failed", "error": str(exc)}
        try:
            value = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            return {"ok": False, "code": "invalid_tool_output", "outcome_unknown": True,
                    "error": "No complete JSON result was received; inspect state before repeating a write.",
                    "exit_code": result.returncode, "stderr": result.stderr[-2000:]}
        if not isinstance(value, dict):
            return {"ok": False, "code": "invalid_tool_output", "outcome_unknown": True,
                    "error": "Tool response was not a JSON object."}
        if result.returncode:
            value = {**value, "ok": False, "exit_code": result.returncode}
            if result.stderr:
                # CLI stderr can explain a post-commit cleanup error. Keep the
                # engine's outcome intact: a failed exit is not a failed commit.
                value["cli_diagnostics"] = {
                    "source": "fist-kb CLI stderr",
                    "stderr": result.stderr[-4000:],
                    "truncated": len(result.stderr) > 4000,
                    "characters": len(result.stderr),
                }
        return value

    def core(self, *, budget: int = 8000, expect_revision: int | None = None) -> dict:
        """Read complete core; the native limit/projection affects linked IDs only."""
        if type(budget) is not int or not 300 <= budget <= 20000:
            raise ValueError("budget must be between 300 and 20000 estimated tokens")
        args = ["core", "--ids-only", "--compact", "--budget", str(budget), "--limit", "1"]
        if expect_revision is not None:
            args += ["--expect-revision", str(expect_revision)]
        value = {**self.run(args), "kb_root": str(self.root), "evidence_root": str(self.root.parent)}
        if value.get("ok") is False:
            return value
        if not isinstance(value.get("core"), dict) or value.get("core_is_projection") is not False:
            return {"ok": False, "code": "invalid_core_output", "kb_root": str(self.root),
                    "error": "The core command did not return a complete core. Do not use a projection as full core."}
        # Include adapter metadata in the same compact-JSON estimate as the
        # engine. Never make a complete core appear to fit by truncating it.
        for _ in range(3):
            value["estimated_tokens"] = (len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 2) // 3
        if value["estimated_tokens"] > budget:
            return {"ok": False, "code": "budget_too_small", "kb_revision": value["kb_revision"],
                    "required_budget": value["estimated_tokens"], "kb_root": str(self.root),
                    "evidence_root": str(self.root.parent),
                    "error": "Complete core plus binding metadata exceeds budget. Raise budget; no partial core was returned."}
        return value

    def context(self, topics: list[str] | None = None, *, budget: int = 2000,
                limit: int = 10, offset: int = 0, expect_revision: int | None = None) -> dict:
        args = ["context", "--brief", "--compact", *page_args(budget, limit, offset, expect_revision)]
        for topic in topics or []:
            args += ["--topic", topic]
        return self.run(args)

    def search(self, query: str, *, budget: int = 4000, limit: int = 10, offset: int = 0,
               expect_revision: int | None = None, history: bool = False,
               records_only: bool = True) -> dict:
        args = ["search", "--compact", *page_args(budget, limit, offset, expect_revision)]
        if history:
            args.append("--history")
        if records_only:
            args.append("--records-only")
        args += ["--", query]
        return self.run(args)

    def get(self, identifiers: list[str], *, fields: list[str] | None = None,
            array: str | None = None, budget: int = 6000, limit: int = 20,
            offset: int = 0, expect_revision: int | None = None,
            metadata_only: bool = True) -> dict:
        if not identifiers:
            raise ValueError("Supply at least one record or source ID")
        args = ["get", *identifiers, "--compact", *page_args(budget, limit, offset, expect_revision)]
        if metadata_only and not array:
            args.append("--metadata-only")
        if array:
            args += ["--array", array]
        if fields:
            args += ["--fields", *fields]
        return self.run(args)

    def draft(self, request: dict[str, Any], expect_revision: int) -> dict:
        """Retain request + draft for an explicit subsequent preview and commit."""
        raw = json.dumps(request, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        folder = self.root / "transactions"
        if folder.is_symlink() or (folder.exists() and folder.resolve().parent != self.root):
            raise ValueError("The transactions directory must be inside this KB")
        folder.mkdir(exist_ok=True)
        token = uuid.uuid4().hex
        source, output = folder / f"{token}-request.json", folder / f"{token}-transaction.json"
        with source.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(raw)
        result = self.run(["draft", str(source), "--output", str(output),
                           "--expect-revision", str(expect_revision), "--compact"])
        return {**result, "request_path": str(source)}

    def transaction_path(self, name: str) -> Path:
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = self.root / "transactions" / path
        path = path.resolve(strict=True)
        if not path.is_relative_to(self.root / "transactions") or not path.is_file():
            raise ValueError("Select a transaction JSON file inside this KB's transactions directory")
        return path

    def preview(self, transaction: str, expect_revision: int, *, budget: int = 6000,
                limit: int = 100, offset: int = 0, transaction_sha256: str | None = None,
                preview_time: str | None = None) -> dict:
        if offset and (not transaction_sha256 or not preview_time):
            raise ValueError("Preview continuation needs the first page's transaction_sha256 and preview_time")
        args = ["apply", str(self.transaction_path(transaction)), "--dry-run", "--preview-values",
                "--compact", "--expect-revision", str(expect_revision), "--budget", str(budget),
                "--limit", str(limit), "--offset", str(offset)]
        if transaction_sha256:
            args += ["--expect-transaction-sha256", transaction_sha256]
        if preview_time:
            args += ["--preview-time", preview_time]
        return self.run(args)

    def apply(self, transaction: str, expect_revision: int, transaction_sha256: str) -> dict:
        if len(transaction_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in transaction_sha256):
            raise ValueError("Pass the exact SHA-256 returned by the reviewed preview")
        return self.run(["apply", str(self.transaction_path(transaction)), "--expect-revision",
                         str(expect_revision), "--expect-transaction-sha256", transaction_sha256, "--compact"])

    def validate(self, *, deep: bool = False) -> dict:
        return self.run(["validate", "--compact", *(["--deep"] if deep else [])])


def page_args(budget: int, limit: int, offset: int, revision: int | None) -> list[str]:
    if offset and revision is None:
        raise ValueError("Continuation needs expect_revision from the first page")
    args = ["--budget", str(budget), "--limit", str(limit), "--offset", str(offset)]
    if revision is not None:
        args += ["--expect-revision", str(revision)]
    return args

