"""User-selected KBs remembered per project, outside the installed plugin.

This is cooperative configuration, not a filesystem sandbox. There is no global
active KB: every unbound tool call names its project and captures one data root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from .api import KnowledgeBase
from .initialize import initialize

CONFIG_NAME = ".fist-kb.json"


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


def status(project: str | Path) -> dict[str, Any]:
    base = project_path(project)
    root = read_binding(base)
    if root is None:
        return {"ok": True, "ready": False, "memory_service_ready": False,
                "workflow_activation": "requires_operating_skill", "project": str(base),
                "next": "Run kb_setup, then load the fist-kb working skill and complete onboarding."}
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
              *, replace: bool = False, allow_separate_store: bool = False) -> dict[str, Any]:
    base = project_path(project)
    if mode not in {"existing", "create"}:
        raise ValueError("Choose mode 'existing' or 'create'.")
    previous = read_binding(base)
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
    checked = KnowledgeBase(root).validate()
    if not checked.get("ok", False):
        return {"ok": False, "ready": False, "created": created, "kb_root": str(root),
                "validation": checked, "error": "Validation failed; the saved selection was not changed."}
    value = {"format": 1, "project": str(base), "kb": str(root.resolve())}
    config = base / CONFIG_NAME
    # Avoid touching an unchanged selection. Replacement concerns only this small
    # pointer; knowledge files and existing agent instructions remain untouched.
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
    return {"ok": True, "ready": True, "project": str(base), "kb_root": str(root.resolve()),
            "created": created, "selection_file": str(config), "migration_performed": False,
            "validation": checked, "memory_service_ready": True,
            "workflow_activation": "requires_operating_skill", **scope}
