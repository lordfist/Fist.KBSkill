"""Build one wheel and one shared plugin ZIP. No expanded plugin source copies."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]

def identity(path: Path) -> dict:
    raw = path.read_bytes()
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest().upper()}

def refresh_manifest(root: Path = ROOT) -> dict:
    package = root / "src" / "fist_kb"
    version = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    manifest = {"format": 1, "package": "fist-kb", "version": version,
                "files": {p.relative_to(package).as_posix(): identity(p)
                          for p in sorted(package.rglob("*.py")) if "__pycache__" not in p.parts}}
    (package / "_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest

def plugin_files(root: Path = ROOT) -> list[Path]:
    files = [root / item for item in (
        "plugin.json", "mcp.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json",
        "pyproject.toml", "uv.lock", "LICENSE", "README.md", "PROVENANCE.json", "VALIDATION.md", "scripts/launch.ps1")]
    for directory in ("src", "skills", "docs"):
        files.extend(p for p in (root / directory).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix != ".pyc")
    return sorted(files)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-only", action="store_true")
    args = parser.parse_args()
    manifest = refresh_manifest()
    if args.manifest_only:
        print(json.dumps({"version": manifest["version"], "files": len(manifest["files"])}))
        return 0
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("Build contributors need uv on PATH.")
    dist = ROOT / "dist"
    if dist.is_symlink() or (dist.exists() and dist.resolve().parent != ROOT.resolve()):
        raise RuntimeError("dist must be an ordinary child of this checkout")
    dist.mkdir(exist_ok=True)
    old_manifest = dist / "release-manifest.json"
    old_files = json.loads(old_manifest.read_text("utf-8"))["files"] if old_manifest.exists() else {}
    expected = set(old_files) | ({"release-manifest.json"} if old_manifest.exists() else set())
    if {p.name for p in dist.iterdir()} != expected:
        raise RuntimeError("Unexpected dist contents; inspect instead of deleting unrecognized files")
    for name, previous in old_files.items():
        path = dist / name
        if path.parent != dist or not path.is_file() or path.is_symlink() or identity(path) != previous:
            raise RuntimeError("A previous generated output changed; inspect it before rebuilding")
    with tempfile.TemporaryDirectory(prefix=".release-", dir=ROOT) as temporary:
        output = Path(temporary)
        subprocess.run([uv, "build", "--python", sys.executable, "--wheel", "--out-dir", str(output)], cwd=ROOT, check=True)
        (output / ".gitignore").unlink(missing_ok=True)
        archive_path = output / f"fist-kb-{manifest['version']}.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in plugin_files():
                if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                    raise RuntimeError("Unexpected linked plugin input")
                entry = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o644 << 16
                archive.writestr(entry, path.read_bytes())
        files = {p.name: identity(p) for p in sorted(output.iterdir()) if p.is_file()}
        for name in old_files:
            (dist / name).unlink()
        for path in output.iterdir():
            shutil.copyfile(path, dist / path.name)
        old_manifest.write_text(json.dumps({"format": 2, "name": "Fist KB", "version": manifest["version"],
            "runtime_bundled": False, "files": files}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "version": manifest["version"], "outputs": len(files), "published": False}))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
