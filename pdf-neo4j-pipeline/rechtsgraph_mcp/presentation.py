"""Compact, stable MCP projections over the full internal retrieval rows.

The SQLite index intentionally retains audit and provenance fields.  Agents do
not need those fields on every call, so the MCP boundary returns a smaller
contract while the full data remains available to local Python callers.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Mapping, Sequence


DOCUMENT_FIELDS = (
    "document_id",
    "document_key",
    "document_global_key",
    "title",
    "canonical_citation",
    "abbreviation",
    "full_citation",
    "source_kind",
    "source_format",
    "is_pdf_fallback",
    "document_type",
    "edition",
    "language",
    "landing_url",
    "evidence",
    "matched_alias",
    "score",
)

UNIT_FIELDS = (
    "unit_id",
    "global_key",
    "document_id",
    "parent_unit_id",
    "unit_type",
    "label",
    "number",
    "title",
    "legal_citation",
    "display_name",
    "sequence",
    "page_start",
    "page_end",
    "depth",
)

PASSAGE_FIELDS = (
    "passage_id",
    "document_id",
    "unit_id",
    "passage_kind",
    "label",
    "legal_citation",
    "page_start",
    "page_end",
    "document_title",
    "canonical_citation",
    "source_kind",
    "evidence",
    "score",
)

def _first_text(*values: Any) -> str | None:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return None


def _short_text(value: Any, max_chars: int = 180) -> str | None:
    text = _first_text(value)
    if text is None or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def minimal_document(value: Mapping[str, Any] | None) -> Dict[str, Any] | None:
    """Return only the human-usable address and label of a document.

    Internal identifiers, provenance, status metadata, and source details remain
    available through ``detail="full"``.  ``document_ref`` deliberately prefers
    a normal abbreviation or corpus key that can be passed back to another MCP
    call without exposing the SQLite primary key in the common case.
    """

    if value is None:
        return None
    reference = _first_text(
        value.get("abbreviation"),
        value.get("document_key"),
        value.get("canonical_citation"),
        value.get("document_global_key"),
        value.get("document_id"),
    )
    result: Dict[str, Any] = {}
    if reference:
        result["document_ref"] = reference
    title = _short_text(
        _first_text(
            value.get("short_title"), value.get("title"), value.get("document_title")
        )
    )
    if title and title != reference:
        result["title"] = title
    return result


def minimal_unit(value: Mapping[str, Any] | None, *, outline: bool = False) -> Dict[str, Any] | None:
    """Return an addressable legal reference without implementation IDs."""

    if value is None:
        return None
    reference = _first_text(
        value.get("legal_citation"),
        value.get("display_name"),
        value.get("label"),
        value.get("global_key"),
        value.get("unit_id"),
    )
    result: Dict[str, Any] = {}
    if reference:
        result["unit_ref"] = reference
    title = _short_text(value.get("title"))
    if title and title not in {reference, value.get("label")}:
        result["title"] = title
    if outline:
        unit_type = _first_text(value.get("unit_type"))
        if unit_type:
            result["type"] = unit_type
        if value.get("depth") is not None:
            result["depth"] = int(value["depth"])
    return result


def _passage_cautions(value: Mapping[str, Any]) -> list[str]:
    evidence = value.get("evidence") or {}
    cautions = evidence.get("cautions") or []
    if isinstance(cautions, str):
        return [cautions]
    return [str(caution) for caution in cautions if caution not in (None, "")]


def minimal_passage(value: Mapping[str, Any], *, include_text: bool = True) -> Dict[str, Any]:
    """Project legal text and citation, retaining only actionable cautions."""

    result: Dict[str, Any] = {}
    citation = _first_text(value.get("legal_citation"), value.get("label"))
    if citation:
        result["citation"] = citation
    if include_text:
        result["text"] = str(value.get("text") or "")
    cautions = _passage_cautions(value)
    if cautions:
        result["cautions"] = cautions
    return result


def _project(value: Mapping[str, Any] | None, fields: Iterable[str]) -> Dict[str, Any] | None:
    if value is None:
        return None
    return {
        field: value[field]
        for field in fields
        if field in value and value[field] not in (None, "")
    }


def compact_document(value: Mapping[str, Any] | None) -> Dict[str, Any] | None:
    return _project(value, DOCUMENT_FIELDS)


def compact_unit(value: Mapping[str, Any] | None) -> Dict[str, Any] | None:
    return _project(value, UNIT_FIELDS)


def compact_passage(value: Mapping[str, Any], *, include_text: bool = True) -> Dict[str, Any]:
    result = _project(value, PASSAGE_FIELDS) or {}
    if include_text:
        result["text"] = str(value.get("text") or "")
    return result


def _search_terms(query: str) -> list[str]:
    return list(dict.fromkeys(
        term.casefold()
        for term in re.findall(r"[0-9A-Za-zÄÖÜäöüß]+", query)
        if len(term) > 1
    ))


def make_snippet(text: str, query: str, max_chars: int) -> tuple[str, bool]:
    """Return a query-centered excerpt without flattening legal line breaks."""

    text = str(text or "")
    max_chars = max(160, min(int(max_chars), 4000))
    if len(text) <= max_chars:
        return text, False
    folded = text.casefold()
    positions = [folded.find(term) for term in _search_terms(query)]
    positions = [position for position in positions if position >= 0]
    focus = min(positions) if positions else 0
    start = max(0, focus - max_chars // 3)
    end = min(len(text), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    if start:
        boundary = text.find(" ", start, min(end, start + 80))
        if boundary >= 0:
            start = boundary + 1
    if end < len(text):
        boundary = text.rfind(" ", max(start, end - 80), end)
        if boundary > start:
            end = boundary
    snippet = text[start:end]
    return ("…" if start else "") + snippet + ("…" if end < len(text) else ""), True


def compact_search_result(
    value: Mapping[str, Any],
    query: str,
    snippet_chars: int,
    *,
    detail: str = "compact",
) -> Dict[str, Any]:
    if detail not in {"compact", "full"}:
        raise ValueError("detail must be compact or full")
    if detail == "full":
        result = compact_passage(value, include_text=False)
    else:
        result = {
            "passage_id": value.get("passage_id"),
            **(minimal_document(value) or {}),
            **minimal_passage(value, include_text=False),
        }
        result = {
            key: item for key, item in result.items()
            if item not in (None, "", [])
        }
    text = str(value.get("text") or "")
    snippet, truncated = make_snippet(text, query, snippet_chars)
    result.update({
        "snippet": snippet,
        "snippet_truncated": truncated,
    })
    if detail == "full":
        result["text_chars"] = len(text)
    return result


def compact_outline(
    payload: Mapping[str, Any], *, detail: str = "compact"
) -> Dict[str, Any]:
    if detail not in {"compact", "full"}:
        raise ValueError("detail must be compact or full")
    document = (
        compact_document(payload.get("document"))
        if detail == "full"
        else minimal_document(payload.get("document"))
    )
    units = (
        [compact_unit(unit) for unit in payload.get("units") or []]
        if detail == "full"
        else [minimal_unit(unit, outline=True) for unit in payload.get("units") or []]
    )
    return {
        "document": document,
        "count": int(payload.get("count") or 0),
        "units": units,
        "truncated": bool(payload.get("truncated")),
    }


def compact_structural_unit(
    payload: Mapping[str, Any], *, detail: str = "compact"
) -> Dict[str, Any]:
    if detail not in {"compact", "full"}:
        raise ValueError("detail must be compact or full")
    requested_subsection = payload.get("requested_subsection")
    matching = payload.get("matching_passages") or []
    all_passages = payload.get("passages") or []
    selected = matching if requested_subsection and matching else all_passages
    if detail == "full":
        return {
            "document": compact_document(payload.get("document")),
            "unit": compact_unit(payload.get("unit")),
            "requested_subsection": requested_subsection,
            "selection": "matching_subsection" if selected is matching else "unit_passages",
            "passage_count": len(selected),
            "passages": [compact_passage(passage) for passage in selected],
            "truncated": bool(payload.get("truncated")),
        }

    result = {
        "document": minimal_document(payload.get("document")),
        "unit": minimal_unit(payload.get("unit")),
        "passage_count": len(selected),
        "passages": [minimal_passage(passage) for passage in selected],
        "truncated": bool(payload.get("truncated")),
    }
    if requested_subsection:
        result["requested_subsection"] = requested_subsection
    return result


def compact_context(
    payload: Mapping[str, Any], *, detail: str = "compact"
) -> Dict[str, Any]:
    if detail not in {"compact", "full"}:
        raise ValueError("detail must be compact or full")
    passages = payload.get("passages") or []
    if detail == "full":
        projected = [compact_passage(passage) for passage in passages]
    else:
        projected = []
        for passage in passages:
            item = {
                "passage_id": passage.get("passage_id"),
                **(minimal_document(passage) or {}),
                **minimal_passage(passage),
            }
            projected.append({
                key: value for key, value in item.items()
                if value not in (None, "", [])
            })
    return {
        "count": len(passages),
        "passages": projected,
        "truncated": bool(payload.get("truncated")),
    }


EXTERNAL_DOCUMENT_FIELDS = (
    "id",
    "title",
    "short_title",
    "document_type",
    "category",
    "jurisdiction",
    "authority",
    "edition",
    "language",
    "publication_date",
    "valid_from",
    "valid_to",
    "priority",
    "access_status",
    "corpus_status",
    "source_kind",
    "landing_url",
    "license_note",
    "graph_mention_count",
    "graph_source_document_count",
    "match_rank",
    "has_valid_local_text",
    "indexed_for_fulltext",
    "retrieval_document_id",
)

EXTERNAL_FILE_FIELDS = (
    "role",
    "local_path",
    "media_type",
    "size_bytes",
    "page_count",
    "validation_status",
    "download_status",
    "final_url",
)


def compact_external_result(value: Mapping[str, Any]) -> Dict[str, Any]:
    result = _project(value, EXTERNAL_DOCUMENT_FIELDS) or {}
    result["aliases"] = list(value.get("aliases") or [])[:20]
    result["files"] = [
        _project(item, EXTERNAL_FILE_FIELDS) or {}
        for item in value.get("files") or []
    ]
    return result


def minimal_external_result(value: Mapping[str, Any]) -> Dict[str, Any]:
    result = {
        "document_ref": value.get("id"),
        "title": _short_text(_first_text(value.get("short_title"), value.get("title"))),
        "document_type": value.get("document_type"),
        "category": value.get("category"),
        "jurisdiction": value.get("jurisdiction"),
        "access_status": value.get("access_status"),
        "corpus_status": value.get("corpus_status"),
        "has_valid_local_text": value.get("has_valid_local_text"),
        "indexed_for_fulltext": value.get("indexed_for_fulltext"),
    }
    return {
        key: item for key, item in result.items()
        if item not in (None, "", [])
    }


def compact_external_search(
    payload: Mapping[str, Any], *, detail: str = "compact"
) -> Dict[str, Any]:
    if detail not in {"compact", "full"}:
        raise ValueError("detail must be compact or full")
    results: Sequence[Mapping[str, Any]] = payload.get("results") or []
    return {
        "query": payload.get("query"),
        "count": len(results),
        "results": [
            compact_external_result(result)
            if detail == "full"
            else minimal_external_result(result)
            for result in results
        ],
    }


__all__ = [
    "compact_context",
    "compact_document",
    "compact_external_search",
    "compact_outline",
    "compact_search_result",
    "compact_structural_unit",
    "compact_unit",
    "make_snippet",
    "minimal_document",
    "minimal_external_result",
]
