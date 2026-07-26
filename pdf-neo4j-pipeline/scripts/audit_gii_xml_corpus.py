#!/usr/bin/env python3
"""Audit a batch of GII XML raw extractions.

The audit is deliberately independent of the extractor.  It reads the
aggregate batch manifest and each referenced raw JSON exactly once, produces a
machine-readable JSON report plus a compact Markdown summary, and exits
non-zero only when a structural invariant is broken.  Missing or partial PDF
alignment is coverage information, not an XML extraction failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = "1.0"
SUCCESS_BATCH_STATUSES = frozenset({"ok", "skipped_existing"})
KNOWN_BATCH_STATUSES = SUCCESS_BATCH_STATUSES | {"error"}
MAX_EXAMPLES = 20

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BATCH_DIR = PROJECT_ROOT / "output" / "gii_xml"


class AuditInputError(RuntimeError):
    """Raised when the audit itself cannot read its required input."""


class FindingCollector:
    """Count invariant failures while bounding potentially large examples."""

    def __init__(self) -> None:
        self._findings: Dict[str, Dict[str, Any]] = {}

    def register(self, code: str, message: str) -> None:
        self._findings.setdefault(
            code,
            {
                "code": code,
                "message": message,
                "count": 0,
                "examples": [],
            },
        )

    def add(
        self,
        code: str,
        message: str,
        example: Optional[Mapping[str, Any]] = None,
        amount: int = 1,
    ) -> None:
        self.register(code, message)
        finding = self._findings[code]
        finding["count"] += max(1, int(amount))
        if example is not None and len(finding["examples"]) < MAX_EXAMPLES:
            finding["examples"].append(dict(example))

    def result(self, code: str) -> Dict[str, Any]:
        finding = self._findings[code]
        return {
            "count": finding["count"],
            "examples": list(finding["examples"]),
        }

    def failures(self) -> List[Dict[str, Any]]:
        return [
            dict(finding)
            for _code, finding in sorted(self._findings.items())
            if finding["count"]
        ]


INTEGRITY_FINDINGS = {
    "batch_errors": "The batch manifest contains extraction errors.",
    "manifest_inconsistencies": "Aggregate manifest counts or records are inconsistent.",
    "missing_raw_outputs": "A successful batch entry has no raw JSON output.",
    "unreadable_raw_outputs": "A referenced raw output is unreadable or malformed.",
    "untracked_raw_outputs": "The raw directory contains JSON files absent from the manifest.",
    "output_digest_mismatches": "A raw output differs from its manifest digest or size.",
    "malformed_documents": "A raw payload violates the one-document batch contract.",
    "missing_ids": "Documents, units, or chunks are missing required IDs.",
    "duplicate_document_ids": "Document IDs are not unique across the corpus.",
    "duplicate_unit_ids": "Structural-unit IDs are not unique across the corpus.",
    "duplicate_chunk_ids": "Chunk IDs are not unique across the corpus.",
    "duplicate_chunk_sequences": "Chunk sequence values are not unique within a structural unit.",
    "zero_unit_documents": "An extracted document has no structural units.",
    "dangling_references": "A unit/chunk hierarchy reference does not resolve consistently.",
    "missing_assets": "XML references source assets absent from its source package.",
    "internal_footnote_leaks": "An internal XML footnote ID leaked into visible extracted text.",
    "invalid_table_shapes": "A CALS table has inconsistent columns or row counts.",
    "invalid_page_ranges": "A populated PDF page range is malformed or outside the PDF.",
    "extraction_error_issues": "The raw payload contains an error-severity extraction issue.",
    "hard_case_ersatzbaustoffv": "ErsatzbaustoffV Anlage 1 Tabelle 1 is not 20 columns by 18 rows.",
    "hard_case_egbgb": "EGBGB Art 232 paragraph units are not nested under the article.",
    "hard_case_abfklaerv": "AbfKlärV does not expose exactly the known Teil 4/Teil 5 hierarchy warnings.",
    "hard_case_missing": "A required hard-case document is absent from the audited batch.",
    "hard_case_ambiguous": "More than one document matches a hard-case identity.",
}


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _load_json_bytes(path: Path) -> Tuple[Dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditInputError("cannot read JSON object {}: {}".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise AuditInputError("{} must contain a JSON object".format(path))
    return value, raw


def _safe_output_path(batch_dir: Path, relative_path: Any) -> Path:
    value = str(relative_path or "").strip()
    if not value:
        raise ValueError("empty output path")
    root = batch_dir.resolve()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("output path escapes batch directory: {!r}".format(value)) from exc
    return candidate


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if not denominator:
        return None
    return round(numerator / denominator, 6)


def _integer(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sorted_counter(counter: Counter[str]) -> Dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: item[0]))


def _record_duplicate(
    identifier: Any,
    kind: str,
    source: Mapping[str, Any],
    seen: Dict[str, Dict[str, Any]],
    findings: FindingCollector,
) -> None:
    code = "duplicate_{}_ids".format(kind)
    identifier_text = str(identifier or "")
    if not identifier_text:
        findings.add(
            "missing_ids",
            INTEGRITY_FINDINGS["missing_ids"],
            {"kind": kind, **dict(source)},
        )
        return
    previous = seen.get(identifier_text)
    if previous is not None:
        findings.add(
            code,
            INTEGRITY_FINDINGS[code],
            {
                "id": identifier_text,
                "first": previous,
                "duplicate": dict(source),
            },
        )
    else:
        seen[identifier_text] = dict(source)


def _visible_strings(value: Any) -> Iterable[str]:
    """Yield human-visible strings while ignoring provenance-only footnote IDs."""

    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _visible_strings(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).casefold()
            if (
                "footnote_id" in key_text
                or key_text.endswith("_id")
                or key_text.startswith("source_")
                or key_text == "attributes"
                or key_text.endswith("_attributes")
                or key_text == "source_element"
            ):
                continue
            # CALS legacy rows use visible column headings as dictionary keys.
            # Inspect those keys as content; an unresolved IDREF can otherwise
            # hide there even when every row value is clean.
            yield str(key)
            yield from _visible_strings(item)


def _footnote_ids(chunks: Sequence[Mapping[str, Any]]) -> List[str]:
    identifiers = set()
    for chunk in chunks:
        direct = chunk.get("source_xml_footnote_id")
        if isinstance(direct, str) and len(direct) >= 3:
            identifiers.add(direct)
        values = chunk.get("source_xml_footnote_ids")
        if isinstance(values, list):
            identifiers.update(
                str(value)
                for value in values
                if isinstance(value, str) and len(value) >= 3
            )
    return sorted(identifiers, key=lambda value: (-len(value), value))


def _internal_footnote_leaks(
    document_id: str,
    units: Sequence[Mapping[str, Any]],
    chunks: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    identifiers = _footnote_ids(chunks)
    if not identifiers:
        return []
    patterns = {
        identifier: re.compile(
            r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])".format(
                re.escape(identifier)
            )
        )
        for identifier in identifiers
    }
    leaks: List[Dict[str, Any]] = []
    selected_fields = (
        "text",
        "title",
        "columns",
        "column_header_text",
        "rows",
        "structured_lists",
        "preformatted_blocks",
    )
    for kind, items, id_field in (
        ("unit", units, "unit_id"),
        ("chunk", chunks, "chunk_id"),
    ):
        for item in items:
            for field in selected_fields:
                if field not in item:
                    continue
                for text in _visible_strings(item[field]):
                    for identifier, pattern in patterns.items():
                        if pattern.search(text):
                            leaks.append(
                                {
                                    "document_id": document_id,
                                    "kind": kind,
                                    "item_id": item.get(id_field),
                                    "field": field,
                                    "footnote_id": identifier,
                                    "text_preview": text[:180],
                                }
                            )
                            if len(leaks) >= MAX_EXAMPLES:
                                return leaks
    return leaks


def _validate_page_range(
    page_range: Any,
    pdf_pages: int,
) -> Optional[str]:
    if page_range is None:
        return None
    if not isinstance(page_range, dict):
        return "page_range is not an object"
    start = _integer(page_range.get("start"))
    end = _integer(page_range.get("end"))
    if start is None or end is None:
        return "page_range start/end are not integers"
    if start < 1 or end < start:
        return "page_range is not positive and ordered"
    if pdf_pages > 0 and end > pdf_pages:
        return "page_range exceeds pdf_pages"
    if pdf_pages <= 0:
        return "page_range exists but pdf_pages is zero"
    return None


def _table_shape_contract(
    table: Mapping[str, Any],
) -> Tuple[List[str], Dict[str, Any]]:
    """Validate the finalized CALS mapping and its lossless matrix together."""

    errors: List[str] = []
    columns = table.get("columns")
    row_count = _integer(table.get("row_count"))
    table_data = table.get("table_data")
    data_rows = (
        table_data.get("rows")
        if isinstance(table_data, dict)
        and isinstance(table_data.get("rows"), list)
        else None
    )
    body_matrix = (
        table_data.get("body_matrix")
        if isinstance(table_data, dict)
        and isinstance(table_data.get("body_matrix"), list)
        else None
    )
    num_columns = (
        _integer(table_data.get("num_columns"))
        if isinstance(table_data, dict)
        else None
    )
    columns_count = len(columns) if isinstance(columns, list) else None
    body_widths = sorted(
        {
            len(row)
            for row in body_matrix or []
            if isinstance(row, list)
        }
    )
    metrics = {
        "columns": columns_count,
        "row_count": row_count,
        "data_rows": len(data_rows) if data_rows is not None else None,
        "num_columns": num_columns,
        "body_rows": len(body_matrix) if body_matrix is not None else None,
        "body_widths": body_widths,
    }

    valid_columns = (
        isinstance(columns, list)
        and bool(columns)
        and all(isinstance(column, str) and column for column in columns)
    )
    if not valid_columns:
        errors.append("columns must be a non-empty list of strings")
    elif len(set(columns)) != len(columns):
        errors.append("columns must be unique")
    if row_count is None or row_count < 0:
        errors.append("row_count must be a non-negative integer")
    if not isinstance(table_data, dict):
        errors.append("table_data must be an object")
        return errors, metrics
    if data_rows is None:
        errors.append("table_data.rows must be a list")
    elif row_count != len(data_rows):
        errors.append("row_count differs from table_data.rows")
    if num_columns is None or num_columns < 1:
        errors.append("table_data.num_columns must be a positive integer")
    elif columns_count is not None and num_columns != columns_count:
        errors.append("table_data.num_columns differs from columns")
    if body_matrix is None:
        errors.append("table_data.body_matrix must be a list")
    else:
        if row_count != len(body_matrix):
            errors.append("row_count differs from table_data.body_matrix")
        if data_rows is not None and len(body_matrix) != len(data_rows):
            errors.append("body_matrix row count differs from table_data.rows")
        malformed_matrix_rows = sum(
            not isinstance(row, list) for row in body_matrix
        )
        if malformed_matrix_rows:
            errors.append(
                "{} body_matrix rows are not lists".format(
                    malformed_matrix_rows
                )
            )
        if columns_count is not None:
            wrong_width_rows = sum(
                1
                for row in body_matrix
                if isinstance(row, list) and len(row) != columns_count
            )
            if wrong_width_rows:
                errors.append(
                    "{} body_matrix rows differ from column width".format(
                        wrong_width_rows
                    )
                )

    if valid_columns and data_rows is not None:
        bad_mapping_rows = sum(
            1
            for row in data_rows
            if not isinstance(row, dict)
            or set(row) != set(columns)
        )
        if bad_mapping_rows:
            errors.append(
                "{} row mappings differ from columns".format(
                    bad_mapping_rows
                )
            )
        if body_matrix is not None:
            content_mismatches = 0
            for mapping, matrix_row in zip(data_rows, body_matrix):
                if not isinstance(mapping, dict) or not isinstance(matrix_row, list):
                    continue
                if len(matrix_row) != len(columns):
                    continue
                content_mismatches += sum(
                    mapping.get(column) != matrix_row[index]
                    for index, column in enumerate(columns)
                )
            if content_mismatches:
                errors.append(
                    "{} body_matrix cells differ from table_data.rows".format(
                        content_mismatches
                    )
                )

    return errors, metrics


def _is_case(
    source_entry: Mapping[str, Any],
    document: Mapping[str, Any],
    names: Sequence[str],
) -> bool:
    values = {
        str(source_entry.get("slug") or "").casefold(),
        str(document.get("document_key") or "").casefold(),
        str(document.get("abbreviation") or "").casefold(),
        str(document.get("canonical_citation") or "").casefold(),
    }
    return any(name.casefold() in values for name in names)


def _hard_case_result(
    name: str,
    candidates: Sequence[Mapping[str, Any]],
    findings: FindingCollector,
    require_hard_cases: bool,
) -> Tuple[Optional[Mapping[str, Any]], Dict[str, Any]]:
    if not candidates:
        result = {
            "name": name,
            "status": "not_present",
            "required": require_hard_cases,
        }
        if require_hard_cases:
            findings.add(
                "hard_case_missing",
                INTEGRITY_FINDINGS["hard_case_missing"],
                {"hard_case": name},
            )
        return None, result
    if len(candidates) > 1:
        findings.add(
            "hard_case_ambiguous",
            INTEGRITY_FINDINGS["hard_case_ambiguous"],
            {
                "hard_case": name,
                "document_ids": [
                    candidate["document"].get("document_id")
                    for candidate in candidates[:MAX_EXAMPLES]
                ],
            },
        )
    selected = candidates[0]
    return selected, {
        "name": name,
        "status": "pending",
        "required": require_hard_cases,
        "document_id": selected["document"].get("document_id"),
        "source_path": selected.get("source_path"),
    }


def _audit_hard_cases(
    candidates: Mapping[str, Sequence[Mapping[str, Any]]],
    findings: FindingCollector,
    require_hard_cases: bool,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {}

    selected, result = _hard_case_result(
        "ErsatzbaustoffV Tabelle 1",
        candidates["ersatzbaustoffv"],
        findings,
        require_hard_cases,
    )
    if selected is not None:
        document = selected["document"]
        units = document.get("structural_units") or []
        annexes = [
            unit
            for unit in units
            if isinstance(unit, dict)
            and unit.get("label") == "Anlage 1"
            and unit.get("unit_type") in {"annex", "appendix"}
        ]
        annex_ids = {unit.get("unit_id") for unit in annexes}
        tables = [
            unit
            for unit in units
            if isinstance(unit, dict)
            and unit.get("unit_type") == "table"
            and unit.get("label") == "Tabelle 1"
            and unit.get("parent_unit_id") in annex_ids
        ]
        table = tables[0] if len(tables) == 1 else None
        table_shape_errors, table_shape = (
            _table_shape_contract(table)
            if isinstance(table, dict)
            else (["matching table is unavailable"], {})
        )
        observed = {
            "matching_tables": len(tables),
            "columns": (
                len(table.get("columns") or [])
                if isinstance(table, dict)
                else None
            ),
            "rows": table.get("row_count") if isinstance(table, dict) else None,
            "unit_id": table.get("unit_id") if isinstance(table, dict) else None,
            "num_columns": table_shape.get("num_columns"),
            "body_rows": table_shape.get("body_rows"),
            "body_widths": table_shape.get("body_widths"),
            "shape_errors": table_shape_errors,
        }
        passed = (
            len(tables) == 1
            and observed["columns"] == 20
            and observed["rows"] == 18
            and not table_shape_errors
        )
        result.update(
            {
                "status": "passed" if passed else "failed",
                "expected": {
                    "matching_tables": 1,
                    "columns": 20,
                    "rows": 18,
                    "num_columns": 20,
                    "body_matrix": "18 rows x 20 columns matching rows",
                },
                "observed": observed,
            }
        )
        if not passed:
            findings.add(
                "hard_case_ersatzbaustoffv",
                INTEGRITY_FINDINGS["hard_case_ersatzbaustoffv"],
                observed,
            )
    results["ersatzbaustoffv_table_1"] = result

    selected, result = _hard_case_result(
        "EGBGB Art 232 hierarchy",
        candidates["egbgb"],
        findings,
        require_hard_cases,
    )
    if selected is not None:
        units = [
            unit
            for unit in selected["document"].get("structural_units") or []
            if isinstance(unit, dict)
        ]
        articles = [
            unit
            for unit in units
            if unit.get("unit_type") == "article"
            and str(unit.get("label") or "").replace("Artikel", "Art").strip()
            == "Art 232"
        ]
        article = articles[0] if len(articles) == 1 else None
        paragraphs = [
            unit
            for unit in units
            if unit.get("unit_type") == "paragraph"
            and "_art_232_para_" in str(unit.get("global_key") or "")
        ]
        article_id = article.get("unit_id") if article else None
        child_ids = set(article.get("child_unit_ids") or []) if article else set()
        bad_paragraphs = [
            {
                "unit_id": unit.get("unit_id"),
                "label": unit.get("label"),
                "parent_unit_id": unit.get("parent_unit_id"),
            }
            for unit in paragraphs
            if unit.get("parent_unit_id") != article_id
            or unit.get("unit_id") not in child_ids
        ]
        paragraph_one = [
            unit for unit in paragraphs if unit.get("label") == "§ 1"
        ]
        paragraph_one_key_ok = (
            len(paragraph_one) == 1
            and str(paragraph_one[0].get("global_key") or "").endswith(
                "_art_232_para_1"
            )
        )
        passed = (
            len(articles) == 1
            and bool(paragraphs)
            and not bad_paragraphs
            and paragraph_one_key_ok
        )
        observed = {
            "matching_articles": len(articles),
            "nested_paragraphs": len(paragraphs),
            "bad_paragraphs": bad_paragraphs[:MAX_EXAMPLES],
            "paragraph_1_key_ok": paragraph_one_key_ok,
        }
        result.update(
            {
                "status": "passed" if passed else "failed",
                "expected": {
                    "matching_articles": 1,
                    "nested_paragraphs_minimum": 1,
                    "bad_paragraphs": 0,
                    "paragraph_1_key_suffix": "_art_232_para_1",
                },
                "observed": observed,
            }
        )
        if not passed:
            findings.add(
                "hard_case_egbgb",
                INTEGRITY_FINDINGS["hard_case_egbgb"],
                observed,
            )
    results["egbgb_art_232"] = result

    selected, result = _hard_case_result(
        "AbfKlärV hierarchy warnings",
        candidates["abfklaerv"],
        findings,
        require_hard_cases,
    )
    if selected is not None:
        document = selected["document"]
        issues = [
            issue
            for issue in selected["issues"]
            if isinstance(issue, dict)
            and issue.get("issue_type") == "xml_hierarchy_rank_conflict"
        ]
        labels = sorted(
            str((issue.get("evidence") or {}).get("label") or "")
            for issue in issues
            if isinstance(issue.get("evidence"), dict)
        )
        units_by_id = {
            unit.get("unit_id"): unit
            for unit in document.get("structural_units") or []
            if isinstance(unit, dict) and unit.get("unit_id")
        }
        unresolved_or_not_uncertain = [
            issue.get("unit_id")
            for issue in issues
            if issue.get("unit_id") not in units_by_id
            or not units_by_id[issue.get("unit_id")].get("is_uncertain")
        ]
        severities = sorted(str(issue.get("severity") or "") for issue in issues)
        passed = (
            labels == ["Teil 4", "Teil 5"]
            and severities == ["warning", "warning"]
            and not unresolved_or_not_uncertain
        )
        observed = {
            "warning_count": len(issues),
            "labels": labels,
            "severities": severities,
            "unresolved_or_not_uncertain": unresolved_or_not_uncertain,
        }
        result.update(
            {
                "status": "passed" if passed else "failed",
                "expected": {
                    "warning_count": 2,
                    "labels": ["Teil 4", "Teil 5"],
                    "severities": ["warning", "warning"],
                },
                "observed": observed,
            }
        )
        if not passed:
            findings.add(
                "hard_case_abfklaerv",
                INTEGRITY_FINDINGS["hard_case_abfklaerv"],
                observed,
            )
    results["abfklaerv_hierarchy_warnings"] = result

    return results


def audit_corpus(
    batch_dir: Path,
    manifest_path: Optional[Path] = None,
    require_hard_cases: bool = False,
) -> Dict[str, Any]:
    """Scan one batch and return its complete serializable audit report."""

    batch_dir = batch_dir.resolve()
    manifest_path = (manifest_path or (batch_dir / "manifest.json")).resolve()
    manifest, _manifest_raw = _load_json_bytes(manifest_path)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise AuditInputError("{} has no entries list".format(manifest_path))

    findings = FindingCollector()
    for code, message in INTEGRITY_FINDINGS.items():
        findings.register(code, message)

    status_counter: Counter[str] = Counter()
    issue_type_counter: Counter[str] = Counter()
    issue_severity_counter: Counter[str] = Counter()
    alignment_status_counter: Counter[str] = Counter()
    alignment_method_counter: Counter[str] = Counter()
    table_parser_counter: Counter[str] = Counter()
    table_shape_counter: Counter[str] = Counter()

    totals = {
        "documents": 0,
        "structural_units": 0,
        "chunks": 0,
        "tables": 0,
        "table_rows": 0,
        "page_refs": 0,
        "pdf_pages": 0,
        "source_assets": 0,
        "missing_assets": 0,
        "extraction_issues": 0,
    }
    alignment_items = {
        "units_total": 0,
        "units_aligned": 0,
        "chunks_total": 0,
        "chunks_aligned": 0,
    }
    seen_document_ids: Dict[str, Dict[str, Any]] = {}
    seen_unit_ids: Dict[str, Dict[str, Any]] = {}
    seen_chunk_ids: Dict[str, Dict[str, Any]] = {}
    expected_paths = set()
    hard_case_candidates: Dict[str, List[Dict[str, Any]]] = {
        "ersatzbaustoffv": [],
        "egbgb": [],
        "abfklaerv": [],
    }

    for manifest_index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            findings.add(
                "manifest_inconsistencies",
                INTEGRITY_FINDINGS["manifest_inconsistencies"],
                {"entry_index": manifest_index, "reason": "entry is not an object"},
            )
            continue
        status = str(raw_entry.get("status") or "unknown")
        status_counter[status] += 1
        item_id = raw_entry.get("item_id")
        if status not in KNOWN_BATCH_STATUSES:
            findings.add(
                "manifest_inconsistencies",
                INTEGRITY_FINDINGS["manifest_inconsistencies"],
                {
                    "entry_index": manifest_index,
                    "item_id": item_id,
                    "reason": "unknown status",
                    "status": status,
                },
            )
        if status == "error":
            findings.add(
                "batch_errors",
                INTEGRITY_FINDINGS["batch_errors"],
                {
                    "entry_index": manifest_index,
                    "item_id": item_id,
                    "error_type": raw_entry.get("error_type"),
                    "error": raw_entry.get("error"),
                },
            )
            continue
        if status not in SUCCESS_BATCH_STATUSES:
            continue

        try:
            output_path = _safe_output_path(batch_dir, raw_entry.get("output_path"))
        except ValueError as exc:
            findings.add(
                "manifest_inconsistencies",
                INTEGRITY_FINDINGS["manifest_inconsistencies"],
                {
                    "entry_index": manifest_index,
                    "item_id": item_id,
                    "reason": str(exc),
                },
            )
            continue
        expected_paths.add(output_path)
        source = {
            "manifest_index": manifest_index,
            "item_id": item_id,
            "source_path": output_path.relative_to(batch_dir).as_posix(),
        }
        if not output_path.is_file():
            findings.add(
                "missing_raw_outputs",
                INTEGRITY_FINDINGS["missing_raw_outputs"],
                source,
            )
            continue
        try:
            payload, raw_bytes = _load_json_bytes(output_path)
        except AuditInputError as exc:
            findings.add(
                "unreadable_raw_outputs",
                INTEGRITY_FINDINGS["unreadable_raw_outputs"],
                {**source, "error": str(exc)},
            )
            continue

        expected_sha256 = str(raw_entry.get("output_sha256") or "")
        actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        expected_bytes = _integer(raw_entry.get("output_bytes"))
        if expected_sha256 and actual_sha256 != expected_sha256:
            findings.add(
                "output_digest_mismatches",
                INTEGRITY_FINDINGS["output_digest_mismatches"],
                {
                    **source,
                    "expected_sha256": expected_sha256,
                    "actual_sha256": actual_sha256,
                },
            )
        if expected_bytes is not None and expected_bytes != len(raw_bytes):
            findings.add(
                "output_digest_mismatches",
                INTEGRITY_FINDINGS["output_digest_mismatches"],
                {
                    **source,
                    "expected_bytes": expected_bytes,
                    "actual_bytes": len(raw_bytes),
                },
            )
        del raw_bytes

        documents = payload.get("documents")
        if not isinstance(documents, list) or len(documents) != 1:
            findings.add(
                "malformed_documents",
                INTEGRITY_FINDINGS["malformed_documents"],
                {
                    **source,
                    "document_count": len(documents)
                    if isinstance(documents, list)
                    else None,
                },
            )
            continue
        document = documents[0]
        if not isinstance(document, dict):
            findings.add(
                "malformed_documents",
                INTEGRITY_FINDINGS["malformed_documents"],
                {**source, "reason": "document is not an object"},
            )
            continue
        source_entry = payload.get("source_manifest_entry")
        if not isinstance(source_entry, dict):
            source_entry = {}
        document_id = str(document.get("document_id") or "")
        source_with_document = {**source, "document_id": document_id or None}
        _record_duplicate(
            document_id,
            "document",
            source_with_document,
            seen_document_ids,
            findings,
        )
        totals["documents"] += 1

        units_value = document.get("structural_units")
        chunks_value = document.get("chunks")
        if not isinstance(units_value, list) or not isinstance(chunks_value, list):
            findings.add(
                "malformed_documents",
                INTEGRITY_FINDINGS["malformed_documents"],
                {
                    **source_with_document,
                    "reason": "structural_units/chunks are not arrays",
                },
            )
            continue
        units = [unit for unit in units_value if isinstance(unit, dict)]
        chunks = [chunk for chunk in chunks_value if isinstance(chunk, dict)]
        if len(units) != len(units_value) or len(chunks) != len(chunks_value):
            findings.add(
                "malformed_documents",
                INTEGRITY_FINDINGS["malformed_documents"],
                {
                    **source_with_document,
                    "reason": "unit/chunk array contains a non-object",
                },
            )
        totals["structural_units"] += len(units)
        totals["chunks"] += len(chunks)
        alignment_items["units_total"] += len(units)
        alignment_items["chunks_total"] += len(chunks)
        if not units:
            findings.add(
                "zero_unit_documents",
                INTEGRITY_FINDINGS["zero_unit_documents"],
                source_with_document,
            )

        local_units = {
            str(unit.get("unit_id")): unit
            for unit in units
            if unit.get("unit_id")
        }
        for unit_index, unit in enumerate(units):
            unit_source = {
                **source_with_document,
                "unit_index": unit_index,
                "unit_id": unit.get("unit_id"),
            }
            _record_duplicate(
                unit.get("unit_id"),
                "unit",
                unit_source,
                seen_unit_ids,
                findings,
            )
            parent_id = unit.get("parent_unit_id")
            if parent_id and str(parent_id) not in local_units:
                findings.add(
                    "dangling_references",
                    INTEGRITY_FINDINGS["dangling_references"],
                    {
                        **unit_source,
                        "field": "parent_unit_id",
                        "target": parent_id,
                    },
                )
            children = unit.get("child_unit_ids") or []
            if not isinstance(children, list):
                findings.add(
                    "dangling_references",
                    INTEGRITY_FINDINGS["dangling_references"],
                    {**unit_source, "field": "child_unit_ids", "reason": "not a list"},
                )
                children = []
            for child_id in children:
                child = local_units.get(str(child_id))
                if child is None or child.get("parent_unit_id") != unit.get("unit_id"):
                    findings.add(
                        "dangling_references",
                        INTEGRITY_FINDINGS["dangling_references"],
                        {
                            **unit_source,
                            "field": "child_unit_ids",
                            "target": child_id,
                        },
                    )

            page_error = _validate_page_range(
                unit.get("page_range"),
                _integer((document.get("metadata") or {}).get("pdf_pages")) or 0,
            )
            if page_error:
                findings.add(
                    "invalid_page_ranges",
                    INTEGRITY_FINDINGS["invalid_page_ranges"],
                    {**unit_source, "reason": page_error, "range": unit.get("page_range")},
                )
            elif unit.get("page_range") is not None:
                alignment_items["units_aligned"] += 1
                method = (unit.get("pdf_alignment") or {}).get("method")
                alignment_method_counter[str(method or "range_without_method")] += 1

            if unit.get("unit_type") != "table":
                continue
            totals["tables"] += 1
            columns = unit.get("columns")
            row_count = _integer(unit.get("row_count"))
            table_data = unit.get("table_data")
            data_rows = (
                table_data.get("rows")
                if isinstance(table_data, dict)
                and isinstance(table_data.get("rows"), list)
                else None
            )
            columns_count = len(columns) if isinstance(columns, list) else None
            actual_rows = len(data_rows) if data_rows is not None else row_count
            if columns_count is not None and actual_rows is not None:
                table_shape_counter["{}x{}".format(columns_count, actual_rows)] += 1
            table_parser_counter[str(unit.get("parser_name") or "unknown")] += 1
            if row_count is not None and row_count >= 0:
                totals["table_rows"] += row_count
            shape_errors, shape_metrics = _table_shape_contract(unit)
            if shape_errors:
                findings.add(
                    "invalid_table_shapes",
                    INTEGRITY_FINDINGS["invalid_table_shapes"],
                    {
                        **unit_source,
                        "errors": shape_errors,
                        **shape_metrics,
                    },
                )

        for chunk_index, chunk in enumerate(chunks):
            chunk_source = {
                **source_with_document,
                "chunk_index": chunk_index,
                "chunk_id": chunk.get("chunk_id"),
            }
            _record_duplicate(
                chunk.get("chunk_id"),
                "chunk",
                chunk_source,
                seen_chunk_ids,
                findings,
            )
            unit_id = chunk.get("unit_id")
            if unit_id and str(unit_id) not in local_units:
                findings.add(
                    "dangling_references",
                    INTEGRITY_FINDINGS["dangling_references"],
                    {**chunk_source, "field": "unit_id", "target": unit_id},
                )
            page_error = _validate_page_range(
                chunk.get("page_range"),
                _integer((document.get("metadata") or {}).get("pdf_pages")) or 0,
            )
            if page_error:
                findings.add(
                    "invalid_page_ranges",
                    INTEGRITY_FINDINGS["invalid_page_ranges"],
                    {
                        **chunk_source,
                        "reason": page_error,
                        "range": chunk.get("page_range"),
                    },
                )
            elif chunk.get("page_range") is not None:
                alignment_items["chunks_aligned"] += 1
                method = (chunk.get("pdf_alignment") or {}).get("method")
                alignment_method_counter[str(method or "range_without_method")] += 1

        chunk_sequences_by_unit: Dict[str, List[Any]] = {}
        for chunk in chunks:
            unit_id = chunk.get("unit_id")
            if unit_id and chunk.get("sequence") is not None:
                chunk_sequences_by_unit.setdefault(str(unit_id), []).append(
                    chunk.get("sequence")
                )
        for unit_id, sequences in chunk_sequences_by_unit.items():
            sequence_keys = [
                json.dumps(
                    sequence,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                for sequence in sequences
            ]
            sequence_values = dict(zip(sequence_keys, sequences))
            duplicate_sequences = sorted(
                (
                    sequence_values[sequence_key]
                    for sequence_key, count in Counter(sequence_keys).items()
                    if count > 1
                ),
                key=lambda value: str(value),
            )
            if duplicate_sequences:
                findings.add(
                    "duplicate_chunk_sequences",
                    INTEGRITY_FINDINGS["duplicate_chunk_sequences"],
                    {
                        **source_with_document,
                        "unit_id": unit_id,
                        "duplicate_sequences": duplicate_sequences,
                    },
                    amount=len(duplicate_sequences),
                )

        for leak in _internal_footnote_leaks(document_id, units, chunks):
            findings.add(
                "internal_footnote_leaks",
                INTEGRITY_FINDINGS["internal_footnote_leaks"],
                {**source_with_document, **leak},
            )

        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        pdf_pages = _integer(metadata.get("pdf_pages")) or 0
        totals["pdf_pages"] += max(0, pdf_pages)
        totals["page_refs"] += len(document.get("page_refs") or [])
        alignment_status_counter[
            str(metadata.get("pdf_alignment_status") or "unknown")
        ] += 1
        source_assets = _integer(metadata.get("source_asset_count")) or 0
        missing_assets = _integer(metadata.get("missing_source_asset_count")) or 0
        totals["source_assets"] += max(0, source_assets)
        totals["missing_assets"] += max(0, missing_assets)
        missing_asset_chunks = [
            chunk
            for chunk in chunks
            if chunk.get("chunk_type") == "source_asset"
            and chunk.get("source_asset_status") == "missing"
        ]
        missing_count = max(missing_assets, len(missing_asset_chunks))
        if missing_count:
            findings.add(
                "missing_assets",
                INTEGRITY_FINDINGS["missing_assets"],
                {
                    **source_with_document,
                    "metadata_count": missing_assets,
                    "chunk_sources": [
                        chunk.get("source_asset")
                        for chunk in missing_asset_chunks[:MAX_EXAMPLES]
                    ],
                },
                amount=missing_count,
            )

        issues = payload.get("extraction_issues")
        if not isinstance(issues, list):
            issues = []
            findings.add(
                "malformed_documents",
                INTEGRITY_FINDINGS["malformed_documents"],
                {**source_with_document, "reason": "extraction_issues is not a list"},
            )
        valid_issues = [issue for issue in issues if isinstance(issue, dict)]
        totals["extraction_issues"] += len(valid_issues)
        for issue in valid_issues:
            issue_type = str(issue.get("issue_type") or "unknown")
            severity = str(issue.get("severity") or "unknown")
            issue_type_counter[issue_type] += 1
            issue_severity_counter[severity] += 1
            if severity.casefold() == "error":
                findings.add(
                    "extraction_error_issues",
                    INTEGRITY_FINDINGS["extraction_error_issues"],
                    {
                        **source_with_document,
                        "issue_type": issue_type,
                        "issue_id": issue.get("issue_id"),
                    },
                )

        candidate = {
            "document": document,
            "issues": valid_issues,
            "source_entry": source_entry,
            "source_path": source["source_path"],
        }
        if _is_case(source_entry, document, ["ersatzbaustoffv"]):
            hard_case_candidates["ersatzbaustoffv"].append(candidate)
        if _is_case(source_entry, document, ["bgbeg", "egbgb"]):
            hard_case_candidates["egbgb"].append(candidate)
        if _is_case(source_entry, document, ["abfkl_rv_2017", "abfklaerv"]):
            hard_case_candidates["abfklaerv"].append(candidate)

    manifest_status_counts = manifest.get("status_counts")
    normalized_manifest_counts = (
        {
            str(key): _integer(value)
            for key, value in manifest_status_counts.items()
        }
        if isinstance(manifest_status_counts, dict)
        else {}
    )
    computed_status_counts = _sorted_counter(status_counter)
    if normalized_manifest_counts != computed_status_counts:
        findings.add(
            "manifest_inconsistencies",
            INTEGRITY_FINDINGS["manifest_inconsistencies"],
            {
                "reason": "status_counts mismatch",
                "manifest": normalized_manifest_counts,
                "computed": computed_status_counts,
            },
        )
    selected_entries = _integer((manifest.get("run") or {}).get("selected_entries"))
    if selected_entries is not None and selected_entries != len(entries):
        findings.add(
            "manifest_inconsistencies",
            INTEGRITY_FINDINGS["manifest_inconsistencies"],
            {
                "reason": "run.selected_entries mismatch",
                "manifest": selected_entries,
                "computed": len(entries),
            },
        )
    manifest_errors = manifest.get("errors")
    error_entry_count = status_counter.get("error", 0)
    if not isinstance(manifest_errors, list) or len(manifest_errors) != error_entry_count:
        findings.add(
            "manifest_inconsistencies",
            INTEGRITY_FINDINGS["manifest_inconsistencies"],
            {
                "reason": "errors list mismatch",
                "manifest": len(manifest_errors)
                if isinstance(manifest_errors, list)
                else None,
                "computed": error_entry_count,
            },
        )

    raw_root = batch_dir / "raw"
    actual_paths = {
        path.resolve()
        for path in raw_root.rglob("*_raw.json")
        if path.is_file()
    } if raw_root.is_dir() else set()
    for path in sorted(actual_paths - expected_paths):
        findings.add(
            "untracked_raw_outputs",
            INTEGRITY_FINDINGS["untracked_raw_outputs"],
            {"source_path": path.relative_to(batch_dir).as_posix()},
        )

    hard_cases = _audit_hard_cases(
        hard_case_candidates,
        findings,
        require_hard_cases,
    )
    failures = findings.failures()
    integrity = {
        code: findings.result(code)
        for code in (
            "duplicate_document_ids",
            "duplicate_unit_ids",
            "duplicate_chunk_ids",
            "duplicate_chunk_sequences",
            "internal_footnote_leaks",
            "missing_assets",
            "zero_unit_documents",
            "invalid_table_shapes",
            "invalid_page_ranges",
            "dangling_references",
            "unreadable_raw_outputs",
        )
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "audit_status": "failed" if failures else "passed",
        "input": {
            "batch_dir": str(batch_dir),
            "manifest_path": str(manifest_path),
            "require_hard_cases": require_hard_cases,
        },
        "manifest": {
            "entries": len(entries),
            "successful_entries": sum(
                status_counter.get(status, 0) for status in SUCCESS_BATCH_STATUSES
            ),
            "raw_files_expected": len(expected_paths),
            "raw_files_found": len(actual_paths),
            "status_counts": computed_status_counts,
            "declared_status_counts": normalized_manifest_counts,
            "errors": error_entry_count,
        },
        "totals": totals,
        "extraction_issues": {
            "by_type": _sorted_counter(issue_type_counter),
            "by_severity": _sorted_counter(issue_severity_counter),
        },
        "alignment": {
            "documents_by_status": _sorted_counter(alignment_status_counter),
            "pdf_pages": totals["pdf_pages"],
            "page_refs": totals["page_refs"],
            "units": {
                "total": alignment_items["units_total"],
                "aligned": alignment_items["units_aligned"],
                "coverage": _ratio(
                    alignment_items["units_aligned"],
                    alignment_items["units_total"],
                ),
            },
            "chunks": {
                "total": alignment_items["chunks_total"],
                "aligned": alignment_items["chunks_aligned"],
                "coverage": _ratio(
                    alignment_items["chunks_aligned"],
                    alignment_items["chunks_total"],
                ),
            },
            "methods": _sorted_counter(alignment_method_counter),
            "absence_is_failure": False,
        },
        "tables": {
            "count": totals["tables"],
            "rows": totals["table_rows"],
            "by_parser": _sorted_counter(table_parser_counter),
            "by_shape": dict(
                sorted(
                    table_shape_counter.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ),
        },
        "integrity": integrity,
        "hard_cases": hard_cases,
        "failure_kind_count": len(failures),
        "failure_occurrence_count": sum(
            int(finding["count"]) for finding in failures
        ),
        "failures": failures,
    }


def _md_cell(value: Any) -> str:
    if value is None:
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _percent(value: Optional[float]) -> str:
    return "—" if value is None else "{:.1%}".format(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the machine report as a concise, human-reviewable summary."""

    totals = report["totals"]
    manifest = report["manifest"]
    alignment = report["alignment"]
    lines = [
        "# GII XML corpus audit",
        "",
        "**Result:** {}  ".format(str(report["audit_status"]).upper()),
        "**Generated:** {}  ".format(report["generated_at"]),
        "**Batch:** `{}`".format(report["input"]["batch_dir"]),
        "",
        "## Corpus",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
        "| Manifest entries | {} |".format(manifest["entries"]),
        "| Successful entries | {} |".format(manifest["successful_entries"]),
        "| Raw files found | {} |".format(manifest["raw_files_found"]),
        "| Documents | {} |".format(totals["documents"]),
        "| Structural units | {} |".format(totals["structural_units"]),
        "| Chunks | {} |".format(totals["chunks"]),
        "| Tables | {} |".format(totals["tables"]),
        "| Table rows | {} |".format(totals["table_rows"]),
        "| Source assets | {} |".format(totals["source_assets"]),
        "| Missing assets | {} |".format(totals["missing_assets"]),
        "| Extraction issues | {} |".format(totals["extraction_issues"]),
        "",
        "## Secondary PDF alignment",
        "",
        "Alignment absence is reported as coverage and does **not** fail the XML audit.",
        "",
        "| Item | Aligned | Total | Coverage |",
        "| --- | ---: | ---: | ---: |",
        "| Units | {} | {} | {} |".format(
            alignment["units"]["aligned"],
            alignment["units"]["total"],
            _percent(alignment["units"]["coverage"]),
        ),
        "| Chunks | {} | {} | {} |".format(
            alignment["chunks"]["aligned"],
            alignment["chunks"]["total"],
            _percent(alignment["chunks"]["coverage"]),
        ),
        "",
        "Document statuses: {}".format(
            ", ".join(
                "`{}`: {}".format(key, value)
                for key, value in alignment["documents_by_status"].items()
            )
            or "none"
        ),
        "",
        "## Tables",
        "",
        "| Shape (columns × rows) | Count |",
        "| --- | ---: |",
    ]
    shapes = list(report["tables"]["by_shape"].items())
    if shapes:
        lines.extend(
            "| {} | {} |".format(_md_cell(shape), count)
            for shape, count in shapes[:30]
        )
    else:
        lines.append("| — | 0 |")
    if len(shapes) > 30:
        lines.append("")
        lines.append("_{} additional shapes are present in the JSON report._".format(
            len(shapes) - 30
        ))

    lines.extend(
        [
            "",
            "## Hard cases",
            "",
            "| Check | Status | Observed |",
            "| --- | --- | --- |",
        ]
    )
    for case in report["hard_cases"].values():
        observed = case.get("observed")
        observed_text = (
            json.dumps(observed, ensure_ascii=False, sort_keys=True)
            if observed is not None
            else "not in this batch"
        )
        lines.append(
            "| {} | {} | {} |".format(
                _md_cell(case["name"]),
                _md_cell(case["status"]),
                _md_cell(observed_text),
            )
        )

    lines.extend(["", "## Invariant failures", ""])
    failures = report["failures"]
    if not failures:
        lines.append("None.")
    else:
        for finding in failures:
            lines.append(
                "- `{}` ({}): {}".format(
                    finding["code"],
                    finding["count"],
                    finding["message"],
                )
            )
            for example in finding["examples"][:3]:
                lines.append(
                    "  - `{}`".format(
                        json.dumps(example, ensure_ascii=False, sort_keys=True)
                    )
                )
    lines.append("")
    return "\n".join(lines)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, str(path))
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, default=DEFAULT_BATCH_DIR)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Aggregate batch manifest (default: BATCH_DIR/manifest.json).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Machine report path (default: BATCH_DIR/audit.json).",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=None,
        help="Human report path (default: BATCH_DIR/audit.md).",
    )
    parser.add_argument(
        "--require-hard-cases",
        action="store_true",
        help="Fail when any of the three named regression documents is absent.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    batch_dir = args.batch_dir.resolve()
    json_out = (args.json_out or (batch_dir / "audit.json")).resolve()
    markdown_out = (args.markdown_out or (batch_dir / "audit.md")).resolve()
    try:
        report = audit_corpus(
            batch_dir,
            args.manifest,
            require_hard_cases=args.require_hard_cases,
        )
    except AuditInputError as exc:
        print("audit input error: {}".format(exc))
        return 2
    atomic_write_text(
        json_out,
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )
    atomic_write_text(markdown_out, render_markdown(report))
    print(
        "{}: {} documents, {} units, {} chunks, {} failure kinds".format(
            report["audit_status"],
            report["totals"]["documents"],
            report["totals"]["structural_units"],
            report["totals"]["chunks"],
            report["failure_kind_count"],
        )
    )
    print("JSON: {}".format(json_out))
    print("Markdown: {}".format(markdown_out))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
