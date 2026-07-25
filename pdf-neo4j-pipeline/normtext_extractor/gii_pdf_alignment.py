"""Align XML-derived legal structure to the official GII PDF page stream.

XML remains authoritative for text, hierarchy, and tables.  This module only
adds PDF evidence: stable page references and best-effort page ranges.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pypdf import PdfReader

try:
    import fitz  # type: ignore
except ImportError:  # pragma: no cover - optional local dependency
    fitz = None


TOKEN_RE = re.compile(r"[a-z0-9§]+", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")
SOFT_LINE_BREAK_RE = re.compile(r"(?<=\w)-\s*\n\s*(?=\w)")
STOPWORDS = {
    "aber",
    "als",
    "am",
    "an",
    "auf",
    "aus",
    "bei",
    "bis",
    "das",
    "dem",
    "den",
    "der",
    "des",
    "die",
    "durch",
    "ein",
    "eine",
    "einem",
    "einen",
    "einer",
    "für",
    "im",
    "in",
    "ist",
    "mit",
    "nach",
    "oder",
    "sich",
    "sind",
    "und",
    "von",
    "vor",
    "zu",
    "zum",
    "zur",
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_str(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def make_id(*parts: Any) -> str:
    return hashlib.sha256(
        "".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:12]


def normalize_alignment_text(value: str) -> str:
    """Normalize PDF/XML text for matching while retaining legal symbols."""
    value = unicodedata.normalize("NFKC", value or "").replace("\x00", "")
    value = value.replace("\u00ad", "")
    value = SOFT_LINE_BREAK_RE.sub("", value)
    value = value.casefold()
    value = re.sub(r"[‐‑‒–—−]", "-", value)
    return SPACE_RE.sub(" ", value).strip()


def alignment_tokens(value: str) -> List[str]:
    return [
        token
        for token in TOKEN_RE.findall(normalize_alignment_text(value))
        if len(token) >= 2 and token not in STOPWORDS
    ]


def _contains_phrase(page_text: str, phrase: str) -> bool:
    phrase = normalize_alignment_text(phrase)
    if not phrase:
        return False
    return bool(
        re.search(
            r"(?<!\w){}(?!\w)".format(re.escape(phrase)),
            page_text,
        )
    )


def _source_order(item: Dict[str, Any], fallback: int) -> Tuple[Any, ...]:
    order = item.get("source_order")
    if isinstance(order, list):
        return tuple(order)
    if isinstance(order, tuple):
        return order
    if isinstance(order, (int, float, str)):
        return (order,)
    sequence = item.get("sequence")
    if isinstance(sequence, (int, float, str)):
        return (sequence,)
    return (fallback,)


def _item_identifier(item: Dict[str, Any]) -> str:
    return str(item.get("chunk_id") or item.get("unit_id") or "")


def _content_text(item: Dict[str, Any]) -> str:
    text = str(item.get("text") or "")
    for prefix in (item.get("label"), item.get("title")):
        prefix_text = str(prefix or "").strip()
        if prefix_text and text.startswith(prefix_text):
            text = text[len(prefix_text) :].lstrip(" \n:-")
    return text


def _anchor_profile(item: Dict[str, Any], tail: bool = False) -> Dict[str, Any]:
    label = str(item.get("label") or "")
    title = str(item.get("title") or "")
    content = _content_text(item)
    selected = content[-500:] if tail else content[:500]
    anchor = " ".join(part for part in (label, title, selected) if part)
    tokens = alignment_tokens(anchor)
    if len(tokens) > 60:
        tokens = tokens[-60:] if tail else tokens[:60]
    phrase_tokens = tokens[-10:] if tail else tokens[:10]
    return {
        "label": label,
        "title": title,
        "anchor": normalize_alignment_text(anchor),
        "tokens": tokens,
        "phrase": " ".join(phrase_tokens),
        "has_content": bool(normalize_alignment_text(content)),
    }


def _page_features(page_objects: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized_pages = [
        normalize_alignment_text(str(page.get("text") or ""))
        for page in page_objects
    ]
    page_token_sets = [set(alignment_tokens(text)) for text in normalized_pages]
    document_frequency: Counter[str] = Counter()
    for tokens in page_token_sets:
        document_frequency.update(tokens)
    page_count = max(len(page_objects), 1)
    idf = {
        token: math.log((page_count + 1) / (count + 1)) + 1.0
        for token, count in document_frequency.items()
    }
    return [
        {
            "page": page,
            "normalized": normalized,
            "tokens": tokens,
            "idf": idf,
        }
        for page, normalized, tokens in zip(
            page_objects,
            normalized_pages,
            page_token_sets,
        )
    ]


def _score_profile(
    profile: Dict[str, Any],
    page: Dict[str, Any],
) -> Tuple[float, str]:
    tokens = list(dict.fromkeys(profile["tokens"]))
    page_tokens = page["tokens"]
    idf = page["idf"]
    total_weight = sum(idf.get(token, 1.0) for token in tokens)
    matched_weight = sum(
        idf.get(token, 1.0) for token in tokens if token in page_tokens
    )
    coverage = matched_weight / total_weight if total_weight else 0.0
    label_match = _contains_phrase(page["normalized"], profile["label"])
    title_match = _contains_phrase(page["normalized"], profile["title"])
    phrase_match = (
        len(profile["phrase"]) >= 16
        and profile["phrase"] in page["normalized"]
    )

    if profile["has_content"] and tokens:
        score = 0.72 * coverage
        score += 0.12 if label_match else 0.0
        score += 0.08 if title_match else 0.0
        score += 0.18 if phrase_match else 0.0
    else:
        score = 0.55 * coverage
        score += 0.30 if label_match else 0.0
        score += 0.25 if title_match else 0.0
    method_parts = []
    if phrase_match:
        method_parts.append("phrase")
    if label_match:
        method_parts.append("label")
    if title_match:
        method_parts.append("title")
    if coverage:
        method_parts.append("idf_tokens")
    return min(score, 1.0), "+".join(method_parts) or "none"


def _minimum_score(profile: Dict[str, Any]) -> float:
    token_count = len(set(profile["tokens"]))
    if profile["has_content"] and token_count >= 8:
        return 0.28
    if profile["has_content"] and token_count:
        return 0.34
    return 0.48


def _best_page(
    item: Dict[str, Any],
    pages: Sequence[Dict[str, Any]],
    start_index: int,
    end_index: Optional[int] = None,
    tail: bool = False,
) -> Optional[Dict[str, Any]]:
    profile = _anchor_profile(item, tail=tail)
    if not profile["tokens"] and not profile["label"] and not profile["title"]:
        return None
    upper = len(pages) - 1 if end_index is None else min(end_index, len(pages) - 1)
    lower = max(0, min(start_index, upper)) if pages else 0
    best: Optional[Dict[str, Any]] = None
    for index in range(lower, upper + 1):
        score, method = _score_profile(profile, pages[index])
        candidate = {
            "page_index": index,
            "page_number": index + 1,
            "score": score,
            "method": method,
            "anchor_preview": profile["anchor"][:160],
        }
        if best is None or score > best["score"] + 1e-9:
            best = candidate
    if best is None or best["score"] < _minimum_score(profile):
        return None
    return best


def _set_alignment(
    item: Dict[str, Any],
    start: int,
    end: int,
    page_objects: Sequence[Dict[str, Any]],
    method: str,
    confidence: float,
    anchor_preview: str = "",
) -> None:
    item["page_range"] = {"start": start, "end": max(start, end)}
    item["pdf_alignment"] = {
        "method": method,
        "confidence": round(float(confidence), 4),
        "anchor_preview": anchor_preview,
    }
    if "chunk_id" in item:
        item["page_id"] = page_objects[start - 1]["page_id"]


def _align_items(
    items: Sequence[Dict[str, Any]],
    pages: Sequence[Dict[str, Any]],
    page_objects: Sequence[Dict[str, Any]],
    bounds_by_parent: Optional[Dict[str, Tuple[int, int]]] = None,
) -> Tuple[int, List[str]]:
    ordered = sorted(
        enumerate(items),
        key=lambda pair: _source_order(pair[1], pair[0]),
    )
    starts: Dict[str, Dict[str, Any]] = {}
    unmatched: List[str] = []
    previous_page_index = 0

    for _fallback, item in ordered:
        identifier = _item_identifier(item)
        lower = previous_page_index
        upper: Optional[int] = None
        if bounds_by_parent is not None:
            parent = str(item.get("unit_id") or "")
            bounds = bounds_by_parent.get(parent)
            if bounds:
                lower = max(0, bounds[0] - 1)
                upper = bounds[1] - 1
        match = _best_page(item, pages, lower, upper)
        if match is None:
            unmatched.append(identifier)
            continue
        starts[identifier] = match
        if bounds_by_parent is None:
            previous_page_index = max(previous_page_index, match["page_index"])

    matched_order = [
        (item, starts[_item_identifier(item)])
        for _fallback, item in ordered
        if _item_identifier(item) in starts
    ]
    for index, (item, start_match) in enumerate(matched_order):
        identifier = _item_identifier(item)
        next_start = (
            matched_order[index + 1][1]["page_index"]
            if index + 1 < len(matched_order)
            else len(pages) - 1
        )
        upper = max(start_match["page_index"], next_start)
        if bounds_by_parent is not None:
            parent_bounds = bounds_by_parent.get(str(item.get("unit_id") or ""))
            if parent_bounds:
                upper = min(upper, parent_bounds[1] - 1)
        tail_match = _best_page(
            item,
            pages,
            start_match["page_index"],
            upper,
            tail=True,
        )
        end_index = (
            tail_match["page_index"]
            if tail_match is not None
            else start_match["page_index"]
        )
        confidence = start_match["score"]
        method = start_match["method"]
        if tail_match is not None and tail_match["page_index"] > start_match["page_index"]:
            confidence = (confidence + tail_match["score"]) / 2
            method += "+tail"
        _set_alignment(
            item,
            start_match["page_number"],
            end_index + 1,
            page_objects,
            method,
            confidence,
            start_match["anchor_preview"],
        )
        starts[identifier] = start_match
    return len(starts), unmatched


def _inherit_unit_ranges(document: Dict[str, Any], page_objects: Sequence[Dict[str, Any]]) -> int:
    units = document.get("structural_units") or []
    by_id = {
        unit.get("unit_id"): unit
        for unit in units
        if unit.get("unit_id")
    }
    inherited = 0
    for _iteration in range(4):
        changed = False
        for unit in reversed(units):
            if isinstance(unit.get("page_range"), dict):
                continue
            children = [
                by_id.get(child_id)
                for child_id in unit.get("child_unit_ids") or []
            ]
            ranges = [
                child["page_range"]
                for child in children
                if child and isinstance(child.get("page_range"), dict)
            ]
            if not ranges:
                continue
            start = min(page_range["start"] for page_range in ranges)
            end = max(page_range["end"] for page_range in ranges)
            _set_alignment(
                unit,
                start,
                end,
                page_objects,
                "child_inherited",
                0.35,
            )
            inherited += 1
            changed = True
        if not changed:
            break
    return inherited


def _inherit_chunk_ranges(document: Dict[str, Any], page_objects: Sequence[Dict[str, Any]]) -> int:
    unit_ranges = {
        unit.get("unit_id"): unit.get("page_range")
        for unit in document.get("structural_units") or []
        if isinstance(unit.get("page_range"), dict)
    }
    inherited = 0
    for chunk in document.get("chunks") or []:
        if isinstance(chunk.get("page_range"), dict):
            continue
        page_range = unit_ranges.get(chunk.get("unit_id"))
        if not page_range:
            continue
        _set_alignment(
            chunk,
            page_range["start"],
            page_range["end"],
            page_objects,
            "parent_inherited",
            0.25,
        )
        inherited += 1
    return inherited


def _alignment_issue(
    document_id: str,
    issue_type: str,
    severity: str,
    description: str,
    evidence: Any,
) -> Dict[str, Any]:
    return {
        "issue_id": "issue_{}".format(
            make_id(document_id, issue_type, json.dumps(evidence, sort_keys=True))
        ),
        "document_id": document_id,
        "unit_id": None,
        "chunk_id": None,
        "issue_type": issue_type,
        "severity": severity,
        "description": description,
        "evidence": evidence,
        "corrected_value": None,
        "review_status": "open",
    }


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _page_references(
    page_objects: Sequence[Dict[str, Any]],
    document_key: str,
    output_base_dir: Optional[os.PathLike[str] | str],
    pages_root_dir: Optional[os.PathLike[str] | str],
) -> List[Dict[str, Any]]:
    output_base = Path(output_base_dir).resolve() if output_base_dir else None
    pages_root = (
        Path(pages_root_dir).resolve()
        if pages_root_dir
        else (output_base / "pages" if output_base else None)
    )
    references: List[Dict[str, Any]] = []
    for page in page_objects:
        reference = {
            "page_id": page["page_id"],
            "page_number": page["page_number"],
            "pdf_page_index": page["pdf_page_index"],
            "text_sha256": page["text_sha256"],
            "text_extraction_backend": page["text_extraction_backend"],
        }
        if pages_root is not None:
            page_path = (
                pages_root
                / document_key
                / "page_{:03d}.json".format(page["page_number"])
            )
            _atomic_write_json(page_path, page)
            reference["path"] = (
                os.path.relpath(page_path, output_base)
                if output_base is not None
                else str(page_path)
            )
        references.append(reference)
    return references


def align_document_to_page_texts(
    document: Dict[str, Any],
    page_texts: Sequence[str],
    source_pdf: Optional[str] = None,
    pdf_sha256: Optional[str] = None,
    backends: Optional[Sequence[str]] = None,
    output_base_dir: Optional[os.PathLike[str] | str] = None,
    pages_root_dir: Optional[os.PathLike[str] | str] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Return an XML document enriched with PDF pages and alignment evidence."""
    aligned = copy.deepcopy(document)
    issues: List[Dict[str, Any]] = []
    document_id = str(aligned.get("document_id") or "")
    if not page_texts:
        aligned.setdefault("metadata", {})["pdf_alignment_status"] = "unavailable"
        issues.append(
            _alignment_issue(
                document_id,
                "pdf_alignment_no_pages",
                "warning",
                "The secondary PDF produced no extractable pages.",
                source_pdf or "",
            )
        )
        return aligned, issues

    pdf_identity = pdf_sha256 or source_pdf or "pdf"
    page_objects = []
    for index, text in enumerate(page_texts):
        page_objects.append(
            {
                "page_id": "pg_{}_{}".format(
                    make_id(document_id, pdf_identity, index),
                    str(index).zfill(3),
                ),
                "page_number": index + 1,
                "pdf_page_index": index,
                "text": text,
                "text_sha256": sha256_str(text),
                "text_extraction_backend": (
                    backends[index]
                    if backends is not None and index < len(backends)
                    else "unknown"
                ),
            }
        )
    pages = _page_features(page_objects)

    units = aligned.get("structural_units") or []
    matched_units, initially_unmatched_units = _align_items(
        units,
        pages,
        page_objects,
    )
    inherited_units = _inherit_unit_ranges(aligned, page_objects)
    unit_bounds = {
        unit.get("unit_id"): (
            unit["page_range"]["start"],
            unit["page_range"]["end"],
        )
        for unit in units
        if unit.get("unit_id") and isinstance(unit.get("page_range"), dict)
    }
    chunks = aligned.get("chunks") or []
    matched_chunks, initially_unmatched_chunks = _align_items(
        chunks,
        pages,
        page_objects,
        bounds_by_parent=unit_bounds,
    )
    inherited_chunks = _inherit_chunk_ranges(aligned, page_objects)

    page_refs = _page_references(
        page_objects,
        str(aligned.get("document_key") or document_id or "document"),
        output_base_dir,
        pages_root_dir,
    )
    aligned["pages"] = page_refs
    aligned["page_refs"] = page_refs
    if source_pdf:
        aligned["source_pdf"] = Path(source_pdf).name

    aligned_unit_count = sum(
        isinstance(unit.get("page_range"), dict) for unit in units
    )
    aligned_chunk_count = sum(
        isinstance(chunk.get("page_range"), dict) for chunk in chunks
    )
    unit_coverage = aligned_unit_count / len(units) if units else 1.0
    chunk_coverage = aligned_chunk_count / len(chunks) if chunks else 1.0
    direct_unit_coverage = matched_units / len(units) if units else 1.0
    direct_chunk_coverage = matched_chunks / len(chunks) if chunks else 1.0
    if unit_coverage >= 0.95 and chunk_coverage >= 0.95:
        status = "aligned"
    elif unit_coverage >= 0.50 or chunk_coverage >= 0.50:
        status = "partial"
    else:
        status = "low_coverage"

    metadata = aligned.setdefault("metadata", {})
    metadata.update(
        {
            "pdf_alignment_status": status,
            "pdf_pages": len(page_objects),
            "source_pdf_sha256": pdf_sha256,
            "pdf_alignment": {
                "unit_count": len(units),
                "direct_unit_matches": matched_units,
                "inherited_unit_matches": inherited_units,
                "aligned_unit_count": aligned_unit_count,
                "unit_coverage": round(unit_coverage, 4),
                "direct_unit_coverage": round(direct_unit_coverage, 4),
                "chunk_count": len(chunks),
                "direct_chunk_matches": matched_chunks,
                "inherited_chunk_matches": inherited_chunks,
                "aligned_chunk_count": aligned_chunk_count,
                "chunk_coverage": round(chunk_coverage, 4),
                "direct_chunk_coverage": round(direct_chunk_coverage, 4),
            },
        }
    )
    remaining_units = [
        identifier
        for identifier in initially_unmatched_units
        if not isinstance(
            next(
                (
                    unit.get("page_range")
                    for unit in units
                    if unit.get("unit_id") == identifier
                ),
                None,
            ),
            dict,
        )
    ]
    remaining_chunks = [
        identifier
        for identifier in initially_unmatched_chunks
        if not isinstance(
            next(
                (
                    chunk.get("page_range")
                    for chunk in chunks
                    if chunk.get("chunk_id") == identifier
                ),
                None,
            ),
            dict,
        )
    ]
    if remaining_units or remaining_chunks:
        issues.append(
            _alignment_issue(
                document_id,
                "pdf_alignment_unmatched_items",
                "warning",
                "Some XML-derived items could not be assigned a PDF page.",
                {
                    "unmatched_unit_count": len(remaining_units),
                    "unmatched_chunk_count": len(remaining_chunks),
                    "unit_examples": remaining_units[:20],
                    "chunk_examples": remaining_chunks[:20],
                },
            )
        )
    return aligned, issues


