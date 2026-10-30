"""User-selected KBs remembered per project, outside the installed plugin.

This is cooperative configuration, not a filesystem sandbox. There is no global
active KB: every unbound tool call names its project and captures one data root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from .api import KnowledgeBase, kb_format, migration_target
from .initialize import initialize
from .process import run_captured

CONFIG_NAME = ".fist-kb.json"
MIGRATION_TIMEOUT = 600


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Supply the current project's absolute directory.")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("The project must be an existing directory.")
    return path


def read_binding(project: str | Path) -> Path | None:
    base = project_path(project)
    config = base / CONFIG_NAME
    if not config.exists():
        return None
    if config.is_symlink() or config.stat().st_size > 16384:
        raise ValueError("The project KB selection is not a small regular configuration file.")
    data = json.loads(config.read_text("utf-8"))
    if (not isinstance(data, dict) or data.get("format") != 1
            or data.get("project") != str(base) or not isinstance(data.get("kb"), str)):
        raise ValueError("The saved KB selection is invalid or belongs to another project; run setup explicitly.")
    root = Path(data["kb"])
    if not root.is_absolute():
        raise ValueError("The saved KB path must be absolute.")
    # Keep a missing target inspectable so an explicit replacement can repair the
    # selection without requiring the user to edit configuration by hand.
    return root.resolve()


def storage_scope(project: Path, root: Path) -> dict[str, Any]:
    """Describe the existing engine boundary; selection never expands it."""
    evidence = root.parent.resolve()
    same = evidence == project
    return {"evidence_root": str(evidence),
            "storage_mode": "project" if same else "separate_store",
            "project_evidence_root_matches": same,
            "storage_note": ("Local evidence must stay inside this project and outside its KnowledgeBase."
                             if same else
                             "This is a separate store. Local evidence is limited to the storage parent, "
                             "not automatically the client project. Project files outside evidence_root "
                             "cannot be registered or prepared. Choose the project folder for project evidence.")}


def legacy_result(project: Path, root: Path, **extra: Any) -> dict[str, Any]:
    """A Fist KB 0.3.x (v07) KB: explain the one-time migration instead of a validation failure."""
    return {"ok": False, "ready": False, "memory_service_ready": False, "code": "v07_kb_requires_migration",
            "project": str(project), "kb_root": str(root), "migration_target": str(migration_target(root)), **extra,
            "next": "This KB uses the Fist KB 0.3.x (v07) layout. With the user's consent, call kb_setup with "
                    "mode 'migrate' and by (your name): it copies the knowledge into the new sibling folder, checks "
                    "that nothing was lost, selects the new KB and leaves the old one unchanged."}


def status(project: str | Path) -> dict[str, Any]:
    base = project_path(project)
    root = read_binding(base)
    if root is None:
        return {"ok": True, "ready": False, "memory_service_ready": False,
                "workflow_activation": "requires_operating_skill", "project": str(base),
                "next": "Run kb_setup, then load the fist-kb working skill and complete onboarding."}
    if kb_format(root) == "v07":
        return legacy_result(base, root, **storage_scope(base, root))
    checked = KnowledgeBase(root).validate()
    return {"ok": checked.get("ok", False), "ready": checked.get("ok", False),
            "project": str(base), "kb_root": str(root), "validation": checked,
            "memory_service_ready": checked.get("ok", False),
            "workflow_activation": "requires_operating_skill", **storage_scope(base, root)}


def choose_directory(title: str) -> str | None:
    """Open a Windows folder picker only when the caller requests setup.

    No shell interpolation of user text; title travels through the environment.
    Cancellation returns None and does not create a KB or selection file.
    """
    if os.name != "nt":
        raise ValueError("This host has no Windows folder picker. Supply the folder in chat to kb_setup.")
    script = """
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:FIST_KB_PICKER_TITLE
$dialog.ShowNewFolderButton = $true
try {
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        [Console]::Write($dialog.SelectedPath)
    }
} finally { $dialog.Dispose() }
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-WindowStyle", "Hidden",
                             "-Command", script], env={**os.environ, "FIST_KB_PICKER_TITLE": title},
                            capture_output=True, encoding="utf-8", check=True)
    return result.stdout.strip() or None


