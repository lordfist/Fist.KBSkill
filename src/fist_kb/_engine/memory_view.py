"""Optional disposable recent-record pointers; never changes canonical KB state.

Call remember only after one full, substantive record was actually returned.
The caller owns authorization, canonical validation and the total output budget.
No identity means disabled. Display is read-only; failures are advisory.
"""
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import stat
import tempfile


MAX_ANCHORS = 15
MAX_TASKS = 64
MAX_CACHE_BYTES = 16384
RECORD_ID = re.compile(r"(?:DEC|FACT|CLAIM|INF|ASM|REC|Q)-[0-9]{4}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")


class CacheError(Exception):
    pass


def _result(status, anchors=None, warnings=None):
    anchors = anchors or []
    return {"enabled": status != "disabled", "status": status,
            "warnings": warnings if warnings is not None else [],
            "anchors": anchors, "total_count": len(anchors),
            "returned_count": len(anchors), "omitted_count": 0}


def _safe(path, directory=False, missing=False):
    """Check every existing component, including Windows junctions/reparse points."""
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing and part == path:
                return None
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CacheError("symlink or reparse component")
        if part != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise CacheError("non-directory cache component")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CacheError("cache must be a regular, unshared file")
    return info


def _paths(kb_root, task_id):
    if task_id is None or task_id == "":
        return None
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id.encode("utf-8")) > 512:
        raise CacheError("task identity must contain 1 to 512 UTF-8 bytes")
    root = Path(os.path.abspath(kb_root))
    _safe(root, directory=True)
    cache = root / ".recent"
    key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return cache, cache / (key + ".json")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CacheError("duplicate cache field")
        result[key] = value
    return result


def _read(path):
    info = _safe(path, missing=True)
    if info is None:
        return []
    if info.st_size > MAX_CACHE_BYTES:
        raise CacheError("cache exceeds size bound")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
            raise CacheError("cache identity changed during access")
        data = stream.read(MAX_CACHE_BYTES + 1)
    if len(data) > MAX_CACHE_BYTES:
        raise CacheError("cache exceeds size bound")
    state = json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
    if (not isinstance(state, dict) or set(state) != {"schema_version", "anchors"}
            or type(state["schema_version"]) is not int or state["schema_version"] != 1
            or not isinstance(state["anchors"], list) or len(state["anchors"]) > MAX_ANCHORS):
        raise CacheError("unsupported cache schema")
    seen = set()
    for row in state["anchors"]:
        if (not isinstance(row, dict) or set(row) != {"id", "fingerprint"}
                or not isinstance(row["id"], str) or not RECORD_ID.fullmatch(row["id"])
                or row["id"] in seen or not isinstance(row["fingerprint"], str)
                or not HASH.fullmatch(row["fingerprint"])):
            raise CacheError("invalid cache anchor")
        seen.add(row["id"])
    return state["anchors"]