def extract_pdf_page_texts(
    pdf_path: os.PathLike[str] | str,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """Extract one text stream per page, choosing the richer local backend."""
    path = Path(pdf_path)
    raw = path.read_bytes()
    reader = PdfReader(io.BytesIO(raw))
    fitz_texts: List[str] = []
    if fitz is not None:
        with fitz.open(stream=raw, filetype="pdf") as fitz_document:
            fitz_texts = [
                (page.get_text("text") or "").replace("\x00", "")
                for page in fitz_document
            ]

    texts: List[str] = []
    backends: List[str] = []
    backend_counts: Counter[str] = Counter()
    for index, page in enumerate(reader.pages):
        pypdf_text = (page.extract_text() or "").replace("\x00", "")
        fitz_text = fitz_texts[index] if index < len(fitz_texts) else ""
        pypdf_normalized = normalize_alignment_text(pypdf_text)
        fitz_normalized = normalize_alignment_text(fitz_text)
        if fitz_normalized and len(fitz_normalized) > len(pypdf_normalized) * 1.05:
            selected, backend = fitz_text, "pymupdf"
        else:
            selected, backend = pypdf_text, "pypdf"
        texts.append(selected)
        backends.append(backend)
        backend_counts[backend] += 1
    return texts, backends, {
        "sha256": sha256_bytes(raw),
        "bytes": len(raw),
        "page_count": len(texts),
        "backend_counts": dict(sorted(backend_counts.items())),
        "secondary_backend": "pymupdf" if fitz is not None else None,
    }


def align_document_with_pdf(
    document: Dict[str, Any],
    pdf_path: os.PathLike[str] | str,
    output_base_dir: Optional[os.PathLike[str] | str] = None,
    pages_root_dir: Optional[os.PathLike[str] | str] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Extract PDF pages and add secondary page evidence to an XML document."""
    texts, backends, pdf_metadata = extract_pdf_page_texts(pdf_path)
    aligned, issues = align_document_to_page_texts(
        document,
        texts,
        source_pdf=str(pdf_path),
        pdf_sha256=pdf_metadata["sha256"],
        backends=backends,
        output_base_dir=output_base_dir,
        pages_root_dir=pages_root_dir,
    )
    aligned.setdefault("metadata", {}).update(
        {
            "source_pdf_bytes": pdf_metadata["bytes"],
            "pdf_text_backend_counts": pdf_metadata["backend_counts"],
            "secondary_pdf_backend": pdf_metadata["secondary_backend"],
        }
    )
    return aligned, issues
