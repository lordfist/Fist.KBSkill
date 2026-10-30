#!/usr/bin/env python3
"""Prepare immutable experiment evidence into bounded, searchable JSONL.

The preparer performs static, read-only parsing.  It never executes source
code, Office macros, PDF actions, HTML scripts, formulas, or external links.
Generated material is written only below the exact KnowledgeBase root and is
bound to the source's registered SHA-256 before and after parsing.
"""

from __future__ import annotations

import argparse
import ast
import collections
import csv
import datetime as dt
import hashlib
import html.parser
import io
import json
import logging
import math
import os
import re
import statistics
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from xml.etree import ElementTree as ET


from . import kb_store as store  # v08 state, validation and commit protocol; configured root.


KB_ROOT = store.KB_ROOT
EXPERIMENT_ROOT = store.BINDING_ROOT
PREPARED_ROOT = KB_ROOT / "prepared"

PREPARER_VERSION = 3
MAX_ARCHIVE_ENTRIES = 20_000
MAX_MEMBER_SIZE = 128 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED = 768 * 1024 * 1024
MAX_COMPRESSION_RATIO = 2_000
TARGET_CHARS = 1_200
HARD_CHARS = 1_800
# Leave room for the reader's source metadata and continuation envelope.
MAX_PREPARED_ITEM_TOKENS = 4_000
MAX_FRAGMENT_TOKENS = 2_500
MAX_TEXT_FILE_BYTES = 32 * 1024 * 1024
MAX_ODS_ROW_CELLS = 100_000
MAX_ODS_MATERIAL_CELLS = 1_000_000

CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
IPV4_RE = re.compile(r"(?<![\w.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\w.])")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5]?[0-9a-f]{3}-[89ab]?[0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
LONG_SECRET_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{40,}(?![0-9A-Za-z])")
ASSIGN_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?key|secret|token|session[_-]?id)\b"
    r"(\s*[:=]\s*)([\"']?)([^\s,;\"']{6,})([\"']?)"
)
URL_QUERY_RE = re.compile(r"([?&](?:token|key|secret|password|session)\s*=)[^&#\s]+", re.I)
SENSITIVE_COLUMN_RE = re.compile(
    r"(?:^|_)(?:id|guid|uuid|cookie|clientkey|device|user|panel|fingerprint|hash|email|ip|token|secret|session)(?:_|$)",
    re.I,
)


class PrepareError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def emit(value: Any, *, compact: bool = False) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=None if compact else 2,
                                separators=(",", ":") if compact else None) + "\n")


def clean_text(value: str) -> str:
    value = CONTROL_CHAR_RE.sub(" ", value)
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def redact_text(value: str, counts: collections.Counter[str]) -> str:
    def replace(pattern: re.Pattern[str], replacement: str, name: str, text: str) -> str:
        def repl(_match: re.Match[str]) -> str:
            counts[name] += 1
            return replacement

        return pattern.sub(repl, text)

    value = replace(EMAIL_RE, "[REDACTED_EMAIL]", "email", value)
    value = replace(IPV4_RE, "[REDACTED_IP]", "ip", value)
    value = replace(UUID_RE, "[REDACTED_UUID]", "uuid", value)
    value = replace(LONG_SECRET_RE, "[REDACTED_LONG_VALUE]", "long_value", value)

    def secret_repl(match: re.Match[str]) -> str:
        counts["credential"] += 1
        return f"{match.group(1)}{match.group(2)}[REDACTED_CREDENTIAL]"

    value = ASSIGN_SECRET_RE.sub(secret_repl, value)

    def url_repl(match: re.Match[str]) -> str:
        counts["url_credential"] += 1
        return match.group(1) + "[REDACTED_URL_VALUE]"

    return URL_QUERY_RE.sub(url_repl, value)