def _fingerprint(record):
    if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
            or not RECORD_ID.fullmatch(record["id"])
            or not isinstance(record.get("statement"), str) or not record["statement"]
            or not isinstance(record.get("status"), str)):
        raise CacheError("a full canonical record is required")
    body = json.dumps(record, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _remove_owned(path, identity):
    """Remove only our unchanged regular file; never follow cleanup indirection."""
    info = _safe(path, missing=True)
    if info and (info.st_dev, info.st_ino) == identity:
        path.unlink()
    elif info:
        raise CacheError("temporary file identity changed; cleanup withheld")


def remember(kb_root, task_id, selected_records):
    """Remember exactly one returned full record, newest first; no waiting/retry."""
    warnings = []
    owned = []
    cache = None
    made_directory = False
    try:
        paths = _paths(kb_root, task_id)
        if paths is None:
            return _result("disabled")
        if not isinstance(selected_records, list) or len(selected_records) != 1:
            raise CacheError("only one substantive detail retrieval may be remembered")
        record = selected_records[0]
        fingerprint = _fingerprint(record)
        entry = {"id": record["id"], "fingerprint": fingerprint}
        cache, path = paths
        if _safe(cache, directory=True, missing=True) is None:
            try:
                cache.mkdir()
                made_directory = True
            except FileExistsError:
                pass
        _safe(cache, directory=True)
        lock = cache / ".write.lock"
        _safe(lock, missing=True)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            return _result("busy", warnings=["Optional recent memory is busy; this access was not recorded."])
        with os.fdopen(fd, "wb") as stream:
            info = os.fstat(stream.fileno())
            owned.append((lock, (info.st_dev, info.st_ino)))
        previous = _read(path)
        if previous and previous[0] == entry:
            result = _result("unchanged", warnings=warnings)
            result.update(recorded_ids=[entry["id"]], tracked_count=len(previous))
            return result
        if not path.exists():
            # A bounded cache, not a retained run history. Full means explicitly
            # remove disposable task files before adding another identity.
            children = list(itertools.islice(cache.iterdir(), MAX_TASKS + 2))
            if len(children) > MAX_TASKS or sum(p.suffix == ".json" for p in children) >= MAX_TASKS:
                raise CacheError("recent task cache is full; remove unused disposable task files")
        state = {"schema_version": 1,
                 "anchors": ([entry] + [row for row in previous if row["id"] != entry["id"]])[:MAX_ANCHORS]}
        data = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_CACHE_BYTES:
            raise CacheError("cache exceeds size bound")
        _safe(cache, directory=True)
        fd, name = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=cache)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            info = os.fstat(stream.fileno())
            owned.append((temporary, (info.st_dev, info.st_ino)))
            stream.write(data)
        _safe(temporary)
        _safe(path, missing=True)
        os.replace(temporary, path)
        result = _result("recorded", warnings=warnings)
        result.update(recorded_ids=[entry["id"]], tracked_count=len(state["anchors"]))
        return result
    except (OSError, ValueError, TypeError, CacheError, RecursionError) as exc:
        warnings.append("Optional recent memory unavailable: " + str(exc))
        return _result("unavailable", warnings=warnings)
    finally:
        for path, identity in reversed(owned):
            try:
                _remove_owned(path, identity)
            except (OSError, CacheError) as exc:
                warnings.append("Optional recent cleanup incomplete: " + str(exc))
        if made_directory and cache is not None:
            try:
                _safe(cache, directory=True)
                cache.rmdir()  # Only succeeds when our new directory is empty.
            except OSError:
                pass
            except CacheError as exc:
                warnings.append("Optional recent cleanup withheld: " + str(exc))


def recent(kb_root, task_id, current_records):
    """Resolve pointers from caller-loaded current records; never touches cache."""
    try:
        paths = _paths(kb_root, task_id)
        if paths is None:
            return _result("disabled")
        cache, path = paths
        if _safe(cache, directory=True, missing=True) is None:
            return _result("empty")
        entries = _read(path)
        wanted = {row["id"] for row in entries}
        if not isinstance(current_records, list) or any(not isinstance(row, dict) for row in current_records):
            raise CacheError("current records must be a canonical record list")
        current = {row["id"]: row for row in current_records if row.get("id") in wanted}
        anchors = []
        for entry in entries:
            key = entry["id"]
            row = current.get(key)
            if row is None:
                anchors.append({"id": key, "missing": True})
                continue
            label = row.get("title")
            if not isinstance(label, str) or not label:
                label = row["statement"]
            anchor = {"id": key, "status": row["status"], "label": label[:80],
                      "label_is_navigation_only": True,
                      "changed_since_access": _fingerprint(row) != entry["fingerprint"]}
            resolution = row.get("current_resolution")
            if isinstance(resolution, dict) and resolution.get("record_ids"):
                pointers = resolution["record_ids"]
                if isinstance(pointers, list):
                    ids = [p for p in pointers if isinstance(p, str) and RECORD_ID.fullmatch(p)]
                    anchor["current_resolution_pointers"] = ids[:4]
                    if len(ids) > 4:
                        anchor["additional_pointers"] = len(ids) - 4
            anchors.append(anchor)
        return _result("ready" if anchors else "empty", anchors)
    except (OSError, ValueError, TypeError, KeyError, CacheError, RecursionError) as exc:
        return _result("unavailable", warnings=["Optional recent memory unavailable: " + str(exc)])
