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
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pypdf import PdfReader

try:
    import fitz  # type: ignore
except ImportError:  # pragma: no cover - optional local dependency
    fitz = None


TOKEN_RE = re.compile(r"[a-z0-9§]+", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")
SOFT_LINE_BREAK_RE = re.compile(r"(?<=\w)-\s*\n\s*(?=\w)")
ALIGNMENT_VERSION = "2.0.0"
TOP_K_CANDIDATES = 8
MAX_CANDIDATE_POOL = 48
MAX_PROFILE_TOKENS = 56
JUMP_PENALTY_PER_PAGE = 0.0008
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
    def sortable(value: Any) -> Tuple[int, Any]:
        if isinstance(value, bool):
            return (0, int(value))
        if isinstance(value, (int, float)):
            return (0, float(value))
        return (1, str(value))

    order = item.get("source_order")
    if isinstance(order, list):
        return tuple(sortable(value) for value in order)
    if isinstance(order, tuple):
        return tuple(sortable(value) for value in order)
    if isinstance(order, (int, float, str)):
        return (sortable(order),)
    sequence = item.get("sequence")
    if isinstance(sequence, (int, float, str)):
        return (sortable(sequence),)
    return (sortable(fallback),)


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
    content_tokens = alignment_tokens(content)
    selected_tokens = (
        content_tokens[-MAX_PROFILE_TOKENS:]
        if tail
        else content_tokens[:MAX_PROFILE_TOKENS]
    )
    context_tokens = alignment_tokens(" ".join((label, title)))
    tokens = list(selected_tokens)
    for token in context_tokens:
        if token not in tokens:
            tokens.append(token)
    phrase_tokens = (
        selected_tokens[-8:] if tail else selected_tokens[:8]
    )
    ngrams = {
        tuple(selected_tokens[index : index + 3])
        for index in range(max(0, len(selected_tokens) - 2))
    }
    normalized_content = normalize_alignment_text(content)
    meaningful_content = (
        len(selected_tokens) >= 4
        and len(normalized_content) >= 24
    ) or (
        len(selected_tokens) >= 3
        and len(normalized_content) >= 24
        and max((len(token) for token in selected_tokens), default=0) >= 10
    ) or (
        len(selected_tokens) >= 1
        and len(normalized_content) >= 8
        and bool(context_tokens)
    )
    return {
        "label": label,
        "title": title,
        "anchor": normalize_alignment_text(
            " ".join(part for part in (label, title, content) if part)
        ),
        "content": normalized_content,
        "content_tokens": selected_tokens,
        "tokens": tokens,
        "phrase": " ".join(phrase_tokens),
        "ngrams": ngrams,
        "has_content": bool(normalized_content),
        "meaningful_content": meaningful_content,
    }


def _toc_penalty(normalized: str, page_index: int) -> float:
    explicit_toc = any(
        marker in normalized
        for marker in (
            "inhaltsübersicht",
            "inhaltsverzeichnis",
            "übersicht über den inhalt",
        )
    )
    provision_references = len(
        re.findall(r"(?:^|\s)(?:§{1,2}|art(?:ikel)?\.?)\s*\d", normalized)
    )
    dotted_leaders = len(re.findall(r"\.{3,}\s*\d+", normalized))
    if explicit_toc:
        return 0.24
    if page_index < 12 and (provision_references >= 8 or dotted_leaders >= 4):
        return 0.14
    return 0.0


def _page_features(page_objects: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    normalized_pages = [
        normalize_alignment_text(str(page.get("text") or ""))
        for page in page_objects
    ]
    page_token_sequences = [
        alignment_tokens(text) for text in normalized_pages
    ]
    page_token_sets = [set(tokens) for tokens in page_token_sequences]
    document_frequency: Counter[str] = Counter()
    for tokens in page_token_sets:
        document_frequency.update(tokens)
    page_count = max(len(page_objects), 1)
    idf = {
        token: math.log((page_count + 1) / (count + 1)) + 1.0
        for token, count in document_frequency.items()
    }
    features: List[Dict[str, Any]] = []
    token_postings: Dict[str, List[int]] = defaultdict(list)
    ngram_postings: Dict[Tuple[str, str, str], List[int]] = defaultdict(list)
    for index, (page, normalized, token_sequence, tokens) in enumerate(
        zip(
            page_objects,
            normalized_pages,
            page_token_sequences,
            page_token_sets,
        )
    ):
        ngrams = {
            tuple(token_sequence[offset : offset + 3])
            for offset in range(max(0, len(token_sequence) - 2))
        }
        feature = {
            "page": page,
            "normalized": normalized,
            "token_sequence": token_sequence,
            "token_text": " ".join(token_sequence),
            "tokens": tokens,
            "ngrams": ngrams,
            "idf": idf,
            "toc_penalty": _toc_penalty(normalized, index),
        }
        features.append(feature)
        for token in tokens:
            token_postings[token].append(index)
        for ngram in ngrams:
            ngram_postings[ngram].append(index)
    return {
        "pages": features,
        "idf": idf,
        "token_postings": token_postings,
        "ngram_postings": ngram_postings,
    }


def _score_profile(
    profile: Dict[str, Any],
    page: Dict[str, Any],
) -> Tuple[float, str]:
    tokens = list(dict.fromkeys(profile["content_tokens"]))
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
        and profile["phrase"] in page["token_text"]
    )
    profile_ngrams = profile["ngrams"]
    matched_ngrams = profile_ngrams.intersection(page["ngrams"])
    ngram_coverage = (
        len(matched_ngrams) / len(profile_ngrams)
        if profile_ngrams
        else 0.0
    )
    ngram_strength = min(1.0, len(matched_ngrams) / 5.0)

    score = 0.52 * coverage
    score += 0.18 * ngram_coverage
    score += 0.12 * ngram_strength
    score += 0.15 if phrase_match else 0.0
    score += 0.025 if label_match else 0.0
    score += 0.015 if title_match else 0.0
    score -= page.get("toc_penalty", 0.0) * (
        0.35 if phrase_match and len(matched_ngrams) >= 3 else 1.0
    )
    method_parts = []
    if phrase_match:
        method_parts.append("phrase")
    if label_match:
        method_parts.append("label")
    if title_match:
        method_parts.append("title")
    if matched_ngrams:
        method_parts.append("ordered_ngrams")
    if coverage:
        method_parts.append("idf_tokens")
    if page.get("toc_penalty", 0.0):
        method_parts.append("toc_penalized")
    return max(0.0, min(score, 1.0)), "+".join(method_parts) or "none"


def _minimum_score(profile: Dict[str, Any]) -> float:
    token_count = len(set(profile["content_tokens"]))
    if token_count >= 20:
        return 0.43
    if token_count >= 8:
        return 0.49
    if token_count <= 2:
        return 0.50
    return 0.56


def _candidate_page_indices(
    profile: Dict[str, Any],
    page_index: Dict[str, Any],
    start_index: int = 0,
    end_index: Optional[int] = None,
) -> List[int]:
    pages = page_index["pages"]
    if not pages:
        return []
    lower = max(0, start_index)
    upper = len(pages) - 1 if end_index is None else min(
        end_index,
        len(pages) - 1,
    )
    if lower > upper:
        return []

    votes: Counter[int] = Counter()
    idf = page_index["idf"]
    unique_tokens = list(dict.fromkeys(profile["content_tokens"]))
    rare_tokens = sorted(
        unique_tokens,
        key=lambda token: (-idf.get(token, 1.0), token),
    )[:12]
    for token in rare_tokens:
        weight = idf.get(token, 1.0)
        for index in page_index["token_postings"].get(token, ()):
            if lower <= index <= upper:
                votes[index] += weight
    for ngram in profile["ngrams"]:
        ngram_weight = 3.0 + sum(idf.get(token, 1.0) for token in ngram) / 3.0
        for index in page_index["ngram_postings"].get(ngram, ()):
            if lower <= index <= upper:
                votes[index] += ngram_weight
    ranked = sorted(votes, key=lambda index: (-votes[index], index))
    return ranked[:MAX_CANDIDATE_POOL]


def _top_candidates(
    profile: Dict[str, Any],
    page_index: Dict[str, Any],
    start_index: int = 0,
    end_index: Optional[int] = None,
    limit: int = TOP_K_CANDIDATES,
) -> List[Dict[str, Any]]:
    if not profile["meaningful_content"]:
        return []
    pages = page_index["pages"]
    candidates: List[Dict[str, Any]] = []
    for index in _candidate_page_indices(
        profile,
        page_index,
        start_index=start_index,
        end_index=end_index,
    ):
        score, method = _score_profile(profile, pages[index])
        candidates.append(
            {
            "page_index": index,
            "page_number": index + 1,
            "score": score,
            "method": method,
            "anchor_preview": profile["anchor"][:160],
            }
        )
    candidates.sort(
        key=lambda candidate: (-candidate["score"], candidate["page_index"])
    )
    if not candidates:
        return []
    threshold = _minimum_score(profile)
    top_score = candidates[0]["score"]
    accepted = [
        candidate
        for candidate in candidates
        if candidate["score"] >= threshold
    ]
    if not accepted:
        return []
    second_score = accepted[1]["score"] if len(accepted) > 1 else None
    ambiguous = (
        second_score is not None
        and top_score - second_score < 0.045
    )
    strong_ordered_evidence = (
        "phrase" in accepted[0]["method"]
        and "ordered_ngrams" in accepted[0]["method"]
    )
    if (
        ambiguous
        and not strong_ordered_evidence
        and top_score < threshold + 0.07
    ):
        return []
    for candidate in accepted:
        candidate["minimum_score"] = threshold
        candidate["ambiguous"] = ambiguous
    return accepted[:limit]


def _best_page(
    item: Dict[str, Any],
    pages: Sequence[Dict[str, Any]] | Dict[str, Any],
    start_index: int,
    end_index: Optional[int] = None,
    tail: bool = False,
) -> Optional[Dict[str, Any]]:
    """Compatibility helper returning a conservative best candidate."""
    page_index = (
        pages
        if isinstance(pages, dict)
        else {
            "pages": list(pages),
            "idf": pages[0]["idf"] if pages else {},
            "token_postings": {
                token: [
                    index
                    for index, page in enumerate(pages)
                    if token in page["tokens"]
                ]
                for token in {
                    token
                    for page in pages
                    for token in page["tokens"]
                }
            },
            "ngram_postings": {
                ngram: [
                    index
                    for index, page in enumerate(pages)
                    if ngram in page.get("ngrams", set())
                ]
                for ngram in {
                    ngram
                    for page in pages
                    for ngram in page.get("ngrams", set())
                }
            },
        }
    )
    candidates = _top_candidates(
        _anchor_profile(item, tail=tail),
        page_index,
        start_index=start_index,
        end_index=end_index,
        limit=1,
    )
    return candidates[0] if candidates else None


def _set_alignment(
    item: Dict[str, Any],
    start: int,
    end: int,
    page_objects: Sequence[Dict[str, Any]],
    method: str,
    confidence: float,
    anchor_preview: str = "",
    evidence: str = "direct",
) -> None:
    item["page_range"] = {"start": start, "end": max(start, end)}
    item["pdf_alignment"] = {
        "method": method,
        "evidence": evidence,
        "confidence": round(float(confidence), 4),
        "anchor_preview": anchor_preview,
    }
    if "chunk_id" in item:
        item["page_id"] = page_objects[start - 1]["page_id"]


def _entry_sort_key(entry: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        _source_order(entry["item"], entry["fallback"]),
        0 if entry["kind"] == "unit" else 1,
        entry["fallback"],
        entry["identifier"],
    )


def _direct_alignment_entries(
    document: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    units = document.get("structural_units") or []
    chunks = document.get("chunks") or []
    units_by_id = {
        str(unit.get("unit_id")): unit
        for unit in units
        if unit.get("unit_id")
    }
    entries: List[Dict[str, Any]] = []
    alignable_chunks_by_unit: Dict[str, int] = Counter()
    profiles_by_identifier: Dict[str, Dict[str, Any]] = {}

    for fallback, chunk in enumerate(chunks):
        identifier = str(chunk.get("chunk_id") or "")
        parent = str(chunk.get("unit_id") or "")
        profile = _anchor_profile(chunk)
        if (
            not identifier
            or parent not in units_by_id
            or str(chunk.get("chunk_type") or "") == "source_asset"
            or not profile["meaningful_content"]
        ):
            continue
        alignable_chunks_by_unit[parent] += 1
        profiles_by_identifier[identifier] = profile
        entries.append(
            {
                "kind": "chunk",
                "item": chunk,
                "identifier": identifier,
                "profile": profile,
                "fallback": fallback,
            }
        )

    unit_fallback_offset = len(chunks)
    for fallback, unit in enumerate(units):
        identifier = str(unit.get("unit_id") or "")
        profile = _anchor_profile(unit)
        if (
            not identifier
            or alignable_chunks_by_unit.get(identifier)
            or not profile["meaningful_content"]
        ):
            continue
        profiles_by_identifier[identifier] = profile
        entries.append(
            {
                "kind": "unit",
                "item": unit,
                "identifier": identifier,
                "profile": profile,
                "fallback": unit_fallback_offset + fallback,
            }
        )
    entries.sort(key=_entry_sort_key)
    return entries, profiles_by_identifier


def _better_node(
    left: Optional[int],
    right: Optional[int],
    nodes: Sequence[Dict[str, Any]],
    adjusted: bool = False,
) -> Optional[int]:
    if left is None:
        return right
    if right is None:
        return left
    score_key = "adjusted_score" if adjusted else "path_score"
    left_node = nodes[left]
    right_node = nodes[right]
    difference = left_node[score_key] - right_node[score_key]
    if abs(difference) > 1e-12:
        return left if difference > 0 else right
    if left_node["match_count"] != right_node["match_count"]:
        return left if left_node["match_count"] > right_node["match_count"] else right
    if left_node["page_index"] != right_node["page_index"]:
        return left if left_node["page_index"] < right_node["page_index"] else right
    return left if left < right else right


def _monotone_matches(
    entries: Sequence[Dict[str, Any]],
    page_index: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Select a monotone subset of top-K candidates with a sparse DP."""
    page_count = len(page_index["pages"])
    if not entries or not page_count:
        return {}, [entry["identifier"] for entry in entries]
    tree: List[Optional[int]] = [None] * (page_count + 1)
    nodes: List[Dict[str, Any]] = []

    def query(page: int) -> Optional[int]:
        position = page + 1
        best: Optional[int] = None
        while position > 0:
            best = _better_node(best, tree[position], nodes, adjusted=True)
            position -= position & -position
        return best

    def update(page: int, node_id: int) -> None:
        position = page + 1
        while position <= page_count:
            tree[position] = _better_node(
                tree[position],
                node_id,
                nodes,
                adjusted=True,
            )
            position += position & -position

    for item_index, entry in enumerate(entries):
        candidates = _top_candidates(entry["profile"], page_index)
        pending: List[int] = []
        for rank, candidate in enumerate(candidates):
            page = candidate["page_index"]
            predecessor = query(page)
            transition_score = 0.0
            match_count = 1
            if predecessor is not None:
                previous = nodes[predecessor]
                transition_score = previous["path_score"] - (
                    JUMP_PENALTY_PER_PAGE
                    * max(0, page - previous["page_index"])
                )
                match_count = previous["match_count"] + 1
            ambiguity_penalty = 0.055 if candidate["ambiguous"] else 0.0
            utility = (
                0.30
                + candidate["score"]
                - candidate["minimum_score"]
                - ambiguity_penalty
            )
            node = {
                "item_index": item_index,
                "identifier": entry["identifier"],
                "page_index": page,
                "candidate": candidate,
                "path_score": transition_score + utility,
                "match_count": match_count,
                "predecessor": predecessor,
                "rank": rank,
            }
            node["adjusted_score"] = (
                node["path_score"] + JUMP_PENALTY_PER_PAGE * page
            )
            nodes.append(node)
            pending.append(len(nodes) - 1)
        for node_id in pending:
            update(nodes[node_id]["page_index"], node_id)

    best = query(page_count - 1)
    selected: Dict[str, Dict[str, Any]] = {}
    while best is not None:
        node = nodes[best]
        selected[node["identifier"]] = node["candidate"]
        best = node["predecessor"]
    unmatched = [
        entry["identifier"]
        for entry in entries
        if entry["identifier"] not in selected
    ]
    return selected, unmatched


def _apply_direct_matches(
    entries: Sequence[Dict[str, Any]],
    selected: Dict[str, Dict[str, Any]],
    page_index: Dict[str, Any],
    page_objects: Sequence[Dict[str, Any]],
) -> Tuple[int, int]:
    selected_in_order = [
        (entry, selected[entry["identifier"]])
        for entry in entries
        if entry["identifier"] in selected
    ]
    direct_units = 0
    direct_chunks = 0
    for index, (entry, start_match) in enumerate(selected_in_order):
        next_start = (
            selected_in_order[index + 1][1]["page_index"]
            if index + 1 < len(selected_in_order)
            else len(page_index["pages"]) - 1
        )
        upper = max(start_match["page_index"], next_start)
        tail_profile = _anchor_profile(entry["item"], tail=True)
        tail_candidates = _top_candidates(
            tail_profile,
            page_index,
            start_index=start_match["page_index"],
            end_index=upper,
        )
        end_match: Optional[Dict[str, Any]] = None
        if tail_candidates:
            best_tail_score = tail_candidates[0]["score"]
            near_best = [
                candidate
                for candidate in tail_candidates
                if candidate["score"] >= best_tail_score - 0.06
            ]
            end_match = min(
                near_best,
                key=lambda candidate: (
                    candidate["page_index"],
                    -candidate["score"],
                ),
            )
        end_index = (
            max(start_match["page_index"], end_match["page_index"])
            if end_match is not None
            else start_match["page_index"]
        )
        confidence = start_match["score"]
        method = "monotone_dp+" + start_match["method"]
        if start_match.get("ambiguous"):
            method += "+sequence_disambiguated"
            confidence = max(0.0, confidence - 0.055)
        if end_match is not None and end_index > start_match["page_index"]:
            confidence = (confidence + end_match["score"]) / 2.0
            method += "+tail"
        _set_alignment(
            entry["item"],
            start_match["page_number"],
            end_index + 1,
            page_objects,
            method,
            confidence,
            start_match["anchor_preview"],
            evidence="direct",
        )
        if entry["kind"] == "chunk":
            direct_chunks += 1
        else:
            direct_units += 1
    return direct_units, direct_chunks


def _derive_unit_ranges_from_chunks(
    document: Dict[str, Any],
    page_objects: Sequence[Dict[str, Any]],
) -> int:
    direct_ranges: Dict[str, List[Dict[str, int]]] = defaultdict(list)
    confidences: Dict[str, List[float]] = defaultdict(list)
    for chunk in document.get("chunks") or []:
        alignment = chunk.get("pdf_alignment")
        page_range = chunk.get("page_range")
        unit_id = str(chunk.get("unit_id") or "")
        if (
            not unit_id
            or not isinstance(page_range, dict)
            or not isinstance(alignment, dict)
            or alignment.get("evidence") != "direct"
        ):
            continue
        direct_ranges[unit_id].append(page_range)
        confidences[unit_id].append(float(alignment.get("confidence") or 0.0))

    derived = 0
    for unit in document.get("structural_units") or []:
        unit_id = str(unit.get("unit_id") or "")
        ranges = direct_ranges.get(unit_id)
        if not ranges:
            continue
        start = min(page_range["start"] for page_range in ranges)
        end = max(page_range["end"] for page_range in ranges)
        _set_alignment(
            unit,
            start,
            end,
            page_objects,
            "direct_chunks_derived",
            sum(confidences[unit_id]) / len(confidences[unit_id]),
            evidence="derived",
        )
        derived += 1
    return derived


def _inherit_unit_ranges(
    document: Dict[str, Any],
    page_objects: Sequence[Dict[str, Any]],
) -> int:
    units = document.get("structural_units") or []
    by_id = {
        unit.get("unit_id"): unit
        for unit in units
        if unit.get("unit_id")
    }
    inherited = 0
    for _iteration in range(len(units) + 1):
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
                evidence="inherited",
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
            evidence="inherited",
        )
        inherited += 1
    return inherited


def _clear_alignment_state(document: Dict[str, Any]) -> None:
    document["pages"] = []
    document["page_refs"] = []
    document["source_pdf"] = None
    for unit in document.get("structural_units") or []:
        unit["page_range"] = None
        unit.pop("pdf_alignment", None)
    for chunk in document.get("chunks") or []:
        chunk["page_range"] = None
        chunk["page_id"] = None
        chunk.pop("pdf_alignment", None)
    metadata = document.setdefault("metadata", {})
    for key in tuple(metadata):
        if (
            key == "pdf_pages"
            or key == "pdf_text_backend_counts"
            or key == "pdf_backend_errors"
            or key == "source_pdf"
            or key.startswith("pdf_alignment")
            or key.startswith("source_pdf_")
            or key.startswith("secondary_pdf_")
        ):
            metadata.pop(key, None)
    metadata["pdf_alignment_version"] = ALIGNMENT_VERSION


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


def _safe_path_component(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", normalized).strip(".-_")
    slug = slug[:56] or fallback
    return "{}-{}".format(slug, make_id(value or fallback))


def _page_references(
    page_objects: Sequence[Dict[str, Any]],
    document_id: str,
    pdf_identity: str,
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
    document_component = _safe_path_component(document_id, "document")
    pdf_component = "pdf-{}".format(
        re.sub(r"[^a-fA-F0-9]", "", pdf_identity)[:20]
        or make_id(pdf_identity)
    )
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
                / document_component
                / pdf_component
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
    _clear_alignment_state(aligned)
    metadata = aligned.setdefault("metadata", {})
    metadata.update(
        {
            "pdf_alignment_version": ALIGNMENT_VERSION,
            "pdf_alignment_requested": True,
            "source_pdf_sha256": pdf_sha256,
        }
    )
    if source_pdf:
        aligned["source_pdf"] = Path(source_pdf).name
        metadata["source_pdf_relative_path"] = str(source_pdf)
    if not page_texts:
        metadata.update(
            {
                "pdf_alignment_status": "unavailable",
                "pdf_pages": 0,
                "source_pdf_sha256": pdf_sha256,
                "pdf_alignment": {
                    "version": ALIGNMENT_VERSION,
                    "unit_count": len(aligned.get("structural_units") or []),
                    "direct_unit_matches": 0,
                    "chunk_derived_unit_matches": 0,
                    "inherited_unit_matches": 0,
                    "aligned_unit_count": 0,
                    "unit_coverage": 0.0,
                    "direct_unit_coverage": 0.0,
                    "chunk_count": len(aligned.get("chunks") or []),
                    "alignable_chunk_count": 0,
                    "direct_chunk_matches": 0,
                    "inherited_chunk_matches": 0,
                    "aligned_chunk_count": 0,
                    "chunk_coverage": 0.0,
                    "direct_chunk_coverage": 0.0,
                    "direct_evidence_coverage": 0.0,
                },
            }
        )
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

    content_identity_payload = json.dumps(
        {
            "page_count": len(page_texts),
            "page_text_sha256": [sha256_str(text) for text in page_texts],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    pdf_identity = pdf_sha256 or sha256_str(content_identity_payload)
    page_objects = []
    for index, text in enumerate(page_texts):
        page_objects.append(
            {
                "page_id": "pg_{}_{}".format(
                    make_id(document_id, pdf_identity, index, sha256_str(text)),
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
    page_index = _page_features(page_objects)
    units = aligned.get("structural_units") or []
    chunks = aligned.get("chunks") or []
    entries, _profiles = _direct_alignment_entries(aligned)
    selected, unmatched_direct_entries = _monotone_matches(
        entries,
        page_index,
    )
    direct_units, direct_chunks = _apply_direct_matches(
        entries,
        selected,
        page_index,
        page_objects,
    )
    chunk_derived_units = _derive_unit_ranges_from_chunks(
        aligned,
        page_objects,
    )
    inherited_units = _inherit_unit_ranges(aligned, page_objects)
    inherited_chunks = _inherit_chunk_ranges(aligned, page_objects)

    page_refs = _page_references(
        page_objects,
        document_id or str(aligned.get("document_key") or "document"),
        pdf_identity,
        output_base_dir,
        pages_root_dir,
    )
    aligned["pages"] = page_refs
    aligned["page_refs"] = page_refs

    aligned_unit_count = sum(
        isinstance(unit.get("page_range"), dict) for unit in units
    )
    aligned_chunk_count = sum(
        isinstance(chunk.get("page_range"), dict) for chunk in chunks
    )
    unit_coverage = aligned_unit_count / len(units) if units else 1.0
    chunk_coverage = aligned_chunk_count / len(chunks) if chunks else 1.0
    evidenced_unit_matches = direct_units + chunk_derived_units
    direct_unit_coverage = (
        evidenced_unit_matches / len(units) if units else 1.0
    )
    direct_chunk_coverage = direct_chunks / len(chunks) if chunks else 1.0
    alignable_chunk_count = sum(
        entry["kind"] == "chunk" for entry in entries
    )
    alignable_unit_count = sum(
        entry["kind"] == "unit" for entry in entries
    )
    direct_entry_matches = direct_chunks + direct_units
    direct_evidence_coverage = (
        direct_entry_matches / len(entries) if entries else 0.0
    )
    if direct_evidence_coverage >= 0.90:
        status = "aligned"
    elif direct_evidence_coverage >= 0.40:
        status = "partial"
    else:
        status = "low_coverage"

    metadata.update(
        {
            "pdf_alignment_status": status,
            "pdf_pages": len(page_objects),
            "source_pdf_sha256": pdf_sha256,
            "pdf_alignment": {
                "version": ALIGNMENT_VERSION,
                "unit_count": len(units),
                "alignable_unit_count": alignable_unit_count,
                "direct_unit_matches": direct_units,
                "chunk_derived_unit_matches": chunk_derived_units,
                "inherited_unit_matches": inherited_units,
                "aligned_unit_count": aligned_unit_count,
                "unit_coverage": round(unit_coverage, 4),
                "direct_unit_coverage": round(direct_unit_coverage, 4),
                "chunk_count": len(chunks),
                "alignable_chunk_count": alignable_chunk_count,
                "direct_chunk_matches": direct_chunks,
                "inherited_chunk_matches": inherited_chunks,
                "aligned_chunk_count": aligned_chunk_count,
                "chunk_coverage": round(chunk_coverage, 4),
                "direct_chunk_coverage": round(direct_chunk_coverage, 4),
                "direct_evidence_coverage": round(
                    direct_evidence_coverage,
                    4,
                ),
            },
        }
    )
    selected_identifiers = set(selected)
    alignable_unit_identifiers = {
        entry["identifier"]
        for entry in entries
        if entry["kind"] == "unit"
    }
    alignable_chunk_identifiers = {
        entry["identifier"]
        for entry in entries
        if entry["kind"] == "chunk"
    }
    remaining_units = [
        str(unit.get("unit_id") or "")
        for unit in units
        if unit.get("unit_id")
        and not isinstance(unit.get("page_range"), dict)
    ]
    remaining_chunks = [
        identifier
        for identifier in unmatched_direct_entries
        if identifier in alignable_chunk_identifiers
    ]
    unmatched_alignable_units = sorted(
        alignable_unit_identifiers - selected_identifiers
    )
    for identifier in unmatched_alignable_units:
        if identifier not in remaining_units:
            remaining_units.append(identifier)
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
    """Extract one text stream per page with independently guarded backends."""
    path = Path(pdf_path)
    raw = path.read_bytes()
    fitz_texts: List[str] = []
    backend_errors: Dict[str, str] = {}
    if fitz is not None:
        try:
            with fitz.open(stream=raw, filetype="pdf") as fitz_document:
                fitz_texts = [
                    (page.get_text("text") or "").replace("\x00", "")
                    for page in fitz_document
                ]
        except Exception as exc:  # pragma: no cover - backend-specific failures
            backend_errors["pymupdf"] = "{}: {}".format(
                type(exc).__name__,
                exc,
            )

    pypdf_texts: List[str] = []
    fallback_indices = [
        index
        for index, text in enumerate(fitz_texts)
        if len(normalize_alignment_text(text)) < 16
    ]
    should_try_pypdf = not fitz_texts or bool(fallback_indices)
    if should_try_pypdf:
        try:
            reader = PdfReader(io.BytesIO(raw))
            pypdf_texts = []
            for page in reader.pages:
                try:
                    pypdf_texts.append(
                        (page.extract_text() or "").replace("\x00", "")
                    )
                except Exception as exc:  # pragma: no cover - malformed pages
                    backend_errors.setdefault(
                        "pypdf_page",
                        "{}: {}".format(type(exc).__name__, exc),
                    )
                    pypdf_texts.append("")
        except Exception as exc:
            backend_errors["pypdf"] = "{}: {}".format(
                type(exc).__name__,
                exc,
            )

    if not fitz_texts and not pypdf_texts and backend_errors:
        raise RuntimeError(
            "No PDF text backend could read {} ({})".format(
                path,
                "; ".join(
                    "{}={}".format(name, error)
                    for name, error in sorted(backend_errors.items())
                ),
            )
        )

    page_count = max(len(fitz_texts), len(pypdf_texts))
    texts: List[str] = []
    backends: List[str] = []
    backend_counts: Counter[str] = Counter()
    for index in range(page_count):
        pypdf_text = (
            pypdf_texts[index] if index < len(pypdf_texts) else ""
        )
        fitz_text = fitz_texts[index] if index < len(fitz_texts) else ""
        pypdf_normalized = normalize_alignment_text(pypdf_text)
        fitz_normalized = normalize_alignment_text(fitz_text)
        if fitz_normalized:
            selected, backend = fitz_text, "pymupdf"
            if (
                pypdf_normalized
                and len(pypdf_normalized) > len(fitz_normalized) * 1.25
            ):
                selected, backend = pypdf_text, "pypdf"
        elif pypdf_normalized:
            selected, backend = pypdf_text, "pypdf"
        else:
            selected = fitz_text or pypdf_text
            backend = "pymupdf" if index < len(fitz_texts) else "pypdf"
        texts.append(selected)
        backends.append(backend)
        backend_counts[backend] += 1
    return texts, backends, {
        "sha256": sha256_bytes(raw),
        "bytes": len(raw),
        "page_count": len(texts),
        "backend_counts": dict(sorted(backend_counts.items())),
        "secondary_backend": (
            "pypdf"
            if fitz_texts
            else ("pymupdf" if fitz is not None else None)
        ),
        "backend_errors": backend_errors,
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
            "pdf_backend_errors": pdf_metadata["backend_errors"],
            "pdf_alignment_version": ALIGNMENT_VERSION,
        }
    )
    return aligned, issues