def configure(project: str | Path, mode: str, folder: str | None = None,
              *, replace: bool = False, allow_separate_store: bool = False, by: str | None = None) -> dict[str, Any]:
    base = project_path(project)
    if mode not in {"existing", "create", "migrate"}:
        raise ValueError("Choose mode 'existing', 'create' or 'migrate'.")
    previous = read_binding(base)
    if mode == "migrate":
        return migrate(base, previous, folder, replace=replace, allow_separate_store=allow_separate_store, by=by)
    selected = folder if folder else choose_directory(
        "Choose the folder for a NEW KnowledgeBase" if mode == "create"
        else "Choose your existing KnowledgeBase (or its project folder)")
    if selected is None:
        return {"ok": True, "cancelled": True, "changed": False, "project": str(base)}
    chosen = project_path(selected)
    root = (chosen if (chosen / "core.json").is_file() else chosen / "KnowledgeBase")
    protected = [Path(__file__).resolve().parent]
    for key in ("FIST_KB_PLUGIN_ROOT", "FIST_KB_RUNTIME_DIR"):
        if os.environ.get(key):
            protected.append(Path(os.environ[key]).resolve())
    if any(root.resolve().is_relative_to(path) or base.is_relative_to(path) for path in protected):
        raise ValueError("Choose project and knowledge folders outside the installed plugin and its runtime.")
    if previous is not None and previous != root and not replace:
        raise ValueError("This project already has a KB. Use replace=true only for a requested change; neither KB is deleted.")
    scope = storage_scope(base, root.resolve())
    if not scope["project_evidence_root_matches"] and not allow_separate_store:
        return {"ok": False, "code": "storage_scope_confirmation_required", "ready": False,
                "changed": False, "created": False, "project": str(base), "kb_root": str(root.resolve()),
                **scope, "next": "Choose the actual project folder for project-file evidence, or explain "
                "the separate-store boundary and retry with allow_separate_store=true only after the user accepts it."}
    created = False
    if mode == "create":
        if (chosen / "core.json").exists() or root.exists():
            raise ValueError("A KB already exists here. Choose 'existing'; setup never overwrites it.")
        initialize(chosen)
        created = True
    if (root / "tools").exists() or (root / "tool-state.json").exists():
        raise ValueError("This appears to be an evolved Seed KB. Review compatibility on a copy before adopting this runtime.")
    if kb_format(root) == "v07":
        return {**legacy_result(base, root, **scope), "created": False, "changed": False}
    checked = KnowledgeBase(root).validate()
    if not checked.get("ok", False):
        return {"ok": False, "ready": False, "created": created, "kb_root": str(root),
                "validation": checked, "error": "Validation failed; the saved selection was not changed."}
    config = save_selection(base, previous, root)
    return {"ok": True, "ready": True, "project": str(base), "kb_root": str(root.resolve()),
            "created": created, "selection_file": str(config), "migration_performed": False,
            "validation": checked, "memory_service_ready": True,
            "workflow_activation": "requires_operating_skill", **scope}


def save_selection(base: Path, previous: Path | None, root: Path) -> Path:
    """Write the project's small selection pointer atomically; an unchanged selection is not touched."""
    value = {"format": 1, "project": str(base), "kb": str(root.resolve())}
    config = base / CONFIG_NAME
    # Replacement concerns only this small pointer; knowledge files and existing agent instructions remain untouched.
    if previous != root:
        if config.is_symlink():
            raise ValueError("Refusing to replace a linked selection file.")
        descriptor, name = tempfile.mkstemp(prefix=".fist-kb-", suffix=".tmp", dir=base)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, config)
        finally:
            temporary.unlink(missing_ok=True)
    return config


