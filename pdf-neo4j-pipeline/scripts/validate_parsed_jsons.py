#!/usr/bin/env python3
"""Validate parsed normtext JSON files and extracted table shapes.

The script started as a small single-table checker. It now keeps that use case
intact, but can also scan a directory of raw extraction JSONs and write a
Markdown/JSON report that highlights likely problem documents for batch runs.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Sequence, Tuple


SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
FAIL_LEVELS = {"none": -1, "error": 0, "warning": 1, "info": 2}
TABLE_LABEL_RE = re.compile(r"^\s*(?:Fortsetzung\s+)?Tabelle\s+\d+[a-z]?\s*(?::|$)", re.IGNORECASE)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "output" / "refactor_4doc" / "raw"


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    file: str
    document_id: Optional[str] = None
    citation: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "file": self.file,
            "document_id": self.document_id,
            "citation": self.citation,
        }


class ValidationContext:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.findings: List[Finding] = []
        self.metrics: Dict[str, Any] = {
            "file": str(path),
            "kind": "unknown",
            "documents": 0,
            "pages": 0,
            "structural_units": 0,
            "chunks": 0,
            "table_chunks": 0,
            "graph_nodes": 0,
            "graph_relationships": 0,
            "extraction_issues": 0,
        }

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        document_id: Optional[str] = None,
        citation: Optional[str] = None,
    ) -> None:
        self.findings.append(
            Finding(
                severity=severity,
                code=code,
                message=message,
                file=str(self.path),
                document_id=document_id,
                citation=citation,
            )
        )


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON value must be an object")
    return payload


def json_files(input_path: Path, recursive: bool = False) -> List[Path]:
    if input_path.is_file():
        return [input_path]
    pattern = "**/*.json" if recursive else "*.json"
    return sorted(path for path in input_path.glob(pattern) if path.is_file())


def as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def table_rows(chunk: Dict[str, Any]) -> List[Any]:
    rows = chunk.get("structured_rows")
    if rows is None:
        rows = chunk.get("rows")
    return rows if isinstance(rows, list) else []


def row_marker(row: Any) -> str:
    if isinstance(row, dict):
        for key in ("Einbauweise Nummer", "Nummer", "Parameter"):
            if key in row:
                return str(row.get(key) or "").strip()
        if row:
            first_key = next(iter(row))
            return str(row.get(first_key) or "").strip()
    if isinstance(row, list) and row:
        return str(row[0] or "").strip()
    if isinstance(row, str):
        return row.split(maxsplit=1)[0] if row.strip() else ""
    return ""


def citation(item: Dict[str, Any]) -> str:
    return (
        item.get("legal_citation")
        or item.get("display_name")
        or item.get("global_key")
        or item.get("chunk_id")
        or item.get("unit_id")
        or "<unknown>"
    )


def page_range_label(item: Dict[str, Any]) -> str:
    page_range = item.get("page_range")
    if not isinstance(page_range, dict):
        return ""
    start = page_range.get("start")
    end = page_range.get("end")
    if start == end:
        return "page {}".format(start)
    return "pages {}-{}".format(start, end)


def validate_id_uniqueness(
    ctx: ValidationContext,
    items: Sequence[Dict[str, Any]],
    id_key: str,
    code_prefix: str,
    document_id: Optional[str],
) -> None:
    ids = [item.get(id_key) for item in items if item.get(id_key)]
    missing = len(items) - len(ids)
    if missing:
        ctx.add(
            "error",
            "{}_MISSING_ID".format(code_prefix),
            "{} item(s) are missing {}".format(missing, id_key),
            document_id=document_id,
        )
    duplicates = [item_id for item_id, count in Counter(ids).items() if count > 1]
    if duplicates:
        ctx.add(
            "error",
            "{}_DUPLICATE_ID".format(code_prefix),
            "duplicate {} values: {}".format(id_key, ", ".join(map(str, duplicates[:10]))),
            document_id=document_id,
        )


def validate_global_keys(
    ctx: ValidationContext,
    items: Sequence[Dict[str, Any]],
    scope: str,
    document_id: Optional[str],
) -> None:
    keys = [item.get("global_key") for item in items if item.get("global_key")]
    missing = len(items) - len(keys)
    if missing:
        ctx.add(
            "warning",
            "{}_MISSING_GLOBAL_KEY".format(scope),
            "{} item(s) are missing global_key".format(missing),
            document_id=document_id,
        )
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    if duplicates:
        ctx.add(
            "warning",
            "{}_DUPLICATE_GLOBAL_KEY".format(scope),
            "duplicate global_key values: {}".format(", ".join(map(str, duplicates[:10]))),
            document_id=document_id,
        )


def validate_page_range(
    ctx: ValidationContext,
    item: Dict[str, Any],
    page_count: int,
    code_prefix: str,
    document_id: Optional[str],
) -> None:
    page_range = item.get("page_range")
    if not isinstance(page_range, dict):
        ctx.add(
            "warning",
            "{}_MISSING_PAGE_RANGE".format(code_prefix),
            "{} has no page_range".format(citation(item)),
            document_id=document_id,
            citation=citation(item),
        )
        return
    start = page_range.get("start")
    end = page_range.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        ctx.add(
            "warning",
            "{}_BAD_PAGE_RANGE".format(code_prefix),
            "{} has non-integer page_range {}".format(citation(item), page_range),
            document_id=document_id,
            citation=citation(item),
        )
        return
    if start > end:
        ctx.add(
            "error",
            "{}_BAD_PAGE_RANGE".format(code_prefix),
            "{} has inverted page_range {}".format(citation(item), page_range),
            document_id=document_id,
            citation=citation(item),
        )
    if page_count and (start < 1 or end > page_count):
        ctx.add(
            "warning",
            "{}_PAGE_RANGE_OUT_OF_BOUNDS".format(code_prefix),
            "{} has page_range {} outside 1-{}".format(citation(item), page_range, page_count),
            document_id=document_id,
            citation=citation(item),
        )


def resolve_page_path(raw_path: Path, page_ref: Dict[str, Any]) -> Optional[Path]:
    rel_path = page_ref.get("path")
    if not rel_path:
        return None
    return (raw_path.parent / rel_path).resolve()


def validate_pages(
    ctx: ValidationContext,
    doc: Dict[str, Any],
    document_id: Optional[str],
    check_page_files: bool,
) -> Tuple[Dict[str, Dict[str, Any]], int]:
    pages = as_list(doc.get("page_refs")) or as_list(doc.get("pages"))
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    expected_page_count = metadata.get("pdf_pages")
    page_count = len(pages)
    if isinstance(expected_page_count, int) and expected_page_count != page_count:
        ctx.add(
            "warning",
            "PAGE_COUNT_MISMATCH",
            "metadata pdf_pages={} but {} page refs exist".format(expected_page_count, page_count),
            document_id=document_id,
        )
    if not pages:
        ctx.add("error", "NO_PAGE_REFS", "document has no page_refs/pages", document_id=document_id)
        return {}, 0

    page_ids = [page.get("page_id") for page in pages if isinstance(page, dict) and page.get("page_id")]
    duplicate_page_ids = [item_id for item_id, count in Counter(page_ids).items() if count > 1]
    if duplicate_page_ids:
        ctx.add(
            "error",
            "DUPLICATE_PAGE_ID",
            "duplicate page_id values: {}".format(", ".join(map(str, duplicate_page_ids[:10]))),
            document_id=document_id,
        )

    numbers = [
        page.get("page_number")
        for page in pages
        if isinstance(page, dict) and isinstance(page.get("page_number"), int)
    ]
    if len(numbers) != len(pages):
        ctx.add("warning", "BAD_PAGE_NUMBER", "some page refs have no integer page_number", document_id=document_id)
    expected = list(range(1, len(numbers) + 1))
    if numbers and sorted(numbers) != expected:
        ctx.add(
            "warning",
            "NON_CONTIGUOUS_PAGE_NUMBERS",
            "page_number values are not contiguous from 1 to {}".format(len(numbers)),
            document_id=document_id,
        )

    if check_page_files:
        missing = []
        bad_json = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            page_path = resolve_page_path(ctx.path, page)
            if page_path is None:
                missing.append("<missing path>")
                continue
            if not page_path.exists():
                missing.append(str(page_path))
                continue
            try:
                page_payload = load_json(page_path)
            except Exception:
                bad_json.append(str(page_path))
                continue
            if page_payload.get("page_number") not in (None, page.get("page_number")):
                ctx.add(
                    "warning",
                    "PAGE_FILE_NUMBER_MISMATCH",
                    "{} has page_number {}, ref says {}".format(
                        page_path, page_payload.get("page_number"), page.get("page_number")
                    ),
                    document_id=document_id,
                )
        if missing:
            ctx.add(
                "error",
                "MISSING_PAGE_FILE",
                "{} referenced page file(s) are missing; first: {}".format(len(missing), missing[0]),
                document_id=document_id,
            )
        if bad_json:
            ctx.add(
                "error",
                "BAD_PAGE_FILE_JSON",
                "{} referenced page file(s) are invalid JSON; first: {}".format(len(bad_json), bad_json[0]),
                document_id=document_id,
            )

    return {page_id: page for page_id, page in zip(page_ids, pages)}, page_count


def validate_table_chunk(
    ctx: ValidationContext,
    chunk: Dict[str, Any],
    unit: Optional[Dict[str, Any]],
    document_id: Optional[str],
) -> None:
    rows = table_rows(chunk)
    columns = chunk.get("columns")
    if not isinstance(columns, list):
        columns = []
    clean_columns = [str(column).strip() for column in columns]
    table_citation = citation(chunk)

    if not chunk.get("parser_name"):
        ctx.add(
            "warning",
            "TABLE_MISSING_PARSER",
            "{} has no parser_name".format(table_citation),
            document_id=document_id,
            citation=table_citation,
        )
    if chunk.get("parser_name") == "text_fallback":
        ctx.add(
            "warning",
            "TABLE_TEXT_FALLBACK",
            "{} used text_fallback parser".format(table_citation),
            document_id=document_id,
            citation=table_citation,
        )
    if not clean_columns:
        ctx.add(
            "error",
            "TABLE_NO_COLUMNS",
            "{} has no columns".format(table_citation),
            document_id=document_id,
            citation=table_citation,
        )
    if not rows:
        ctx.add(
            "error",
            "TABLE_NO_ROWS",
            "{} has no rows".format(table_citation),
            document_id=document_id,
            citation=table_citation,
        )

    blank_columns = [index for index, column in enumerate(clean_columns, start=1) if not column]
    if blank_columns:
        ctx.add(
            "warning",
            "TABLE_BLANK_COLUMN_NAME",
            "{} has blank column name(s) at positions {}".format(table_citation, blank_columns[:10]),
            document_id=document_id,
            citation=table_citation,
        )
    duplicate_columns = [column for column, count in Counter(clean_columns).items() if column and count > 1]
    if duplicate_columns:
        ctx.add(
            "warning",
            "TABLE_DUPLICATE_COLUMNS",
            "{} has duplicate column names: {}".format(table_citation, ", ".join(duplicate_columns[:10])),
            document_id=document_id,
            citation=table_citation,
        )

    row_page_ranges = chunk.get("row_page_ranges")
    if isinstance(row_page_ranges, list) and row_page_ranges and len(row_page_ranges) != len(rows):
        ctx.add(
            "warning",
            "TABLE_ROW_PAGE_RANGE_MISMATCH",
            "{} has {} rows but {} row_page_ranges".format(table_citation, len(rows), len(row_page_ranges)),
            document_id=document_id,
            citation=table_citation,
        )

    if unit and isinstance(unit.get("row_count"), int) and unit.get("row_count") != len(rows):
        ctx.add(
            "warning",
            "TABLE_UNIT_ROW_COUNT_MISMATCH",
            "{} unit row_count={} but chunk has {} rows".format(
                table_citation, unit.get("row_count"), len(rows)
            ),
            document_id=document_id,
            citation=table_citation,
        )

    bad_row_shapes = 0
    all_empty_rows = 0
    row_fingerprints: Counter = Counter()
    markers: List[str] = []
    values_by_column: DefaultDict[str, List[str]] = defaultdict(list)

    for row in rows:
        if isinstance(row, dict):
            row_keys = set(row)
            column_set = set(clean_columns)
            if column_set and row_keys != column_set:
                bad_row_shapes += 1
            values = [str(row.get(column) or "").strip() for column in clean_columns]
            if not values and row:
                values = [str(value or "").strip() for value in row.values()]
            for column in clean_columns:
                values_by_column[column].append(str(row.get(column) or "").strip())
        elif isinstance(row, list):
            if clean_columns and len(row) != len(clean_columns):
                bad_row_shapes += 1
            values = [str(value or "").strip() for value in row]
            for column, value in zip(clean_columns, values):
                values_by_column[column].append(value)
        else:
            values = [str(row or "").strip()]
            if clean_columns and len(clean_columns) != 1:
                bad_row_shapes += 1

        if not any(values):
            all_empty_rows += 1
        fingerprint = "\u241f".join(values)
        if fingerprint.strip("\u241f"):
            row_fingerprints[fingerprint] += 1
        marker = row_marker(row)
        if marker:
            markers.append(marker)

    if bad_row_shapes:
        ctx.add(
            "warning",
            "TABLE_BAD_ROW_SHAPE",
            "{} has {} row(s) whose keys/length do not match columns".format(table_citation, bad_row_shapes),
            document_id=document_id,
            citation=table_citation,
        )
    if all_empty_rows:
        ctx.add(
            "warning",
            "TABLE_EMPTY_ROWS",
            "{} has {} fully empty row(s)".format(table_citation, all_empty_rows),
            document_id=document_id,
            citation=table_citation,
        )

    duplicate_rows = [count for _, count in row_fingerprints.items() if count > 1]
    if duplicate_rows:
        ctx.add(
            "warning",
            "TABLE_DUPLICATE_ROWS",
            "{} has {} duplicated row fingerprint group(s)".format(table_citation, len(duplicate_rows)),
            document_id=document_id,
            citation=table_citation,
        )

    duplicate_markers = [marker for marker, count in Counter(markers).items() if count > 1]
    if (
        duplicate_markers
        and len(set(markers)) > 1
        and chunk.get("parser_name") == "multi_panel_continuation"
    ):
        ctx.add(
            "warning",
            "TABLE_DUPLICATE_ROW_MARKERS",
            "{} has repeated row markers: {}".format(table_citation, ", ".join(duplicate_markers[:10])),
            document_id=document_id,
            citation=table_citation,
        )

    empty_value_columns = [
        column
        for column, values in values_by_column.items()
        if column and values and not any(value for value in values)
    ]
    if empty_value_columns and len(rows) > 1:
        ctx.add(
            "warning",
            "TABLE_EMPTY_COLUMNS",
            "{} has columns that are empty in every row: {}".format(
                table_citation, ", ".join(empty_value_columns[:10])
            ),
            document_id=document_id,
            citation=table_citation,
        )


def validate_text_chunk(
    ctx: ValidationContext,
    chunk: Dict[str, Any],
    document_id: Optional[str],
) -> None:
    text = chunk.get("text")
    chunk_type = chunk.get("chunk_type")
    chunk_citation = citation(chunk)
    if chunk_type != "table_rows" and (not isinstance(text, str) or not text.strip()):
        ctx.add(
            "warning",
            "EMPTY_TEXT_CHUNK",
            "{} has empty text".format(chunk_citation),
            document_id=document_id,
            citation=chunk_citation,
        )
    if isinstance(text, str) and len(text) > 15000:
        ctx.add(
            "warning",
            "VERY_LARGE_CHUNK",
            "{} has {} characters".format(chunk_citation, len(text)),
            document_id=document_id,
            citation=chunk_citation,
        )
    if chunk_type != "table_rows" and isinstance(text, str):
        table_lines = [line.strip() for line in text.splitlines() if TABLE_LABEL_RE.match(line)]
        if table_lines:
            ctx.add(
                "warning",
                "POSSIBLE_MISSED_TABLE",
                "{} contains table-like heading(s): {}".format(chunk_citation, "; ".join(table_lines[:3])),
                document_id=document_id,
                citation=chunk_citation,
            )
    if chunk_type == "paragraph_text" and isinstance(text, str):
        subsection_markers = re.findall(r"(?m)^\(\d+[a-z]?\)", text)
        if len(subsection_markers) >= 2:
            ctx.add(
                "info",
                "PARAGRAPH_TEXT_CONTAINS_SUBSECTIONS",
                "{} contains {} subsection markers but is one paragraph_text chunk".format(
                    chunk_citation, len(subsection_markers)
                ),
                document_id=document_id,
                citation=chunk_citation,
            )


def validate_raw_document(ctx: ValidationContext, doc: Dict[str, Any], check_page_files: bool) -> None:
    document_id = doc.get("document_id")
    document_key = doc.get("document_key") or doc.get("document_global_key")
    for required in ("document_id", "document_key", "document_global_key", "title"):
        if not doc.get(required):
            ctx.add(
                "warning",
                "DOCUMENT_MISSING_FIELD",
                "document is missing {}".format(required),
                document_id=document_id,
            )

    page_by_id, page_count = validate_pages(ctx, doc, document_id, check_page_files)
    units = as_list(doc.get("structural_units"))
    chunks = as_list(doc.get("chunks"))
    ctx.metrics["pages"] += page_count
    ctx.metrics["structural_units"] += len(units)
    ctx.metrics["chunks"] += len(chunks)
    ctx.metrics["table_chunks"] += sum(1 for chunk in chunks if chunk.get("chunk_type") == "table_rows")

    if not units:
        ctx.add("error", "NO_STRUCTURAL_UNITS", "document has no structural_units", document_id=document_id)
    if not chunks:
        ctx.add("error", "NO_CHUNKS", "document has no chunks", document_id=document_id)
    if page_count and len(chunks) / max(page_count, 1) < 0.2:
        ctx.add(
            "warning",
            "LOW_CHUNK_DENSITY",
            "document has {} chunks for {} pages".format(len(chunks), page_count),
            document_id=document_id,
        )

    validate_id_uniqueness(ctx, units, "unit_id", "UNIT", document_id)
    validate_id_uniqueness(ctx, chunks, "chunk_id", "CHUNK", document_id)
    validate_global_keys(ctx, units, "UNIT", document_id)
    validate_global_keys(ctx, chunks, "CHUNK", document_id)

    unit_by_id = {unit.get("unit_id"): unit for unit in units if unit.get("unit_id")}
    chunk_by_id = {chunk.get("chunk_id"): chunk for chunk in chunks if chunk.get("chunk_id")}
    chunks_by_unit: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)

    paragraph_units = [unit for unit in units if unit.get("unit_type") == "paragraph"]
    if not paragraph_units:
        ctx.add("warning", "NO_PARAGRAPH_UNITS", "document has no paragraph structural units", document_id=document_id)

    for unit in units:
        validate_page_range(ctx, unit, page_count, "UNIT", document_id)
        parent_id = unit.get("parent_unit_id")
        if parent_id and parent_id not in unit_by_id:
            ctx.add(
                "error",
                "UNIT_MISSING_PARENT",
                "{} points to missing parent_unit_id {}".format(citation(unit), parent_id),
                document_id=document_id,
                citation=citation(unit),
            )
        for child_id in as_list(unit.get("child_unit_ids")):
            child = unit_by_id.get(child_id)
            if child is None:
                ctx.add(
                    "error",
                    "UNIT_MISSING_CHILD",
                    "{} lists missing child_unit_id {}".format(citation(unit), child_id),
                    document_id=document_id,
                    citation=citation(unit),
                )
            elif child.get("parent_unit_id") != unit.get("unit_id"):
                ctx.add(
                    "warning",
                    "UNIT_CHILD_BACKREF_MISMATCH",
                    "{} lists child {}, but child parent is {}".format(
                        citation(unit), child_id, child.get("parent_unit_id")
                    ),
                    document_id=document_id,
                    citation=citation(unit),
                )

    for chunk in chunks:
        validate_page_range(ctx, chunk, page_count, "CHUNK", document_id)
        unit_id = chunk.get("unit_id")
        if unit_id:
            chunks_by_unit[unit_id].append(chunk)
        if not unit_id or unit_id not in unit_by_id:
            ctx.add(
                "error",
                "CHUNK_MISSING_UNIT",
                "{} points to missing unit_id {}".format(citation(chunk), unit_id),
                document_id=document_id,
                citation=citation(chunk),
            )
        page_id = chunk.get("page_id")
        if page_id and page_by_id and page_id not in page_by_id:
            ctx.add(
                "warning",
                "CHUNK_PAGE_ID_NOT_FOUND",
                "{} points to missing page_id {}".format(citation(chunk), page_id),
                document_id=document_id,
                citation=citation(chunk),
            )
        parent_id = chunk.get("parent_chunk_id")
        if parent_id and parent_id not in chunk_by_id:
            ctx.add(
                "error",
                "CHUNK_MISSING_PARENT",
                "{} points to missing parent_chunk_id {}".format(citation(chunk), parent_id),
                document_id=document_id,
                citation=citation(chunk),
            )
        for child_id in as_list(chunk.get("child_chunk_ids")):
            child = chunk_by_id.get(child_id)
            if child is None:
                ctx.add(
                    "error",
                    "CHUNK_MISSING_CHILD",
                    "{} lists missing child_chunk_id {}".format(citation(chunk), child_id),
                    document_id=document_id,
                    citation=citation(chunk),
                )
            elif child.get("parent_chunk_id") != chunk.get("chunk_id"):
                ctx.add(
                    "warning",
                    "CHUNK_CHILD_BACKREF_MISMATCH",
                    "{} lists child {}, but child parent is {}".format(
                        citation(chunk), child_id, child.get("parent_chunk_id")
                    ),
                    document_id=document_id,
                    citation=citation(chunk),
                )
        validate_text_chunk(ctx, chunk, document_id)
        if chunk.get("chunk_type") == "table_rows":
            validate_table_chunk(ctx, chunk, unit_by_id.get(unit_id), document_id)

    for unit_id, unit in unit_by_id.items():
        if not chunks_by_unit.get(unit_id) and not as_list(unit.get("child_unit_ids")):
            ctx.add(
                "warning",
                "UNIT_WITHOUT_CHUNKS",
                "{} has no chunks and no child units".format(citation(unit)),
                document_id=document_id,
                citation=citation(unit),
            )

    sequences_by_unit: DefaultDict[str, List[Any]] = defaultdict(list)
    for chunk in chunks:
        if chunk.get("unit_id") and chunk.get("sequence") is not None:
            sequences_by_unit[chunk["unit_id"]].append(chunk.get("sequence"))
    for unit_id, sequences in sequences_by_unit.items():
        duplicates = [seq for seq, count in Counter(sequences).items() if count > 1]
        if duplicates:
            unit = unit_by_id.get(unit_id, {})
            ctx.add(
                "warning",
                "DUPLICATE_CHUNK_SEQUENCE",
                "{} has duplicate chunk sequence values: {}".format(
                    citation(unit), ", ".join(map(str, duplicates[:10]))
                ),
                document_id=document_id,
                citation=citation(unit),
            )


def validate_raw_payload(ctx: ValidationContext, payload: Dict[str, Any], check_page_files: bool) -> None:
    ctx.metrics["kind"] = "raw"
    documents = as_list(payload.get("documents"))
    ctx.metrics["documents"] = len(documents)
    ctx.metrics["extraction_issues"] = len(as_list(payload.get("extraction_issues")))
    if not documents:
        ctx.add("error", "NO_DOCUMENTS", "raw payload has no documents")
    for doc in documents:
        if not isinstance(doc, dict):
            ctx.add("error", "BAD_DOCUMENT", "document entry is not an object")
            continue
        validate_raw_document(ctx, doc, check_page_files)
    for issue in as_list(payload.get("extraction_issues")):
        if isinstance(issue, dict) and issue.get("severity") in ("error", "warning"):
            ctx.add(
                issue.get("severity", "warning"),
                "EXTRACTOR_ISSUE",
                "{}: {}".format(issue.get("issue_type", "issue"), issue.get("description", "")),
                document_id=issue.get("document_id"),
                citation=page_range_label(issue) or None,
            )


def validate_graph_payload(ctx: ValidationContext, payload: Dict[str, Any]) -> None:
    ctx.metrics["kind"] = "graph"
    nodes = as_list(payload.get("nodes"))
    relationships = as_list(payload.get("relationships"))
    ctx.metrics["graph_nodes"] = len(nodes)
    ctx.metrics["graph_relationships"] = len(relationships)
    validate_id_uniqueness(ctx, nodes, "id", "GRAPH_NODE", None)
    validate_id_uniqueness(ctx, relationships, "id", "GRAPH_REL", None)
    node_ids = {node.get("id") for node in nodes if isinstance(node, dict)}
    missing_start = []
    missing_end = []
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        start_id = rel.get("start_node_id") or rel.get("start_id")
        end_id = rel.get("end_node_id") or rel.get("end_id")
        if start_id not in node_ids:
            missing_start.append(rel.get("id"))
        if end_id not in node_ids:
            missing_end.append(rel.get("id"))
    if missing_start:
        ctx.add(
            "error",
            "GRAPH_REL_MISSING_START",
            "{} relationship(s) point to missing start_id; first: {}".format(len(missing_start), missing_start[0]),
        )
    if missing_end:
        ctx.add(
            "error",
            "GRAPH_REL_MISSING_END",
            "{} relationship(s) point to missing end_id; first: {}".format(len(missing_end), missing_end[0]),
        )
    if not any("Document" in as_list(node.get("labels")) for node in nodes if isinstance(node, dict)):
        ctx.add("warning", "GRAPH_NO_DOCUMENT_NODE", "graph has no Document node")
    if relationships and not any(rel.get("type") == "REFERS_TO" for rel in relationships if isinstance(rel, dict)):
        ctx.add("info", "GRAPH_NO_REFERS_TO", "graph has no REFERS_TO relationships")


def validate_file(path: Path, check_page_files: bool) -> ValidationContext:
    ctx = ValidationContext(path)
    try:
        payload = load_json(path)
    except Exception as exc:
        ctx.add("error", "INVALID_JSON", str(exc))
        return ctx
    if "documents" in payload:
        validate_raw_payload(ctx, payload, check_page_files)
    elif "nodes" in payload and "relationships" in payload:
        validate_graph_payload(ctx, payload)
    else:
        ctx.add("warning", "UNKNOWN_JSON_SHAPE", "JSON is neither raw extraction nor content graph")
    return ctx


def collect_table_chunks(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for document in payload.get("documents", []):
        for chunk in document.get("chunks", []):
            if chunk.get("chunk_type") == "table_rows":
                yield chunk


def print_legacy_table_summary(args: argparse.Namespace) -> int:
    payload = load_json(Path(args.input))
    chunks = [
        chunk
        for chunk in collect_table_chunks(payload)
        if args.contains is None or args.contains in citation(chunk)
    ]
    failures = []

    print("tables\t{}".format(len(chunks)))
    for chunk in chunks:
        rows = table_rows(chunk)
        columns = chunk.get("columns") or []
        markers = [row_marker(row) for row in rows]
        summary = "{}\tparser={}\tcolumns={}\trows={}".format(
            citation(chunk), chunk.get("parser_name") or "", len(columns), len(rows)
        )
        if markers:
            summary += "\tfirst={}\tlast={}".format(markers[0], markers[-1])
        print(summary)

        if args.expect_columns is not None and len(columns) != args.expect_columns:
            failures.append(
                "{}: expected {} columns, got {}".format(
                    citation(chunk), args.expect_columns, len(columns)
                )
            )
        if args.expect_rows is not None and len(rows) != args.expect_rows:
            failures.append(
                "{}: expected {} rows, got {}".format(
                    citation(chunk), args.expect_rows, len(rows)
                )
            )
        if args.row_marker_regex:
            marker_re = re.compile(args.row_marker_regex)
            bad_markers = [marker for marker in markers if not marker_re.match(marker)]
            if bad_markers:
                failures.append(
                    "{}: row markers failed regex {}: {}".format(
                        citation(chunk), args.row_marker_regex, ", ".join(bad_markers[:5])
                    )
                )

    if failures:
        print("\nFAIL", file=sys.stderr)
        for failure in failures:
            print("- {}".format(failure), file=sys.stderr)
        return 1

    print("OK")
    return 0


def sorted_findings(findings: Sequence[Finding]) -> List[Finding]:
    return sorted(
        findings,
        key=lambda item: (
            SEVERITY_ORDER.get(item.severity, 99),
            item.file,
            item.document_id or "",
            item.code,
            item.citation or "",
        ),
    )


def issue_counts(findings: Sequence[Finding]) -> Counter:
    return Counter(finding.severity for finding in findings)


def code_counts(findings: Sequence[Finding]) -> Counter:
    return Counter(finding.code for finding in findings)


def write_markdown_report(path: Path, contexts: Sequence[ValidationContext], max_issues: int) -> None:
    all_findings = sorted_findings([finding for ctx in contexts for finding in ctx.findings])
    counts = issue_counts(all_findings)
    lines = [
        "# Parsed JSON Validation Report",
        "",
        "Generated: `{}`".format(datetime.now().isoformat(timespec="seconds")),
        "",
        "## Summary",
        "",
        "- Files checked: `{}`".format(len(contexts)),
        "- Errors: `{}`".format(counts.get("error", 0)),
        "- Warnings: `{}`".format(counts.get("warning", 0)),
        "- Info: `{}`".format(counts.get("info", 0)),
        "",
        "## Files",
        "",
        "| File | Kind | Docs | Pages | Units | Chunks | Tables | Graph Nodes | Graph Rels | Errors | Warnings |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for ctx in contexts:
        file_counts = issue_counts(ctx.findings)
        metrics = ctx.metrics
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                Path(metrics["file"]).name,
                metrics.get("kind", "unknown"),
                metrics.get("documents", 0),
                metrics.get("pages", 0),
                metrics.get("structural_units", 0),
                metrics.get("chunks", 0),
                metrics.get("table_chunks", 0),
                metrics.get("graph_nodes", 0),
                metrics.get("graph_relationships", 0),
                file_counts.get("error", 0),
                file_counts.get("warning", 0),
            )
        )

    lines.extend(["", "## Most Common Finding Codes", ""])
    for code, count in code_counts(all_findings).most_common(25):
        lines.append("- `{}`: `{}`".format(code, count))

    lines.extend(["", "## Findings", ""])
    if not all_findings:
        lines.append("No findings.")
    else:
        shown = all_findings[:max_issues]
        for finding in shown:
            location = Path(finding.file).name
            if finding.document_id:
                location += " / {}".format(finding.document_id)
            if finding.citation:
                location += " / {}".format(finding.citation)
            lines.append(
                "- **{}** `{}` {}: {}".format(
                    finding.severity.upper(), finding.code, location, finding.message
                )
            )
        if len(all_findings) > max_issues:
            lines.append("")
            lines.append(
                "_{} additional finding(s) omitted from Markdown report; use the JSON report for the full list._".format(
                    len(all_findings) - max_issues
                )
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json_report(path: Path, contexts: Sequence[ValidationContext]) -> None:
    findings = [finding for ctx in contexts for finding in ctx.findings]
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files_checked": len(contexts),
        "counts": dict(issue_counts(findings)),
        "files": [ctx.metrics for ctx in contexts],
        "findings": [finding.as_dict() for finding in sorted_findings(findings)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_report_path(input_path: Path) -> Path:
    if input_path.name == "raw":
        return input_path.parent / "validation" / "parsed_json_validation.md"
    if input_path.name == "content_graphs":
        return input_path.parent / "validation" / "content_graph_validation.md"
    if input_path.is_dir():
        return input_path / "validation_report.md"
    return input_path.with_suffix(".validation.md")


def default_json_report_path(report_path: Path) -> Path:
    return report_path.with_suffix(".json")


def should_fail(findings: Sequence[Finding], fail_on: str) -> bool:
    threshold = FAIL_LEVELS[fail_on]
    if threshold < 0:
        return False
    return any(SEVERITY_ORDER.get(finding.severity, 99) <= threshold for finding in findings)


def validate(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    if args.legacy_summary:
        if not input_path.is_file():
            print("--legacy-summary requires a single raw JSON file", file=sys.stderr)
            return 2
        return print_legacy_table_summary(args)

    paths = json_files(input_path, recursive=args.recursive)
    if not paths:
        print("No JSON files found under {}".format(input_path), file=sys.stderr)
        return 2

    contexts = [validate_file(path, check_page_files=not args.skip_page_files) for path in paths]
    all_findings = [finding for ctx in contexts for finding in ctx.findings]
    counts = issue_counts(all_findings)

    report_path = Path(args.report) if args.report else default_report_path(input_path)
    write_markdown_report(report_path, contexts, args.max_issues)
    json_report_path = None
    if not args.no_json_report:
        json_report_path = Path(args.json_report) if args.json_report else default_json_report_path(report_path)
        write_json_report(json_report_path, contexts)

    print("files\t{}".format(len(contexts)))
    print("errors\t{}".format(counts.get("error", 0)))
    print("warnings\t{}".format(counts.get("warning", 0)))
    print("info\t{}".format(counts.get("info", 0)))
    print("report\t{}".format(report_path))
    if json_report_path:
        print("json_report\t{}".format(json_report_path))
    return 1 if should_fail(all_findings, args.fail_on) else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="Raw/content-graph JSON file or directory of JSON files; defaults to {}".format(DEFAULT_INPUT),
    )
    parser.add_argument("--recursive", action="store_true", help="Scan JSON files recursively when input is a directory")
    parser.add_argument("--report", help="Markdown report path")
    parser.add_argument("--json-report", help="Optional machine-readable JSON report path")
    parser.add_argument("--no-json-report", action="store_true", help="Only write the Markdown report")
    parser.add_argument(
        "--fail-on",
        choices=sorted(FAIL_LEVELS),
        default="error",
        help="Exit non-zero when findings at this severity or higher exist",
    )
    parser.add_argument(
        "--skip-page-files",
        action="store_true",
        help="Do not verify existence/basic JSON shape of referenced page files",
    )
    parser.add_argument("--max-issues", type=int, default=500, help="Maximum findings listed in Markdown report")

    parser.add_argument(
        "--legacy-summary",
        action="store_true",
        help="Print the old table summary for a single raw JSON file",
    )
    parser.add_argument("--contains", help="Legacy summary: only validate tables whose citation contains this text")
    parser.add_argument("--expect-columns", type=int, help="Legacy summary: expected column count")
    parser.add_argument("--expect-rows", type=int, help="Legacy summary: expected row count")
    parser.add_argument("--row-marker-regex", help="Legacy summary: expected row marker regex")
    return parser.parse_args()


def main() -> None:
    raise SystemExit(validate(parse_args()))


if __name__ == "__main__":
    main()
