"""Explicit data binding and identities of the installed Python package.

The manifest detects byte changes. It is not a sandbox, signature, or permission.
Data-directory tools, hooks and Python files are never imported by this package.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_ROOT / "_manifest.json"
ENGINE_FILES = ("kb.py", "kb_store.py", "kb_read.py", "kb_write.py", "kb_migrate.py", "kb_schema.json",
                "guest.py", "prepare.py", "source_versions.py")


class PackageIntegrityError(RuntimeError):
    pass


def configured_root(value: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the explicitly selected data directory without creating anything."""
    selected = value if value is not None else os.environ.get("FIST_KB_ROOT")
    if selected is None or not str(selected).strip():
        raise ValueError("Select a KB with --kb PATH or FIST_KB_ROOT; no implicit data root is used.")
    return Path(selected).expanduser().resolve()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest key: {key}")
        result[key] = value
    return result


def package_manifest() -> tuple[dict[str, Any], str]:
    """Read shipped expected identities; never derive them from current source files."""
    try:
        raw = MANIFEST_PATH.read_bytes()
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(manifest, dict) or manifest.get("format") != 1:
            raise ValueError("Unsupported package manifest format")
        if manifest.get("package") != "fist-kb" or not isinstance(manifest.get("version"), str):
            raise ValueError("Missing package/version identity")
        files = manifest.get("files")
        required = {f"_engine/{name}" for name in ENGINE_FILES} | {"runtime.py"}
        if not isinstance(files, dict) or not required.issubset(files):
            raise ValueError("Package manifest omits required runtime modules")
        for name, identity in files.items():
            if not isinstance(name, str):
                raise ValueError("Package identity path must be a string")
            path = PurePosixPath(name)
            if ("\\" in name or ":" in name or path.is_absolute() or not path.parts
                    or any(part in {".", "..", ""} for part in name.split("/"))):
                raise ValueError("Package identity path is not a bounded relative path")
            if (not isinstance(identity, dict) or set(identity) != {"bytes", "sha256"}
                    or type(identity["bytes"]) is not int or identity["bytes"] < 0
                    or not isinstance(identity["sha256"], str) or len(identity["sha256"]) != 64
                    or any(char not in "0123456789ABCDEF" for char in identity["sha256"])):
                raise ValueError(f"Invalid package identity: {name}")
        return manifest, hashlib.sha256(raw).hexdigest().upper()
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise PackageIntegrityError(f"Package manifest is unavailable or invalid: {error}") from error


def verify_package() -> dict[str, Any]:
    """Check declared packaged bytes, not files under the configured KB root."""
    manifest, digest = package_manifest()
    errors = []
    for name, expected in manifest["files"].items():
        path = PACKAGE_ROOT.joinpath(*PurePosixPath(name).parts)
        try:
            if not path.resolve().is_relative_to(PACKAGE_ROOT):
                raise OSError("Path leaves installed package")
            for item in [path, *path.parents]:
                if item == PACKAGE_ROOT:
                    break
                info = item.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise OSError("Linked package file")
            raw = path.read_bytes()
            actual = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest().upper()}
            if actual != expected:
                errors.append({"path": name, "expected": expected, "actual": actual})
        except OSError as error:
            errors.append({"path": name, "error": str(error)})
    return {"ok": not errors, "operation": "verify_package", "package": manifest["package"],
            "version": manifest["version"], "manifest_sha256": digest,
            "files_checked": len(manifest["files"]), "errors": errors, "effects": "read_only",
            "scope": "Packaged file identities; not authenticity, authority, or semantic correctness."}
