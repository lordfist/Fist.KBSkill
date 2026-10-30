"""Fist KB reads (Seed KB v08 engine): orientation, known records, routes, search, filters and change lists in one operation.

A read never fails because something is large. Every mode builds an ordered list of parts; a part that does
not fit one page is split by field and then by text chunk, and a continuation token returns the rest. The
token carries the request and the revision, so a changed KB is reported instead of mixing revisions.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import functools
import json
import re
from typing import Any

from . import kb_store as store
from .kb_store import KBError, KINDS, LIMITS, SCHEMA, estimate_tokens, utf8_len

READ_KEYS = {"ids", "fields", "log", "history", "topic", "query", "scope", "kinds", "status", "changes_since",
             "request", "limit", "budget", "continue"}
DEFAULT_BUDGET, ORIENTATION_BUDGET, MIN_BUDGET, MAX_BUDGET = 4000, 6000, 600, 30000
TASK_ESSENTIALS = ("id", "kind", "title", "status", "step", "next", "stop")
RULES_NOTE = "Titles only: read these decisions with {\"ids\": [...]} before relying on them."
NO_TASK_HINT = ("No current task is set. For an assigned task, create one with kb_write "
                "{\"new\": {\"ref\": \"t\", \"kind\": \"task\", ...}} and {\"core\": {\"current_task\": \"$t\"}}.")
SOURCE_NOTE = "sha256 is the hash recorded at registration; fist-kb admin validate --deep re-checks the files."
PAGE_RESERVE = 450  # Page metadata, completeness and continuation; mode metadata is kept below this.
DEFAULT_LIMIT, MAX_LIMIT = 25, 200
# Recorded status only: a record marked superseded or historical ranks below current ones and names its successor.
STATUS_WEIGHT = {"superseded": 0.5, "historical": 0.5, "disputed": 0.75, "cancelled": 0.5}
SUMMARY_STATEMENT = 240
ORIENTATION_LOG = 3
RECENT_WRITES = 5
AUTOMATIC_VIEW = {"created_rev"}


def read(request: Any = None) -> dict[str, Any]:
    """The kb_read operation. Never raises; errors are only for malformed requests or invalid state."""
    try:
        return _read({} if request is None else request)
    except KBError as exc:
        return {"ok": False, "error": exc.payload()}
    except Exception as exc:
        return {"ok": False, "error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}}


def _read(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise KBError("request_invalid", "a read request is one JSON object", "Send {} for orientation.")
    offset = 0
    token_revision = None
    if "continue" in request:
        if set(request) != {"continue"}:
            raise KBError("request_invalid", "send the continue token alone", "{\"continue\": TOKEN}")
        request, offset, token_revision = decode_token(request["continue"])
    check_request(request)
    budget = request.get("budget", DEFAULT_BUDGET if set(request) - {"budget"} else ORIENTATION_BUDGET)
    state = store.load_state()
    store.require_valid(state)
    if token_revision is not None and token_revision != state.revision:
        raise KBError("revision_changed", f"the KB changed since the first page (revision {token_revision} -> "
                      f"{state.revision})", "Read again without continue.")
    meta, parts = build(state, request)
    capacity = budget - PAGE_RESERVE
    split = [piece for part in parts for piece in split_part(part, capacity)]
    page, used = [], 0
    for part in split[offset:]:
        size = estimate_tokens(part) + 1
        if page and used + size > capacity:
            break
        page.append(part)
        used += size
    end = offset + len(page)
    result = {"ok": True, "revision": state.revision, **meta, "results": page,
              "page": {"first": offset, "returned": len(page), "total": len(split)},
              "complete": end >= len(split)}
    result["continue"] = None if result["complete"] else encode_token(request, end, state.revision)
    tools = store.tool_status()
    if tools["status"] != "ok":
        result["tools"] = tools
    return result


def check_request(request: dict[str, Any]) -> None:
    unknown = sorted(set(request) - READ_KEYS)
    if unknown:
        raise KBError("request_invalid", f"unknown read keys: {', '.join(unknown)}",
                      f"Allowed keys: {', '.join(sorted(READ_KEYS - {'continue'}))}.")
    for key in ("ids", "fields", "kinds", "status"):
        if key in request and (not isinstance(request[key], list) or not request[key]
                               or any(not isinstance(item, str) for item in request[key])):
            raise KBError("request_invalid", f"{key} must be a nonempty list of strings")
    for key in ("topic", "query", "request"):
        if key in request and (not isinstance(request[key], str) or not request[key].strip()):
            raise KBError("request_invalid", f"{key} must be nonempty text")
    budget = request.get("budget", DEFAULT_BUDGET)
    if not isinstance(budget, int) or not MIN_BUDGET <= budget <= MAX_BUDGET:
        raise KBError("request_invalid", f"budget must be an integer from {MIN_BUDGET} to {MAX_BUDGET}",
                      "Omit it for the default page of about 4,000 estimated tokens.")
    limit = request.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise KBError("request_invalid", f"limit must be an integer from 1 to {MAX_LIMIT}")
    log = request.get("log")
    if log is not None and not (log == "all" or (isinstance(log, int) and log >= 0) or (
            isinstance(log, dict) and set(log) <= {"from", "to"} and all(isinstance(v, int) for v in log.values()))):
        raise KBError("request_invalid", "log is a count of newest entries, \"all\" or {\"from\": n, \"to\": m}")
    history = request.get("history")
    if history is not None and not isinstance(history, bool) and not (
            isinstance(history, list) and all(isinstance(item, str) for item in history)):
        raise KBError("request_invalid", "history is true or a list of field names")
    if request.get("scope", "current") not in {"current", "all"}:
        raise KBError("request_invalid", "scope is \"current\" (fields and log) or \"all\" (also history)")
    since = request.get("changes_since")
    if since is not None and (not isinstance(since, int) or since < 0):
        raise KBError("request_invalid", "changes_since is a revision number")
    for kind in request.get("kinds", []):
        if kind not in KINDS:
            raise KBError("request_invalid", f"unknown kind {kind!r}", f"Kinds: {', '.join(KINDS)}.")


def encode_token(request: dict[str, Any], offset: int, revision: int) -> str:
    raw = json.dumps({"v": 1, "rev": revision, "o": offset, "req": request}, ensure_ascii=False,
                     separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_token(token: Any) -> tuple[dict[str, Any], int, int]:
    try:
        raw = base64.urlsafe_b64decode(str(token) + "=" * (-len(str(token)) % 4))
        value = json.loads(raw.decode("utf-8"))
        assert value["v"] == 1 and isinstance(value["req"], dict) and isinstance(value["o"], int)
        return value["req"], value["o"], value["rev"]
    except (binascii.Error, ValueError, KeyError, TypeError, AssertionError, UnicodeDecodeError) as exc:
        raise KBError("request_invalid", "the continue token is not valid", "Use the token exactly as returned.") from exc


# ------------------------------------------------------------------------------------------------ modes


def build(state: store.State, request: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if "request" in request:
        return request_mode(state, request["request"])
    if "ids" in request:
        return ids_mode(state, request)
    if "changes_since" in request:
        return changes_mode(state, request["changes_since"])
    if "query" in request:
        return search_mode(state, request)
    if "topic" in request:
        return topic_mode(state, request)
    if "kinds" in request or "status" in request:
        return list_mode(state, request)
    return orientation(state)


def orientation(state: store.State) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    core = {key: value for key, value in state.core.items() if key != "state_files"}
    parts: list[dict[str, Any]] = [{"core": core}]
    task_id = state.core.get("current_task")
    detail: dict[str, Any] = {}
    if task_id in state.by_id:
        # Essentials first, so the first page always says what is being done, what is next and where to stop.
        task = view(state, state.by_id[task_id], {})
        essentials = {key: task[key] for key in TASK_ESSENTIALS if key in task}
        rules = [{"id": rid, "title": state.by_id[rid]["title"], "status": state.by_id[rid]["status"]}
                 for rid in state.by_id[task_id].get("rules", []) if rid in state.by_id]
        if rules:
            essentials["rules"] = rules
            essentials["rules_note"] = RULES_NOTE
        parts.append({"current_task": essentials})
        detail = {key: value for key, value in task.items() if key not in TASK_ESSENTIALS and key != "rules"}
    else:
        parts.append({"current_task": None, "hint": NO_TASK_HINT})
    parts.append({"recent_writes": [summary(entry) for entry in state.writes[-RECENT_WRITES:]]})
    parts.append({"index": index(state)})
    if detail:
        parts.append({"current_task_detail": {"id": task_id, **detail}})
        parts.extend(log_parts(state, task_id, ORIENTATION_LOG))
    for rid in state.core.get("pinned", []):
        if rid != task_id and rid in state.by_id:
            parts.append({"pinned": view(state, state.by_id[rid], {})})
    return {"mode": "orientation"}, parts


def index(state: store.State) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {}
    for row in state.records:
        counts.setdefault(row["kind"], {}).setdefault(row["status"], 0)
        counts[row["kind"]][row["status"]] += 1
    open_questions = [row["id"] for row in state.records if row["kind"] == "open_question" and row["status"] == "open"]
    active_tasks = [row["id"] for row in state.records if row["kind"] == "task"
                    and row["status"] not in {"completed", "cancelled"}]
    return {"counts": counts, "topics": state.topics(), "open_questions": open_questions[-10:],
            "active_tasks": active_tasks[-10:], "sources": len(state.sources.get("sources", []))}


def ids_mode(state: store.State, request: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    parts: list[dict[str, Any]] = []
    missing = []
    sources = {row["source_id"]: row for row in state.sources.get("sources", [])}
    for rid in dict.fromkeys(request["ids"]):
        if rid in state.by_id:
            parts.append(view(state, state.by_id[rid], request))
            if request.get("log"):
                parts.extend(log_parts(state, rid, request["log"]))
            if request.get("history"):
                parts.extend(history_parts(state, rid, request["history"]))
        elif rid in sources:
            parts.append({"source": sources[rid], "note": SOURCE_NOTE})
        else:
            prefix = rid.split("-")[0]
            near = difflib.get_close_matches(rid, [item for item in state.by_id if item.split("-")[0] == prefix],
                                             n=3, cutoff=0.5)
            missing.append({"id": rid, "nearest": near})
    meta: dict[str, Any] = {"mode": "ids"}
    if missing:
        meta["missing"] = missing[:20]
    return meta, parts


def topic_mode(state: store.State, request: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    topic = request["topic"]
    members = filtered(state, state.members(topic), request)
    meta: dict[str, Any] = {"mode": "topic", "topic": topic, "members": len(members)}
    route = state.core.get("routes", {}).get(topic)
    if route and route.get("about"):
        meta["about"] = route["about"]
    if not members and route is None:
        meta["missing_topic"] = True
        meta["nearest_topics"] = difflib.get_close_matches(topic, list(state.topics()), n=5, cutoff=0.4)
    limit = request.get("limit", DEFAULT_LIMIT)
    capacity = request.get("budget", DEFAULT_BUDGET) - PAGE_RESERVE
    parts, used = [], 0
    for rid in members[:limit]:
        full = view(state, state.by_id[rid], request)
        size = estimate_tokens(full)
        # The material answer: full current fields for the first records, summaries after half a page.
        if used + size <= capacity // 2 or not parts:
            parts.append(full)
            used += size
        else:
            parts.append(summary_view(state.by_id[rid]))
    if len(members) > limit:
        meta["omitted_members"] = len(members) - limit
    return meta, parts


def list_mode(state: store.State, request: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    priority = {kind: index for index, kind in enumerate(SCHEMA["kind_priority"])}
    rows = filtered(state, [row["id"] for row in state.records], request)
    rows.sort(key=lambda rid: (priority.get(state.by_id[rid]["kind"], 99), -state.by_id[rid].get("updated_rev", 0)))
    limit = request.get("limit", DEFAULT_LIMIT)
    meta = {"mode": "list", "matches": len(rows)}
    if len(rows) > limit:
        meta["omitted_matches"] = len(rows) - limit
    return meta, [summary_view(state.by_id[rid]) for rid in rows[:limit]]


def changes_mode(state: store.State, since: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    writes = [summary(entry, ops=True) for entry in state.writes if entry["rev"] > since]
    return {"mode": "changes", "since": since, "writes": len(writes)}, writes


def request_mode(state: store.State, request_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entry = state.requests.get(request_id)
    if entry is None:
        return {"mode": "request", "request_id": request_id, "found": False}, []
    return {"mode": "request", "request_id": request_id, "found": True}, [summary(entry, ops=True)]


def filtered(state: store.State, ids: list[str], request: dict[str, Any]) -> list[str]:
    kinds, statuses = request.get("kinds"), request.get("status")
    return [rid for rid in ids if (not kinds or state.by_id[rid]["kind"] in kinds)
            and (not statuses or state.by_id[rid]["status"] in statuses)]


def summary(entry: dict[str, Any], ops: bool = False) -> dict[str, Any]:
    result = {"rev": entry["rev"], "at": entry.get("at"), "by": entry.get("by"), "note": entry.get("note")}
    if ops:
        result["ops"] = entry.get("ops", [])
        if entry.get("ids"):
            result["ids"] = entry["ids"]
    else:
        result["ids"] = sorted({op["id"] for op in entry.get("ops", []) if op.get("id")})
    return result


# ------------------------------------------------------------------------------------------------ views


def view(state: store.State, row: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """The current fields of one record in a fixed priority order, with counts of its log and history."""
    fields = request.get("fields")
    order = [*SCHEMA["view_priority"], *sorted(row)]
    result: dict[str, Any] = {"id": row["id"], "kind": row["kind"]}
    clipped = []
    for key in dict.fromkeys(order):
        if key not in row or key in {"id", "kind"} or key in AUTOMATIC_VIEW:
            continue
        if fields and key not in fields:
            continue
        value = row[key]
        if key == "quote" and not (fields and "quote" in fields) and utf8_len(value) > LIMITS["quote_view"]:
            value = value[:LIMITS["quote_view"]] + "…"
            clipped.append("quote")
        result[key] = value
    if fields:
        absent = [name for name in fields if name not in row]
        if absent:
            result["absent_fields"] = absent
    logs, history = len(state.logs.get(row["id"], [])), len(state.history.get(row["id"], []))
    if logs:
        result["log_count"] = logs
    if history:
        result["history_count"] = history
    if clipped:
        result["clipped"] = clipped
    return result


def summary_view(row: dict[str, Any]) -> dict[str, Any]:
    statement = row["statement"]
    if len(statement) > SUMMARY_STATEMENT:
        statement = statement[:SUMMARY_STATEMENT] + "…"
    return {"id": row["id"], "kind": row["kind"], "status": row["status"], "title": row["title"],
            "statement": statement, "summary": True}


def log_parts(state: store.State, rid: str, selector: Any) -> list[dict[str, Any]]:
    entries = state.logs.get(rid, [])
    if selector == "all":
        chosen = entries
    elif isinstance(selector, dict):
        chosen = [entry for entry in entries if selector.get("from", 1) <= entry["n"] <= selector.get("to", entry["n"])]
    else:
        chosen = entries[-selector:] if selector else []
    return [{"id": rid, "log": entry["n"], "at": entry["at"], "by": entry.get("by"), "text": entry["text"]}
            for entry in chosen]


def history_parts(state: store.State, rid: str, selector: Any) -> list[dict[str, Any]]:
    entries = state.history.get(rid, [])
    if isinstance(selector, list):
        entries = [entry for entry in entries if entry["field"] in selector]
    return [{"id": rid, "history": entry["n"], "field": entry["field"], "at": entry["at"], "by": entry.get("by"),
             "value": entry["value"]} for entry in entries]


# ---------------------------------------------------------------------------------------------- paging


def split_part(part: dict[str, Any], capacity: int) -> list[dict[str, Any]]:
    """Split a part that does not fit one page: a record by fields, any long text by chunks."""
    if estimate_tokens(part) + 1 <= capacity:
        return [part]
    if "kind" in part and "id" in part and "title" in part:
        head = {key: part[key] for key in ("id", "kind", "status", "title") if key in part}
        head["field_index"] = {key: utf8_len(json.dumps(value, ensure_ascii=False))
                               for key, value in part.items() if key not in head}
        head["split"] = True
        pieces = split_part(head, capacity) if estimate_tokens(head) + 1 > capacity else [head]
        for key, value in part.items():
            if key not in {"id", "kind", "status", "title"}:
                pieces.extend(split_part({"id": part["id"], "field": key, "value": value}, capacity))
        return pieces
    label = {key: part[key] for key in ("id", "field", "log", "history") if key in part}
    text_key = next((key for key in ("text", "value") if isinstance(part.get(key), str)), None)
    text = part[text_key] if text_key else json.dumps(part, ensure_ascii=False)
    return chunk_text(label, text, "text" if text_key else "json", capacity)


def chunk_text(label: dict[str, Any], text: str, key: str, capacity: int) -> list[dict[str, Any]]:
    size = max(200, (capacity - estimate_tokens(label) - 40) * 3 // 2)
    while True:
        chunks = [text[start:start + size] for start in range(0, len(text), size)] or [""]
        pieces = [{**label, "chunk": number, "of": len(chunks), key: chunk} for number, chunk in enumerate(chunks, 1)]
        if all(estimate_tokens(piece) + 1 <= capacity for piece in pieces) or size <= 50:
            return pieces
        size //= 2


# ---------------------------------------------------------------------------------------------- search


def token_terms(text: str) -> list[str]:
    return [part for part in re.findall(r"[\w.-]+", text.casefold()) if len(part) > 1]


def text_blob(value: Any) -> str:
    return value.casefold() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).casefold()


# One common inflection, longest first; the remaining form keeps at least four letters (from v07).
WORD_FORM_SUFFIXES = ("ings", "ions", "ing", "ion", "ies", "ied", "ed", "en", "es", "s")
FULL_MATCH, WORD_START_MATCH, INSIDE_WORD_MATCH = 1.0, 2 / 3, 1 / 3


@functools.lru_cache(maxsize=512)
def word_form(term: str) -> re.Pattern[str] | None:
    if term.isalpha():
        for suffix in WORD_FORM_SUFFIXES:
            if term.endswith(suffix) and len(term) - len(suffix) >= 4:
                return re.compile(r"(?<![\w-])" + re.escape(term[:-len(suffix)]) + r"[^\W\d_]*(?![\w-])")
    return None


@functools.lru_cache(maxsize=512)
def whole_word(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![^\W_])" + re.escape(term) + r"(?:e?s)?(?![^\W_])")


@functools.lru_cache(maxsize=512)
def word_start(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![^\W_])" + re.escape(term))


def match_strength(term: str, text: str) -> float:
    form = word_form(term)
    if term not in text and not (form and form.search(text)):
        return 0.0
    if whole_word(term).search(text) or (form and form.search(text)):
        return FULL_MATCH
    if word_start(term).search(text):
        return WORD_START_MATCH
    return INSIDE_WORD_MATCH


def match_score(found: float, total: int, weighted: float) -> int:
    if not found or not total:
        return 0
    coverage = found / total
    score = round((weighted + found * 30) * coverage * coverage)
    if total > 1 and found == total:
        score += 40
    return max(score, 1)


def snippet(text: str, terms: list[str], width: int = 300) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= width:
        return flat
    lower = flat.casefold()
    starts = [position for term in terms if (position := lower.find(term)) >= 0]
    start = max(0, (min(starts) if starts else 0) - width // 4)
    result = flat[start:start + width]
    return ("…" if start else "") + result + ("…" if start + width < len(flat) else "")


def places(state: store.State, row: dict[str, Any], scope: str) -> list[tuple[str, str]]:
    result = [(key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
              for key, value in row.items() if key not in store.AUTOMATIC_FIELDS - {"id"} and key != "origin"]
    result.extend((f"log {entry['n']}", entry["text"]) for entry in state.logs.get(row["id"], []))
    if scope == "all":
        result.extend((f"history {entry['n']} ({entry['field']})",
                       entry["value"] if isinstance(entry["value"], str) else json.dumps(entry["value"], ensure_ascii=False))
                      for entry in state.history.get(row["id"], []))
    return result


def search_mode(state: store.State, request: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    query = request["query"]
    terms = list(dict.fromkeys(token_terms(query)))
    weights = SCHEMA["search_weights"]
    candidates = state.members(request["topic"]) if "topic" in request else [row["id"] for row in state.records]
    scope = request.get("scope", "current")
    scored = []
    for rid in filtered(state, candidates, request):
        row = state.by_id[rid]
        best: dict[str, float] = {}
        weighted = 0.0
        # Fields add up (a record has few); log and history count once, at their best entry, so a record with
        # hundreds of matching notes cannot outrank one that answers in its title or statement.
        repeated = {"log": 0.0, "history": 0.0}
        hits = []
        for where, text in places(state, row, scope):
            blob = text.casefold()
            found = {term: strength for term in terms if (strength := match_strength(term, blob))}
            if not found:
                continue
            base = where.split(" ", 1)[0]
            weight = weights.get(base, 2)
            gain = sum(found.values()) * weight
            if base in repeated:
                repeated[base] = max(repeated[base], gain)
            else:
                weighted += gain
            hits.append((gain, where, text))
            for term, strength in found.items():
                best[term] = max(best.get(term, 0.0), strength)
        score = match_score(sum(best.values()), len(terms), weighted + sum(repeated.values()))
        if score:
            score = max(1, round(score * STATUS_WEIGHT.get(row["status"], 1.0)))
        if query.strip().casefold() == rid.casefold():
            score += 20000
        if score:
            hits.sort(key=lambda item: -item[0])
            scored.append((score, rid, [{"where": where, "snippet": snippet(text, terms)} for _, where, text in hits[:3]],
                           len(hits)))
    # Equal scores keep the authored lead order of a route the query names (v07 route precedence), then ID order.
    named = [topic for topic in state.core.get("routes", {})
             if terms and all(match_strength(term, topic.replace("_", " ")) == FULL_MATCH for term in terms)]
    lead = {}
    for topic in named:
        for position, rid in enumerate(state.members(topic)):
            lead[rid] = min(lead.get(rid, position), position)
    scored.sort(key=lambda item: (-item[0], lead.get(item[1], len(lead) + 1), item[1]))
    limit = request.get("limit", DEFAULT_LIMIT)
    parts = []
    for score, rid, matched, count in scored[:limit]:
        row = state.by_id[rid]
        part = {"id": rid, "kind": row["kind"], "status": row["status"], "title": row["title"], "score": score,
                "matched": matched}
        if count > len(matched):
            part["more_places"] = count - len(matched)
        if row.get("superseded_by"):
            part["superseded_by"] = row["superseded_by"]
        parts.append(part)
    sources = [row for row in state.sources.get("sources", [])
               if terms and all(match_strength(term, text_blob(row)) for term in terms)]
    parts.extend({"source": row, "note": SOURCE_NOTE} for row in sources[:5])
    evidence = []
    if "topic" not in request and not request.get("kinds"):
        for source_id, row in store.prepared_rows(state):
            text = " ".join(str(value) for key, value in row.items() if isinstance(value, str) and key != "type")
            blob = text.casefold()
            found = {term: strength for term in terms if (strength := match_strength(term, blob))}
            if found:
                evidence.append((match_score(sum(found.values()), len(terms), sum(found.values()) * 2), source_id, row, text))
        evidence.sort(key=lambda item: (-item[0], item[1], str(item[2].get("locator"))))
        parts.extend({"evidence": source_id, "locator": row.get("locator"), "score": score,
                      "snippet": snippet(text, terms)} for score, source_id, row, text in evidence[:limit])
    meta: dict[str, Any] = {"mode": "search", "query": query, "scope": scope, "matches": len(scored)}
    if evidence:
        meta["evidence_matches"] = len(evidence)
    if "topic" in request:
        meta["topic"] = request["topic"]
    if len(scored) > limit:
        meta["omitted_matches"] = len(scored) - limit
    if not scored:
        meta["searched"] = "current fields and log entries" + (" and history" if scope == "all" else "") + \
            (f" of topic {request['topic']}" if "topic" in request else " of all records, and prepared evidence")
    return meta, parts