def migrate(base: Path, previous: Path | None, folder: str | None, *, replace: bool,
            allow_separate_store: bool, by: str | None) -> dict[str, Any]:
    """Migrate a Fist KB 0.3.x (v07) KB into a new sibling folder and select it; the old KB is only read.

    The engine rebuilds every old field from the new KB and compares it before publishing anything.
    """
    if not isinstance(by, str) or not by.strip():
        raise ValueError("Migration needs by: the name recorded as the migration's writer.")
    if folder:
        chosen = project_path(folder)
        old = chosen if (chosen / "core.json").is_file() else chosen / "KnowledgeBase"
    elif previous is not None:
        old = previous
    else:
        raise ValueError("This project has no KB selection. Give the folder of the KB to migrate.")
    old = old.resolve()
    if not (old / "core.json").is_file():
        raise ValueError(f"No KB to migrate at {old}.")
    found = kb_format(old)
    if found == "v08":
        raise ValueError("This KB already uses the current layout; select it with mode 'existing'.")
    if found != "v07":
        raise ValueError("This folder does not hold a recognized Fist KB 0.3.x (v07) KB.")
    if previous is not None and previous != old and not replace:
        raise ValueError("This project already has a different KB. Use replace=true only for a requested change; "
                         "neither KB is deleted.")
    target = migration_target(old)
    protected = [Path(__file__).resolve().parent]
    for key in ("FIST_KB_PLUGIN_ROOT", "FIST_KB_RUNTIME_DIR"):
        if os.environ.get(key):
            protected.append(Path(os.environ[key]).resolve())
    if any(target.resolve().is_relative_to(path) or base.is_relative_to(path) for path in protected):
        raise ValueError("Choose project and knowledge folders outside the installed plugin and its runtime.")
    scope = storage_scope(base, target.resolve())
    if not scope["project_evidence_root_matches"] and not allow_separate_store:
        return {"ok": False, "code": "storage_scope_confirmation_required", "ready": False, "changed": False,
                "project": str(base), "old_kb_root": str(old), "kb_root": str(target), **scope,
                "next": "The migrated KB keeps the old KB's parent as its evidence boundary. Explain it and retry "
                        "with allow_separate_store=true only after the user accepts it."}
    if target.exists():
        raise ValueError(f"{target} already exists. Inspect it; setup never overwrites or deletes a folder.")
    target.mkdir()
    command = [sys.executable, "-X", "utf8", "-B", "-P", "-m", "fist_kb", "--kb", str(target),
               "admin", "migrate-v07", "--from", str(old), "--by", by.strip()]
    try:
        completed = run_captured(command, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                 timeout=MIGRATION_TIMEOUT)
        result = json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        result = {"ok": False, "outcome_unknown": True,
                  "error": {"code": "execution_timeout", "message": "The migration timed out."}}
    except ValueError:
        result = {"ok": False, "error": {"code": "invalid_tool_output", "message": completed.stderr[-2000:]}}
    if not isinstance(result, dict) or result.get("ok") is not True or result.get("published") is not True:
        left = target.exists() and any(target.iterdir())
        if not left and target.exists():
            target.rmdir()
        return {"ok": False, "ready": False, "changed": False, "project": str(base), "old_kb_root": str(old),
                "kb_root": str(target), "target_left_for_inspection": left, "migration": result,
                "next": "Nothing was selected and the old KB is unchanged. Report the migration result to the user."}
    checked = KnowledgeBase(target).validate()
    if not checked.get("ok", False):
        return {"ok": False, "ready": False, "changed": False, "project": str(base), "old_kb_root": str(old),
                "kb_root": str(target), "migration": result, "validation": checked,
                "next": "The migrated KB did not validate; nothing was selected and the old KB is unchanged."}
    config = save_selection(base, previous, target.resolve())
    return {"ok": True, "ready": True, "project": str(base), "kb_root": str(target.resolve()),
            "old_kb_root": str(old), "old_kb_changed": False, "created": True, "migration_performed": True,
            "selection_file": str(config), "migration": result, "validation": checked, "memory_service_ready": True,
            "workflow_activation": "requires_operating_skill", **scope,
            "next": "Load the working skill and kb_read({}). Review the migration report's navigation_changes and "
                    "task_candidates; create a task record if the work has one. The old KB folder stays as it was "
                    "and can be archived by the user."}
