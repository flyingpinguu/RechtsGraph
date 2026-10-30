"""Small, conservative evidence projections for retrieval results.

Canonical document metadata remains in ``documents.metadata_json`` for audit.
This module derives only the fields that are useful while reading a hit.  It
does not turn corpus/build dates into legal validity intervals.
"""

from __future__ import annotations

import json
from typing import Any, Mapping


_NOT_IN_FORCE_STATUSES = {
    "expired",
    "not_in_force",
    "repealed",
    "revoked",
}

_UNCERTAIN_ALIGNMENT_STATUSES = {
    "error",
    "low_coverage",
    "not_attempted",
    "partial",
    "unavailable",
}


def parse_metadata_json(value: Any) -> dict[str, Any]:
    """Decode stored metadata without allowing malformed legacy rows to fail."""

    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def normalize_legal_status(value: Any) -> str:
    """Normalize only explicit negative status values.

    EUR-Lex values such as ``none`` describe source fields and are not a fresh
    finding that an act is in force.  Unknown or positive-looking values remain
    unknown because temporal applicability cannot be inferred from this index.
    """

    normalized = (
        str(value or "")
        .strip()
        .casefold()
        .replace(" ", "_")
        .replace("-", "_")
    )
    return "not_in_force" if normalized in _NOT_IN_FORCE_STATUSES else "unknown"


def compact_evidence(
    metadata: Mapping[str, Any] | None,
    row: Mapping[str, Any] | None = None,
    passage_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return compact source, date, legal-status, and PDF-quality evidence."""

    metadata = metadata or {}
    row = row or {}
    passage_metadata = passage_metadata or {}
    source_url = (
        metadata.get("source_url")
        or metadata.get("landing_url")
        or row.get("landing_url")
    )
    source_format = row.get("source_format") or metadata.get("source_format")
    raw_status = metadata.get("legal_status")
    evidence: dict[str, Any] = {
        "legal_status": {
            "raw": raw_status,
            "normalized": normalize_legal_status(raw_status),
        },
        "date_coverage": "unknown",
        "temporal_applicability": "undetermined",
    }
    if source_url not in (None, ""):
        evidence["source_url"] = source_url
    if source_format not in (None, ""):
        evidence["source_format"] = source_format
    consolidation_date = metadata.get("consolidation_date")
    if consolidation_date not in (None, ""):
        evidence["consolidation_date"] = consolidation_date

    alignment_status = metadata.get("pdf_alignment_status")
    if alignment_status not in (None, ""):
        evidence["pdf_alignment_status"] = alignment_status
    extraction_backend = (
        passage_metadata.get("text_extraction_backend")
        or metadata.get("text_extraction_backend")
    )
    if extraction_backend not in (None, ""):
        evidence["text_extraction_backend"] = extraction_backend

    passage_alignment = passage_metadata.get("pdf_alignment")
    alignment_evidence = (
        passage_alignment.get("evidence")
        if isinstance(passage_alignment, Mapping)
        else None
    )
    if alignment_evidence not in (None, ""):
        evidence["pdf_alignment_evidence"] = alignment_evidence

    format_key = str(source_format or "").casefold()
    passage_kind = str(row.get("passage_kind") or "").casefold()
    cautions: list[str] = []
    if "pdf" in format_key or passage_kind == "pdf_fallback":
        cautions.append("pdf_text_extraction")
    if format_key == "eurlex_pdf_original" or passage_kind == "pdf_fallback":
        cautions.append("structure_and_tables_best_effort")
    if str(alignment_status or "").casefold() in _UNCERTAIN_ALIGNMENT_STATUSES:
        cautions.append("pdf_page_alignment_uncertain")
    if alignment_evidence and str(alignment_evidence).casefold() != "direct":
        cautions.append("pdf_page_range_not_directly_aligned")
    if cautions:
        evidence["cautions"] = list(dict.fromkeys(cautions))
    return evidence


__all__ = ["compact_evidence", "normalize_legal_status", "parse_metadata_json"]