def bounded_text(value: str) -> list[str]:
    value = clean_text(value)
    if not value:
        return []
    chunks: list[str] = []
    remaining = value
    while len(remaining) > HARD_CHARS:
        split = remaining.rfind(" ", 0, TARGET_CHARS)
        if split < TARGET_CHARS // 2:
            split = TARGET_CHARS
        chunks.append(remaining[:split].strip())
        remaining = remaining[split:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def archive_preflight(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise PrepareError(f"archive has too many entries: {len(infos)}")
            total = 0
            normalized: set[str] = set()
            for info in infos:
                name = info.filename.replace("\\", "/")
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise PrepareError(f"unsafe archive member path: {name!r}")
                folded = str(pure).casefold()
                if folded in normalized:
                    raise PrepareError(f"duplicate normalized archive member: {name!r}")
                normalized.add(folded)
                if info.flag_bits & 0x1:
                    raise PrepareError(f"encrypted archive member is unsupported: {name!r}")
                if info.file_size > MAX_MEMBER_SIZE:
                    raise PrepareError(f"archive member is too large: {name!r}")
                total += info.file_size
                if total > MAX_ARCHIVE_UNCOMPRESSED:
                    raise PrepareError("archive expands beyond the configured safety ceiling")
                if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                    raise PrepareError(f"suspicious compression ratio: {name!r}")
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if unix_mode and (unix_mode & 0o170000) not in {0, 0o100000, 0o040000}:
                    raise PrepareError(f"archive contains a link or special member: {name!r}")
            return {
                "entry_count": len(infos),
                "uncompressed_bytes": total,
                "compressed_bytes": path.stat().st_size,
            }
    except zipfile.BadZipFile as exc:
        raise PrepareError(f"invalid ZIP container: {path}") from exc


def safe_xml_bytes(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        payload = archive.read(name)
    except KeyError as exc:
        raise PrepareError(f"required XML member is missing: {name}") from exc
    upper = payload[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise PrepareError(f"DTD/entity declarations are unsupported in {name}")
    return payload


def parse_xml(payload: bytes, label: str) -> ET.Element:
    if b"<!DOCTYPE" in payload[:4096].upper() or b"<!ENTITY" in payload[:4096].upper():
        raise PrepareError(f"DTD/entity declarations are unsupported in {label}")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise PrepareError(f"malformed XML in {label}: {exc}") from exc


def add_text_records(
    output: list[dict[str, Any]],
    record_type: str,
    locator_prefix: str,
    indexed_values: list[tuple[int, str, str | None]],
    redactions: collections.Counter[str],
) -> None:
    pending: list[str] = []
    start_index: int | None = None
    end_index: int | None = None
    heading: str | None = None

    def flush() -> None:
        nonlocal pending, start_index, end_index, heading
        if not pending or start_index is None or end_index is None:
            pending = []
            return
        joined = redact_text("\n".join(pending), redactions)
        for part_index, part in enumerate(bounded_text(joined), 1):
            row: dict[str, Any] = {
                "type": record_type,
                "locator": f"{locator_prefix}:{start_index}-{end_index}/chunk:{part_index}",
                "text": part,
            }
            if heading:
                row["heading"] = heading
            output.append(row)
        pending = []
        start_index = None
        end_index = None

    for index, text, row_heading in indexed_values:
        text = clean_text(text)
        if not text:
            continue
        projected = len("\n".join(pending)) + len(text) + 1
        if pending and (row_heading != heading or projected > TARGET_CHARS):
            flush()
        if start_index is None:
            start_index = index
            heading = row_heading
        end_index = index
        pending.append(text)
    flush()


def qname(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def word_text(element: ET.Element) -> str:
    pieces: list[str] = []
    for node in element.iter():
        if node.tag == qname(W_NS, "t") and node.text:
            pieces.append(node.text)
        elif node.tag == qname(W_NS, "tab"):
            pieces.append("\t")
        elif node.tag in {qname(W_NS, "br"), qname(W_NS, "cr")}:
            pieces.append("\n")
    return clean_text("".join(pieces))


def word_paragraph_style(element: ET.Element) -> str | None:
    ppr = element.find(qname(W_NS, "pPr"))
    if ppr is None:
        return None
    style = ppr.find(qname(W_NS, "pStyle"))
    if style is None:
        return None
    return style.attrib.get(qname(W_NS, "val"))


def prepare_docx(path: Path, redactions: collections.Counter[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    archive_info = archive_preflight(path)
    rows: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {
        "text": "document body plus available headers, footers, footnotes and endnotes",
        "tables": "structural cell text",
        "visuals": "inventory and embedded alternative text only; no rendering or OCR",
        "macros": "not executed",
        "external_relationships": "inventoried only; not followed",
    }
    with zipfile.ZipFile(path, "r") as archive:
        names = {info.filename for info in archive.infolist()}
        root = parse_xml(safe_xml_bytes(archive, "word/document.xml"), "word/document.xml")
        body = root.find(qname(W_NS, "body"))
        paragraph_index = 0
        table_index = 0
        pending_paragraphs: list[tuple[int, str, str | None]] = []
        current_heading: str | None = None
        if body is not None:
            for child in list(body):
                if child.tag == qname(W_NS, "p"):
                    paragraph_index += 1
                    text = word_text(child)
                    style = word_paragraph_style(child)
                    if style and "heading" in style.casefold() and text:
                        current_heading = text
                    pending_paragraphs.append((paragraph_index, text, current_heading))
                    for drawing_index, docpr in enumerate(child.iter(qname(WP_NS, "docPr")), 1):
                        alt = docpr.attrib.get("descr") or docpr.attrib.get("title")
                        rows.append(
                            {
                                "type": "visual_reference",
                                "locator": f"paragraph:{paragraph_index}/visual:{drawing_index}",
                                "data": {
                                    "name": redact_text(docpr.attrib.get("name", ""), redactions),
                                    "alternative_text": redact_text(alt or "", redactions),
                                    "inspection": "not rendered by preparer",
                                },
                            }
                        )
                elif child.tag == qname(W_NS, "tbl"):
                    add_text_records(rows, "paragraph", "paragraph", pending_paragraphs, redactions)
                    pending_paragraphs = []
                    table_index += 1
                    for row_index, tr in enumerate(child.findall(qname(W_NS, "tr")), 1):
                        cells = [
                            redact_text(word_text(cell), redactions)
                            for cell in tr.findall(qname(W_NS, "tc"))
                        ]
                        rows.append(
                            {
                                "type": "table_row",
                                "locator": f"table:{table_index}/row:{row_index}",
                                "data": {"cells": cells, "heading": current_heading},
                            }
                        )
            add_text_records(rows, "paragraph", "paragraph", pending_paragraphs, redactions)

        for prefix, kind in (
            ("word/header", "header"),
            ("word/footer", "footer"),
            ("word/footnotes", "footnote"),
            ("word/endnotes", "endnote"),
        ):
            for name in sorted(item for item in names if item.startswith(prefix) and item.endswith(".xml")):
                part = parse_xml(safe_xml_bytes(archive, name), name)
                values: list[tuple[int, str, str | None]] = []
                for index, paragraph in enumerate(part.iter(qname(W_NS, "p")), 1):
                    values.append((index, word_text(paragraph), None))
                add_text_records(rows, kind, name, values, redactions)

        media = sorted(name for name in names if name.startswith("word/media/") and not name.endswith("/"))
        if media:
            rows.append(
                {
                    "type": "media_inventory",
                    "locator": "package:word/media",
                    "data": {
                        "count": len(media),
                        "members": media,
                        "coverage": "bytes left inside original source; no extraction or rendering",
                    },
                }
            )

        external: list[dict[str, str]] = []
        for name in sorted(item for item in names if item.endswith(".rels")):
            rel_root = parse_xml(safe_xml_bytes(archive, name), name)
            for rel in rel_root.iter(qname(PKG_REL_NS, "Relationship")):
                if rel.attrib.get("TargetMode") == "External":
                    external.append(
                        {
                            "part": name,
                            "type": rel.attrib.get("Type", ""),
                            "target": redact_text(rel.attrib.get("Target", ""), redactions),
                        }
                    )
        if external:
            rows.append(
                {
                    "type": "external_relationship_inventory",
                    "locator": "package:relationships",
                    "data": external,
                }
            )

        if "docProps/core.xml" in names:
            props = parse_xml(safe_xml_bytes(archive, "docProps/core.xml"), "docProps/core.xml")
            metadata = {
                child.tag.rsplit("}", 1)[-1]: redact_text(clean_text(child.text or ""), redactions)
                for child in list(props)
                if child.text
            }
            rows.insert(0, {"type": "document_metadata", "locator": "package:core-properties", "data": metadata})

    coverage["archive"] = archive_info
    coverage["body_paragraphs"] = paragraph_index
    coverage["body_tables"] = table_index
    return rows, coverage


def prepare_pdf(path: Path, redactions: collections.Counter[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PrepareError("pypdf is unavailable; PDF preparation cannot proceed") from exc
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:
        raise PrepareError(f"PDF reader could not open {path.name}: {type(exc).__name__}") from exc
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = 0
        if not unlocked:
            raise PrepareError("encrypted PDF cannot be read without a password")
    rows: list[dict[str, Any]] = []
    metadata = {}
    if reader.metadata:
        metadata = {
            str(key): redact_text(clean_text(str(value)), redactions)
            for key, value in reader.metadata.items()
            if value is not None
        }
    if metadata:
        rows.append({"type": "document_metadata", "locator": "pdf:metadata", "data": metadata})
    empty_pages: list[int] = []
    for page_number, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = redact_text(text, redactions)
        parts = bounded_text(text)
        if not parts:
            empty_pages.append(page_number)
        for part_index, part in enumerate(parts, 1):
            rows.append(
                {
                    "type": "page_text",
                    "locator": f"page:{page_number}/chunk:{part_index}",
                    "text": part,
                }
            )
    coverage = {
        "pages": len(reader.pages),
        "text": "pypdf page-text extraction; reading order and tables may be imperfect",
        "textless_pages": empty_pages,
        "visuals": "not rendered or inspected by preparer",
        "ocr": "not performed",
        "attachments_and_actions": "not executed or extracted",
    }
    return rows, coverage


class StaticHTMLCollector(html.parser.HTMLParser):
    BLOCKS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "td", "th", "caption",
              "div", "section", "article", "main", "header", "footer", "aside", "nav", "body",
              "ul", "ol", "table", "tr", "blockquote", "dl", "dt", "dd", "figure", "figcaption"}
    HIDDEN = {"head", "script", "style", "noscript", "template"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.current_tag: str | None = None
        self.current: list[str] = []
        self.stack: list[tuple[str, bool]] = []
        self.references: list[dict[str, str]] = []

    def flush(self) -> None:
        if self.current_tag and self.current:
            text = clean_text("".join(self.current))
            if text:
                self.blocks.append((self.current_tag, text))
        self.current_tag = None
        self.current = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attr = {key.casefold(): value or "" for key, value in attrs}
        if tag in {"a", "img", "iframe", "link", "script"}:
            key = "href" if "href" in attr else "src" if "src" in attr else None
            if key and attr[key]:
                target = attr[key]
                if target.startswith("data:"):
                    target = "[DATA_URI_OMITTED]"
                self.references.append({"tag": tag, "attribute": key, "target": target})
        hidden = ((bool(self.stack) and self.stack[-1][1]) or tag in self.HIDDEN
                  or "hidden" in attr or bool(re.search(
                      r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)",
                      attr.get("style", ""), re.I)))
        if tag in self.BLOCKS and not hidden:
            self.flush()
        if tag not in self.VOID:
            self.stack.append((tag, hidden))
        elif tag in {"br", "hr"} and not hidden:
            self.handle_data("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if any(item in self.BLOCKS and not hidden for item, hidden in self.stack[index:]):
                    self.flush()
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1][1]:
            return
        if self.current_tag is None:
            self.current_tag = next((tag for tag, _ in reversed(self.stack) if tag in self.BLOCKS), "text")
        self.current.append(data)


def decode_text_file(path: Path) -> tuple[str, str]:
    size = path.stat().st_size
    if size > MAX_TEXT_FILE_BYTES:
        raise PrepareError(f"text file exceeds {MAX_TEXT_FILE_BYTES} byte safety ceiling")
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return payload.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise PrepareError(f"unsupported text encoding: {path.name}")


def prepare_html(path: Path, redactions: collections.Counter[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text, encoding = decode_text_file(path)
    collector = StaticHTMLCollector()
    try:
        collector.feed(text)
        collector.close()
        collector.flush()
    except Exception as exc:
        raise PrepareError(f"HTML parsing failed: {type(exc).__name__}") from exc
    rows: list[dict[str, Any]] = []
    current_heading: str | None = None
    for index, (tag, value) in enumerate(collector.blocks, 1):
        value = redact_text(value, redactions)
        if tag.startswith("h") and len(tag) == 2:
            current_heading = clean_text(value)
        for part_index, part in enumerate(bounded_text(value), 1):
            row: dict[str, Any] = {
                "type": "html_block",
                "locator": f"html:block:{index}/chunk:{part_index}",
                "text": part,
                "html_tag": tag,
            }
            if current_heading:
                row["heading"] = current_heading
            rows.append(row)
    if collector.references:
        rows.append(
            {
                "type": "reference_inventory",
                "locator": "html:references",
                "data": [
                    {**item, "target": redact_text(item["target"], redactions)}
                    for item in collector.references
                ],
            }
        )
    return rows, {
        "encoding": encoding,
        "visible_and_inline_code_blocks": len(collector.blocks),
        "references": len(collector.references),
        "scripts": "script/style/template/noscript/head content omitted; no execution or fetching",
        "visibility": "static text in document order; hidden attributes and inline display:none/visibility:hidden omitted; external CSS and rendering not evaluated",
        "external_resources": "not fetched",
    }


def infer_delimiter(sample: str) -> str:
    # Parse complete records: cutting inside a quoted field changes its meaning.
    # Ragged data rows remain supported; widths cannot resolve two plausible headers.
    viable: list[str] = []
    for delimiter in (",", ";", "\t", "|"):
        reader = csv.reader(io.StringIO(sample, newline=""), delimiter=delimiter, strict=True)
        try:
            header = next(reader, [])
            if len(header) < 2:
                continue
            for index, _row in enumerate(reader):
                if index >= 99:
                    break
        except csv.Error:
            continue
        viable.append(delimiter)
    if not viable:
        raise PrepareError("CSV delimiter could not be inferred")
    if len(viable) > 1:
        raise PrepareError("CSV delimiter is ambiguous among " + repr(viable))
    return viable[0]


def numeric(value: str) -> float | None:
    candidate = value.strip().replace("%", "")
    if not candidate:
        return None
    try:
        number = float(candidate)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def summarize_table_rows(
    headers: list[str],
    rows: Iterable[list[str]],
    redactions: collections.Counter[str],
) -> tuple[dict[str, Any], int]:
    column_count = len(headers)
    missing = [0] * column_count
    numeric_values: list[list[float]] = [[] for _ in range(column_count)]
    nonempty = [0] * column_count
    row_count = 0
    for row in rows:
        row_count += 1
        for index in range(column_count):
            value = row[index] if index < len(row) else ""
            if not value.strip():
                missing[index] += 1
                continue
            nonempty[index] += 1
            number = numeric(value)
            if number is not None:
                numeric_values[index].append(number)
            # Count redactions without persisting the raw value.
            redact_text(value, redactions)
    columns: list[dict[str, Any]] = []
    for index, header in enumerate(headers):
        values = numeric_values[index]
        column: dict[str, Any] = {
            "name": clean_text(header) or f"column_{index + 1}",
            "sensitive_name": bool(SENSITIVE_COLUMN_RE.search(clean_text(header))),
            "nonempty": nonempty[index],
            "missing": missing[index],
            "numeric_count": len(values),
        }
        if values and not column["sensitive_name"]:
            column["numeric_summary"] = {
                "min": min(values),
                "max": max(values),
                "sum": math.fsum(values),
                "mean": statistics.fmean(values),
            }
        columns.append(column)
    return {"columns": columns}, row_count


def prepare_csv(path: Path, redactions: collections.Counter[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text, encoding = decode_text_file(path)
    delimiter = infer_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        headers = next(reader)
    except StopIteration:
        headers = []
    try:
        summary, count = summarize_table_rows(headers, reader, redactions)
    except csv.Error as exc:
        raise PrepareError(f"CSV record parsing failed near physical line {reader.line_num}") from exc
    summary.update({"delimiter": delimiter, "headers": [clean_text(value) for value in headers], "row_count": count})
    return [
        {
            "type": "table_summary",
            "locator": "csv:table",
            "data": summary,
        }
    ], {
        "encoding": encoding,
        "rows": count,
        "columns": len(headers),
        "raw_rows": "not retained",
        "formulas": "not evaluated",
    }


ODS_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
ODS_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
ODS_OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def ods_cell_text(cell: ET.Element) -> str:
    parts: list[str] = []
    for paragraph in cell.iter(qname(ODS_TEXT_NS, "p")):
        parts.append("".join(paragraph.itertext()))
    return clean_text(" ".join(parts))


def prepare_ods(path: Path, redactions: collections.Counter[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    archive_info = archive_preflight(path)
    with zipfile.ZipFile(path, "r") as archive:
        root = parse_xml(safe_xml_bytes(archive, "content.xml"), "content.xml")
    rows: list[dict[str, Any]] = []
    sheet_count = 0
    total_logical_rows = 0
    material_cells = 0
    for sheet in root.iter(qname(ODS_TABLE_NS, "table")):
        sheet_count += 1
        name = sheet.attrib.get(qname(ODS_TABLE_NS, "name"), f"Sheet{sheet_count}")
        hidden = sheet.attrib.get(qname(ODS_TABLE_NS, "display")) == "false"
        material_rows: list[list[str]] = []
        logical_rows = 0
        formula_count = 0
        for row in sheet.findall(qname(ODS_TABLE_NS, "table-row")):
            row_repeat = int(row.attrib.get(qname(ODS_TABLE_NS, "number-rows-repeated"), "1"))
            # ODS producers commonly encode the unused tail of a spreadsheet as
            # one repeated blank row. Count it arithmetically; never expand it.
            if row_repeat < 1:
                raise PrepareError("ODS row repetition must be positive")
            if row_repeat > 10_000_000:
                raise PrepareError(f"ODS row repetition is excessive in sheet {name!r}")
            logical_rows += row_repeat
            values: list[str] = []
            pending_blanks = 0
            for cell in list(row):
                if cell.tag not in {qname(ODS_TABLE_NS, "table-cell"), qname(ODS_TABLE_NS, "covered-table-cell")}:
                    continue
                col_repeat = int(cell.attrib.get(qname(ODS_TABLE_NS, "number-columns-repeated"), "1"))
                if col_repeat < 1:
                    raise PrepareError("ODS column repetition must be positive")
                if col_repeat > 100_000:
                    raise PrepareError(f"ODS column repetition is excessive in sheet {name!r}")
                value = ods_cell_text(cell)
                if qname(ODS_TABLE_NS, "formula") in cell.attrib:
                    formula_count += col_repeat
                if value:
                    extent = len(values) + pending_blanks + col_repeat
                    if extent > MAX_ODS_ROW_CELLS or material_cells + extent > MAX_ODS_MATERIAL_CELLS:
                        raise PrepareError(
                            "ODS material cell limit exceeded: "
                            f"row limit {MAX_ODS_ROW_CELLS}, document limit {MAX_ODS_MATERIAL_CELLS}"
                        )
                    values.extend([""] * pending_blanks)
                    pending_blanks = 0
                    values.extend([value] * col_repeat)
                else:
                    # Empty prefixes/interior cells still occupy columns. Trailing
                    # unused spreadsheet cells need no allocation.
                    pending_blanks += col_repeat
            while values and not values[-1]:
                values.pop()
            if values:
                material_rows.append(values)
                material_cells += len(values)
            if len(material_rows) > 100_000:
                raise PrepareError(f"ODS sheet has too many material rows: {name!r}")
        total_logical_rows += logical_rows
        headers = material_rows[0] if material_rows else []
        data_rows = material_rows[1:] if material_rows else []
        summary, material_count = summarize_table_rows(headers, data_rows, redactions)
        summary.update(
            {
                "sheet": redact_text(name, redactions),
                "hidden": hidden,
                "headers": [clean_text(value) for value in headers],
                "logical_rows_including_repetition": logical_rows,
                "material_data_rows": material_count,
                "formula_cell_count": formula_count,
            }
        )
        rows.append(
            {
                "type": "sheet_summary",
                "locator": f"ods:sheet:{sheet_count}",
                "data": summary,
            }
        )
    return rows, {
        "archive": archive_info,
        "sheets": sheet_count,
        "logical_rows": total_logical_rows,
        "raw_rows": "not retained",
        "formulas": "counted as inert text; not evaluated",
        "external_data": "not refreshed or followed",
    }


def prepare_python(path: Path, redactions: collections.Counter[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text, encoding = decode_text_file(path)
    rows: list[dict[str, Any]] = []
    syntax_status = "valid"
    try:
        tree = ast.parse(text, filename=path.name)
    except SyntaxError as exc:
        syntax_status = f"syntax_error_at_line_{exc.lineno}"
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                rows.append(
                    {
                        "type": "code_symbol",
                        "locator": f"line:{node.lineno}-{getattr(node, 'end_lineno', node.lineno)}",
                        "data": {
                            "kind": kind,
                            "name": node.name,
                            "docstring": redact_text(ast.get_docstring(node) or "", redactions),
                        },
                    }
                )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names]
                rows.append(
                    {
                        "type": "import_reference",
                        "locator": f"line:{node.lineno}",
                        "data": {
                            "module": getattr(node, "module", None),
                            "names": names,
                            "followed": False,
                        },
                    }
                )
    lines = text.splitlines()
    start = 1
    while start <= len(lines):
        end = start
        content: list[str] = []
        while end <= len(lines) and len("\n".join(content)) < TARGET_CHARS and end - start < 80:
            content.append(lines[end - 1])
            end += 1
        value = redact_text("\n".join(content), redactions)
        for part_index, part in enumerate(bounded_text(value), 1):
            rows.append(
                {
                    "type": "code_chunk",
                    "locator": f"line:{start}-{end - 1}/chunk:{part_index}",
                    "text": part,
                }
            )
        start = end
    return rows, {
        "encoding": encoding,
        "language": "python",
        "syntax": syntax_status,
        "execution": "not executed or imported",
        "imports": "inventoried only; not followed",
    }


def prepare_text_or_code(path: Path, redactions: collections.Counter[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text, encoding = decode_text_file(path)
    text = redact_text(text, redactions)
    lines = text.splitlines()
    rows: list[dict[str, Any]] = []
    start = 1
    while start <= len(lines):
        end = start
        content: list[str] = []
        while end <= len(lines) and len("\n".join(content)) < TARGET_CHARS and end - start < 100:
            content.append(lines[end - 1])
            end += 1
        for part_index, part in enumerate(bounded_text("\n".join(content)), 1):
            rows.append(
                {
                    "type": "text_chunk",
                    "locator": f"line:{start}-{end - 1}/chunk:{part_index}",
                    "text": part,
                }
            )
        start = end
    return rows, {
        "encoding": encoding,
        "text": "static bounded line chunks",
        "execution": "not executed",
        "external_references": "not followed",
    }


def parser_for(path: Path):
    suffix = path.suffix.casefold()
    if suffix == ".docx":
        return "docx-zip-xml", prepare_docx
    if suffix == ".pdf":
        return "pdf-pypdf", prepare_pdf
    if suffix in {".html", ".htm"}:
        return "html-static", prepare_html
    if suffix == ".csv":
        return "csv-safe-aggregate", prepare_csv
    if suffix == ".ods":
        return "ods-zip-xml-safe-aggregate", prepare_ods
    if suffix == ".py":
        return "python-ast-static", prepare_python
    if suffix in {".txt", ".md", ".rst", ".json", ".xml", ".js", ".ts", ".cs", ".sln", ".csproj", ".scala", ".sql", ".yaml", ".yml"}:
        return "static-text", prepare_text_or_code
    raise PrepareError(f"unsupported preparation format: {suffix or '[no extension]'}")


def effective_preparation_level(source: dict[str, Any], path: Path) -> str:
    existing = source.get("preparation")
    prior = existing.get("level") if isinstance(existing, dict) else None
    if prior not in {None, "registered_only", "external_reference"}:
        return str(prior)
    suffix = path.suffix.casefold()
    if suffix in {".csv", ".ods"}:
        return "structured_tables"
    if suffix == ".docx":
        return "complete_relevant_extraction"
    return "searchable_text"


def validate_source_path(path: Path) -> Path:
    if not path.is_absolute():
        raise PrepareError("source path must be absolute")
    raw_without_drive = str(path)[len(path.drive) :]
    if ":" in raw_without_drive:
        raise PrepareError("NTFS alternate-data-stream syntax is not allowed")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PrepareError(f"source cannot be resolved: {path}") from exc
    if not resolved.is_file():
        raise PrepareError(f"source is not a regular file: {path}")
    if not store.within(resolved, EXPERIMENT_ROOT) or store.within(resolved, KB_ROOT):
        raise PrepareError(f"source must be inside the experiment root and outside KnowledgeBase: {path}")
    return resolved


def inspect_path(path: Path) -> dict[str, Any]:
    resolved = validate_source_path(path)
    result: dict[str, Any] = {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": store.sha256_file(resolved),
        "suffix": resolved.suffix.casefold(),
    }
    if resolved.suffix.casefold() in {".docx", ".ods"}:
        result["container"] = archive_preflight(resolved)
    if resolved.suffix.casefold() == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(resolved), strict=False)
            result["pdf"] = {"pages": len(reader.pages), "encrypted": bool(reader.is_encrypted)}
        except Exception as exc:
            result["pdf"] = {"readable": False, "error_type": type(exc).__name__}
    return result


def bounded_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep small rows intact; split large JSON structures without losing values.

    Continuations identify the original row and ordered JSON-pointer fragments.
    Container fragments declare their type/count; value fragments retain whole
    subtrees. Long strings carry exact character offsets, including whitespace.
    The original row can therefore be reconstructed without guessing omitted
    keys, list positions, text separators or nested context.
    """
    if store.estimate_tokens(row) <= MAX_PREPARED_ITEM_TOKENS and (
        "text" not in row or len(str(row["text"])) <= HARD_CHARS
    ):
        return [row]
    locator = str(row.get("locator", ""))
    fragments: list[dict[str, Any]] = []

    def visit(value: Any, pointer: str) -> None:
        fragment = {"pointer": pointer, "value": value}
        if store.estimate_tokens(fragment) <= MAX_FRAGMENT_TOKENS and not (
            isinstance(value, str) and len(value) > HARD_CHARS
        ):
            fragments.append(fragment)
        elif isinstance(value, dict):
            fragments.append({"pointer": pointer, "container": "object", "count": len(value)})
            for key, item in value.items():
                visit(item, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(value, list):
            fragments.append({"pointer": pointer, "container": "array", "count": len(value)})
            pending: list[Any] = []
            start = 0

            def flush_items() -> None:
                if pending:
                    fragments.append({"pointer": pointer, "items": list(pending), "start": start,
                                      "end": start + len(pending), "total_items": len(value)})
                    pending.clear()

            for index, item in enumerate(value):
                candidate = {"pointer": pointer, "items": [*pending, item],
                             "start": start, "end": index + 1, "total_items": len(value)}
                if store.estimate_tokens(candidate) > MAX_FRAGMENT_TOKENS:
                    flush_items()
                    start = index
                    candidate["items"] = [item]
                    candidate["start"] = start
                if store.estimate_tokens(candidate) > MAX_FRAGMENT_TOKENS:
                    visit(item, pointer + "/" + str(index))
                    start = index + 1
                else:
                    pending.append(item)
            flush_items()
        elif isinstance(value, str):
            for start in range(0, len(value), TARGET_CHARS):
                text = value[start:start + TARGET_CHARS]
                fragments.append({"pointer": pointer, "text": text, "start": start,
                                  "end": start + len(text), "total_chars": len(value)})
        else:
            raise PrepareError(f"unrepresentable oversized value at {locator}{pointer}")

    visit(row, "")
    total = len(fragments)
    output = []
    for index, fragment in enumerate(fragments, 1):
        part = {"type": "structured_fragment", "locator": f"{locator}/part:{index}",
                "original_type": row.get("type"),
                "continuation": {"format": "json-pointer-v1", "source_locator": locator,
                                 "part": index, "total_parts": total},
                "fragment": fragment}
        if store.estimate_tokens(part) > MAX_PREPARED_ITEM_TOKENS:
            raise PrepareError(f"prepared locator or structure exceeds bounded item limit at {locator}")
        output.append(part)
    return output


def duplicates(values: Iterable[str]) -> list[str]:
    seen, repeated = set(), []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def make_prepared(
    source: dict[str, Any], path: Path, preparation_version: int
) -> tuple[list[dict[str, Any]], bytes, dict[str, Any]]:
    expected = str(source.get("sha256", "")).upper()
    before = store.sha256_file(path)
    if before != expected:
        raise PrepareError(
            f"registered source hash mismatch for {source['source_id']}: expected {expected}, actual {before}"
        )
    redactions: collections.Counter[str] = collections.Counter()
    parser_name, parser = parser_for(path)
    content_rows, coverage = parser(path, redactions)
    after = store.sha256_file(path)
    if after != before:
        raise PrepareError(f"source changed during preparation: {source['source_id']}")
    summary = redact_text(str(source.get("summary", "")), redactions)
    meta = {
        "type": "meta",
        "source_id": source["source_id"],
        "source_sha256": before,
        "preparation_version": preparation_version,
        "preparer_version": PREPARER_VERSION,
        "parser": parser_name,
        "generated_at": utc_now(),
        "coverage": coverage,
        "redactions": dict(sorted(redactions.items())),
    }
    if store.estimate_tokens(meta) > MAX_PREPARED_ITEM_TOKENS:
        raise PrepareError("preparation metadata exceeds bounded item limit; narrow the input before preparation")
    rows: list[dict[str, Any]] = [meta]
    if summary:
        rows.extend(bounded_rows({"type": "summary", "locator": "catalog:summary", "text": summary}))
    for content in content_rows:
        rows.extend(bounded_rows(content))
    repeated = duplicates(str(row.get("locator")) for row in rows[1:] if row.get("locator"))
    if repeated:
        raise PrepareError(f"duplicate prepared locators: {repeated[:3]}")
    for row in rows:
        if store.estimate_tokens(row) > MAX_PREPARED_ITEM_TOKENS:
            raise PrepareError(f"prepared item exceeds bounded limit at {row.get('locator')}")
    payload = store.jsonl_bytes(rows)
    result = {"source_id": source["source_id"], "path": str(path), "source_sha256": before, "parser": parser_name,
              "records": len(rows), "coverage": coverage, "redactions": dict(sorted(redactions.items()))}
    return rows, payload, result


def command_inspect(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, "inspection": inspect_path(Path(args.path))}


def prepare(source_ids: list[str], actor: str, upgrade: bool = False) -> dict[str, Any]:
    """Prepare registered file sources as one revision. Prepared files are published first, then the catalog and
    core through the v08 commit (core last); a failure after a prepared file is replaced reports uncertain."""
    progress = {"replaced": [], "committed": False, "verified": False, "cleanup_errors": []}
    try:
        return _prepare(source_ids, actor, upgrade, progress)
    except (store.KBError, PrepareError, OSError) as exc:
        outcome, stage = store.outcome_for(progress)
        error = exc.payload() if isinstance(exc, store.KBError) else {"code": "prepare_failed", "message": str(exc)}
        result = {"ok": False, "outcome": outcome, "error": error}
        if outcome != "not_written":
            result["detail"] = {"write_outcome": store.write_outcome(progress, stage)}
        return result


def _prepare(source_ids: list[str], actor: str, upgrade: bool, progress: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(actor, str) or not actor.strip():
        raise PrepareError("prepare requires an explicit nonempty --actor")
    tools = store.tool_status()
    if tools["status"] != "ok":
        raise store.KBError("tools_unverified", f"tool identity is {tools['status']}", tools.get("hint", ""))
    state = store.load_state()
    store.require_valid(state)
    if len(set(source_ids)) != len(source_ids):
        raise PrepareError("source id list contains duplicates")
    sources = json.loads(json.dumps(state.sources))
    lookup = {row["source_id"]: row for row in sources.get("sources", [])}
    missing = [source_id for source_id in source_ids if source_id not in lookup]
    if missing:
        raise PrepareError(f"unknown source ids: {', '.join(missing)}")
    candidates: dict[Path, bytes] = {}
    results, noops = [], []
    for source_id in source_ids:
        source = lookup[source_id]
        if source.get("supersession") is not None:
            raise PrepareError(f"historical source {source_id} cannot be prepared; source lineage is not ported to v08")
        locator = source.get("locator", {})
        if locator.get("type") != "file":
            raise PrepareError(f"source is not a local file: {source_id}")
        path = validate_source_path(Path(locator["path"]))
        existing = source.get("preparation")
        if isinstance(existing, dict) and existing.get("status") in {"prepared", "partial"} and not upgrade:
            verify_one(source)
            noops.append(source_id)
            continue
        version = int(existing.get("version", 0)) + 1 if isinstance(existing, dict) else 1
        _rows, payload, result = make_prepared(source, path, version)
        destination = (PREPARED_ROOT / f"{source_id}.jsonl").resolve()
        if not store.within(destination, KB_ROOT):
            raise PrepareError(f"prepared destination escapes KnowledgeBase: {destination}")
        payload_sha = store.sha256_bytes(payload)
        if destination.exists() and not upgrade and store.sha256_file(destination) != payload_sha:
            raise PrepareError(f"prepared destination already exists with different content: {destination.name}")
        candidates[destination] = payload
        coverage = result["coverage"]
        status = "partial" if any(phrase in json.dumps(coverage, ensure_ascii=False).casefold()
                                  for phrase in ("not rendered", "not performed", "imperfect", "not retained")) else "prepared"
        source["preparation"] = {
            "status": status, "level": effective_preparation_level(source, path),
            "path": destination.relative_to(KB_ROOT).as_posix(), "version": version,
            "preparer_version": PREPARER_VERSION, "source_sha256": result["source_sha256"],
            "prepared_sha256": payload_sha, "prepared_at": utc_now(), "coverage": coverage,
        }
        results.append({**result, "prepared_sha256": payload_sha, "status": status})
    if not results:
        return {"ok": True, "outcome": "not_written", "status": "NO-OP", "revision": state.revision,
                "already_prepared": noops}
    at = store.utc_now()
    revision = state.revision + 1
    journal = [*state.journal, {"type": "write", "rev": revision, "at": at, "by": actor,
                                "note": f"prepared {', '.join(row['source_id'] for row in results)}",
                                "ops": [{"op": "prepare", "id": row["source_id"]} for row in results]}]
    core = {**state.core, "revision": revision,
            "last_write": {"rev": revision, "at": at, "by": actor, "note": f"prepared {len(results)} source(s)"}}
    PREPARED_ROOT.mkdir(exist_ok=True)
    for destination, payload in sorted(candidates.items(), key=lambda item: str(item[0])):
        staged = store.stage_file(destination, payload)
        progress["uncertain"] = True
        os.replace(staged, destination)
        progress["uncertain"] = False
        progress["replaced"].append(destination.relative_to(KB_ROOT).as_posix())
    store.commit(state, core, state.records, journal, sources, progress)
    return {"ok": True, "outcome": "committed", "revision": revision, "prepared": results, "already_prepared": noops}


def verify_one(source: dict[str, Any]) -> dict[str, Any]:
    """Re-hash one file source and its prepared representation and check the binding between them."""
    source_id = source["source_id"]
    path = validate_source_path(Path(source.get("locator", {}).get("path", "")))
    actual_source_sha = store.sha256_file(path)
    expected_source_sha = str(source.get("sha256", "")).upper()
    check = store.prepared_check(source)
    ok = actual_source_sha == expected_source_sha and check["status"] == "ok"
    result = {"source_id": source_id, "ok": ok, "source_sha256": actual_source_sha, "prepared": check}
    if not ok:
        raise PrepareError(f"prepared/source hash chain is stale or inconsistent: {source_id}")
    return result


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    state = store.load_state()
    store.require_valid(state)
    lookup = {row["source_id"]: row for row in state.sources.get("sources", [])}
    if args.source_id not in lookup:
        raise PrepareError(f"unknown source id: {args.source_id}")
    return {"ok": True, "verification": verify_one(lookup[args.source_id])}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare read-only experiment evidence for the KnowledgeBase")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="read-only format and identity inspection")
    inspect.add_argument("path")
    prepare_parser = subparsers.add_parser("prepare", help="prepare registered source ids as one revision")
    prepare_parser.add_argument("source_ids", nargs="+")
    prepare_parser.add_argument("--actor", required=True, help="explicit attribution for this authorized preparation")
    prepare_parser.add_argument("--upgrade", action="store_true", help="replace a prior preparation with a new version")
    verify = subparsers.add_parser("verify", help="rehash one source and its prepared representation")
    verify.add_argument("source_id")
    for sub in (inspect, prepare_parser, verify):
        sub.add_argument("--compact", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "inspect":
            result = command_inspect(args)
        elif args.command == "prepare":
            result = prepare(args.source_ids, args.actor, args.upgrade)
        else:
            result = command_verify(args)
    except (store.KBError, PrepareError, OSError) as exc:
        result = {"ok": False, "error": exc.payload() if isinstance(exc, store.KBError)
                  else {"code": "prepare_failed", "message": str(exc)}}
    emit(result, compact=args.compact)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
