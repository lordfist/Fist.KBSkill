"""Read-only identities of the installed Fist KB package and selected data state."""
from __future__ import annotations

from . import kb
from ..runtime import PackageIntegrityError, verify_package


def verify() -> dict:
    core, records, sources = kb.load_state()
    kb.require_valid(core, records, sources)
    result = verify_package()
    result["kb_revision"] = core["kb_revision"]
    result["kb_root"] = str(kb.KB_ROOT)
    return result


def main(argv=None) -> int:
    parser = kb.JSONArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=["verify"])
    parser.add_argument("--compact", action="store_true")
    try:
        args = parser.parse_args(argv)
        result = verify()
        kb.emit(result, compact=args.compact)
        return 0 if result["ok"] else 2
    except (kb.KBError, PackageIntegrityError, OSError, ValueError) as error:
        kb.emit({"ok": False, "operation": "verify_package", "error": str(error), "effects": "read_only"}, compact=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
