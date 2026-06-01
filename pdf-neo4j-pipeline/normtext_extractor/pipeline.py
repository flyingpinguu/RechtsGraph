#!/usr/bin/env python3
"""Extract structured text from German legal PDFs.

Usage:
    .venv/bin/python scripts/extract_normtext.py \\
        --output output/pilot/normtext_pilot.json \\
        ../abfall_pdfs/01_KrWG.pdf ../abfall_pdfs/02_AVV.pdf \\
        ../gefahrgut_pdfs/GGBefG.pdf

Uses pypdf as the primary backend. If PyMuPDF is installed, it is used as a
secondary extraction backend for fallback and comparison.
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

from pypdf import PdfReader

from normtext_extractor.rules import RuleSet, load_rules
from normtext_extractor.table_parsers import TableParseContext, default_table_parser_registry

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - optional local dependency
    fitz = None


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
PARA_RE = re.compile(r"^(\u00a7\s*\d+[a-z]?)(?:\s+(.+))?$")
ANNEX_RE = re.compile(r"^(Anlage\s+\d+[a-z]?)(?:\s+(.+))?$")
SECTION_RE = re.compile(r"^(?:Abschnitt|A\s*b\s*s\s*c\s*h\s*n\s*i\s*t\s*t)\s+\d+", re.IGNORECASE)
SUBSECTION_GROUP_RE = re.compile(
    r"^(?:Unterabschnitt|U\s*n\s*t\s*e\s*r\s*a\s*b\s*s\s*c\s*h\s*n\s*i\s*t\s*t)\s+\d+",
    re.IGNORECASE,
)
PARA_REFERENCE_TAIL_RE = re.compile(
    r"^(?:Abs\.|Absatz\b|Satz\b|Nummern?\b|Nr\.|Buchstabe\b|Buchst\.|§|und\b|oder\b|,)",
    re.IGNORECASE,
)
ANNEX_REFERENCE_TAIL_RE = re.compile(
    r"^(?:Tabelle|Abs\.|Absatz|Satz|Nummer|Nr\.|Buchstabe|Buchst\.|und|oder|sowie|,)\b",
    re.IGNORECASE,
)
SUBSECTION_RE = re.compile(r"\((\d+)\)\s")
LIST_ITEM_RE = re.compile(r"^\s*((?:\d+[a-z]?|[a-z])[\.)])\s+(.+)$")
AVV_WASTE_RE = re.compile(r"^(\d{2}\s\d{2}(?:\s\d{2})?\*?)\s+(.+)$")
PAGE_HEADER_RE = re.compile(
    r"^(?:Ein Service des Bundesministeriums der Justiz.*gesetze-im-internet\.de|"
    r"Ein Service des Bundesministerium der Justiz.*|"
    r"Ein Service des Bundesministeriums der Justiz sowie des Bundesamts für|"
    r"sowie des Bundesamts für Justiz.*gesetze-im-internet\.de|"
    r"Justiz\s+.+www\.gesetze-im-internet\.de|"
    r"-\s*Seite\s+\d+\s+von\s+\d+\s*-)$"
)
SPLIT_TABLE_SECTIONS_AS_UNITS = False
NESTED_TABLE_ROW_MARKER_RE = re.compile(r"^(?:\d+[a-z]?|[A-ZÄÖÜ]{1,6}\d+[a-z]?)$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_str(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def make_id(*parts):
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:12]


def slugify(value):
    value = (value or "").lower()
    replacements = {
        "§": "para",
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def doc_key_from_metadata(source_pdf, canonical_citation, metadata):
    abbreviation = (metadata or {}).get("abbreviation") or canonical_citation
    key = slugify(abbreviation)
    if key:
        return key
    return slugify(os.path.splitext(os.path.basename(source_pdf))[0])


def document_global_key_from_metadata(source_pdf, title, canonical_citation, metadata):
    long_name = (metadata or {}).get("short_title") or title or canonical_citation
    key = slugify(long_name)
    if key:
        return key
    return doc_key_from_metadata(source_pdf, canonical_citation, metadata)


def paragraph_number(label):
    return label.replace("\u00a7", "").strip()


def unit_slug(doc_key, unit_type, number):
    return "{}_{}_{}".format(doc_key, unit_type, slugify(str(number)))


def chunk_slug(*parts):
    return "_".join(
        slugify(str(part))
        for part in parts
        if part is not None and str(part).strip()
    )


def legal_citation(citation_prefix, unit_type, label, title=None):
    if unit_type == "paragraph":
        return "{} {}".format(citation_prefix, label)
    if unit_type == "annex":
        return "{} {}".format(citation_prefix, label)
    if unit_type == "table":
        return "{} {}".format(citation_prefix, label)
    return "{} {}".format(citation_prefix, label)


def relative_path(path, base_dir):
    try:
        return os.path.relpath(path, base_dir)
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# PDF reading
# ---------------------------------------------------------------------------

def read_pdf(path):
    raw = open(path, "rb").read()
    reader = PdfReader(io.BytesIO(raw))
    return raw, reader


def extract_fitz_page_texts(path):
    if fitz is None:
        return []
    texts = []
    with fitz.open(path) as doc:
        for page in doc:
            texts.append((page.get_text("text") or "").replace("\x00", ""))
    return texts


def extract_fitz_page_words(path):
    if fitz is None:
        return []
    word_pages = []
    with fitz.open(path) as doc:
        for page in doc:
            word_pages.append(page.get_text("words") or [])
    return word_pages


def extract_fitz_page_lines(path):
    if fitz is None:
        return []
    line_pages = []
    with fitz.open(path) as doc:
        for page in doc:
            page_lines = []
            for drawing in page.get_drawings():
                for item in drawing.get("items", []):
                    if item[0] != "l":
                        continue
                    start, end = item[1], item[2]
                    x0, y0 = float(start.x), float(start.y)
                    x1, y1 = float(end.x), float(end.y)
                    if abs(x0 - x1) < 1.0:
                        page_lines.append(
                            {
                                "orientation": "vertical",
                                "x": (x0 + x1) / 2,
                                "y0": min(y0, y1),
                                "y1": max(y0, y1),
                            }
                        )
                    elif abs(y0 - y1) < 1.0:
                        page_lines.append(
                            {
                                "orientation": "horizontal",
                                "y": (y0 + y1) / 2,
                                "x0": min(x0, x1),
                                "x1": max(x0, x1),
                            }
                        )
            line_pages.append(page_lines)
    return line_pages


def normalize_for_compare(text):
    return re.sub(r"\s+", " ", text or "").strip()


def choose_page_text(pypdf_text, fitz_text):
    """Choose the richer text backend while preserving deterministic behavior."""
    if not fitz_text:
        return pypdf_text, "pypdf"
    if not pypdf_text:
        return fitz_text, "pymupdf"
    pypdf_norm = normalize_for_compare(pypdf_text)
    fitz_norm = normalize_for_compare(fitz_text)
    if len(fitz_norm) > len(pypdf_norm) * 1.05:
        return fitz_text, "pymupdf"
    return pypdf_text, "pypdf"


def collect_after_label(lines, label, stop_labels):
    collected = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if not in_block:
            if stripped == label:
                in_block = True
            elif stripped.startswith(label):
                value = stripped[len(label):].strip()
                if value:
                    collected.append(value)
                in_block = True
            continue
        if any(stripped.startswith(stop) for stop in stop_labels):
            break
        if stripped:
            collected.append(stripped)
    return " ".join(collected).strip()


def extract_meta(reader, path):
    info = reader.metadata or {}
    title = info.get("/Title", "")
    first_text = reader.pages[0].extract_text() or ""
    first_lines = [line.strip() for line in first_text.splitlines()]

    for idx, line in enumerate(first_lines):
        if line.startswith("(") and " - " in line and line.endswith(")") and idx > 0:
            title = first_lines[idx - 1]
            break
    if not title:
        title = os.path.basename(path)

    date_enacted = None
    date_match = re.search(r"Ausfertigungsdatum:\s*(\d{2}\.\d{2}\.\d{4})", first_text)
    if date_match:
        date_enacted = date_match.group(1)

    full_citation = collect_after_label(
        first_lines,
        "Vollzitat:",
        ["Stand:", "Fußnote", "§ "],
    )
    if not full_citation:
        full_citation = title

    canonical_citation = title
    abbrev_match = re.search(r"\(([^()]+?)\s+-\s+([A-Za-z0-9]+)\)", first_text)
    metadata = {}
    if abbrev_match:
        metadata["short_title"] = abbrev_match.group(1).strip()
        metadata["abbreviation"] = abbrev_match.group(2).strip()
        canonical_citation = metadata["abbreviation"]

    status_note = collect_after_label(first_lines, "Stand:", ["Fußnote", "§ "])
    if status_note:
        metadata["status_note"] = status_note

    return title, date_enacted, full_citation, canonical_citation, metadata


# ---------------------------------------------------------------------------
# Paragraph detection
# ---------------------------------------------------------------------------

def normalize_page_lines(page_text):
    """Return legal-text lines with recurring gesetze-im-internet headers removed."""
    normalized = []
    for line in page_text.splitlines():
        clean = line.replace("\x00", "").strip()
        if PAGE_HEADER_RE.match(clean):
            continue
        normalized.append(line)
    return normalized


def clean_line(line):
    return (line or "").replace("\x00", "").strip()


def match_para_heading(stripped):
    match = PARA_RE.match(stripped)
    if not match:
        return None
    tail = (match.group(2) or "").strip()
    if tail and PARA_REFERENCE_TAIL_RE.match(tail):
        return None
    if tail and tail[0].islower():
        return None
    return match


def is_allowed_annex_lowercase_tail(tail):
    normalized = normalize_for_compare(tail)
    return (
        normalized.startswith("(zu ")
        or normalized.startswith("zu §")
        or normalized.startswith("zu den §")
    )


def match_annex_heading(stripped):
    match = ANNEX_RE.match(stripped)
    if not match:
        return None
    tail = (match.group(2) or "").strip()
    if tail and ANNEX_REFERENCE_TAIL_RE.match(tail):
        return None
    if tail and tail[0].islower() and not is_allowed_annex_lowercase_tail(tail):
        return None
    if tail.startswith("zu ") and not is_allowed_annex_lowercase_tail(tail):
        return None
    return match


def is_section_listing_line(stripped):
    return bool(SECTION_RE.match(stripped) or SUBSECTION_GROUP_RE.match(stripped))


def is_structure_listing_line(stripped):
    return bool(match_para_heading(stripped) or match_annex_heading(stripped) or is_section_listing_line(stripped))


def document_line_stream(page_text_map):
    stream = []
    for page_idx in sorted(page_text_map):
        for line in normalize_page_lines(page_text_map[page_idx]):
            stream.append((page_idx, line))
    return stream


def next_significant_lines(stream, index, limit=4):
    lines = []
    for _page_idx, line in stream[index + 1:]:
        stripped = clean_line(line)
        if not stripped:
            continue
        lines.append(stripped)
        if len(lines) >= limit:
            break
    return lines


def looks_like_real_unit_start(stream, index):
    stripped = clean_line(stream[index][1])
    if not (match_para_heading(stripped) or match_annex_heading(stripped)):
        return False
    lookahead = next_significant_lines(stream, index)
    if not lookahead:
        return False
    if is_structure_listing_line(lookahead[0]):
        return False
    if len(lookahead) > 1 and is_structure_listing_line(lookahead[1]):
        return False
    for line in lookahead:
        if is_structure_listing_line(line):
            return False
        if SUBSECTION_RE.search(line):
            return True
    return True


def first_real_unit_start_index(stream):
    for index, (_page_idx, line) in enumerate(stream):
        match = match_para_heading(clean_line(line))
        if not match:
            continue
        if paragraph_number(match.group(1)) == "1" and looks_like_real_unit_start(stream, index):
            return index
    for index, (_page_idx, _line) in enumerate(stream):
        if looks_like_real_unit_start(stream, index):
            return index
    return None


def filtered_document_lines(page_text_map):
    """Return document lines with the table of contents removed."""
    stream = document_line_stream(page_text_map)
    filtered = []
    in_toc = False
    body_start_index = first_real_unit_start_index(stream)
    for index, (page_idx, line) in enumerate(stream):
        if body_start_index is not None and index < body_start_index:
            continue
        stripped = clean_line(line)
        if stripped.startswith("Inhaltsübersicht") or stripped.startswith("Inhaltsuebersicht"):
            in_toc = True
            continue
        if in_toc:
            if looks_like_real_unit_start(stream, index):
                in_toc = False
                filtered.append((page_idx, line))
            continue
        filtered.append((page_idx, line))
    return filtered


def is_top_level_structure_heading(stripped):
    return match_para_heading(stripped) or match_annex_heading(stripped)


def detect_paras_across_pages(page_text_map):
    """Return paragraph units spanning page boundaries.

    Page-wise parsing loses text whenever a paragraph continues on the next PDF
    page. Keeping a document-wide line stream makes the next paragraph heading,
    not the page break, the unit boundary.
    """
    results = []
    current = None
    in_annexes = False
    seen_paragraph = False

    for page_idx, line in filtered_document_lines(page_text_map):
        stripped = clean_line(line)
        if match_annex_heading(stripped):
            if not seen_paragraph:
                continue
            if current is not None:
                current["text"] = "\n".join(current["lines"]).strip()
                results.append(current)
                current = None
            in_annexes = True
            continue
        if in_annexes:
            continue

        m = match_para_heading(stripped)
        if m:
            seen_paragraph = True
            if current is not None:
                current["text"] = "\n".join(current["lines"]).strip()
                results.append(current)
            current = {
                "start_page": page_idx,
                "end_page": page_idx,
                "label": m.group(1).strip(),
                "title": (m.group(2) or "").strip() or None,
                "lines": [line],
            }
            continue

        if current is not None:
            current["lines"].append(line)
            current["end_page"] = page_idx

    if current is not None:
        current["text"] = "\n".join(current["lines"]).strip()
        results.append(current)

    return results


def detect_annexes_across_pages(page_text_map):
    """Return Anlage blocks as structural units."""
    results = []
    current = None
    seen_paragraph = False

    for page_idx, line in filtered_document_lines(page_text_map):
        stripped = clean_line(line)
        if match_para_heading(stripped):
            seen_paragraph = True
        m = match_annex_heading(stripped)
        if m:
            if not seen_paragraph:
                continue
            if current is not None:
                current["text"] = "\n".join(current["lines"]).strip()
                results.append(current)
            current = {
                "start_page": page_idx,
                "end_page": page_idx,
                "label": m.group(1).strip(),
                "title": (m.group(2) or "").strip() or None,
                "lines": [line],
                "line_pages": [page_idx],
            }
            continue

        if current is not None:
            current["lines"].append(line)
            current["line_pages"].append(page_idx)
            current["end_page"] = page_idx

    if current is not None:
        current["text"] = "\n".join(current["lines"]).strip()
        results.append(current)

    return results


# ---------------------------------------------------------------------------
# Subsection chunking
# ---------------------------------------------------------------------------

def split_into_subsections(para_text):
    """Split paragraph text into subsections by '(n)' markers."""
    subsections = []
    lines = para_text.splitlines()
    current_idx = None
    current_lines = []
    preamble_lines = []

    for line in lines:
        m = SUBSECTION_RE.search(line)
        if m:
            if current_lines:
                subsections.append((current_idx, "\n".join(current_lines).strip()))
                current_lines = []
            current_idx = int(m.group(1))
            if preamble_lines:
                current_lines.extend(preamble_lines)
                preamble_lines = []
            current_lines.append(line)
        else:
            if current_idx is None:
                preamble_lines.append(line)
            else:
                current_lines.append(line)

    if current_lines:
        idx_label = current_idx if current_idx is not None else 0
        subsections.append((idx_label, "\n".join(current_lines).strip()))

    if not subsections:
        subsections.append((0, para_text.strip()))
    return subsections


def make_subsection_citation(unit_citation, sub_num):
    if sub_num:
        return "{} Abs. {}".format(unit_citation, sub_num)
    return unit_citation


def split_into_list_items(text):
    """Split numbered/lettered legal list items inside a parent chunk.

    This deliberately keeps the parent chunk intact. The returned list items are
    child chunks that make enumerations addressable without losing the full
    Absatz context.
    """
    items = []
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        match = LIST_ITEM_RE.match(stripped)
        if match:
            if current is not None:
                current["text"] = "\n".join(current["lines"]).strip()
                items.append(current)
            marker = match.group(1)
            marker_clean = marker.rstrip(".)")
            item_type = "letter_item" if marker[0].isalpha() else "numbered_item"
            current = {
                "marker": marker_clean,
                "raw_marker": marker,
                "list_item_type": item_type,
                "lines": [stripped],
            }
            continue
        if current is not None:
            current["lines"].append(line)
    if current is not None:
        current["text"] = "\n".join(current["lines"]).strip()
        items.append(current)
    return items


def list_item_citation(parent_citation, item):
    marker = item["marker"]
    if item["list_item_type"] == "letter_item":
        return "{} Buchst. {}".format(parent_citation, marker)
    return "{} Nr. {}".format(parent_citation, marker)


def finalize_annex_chunk(chunk):
    chunk["text"] = "\n".join(chunk["lines"]).strip()
    pages = chunk.get("line_pages") or []
    if pages:
        chunk["page_range"] = {"start": min(pages) + 1, "end": max(pages) + 1}
    return chunk


def page_range_from_page(page_number):
    if page_number is None:
        return None
    return {"start": page_number, "end": page_number}


def merge_page_ranges(page_ranges):
    ranges = [page_range for page_range in page_ranges if page_range]
    if not ranges:
        return None
    return {
        "start": min(page_range["start"] for page_range in ranges),
        "end": max(page_range["end"] for page_range in ranges),
    }


def split_annex_into_chunks(annex):
    """Split Anlage text into text and table blocks.

    Besides explicit "Tabelle ..." headings, some federal annexes introduce
    tables only through their column header. Keep the detector narrow so form
    fields in Musteranlagen do not become legal sections again.
    """
    if isinstance(annex, dict):
        annex_lines = annex.get("lines", [])
        annex_line_pages = annex.get("line_pages", [annex.get("start_page", 0)] * len(annex_lines))
    else:
        annex_lines = str(annex).splitlines()
        annex_line_pages = [0] * len(annex_lines)

    chunks = []
    current = None
    heading_re = re.compile(r"^(Tabelle\s+\d+[a-z]?\s*:?.*)$")
    has_explicit_table_heading = any(
        heading_re.match(line.strip())
        for line in annex_lines
    )
    implicit_table_count = 0
    for line_idx, (line, page_idx) in enumerate(zip(annex_lines, annex_line_pages)):
        stripped = line.strip()
        match = heading_re.match(stripped)
        if match:
            if current is not None:
                chunks.append(finalize_annex_chunk(current))
            label = match.group(1)
            current = {
                "label": label,
                "chunk_type": "table_block",
                "lines": [line],
                "line_pages": [page_idx],
            }
            continue
        if (
            not has_explicit_table_heading
            and
            is_implicit_annex_table_header(annex_lines, line_idx)
            and (current is None or current.get("chunk_type") != "table_block")
        ):
            if current is not None:
                chunks.append(finalize_annex_chunk(current))
            implicit_table_count += 1
            current = {
                "label": "Tabelle {}".format(implicit_table_count),
                "chunk_type": "table_block",
                "lines": [line],
                "line_pages": [page_idx],
                "is_implicit_table": True,
            }
            continue
        if current is None:
            current = {
                "label": None,
                "chunk_type": "annex_text",
                "lines": [line],
                "line_pages": [page_idx],
            }
        else:
            current["lines"].append(line)
            current["line_pages"].append(page_idx)
    if current is not None:
        chunks.append(finalize_annex_chunk(current))
    return [chunk for chunk in chunks if chunk.get("text")]


def is_implicit_annex_table_header(lines, index):
    """Detect table starts where the PDF text has no explicit Tabelle label."""
    if index >= len(lines):
        return False
    if not looks_like_table_header_line(lines[index]):
        return False
    return has_following_table_rows(lines, index)


def has_following_table_rows(lines, index, window=7):
    lookahead = [normalize_for_compare(line) for line in lines[index + 1 : index + 1 + window]]
    row_like = [
        line for line in lookahead
        if line and not looks_like_table_header_line(line) and re.search(r"\d|[<>=%]", line)
    ]
    return len(row_like) >= 2


def collect_multiline_table_header(lines, header_index):
    header_parts = [lines[header_index]]
    header_end_index = header_index
    for idx in range(header_index + 1, min(len(lines), header_index + 6)):
        candidate = normalize_for_compare(lines[idx])
        if not candidate:
            continue
        if re.search(r"\d|[<>=%]", candidate):
            break
        if not looks_like_table_header_line(candidate):
            break
        header_parts.append(lines[idx])
        header_end_index = idx
    return normalize_for_compare(" ".join(header_parts)), header_end_index


def looks_like_table_header_line(line):
    normalized = normalize_for_compare(line)
    if not normalized:
        return False
    if re.match(r"^(?:Fortsetzung\s+)?Tabelle\s+\d+[a-z]?:", normalized):
        return False
    if re.match(r"^\(?\d", normalized):
        return False
    if normalized.endswith((".", ";")):
        return False
    spaced_cells = [part for part in re.split(r"\s{2,}", normalized) if part.strip()]
    if len(spaced_cells) >= 2:
        return True
    tokens = normalized.split()
    return 2 <= len(tokens) <= 10 and not re.search(r"\d|[<>=%]", normalized)


def infer_table_columns(header_text):
    header = normalize_for_compare(header_text)
    if not header:
        return []
    parts = [part.strip() for part in re.split(r"\s{2,}", header) if part.strip()]
    if len(parts) >= 2:
        return parts
    tokens = header.split()
    if 2 <= len(tokens) <= 10:
        return tokens
    return [header]


def is_table_section_header(line):
    normalized = normalize_for_compare(line)
    if not normalized or re.search(r"\d|[<>=%]", normalized):
        return False
    if looks_like_table_header_line(normalized):
        return True
    return 1 <= len(normalized.split()) <= 6 and not normalized.endswith((".", ";", ","))


def is_table_note_start(line):
    normalized = normalize_for_compare(line)
    return (
        normalized == "-----"
        or re.match(r"^\d+\)\s", normalized)
        or re.match(r"^[*]+(?:\s|$)", normalized)
        or re.match(r"^[a-z]\)\s", normalized)
    )
def split_row_into_cells(row_str, num_columns):
    # Try splitting by 2 or more spaces first
    cells = [c.strip() for c in re.split(r"\s{2,}", row_str) if c.strip()]
    if len(cells) == num_columns:
        return cells
    if len(cells) > num_columns:
        return [" ".join(cells[:-num_columns+1])] + cells[-num_columns+1:]

    # If 2 columns, fallback to splitting off the last token if it looks like a number/value
    if num_columns == 2:
        m = re.match(r"^(.*?)\s+([<>]?=?\s*\d.*)$", row_str)
        if m:
            return [m.group(1).strip(), m.group(2).strip()]

    return [row_str]


def group_positioned_words_into_rows(words, y_tolerance=7.0):
    rows = []
    current = []
    current_y = None
    for word in sorted(words, key=lambda w: (w[1], w[0])):
        x0, y0, x1, y1, text = word[:5]
        if current_y is None or abs(y0 - current_y) <= y_tolerance:
            current.append(word)
            current_y = y0 if current_y is None else (current_y + y0) / 2
            continue
        rows.append(sorted(current, key=lambda w: w[0]))
        current = [word]
        current_y = y0
    if current:
        rows.append(sorted(current, key=lambda w: w[0]))
    return rows


def positioned_row_text(row):
    return normalize_for_compare(" ".join(word[4] for word in row))


def is_material_class_header_row(row, parameter_dim_row=None):
    if parameter_dim_row is None:
        return False
    return material_header_columns(row, parameter_dim_row) is not None


def row_looks_like_wide_key_header(row, page_lines=None):
    text = positioned_row_text(row)
    if not text or PAGE_HEADER_RE.match(text) or is_table_label_row(row):
        return False
    if re.search(r"\d|[<>=%]", text):
        return False
    words = sorted(row, key=lambda word: word[0])
    if len(words) < 2 or len(words) > 4:
        return False
    if page_lines and len(vertical_grid_positions_at_y(page_lines, row_center_y(row))) < 4:
        return False
    first_text = words[0][4]
    if re.match(r"^\(?\d", first_text):
        return False
    first_center = (words[0][0] + words[0][2]) / 2
    second_center = (words[1][0] + words[1][2]) / 2
    return first_center < 180 and second_center < 260 and second_center - first_center > 12


def row_dim_center(row):
    key_header = wide_table_key_header(row)
    if key_header is None:
        return None
    return key_header["key_centers"][1]


def is_material_header_continuation(row, dim_center):
    if dim_center is None or not row:
        return False
    centers = [(word[0] + word[2]) / 2 for word in row]
    if any(center <= dim_center + 8 for center in centers):
        return False
    return len(centers) >= 2 and max(centers) - min(centers) > 20


def wide_table_key_header(row):
    words = sorted(row, key=lambda word: word[0])
    if len(words) < 2:
        return None
    if re.match(r"^\(?\d", words[0][4]):
        return None
    key_words = words[:2]
    return {
        "key_columns": [word[4] for word in key_words],
        "key_centers": [(word[0] + word[2]) / 2 for word in key_words],
    }


def material_header_columns(header_row, parameter_dim_row=None, continuation_row=None):
    dim_row = parameter_dim_row or header_row
    key_header = wide_table_key_header(dim_row)
    if key_header is None:
        return None
    dim_center = key_header["key_centers"][1]
    material_header_words = [
        word for word in header_row
        if (word[0] + word[2]) / 2 > dim_center + 8
    ]
    if len(material_header_words) < 3:
        return None
    material_columns = [word[4] for word in material_header_words]
    if continuation_row:
        continuation_words = list(continuation_row)
        combined_columns = []
        for word, column in zip(material_header_words, material_columns):
            center = (word[0] + word[2]) / 2
            closest = min(
                continuation_words,
                key=lambda candidate: abs(((candidate[0] + candidate[2]) / 2) - center),
                default=None,
            )
            if closest is not None and abs(((closest[0] + closest[2]) / 2) - center) <= 14:
                combined_columns.append("{} {}".format(column, closest[4]).strip())
            else:
                combined_columns.append(column)
        material_columns = combined_columns
    columns = key_header["key_columns"] + material_columns
    centers = key_header["key_centers"] + [
        (word[0] + word[2]) / 2 for word in material_header_words
    ]
    return columns, centers


def row_inside_wide_value_grid(row, centers, page_lines):
    if not page_lines or not centers:
        return True
    positions = vertical_grid_positions_at_y(page_lines, row_center_y(row))
    if len(positions) < 2:
        return False
    return positions[0] <= centers[0] + 2 and positions[-1] >= centers[-1] - 2


def material_table_stop_row(row, centers=None, page_lines=None):
    text = positioned_row_text(row)
    if not text:
        return True
    if PAGE_HEADER_RE.match(text):
        return True
    if re.match(r"^(?:Fortsetzung\s+)?Tabelle\s+\d+[a-z]?:", text):
        return True
    if re.match(r"^\d+\)\s", text):
        return True
    if page_lines and centers and not row_inside_wide_value_grid(row, centers, page_lines):
        return True
    return False


def material_table_row_to_cells(row, boundaries):
    cells = [[] for _ in range(len(boundaries) - 1)]
    for word in row:
        x0, _y0, x1, _y1, text = word[:5]
        center = (x0 + x1) / 2
        for idx in range(len(boundaries) - 1):
            if boundaries[idx] <= center < boundaries[idx + 1]:
                cells[idx].append(text)
                break
    return [normalize_for_compare(" ".join(cell)) for cell in cells]


def append_material_cells(target, cells):
    for idx, cell in enumerate(cells):
        if not cell:
            continue
        target[idx] = normalize_for_compare("{} {}".format(target[idx], cell))


def material_cells_to_row(columns, cells):
    row = {}
    for column, cell in zip(columns, cells):
        row[column] = cell
    return row


def is_material_section_label(cells):
    if not cells:
        return False
    return bool(cells[0]) and not any(cells[1:]) and not re.search(r"\d", cells[0])


def parse_material_panel_rows(page_rows, start_idx, end_idx, columns, centers, page_number, page_lines=None):
    boundaries = [-float("inf")]
    for left, right in zip(centers, centers[1:]):
        boundaries.append((left + right) / 2)
    boundaries.append(float("inf"))

    parsed_rows = []
    row_page_ranges = []
    current = None
    current_page_range = None
    for row in page_rows[start_idx:end_idx]:
        if material_table_stop_row(row, centers, page_lines):
            if current is not None:
                break
            continue
        cells = material_table_row_to_cells(row, boundaries)
        if not any(cells):
            continue
        if is_material_section_label(cells):
            continue
        first_cell = cells[0]
        second_cell = cells[1] if len(cells) > 1 else ""
        material_values = any(cells[2:])
        starts_new = bool(first_cell and (second_cell or material_values))
        if current is not None and first_cell and not second_cell and material_values and current[1]:
            starts_new = False
        continuation = current is not None and not starts_new
        if starts_new:
            if current is not None:
                parsed_rows.append(material_cells_to_row(columns, current))
                row_page_ranges.append(current_page_range)
            current = cells
            current_page_range = page_range_from_page(page_number)
        elif continuation:
            append_material_cells(current, cells)
        elif first_cell:
            current = cells
            current_page_range = page_range_from_page(page_number)
    if current is not None:
        parsed_rows.append(material_cells_to_row(columns, current))
        row_page_ranges.append(current_page_range)
    return parsed_rows, row_page_ranges


def merge_material_panel_sections(sections, all_columns):
    merged_rows = []
    merged_page_ranges = []
    row_index_by_key = {}
    for section in sections:
        rows = section.get("rows", [])
        row_page_ranges = section.get("row_page_ranges", [])
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            key_columns = section.get("columns", [])[:2]
            key = tuple(
                normalize_for_compare(row.get(column, ""))
                for column in key_columns
            )
            if not key[0]:
                key = ("__row_{}__".format(len(merged_rows)), "")
            if key not in row_index_by_key:
                merged = {column: "" for column in all_columns}
                for column, value in row.items():
                    if column in merged:
                        merged[column] = value
                row_index_by_key[key] = len(merged_rows)
                merged_rows.append(merged)
                merged_page_ranges.append(
                    row_page_ranges[idx] if idx < len(row_page_ranges) else None
                )
                continue

            merged_idx = row_index_by_key[key]
            merged = merged_rows[merged_idx]
            for column, value in row.items():
                if column not in merged or value in (None, ""):
                    continue
                current_value = merged.get(column)
                if not current_value:
                    merged[column] = value
                elif current_value != value:
                    merged[column] = "{} | {}".format(current_value, value)
            new_page_range = row_page_ranges[idx] if idx < len(row_page_ranges) else None
            merged_page_ranges[merged_idx] = merge_page_ranges(
                [merged_page_ranges[merged_idx], new_page_range]
            )
    return merged_rows, merged_page_ranges


def parse_table_title_from_text(table_text, fallback):
    lines = [line.strip() for line in table_text.splitlines() if line.strip()]
    first_line = lines[0] if lines else ""
    match = re.match(r"^(Tabelle\s+\d+[a-z]?)\s*:?\s*(.*)$", first_line)
    if match and match.group(2).strip():
        title_lines = [match.group(2).strip()]
        for line in lines[1:4]:
            previous = title_lines[-1].rstrip()
            if not previous.endswith((",", "-", "–")):
                break
            title_lines.append(line)
        return normalize_for_compare(" ".join(title_lines))
    return fallback


def is_table_label_row(row):
    text = positioned_row_text(row)
    return bool(re.match(r"^(?:Fortsetzung\s+)?Tabelle\s+\d+[a-z]?:", text))


def row_starts_symbol_table_footnote(row):
    words = sorted(row, key=lambda word: word[0])
    if len(words) < 2:
        return False
    first = words[0][4]
    rest = normalize_for_compare(" ".join(word[4] for word in words[1:5]))
    return bool(re.match(r"^\d+$", first)) and rest.startswith(("Zulässig", "Zugelassen", "Nicht zugelassen"))


def is_nested_table_row_marker(text):
    return bool(NESTED_TABLE_ROW_MARKER_RE.match(text or ""))


def nested_column_number_tokens(row):
    tokens = [word[4] for word in row if word[0] > 145 and re.match(r"^\d+$", word[4])]
    if len(tokens) < 3:
        return []
    expected = [str(idx) for idx in range(1, len(tokens) + 1)]
    return tokens if tokens == expected else []


def row_center_y(row):
    if not row:
        return 0
    return sum((word[1] + word[3]) / 2 for word in row) / len(row)


def group_header_row_words(row, min_x):
    groups = []
    current = []
    last_x1 = None
    for word in sorted(row, key=lambda candidate: candidate[0]):
        if word[2] < min_x:
            continue
        if last_x1 is None or word[0] - last_x1 <= 22:
            current.append(word)
        else:
            groups.append(current)
            current = [word]
        last_x1 = word[2]
    if current:
        groups.append(current)
    return groups


def header_group_text(group):
    return normalize_for_compare(" ".join(word[4] for word in sorted(group, key=lambda candidate: candidate[0])))


def normalize_header_identity(text):
    return slugify(normalize_for_compare(text)).replace("_", "")


def word_center(word):
    return (word[0] + word[2]) / 2, (word[1] + word[3]) / 2


def sorted_unique_positions(values, tolerance=1.0):
    positions = []
    for value in sorted(values):
        if not positions or abs(value - positions[-1]) > tolerance:
            positions.append(value)
        else:
            positions[-1] = (positions[-1] + value) / 2
    return positions


def vertical_grid_positions_at_y(page_lines, y, tolerance=0.75):
    if not page_lines:
        return []
    return sorted_unique_positions(
        line["x"]
        for line in page_lines
        if (
            line.get("orientation") == "vertical"
            and line["y0"] - tolerance <= y <= line["y1"] + tolerance
        )
    )


def column_bounds_from_centers(value_centers):
    if not value_centers:
        return []
    if len(value_centers) == 1:
        return [value_centers[0] - 30, value_centers[0] + 30]
    bounds = [value_centers[0] - (value_centers[1] - value_centers[0]) / 2]
    bounds.extend((left + right) / 2 for left, right in zip(value_centers, value_centers[1:]))
    bounds.append(value_centers[-1] + (value_centers[-1] - value_centers[-2]) / 2)
    return bounds


def value_indices_for_span(left, right, value_centers, tolerance=1.5):
    return [
        idx for idx, center in enumerate(value_centers)
        if left - tolerance <= center <= right + tolerance
    ]


def best_data_grid_positions(page_rows, start_idx, end_idx, page_lines):
    best = []
    for row in page_rows[start_idx:end_idx]:
        text = positioned_row_text(row)
        if not text or PAGE_HEADER_RE.match(text) or row_starts_symbol_table_footnote(row):
            continue
        positions = vertical_grid_positions_at_y(page_lines, row_center_y(row))
        if len(positions) > len(best):
            best = positions
    return best


def row_inside_value_grid(row, value_left, value_centers, page_lines):
    if not page_lines:
        return True
    positions = vertical_grid_positions_at_y(page_lines, row_center_y(row))
    if not positions:
        return False
    if not any(abs(position - value_left) <= 2 for position in positions):
        return False
    return len([position for position in positions if position >= value_left - 1]) >= len(value_centers) + 1


def infer_nested_value_layout(page_rows, data_start_idx, page_end_idx, number_row_idx, page_lines):
    number_words = [
        word for word in page_rows[number_row_idx]
        if word[0] > 145 and re.match(r"^\d+$", word[4])
    ]
    if not number_words:
        return [], None
    first_number_center = min((word[0] + word[2]) / 2 for word in number_words)
    grid_positions = best_data_grid_positions(page_rows, data_start_idx, page_end_idx, page_lines)
    if len(grid_positions) >= len(number_words) + 1:
        left_edges = [position for position in grid_positions if position <= first_number_center]
        if left_edges:
            value_left = max(left_edges)
            value_grid = [position for position in grid_positions if position >= value_left - 1]
            if len(value_grid) >= 2:
                centers = [
                    (left + right) / 2
                    for left, right in zip(value_grid, value_grid[1:])
                ]
                marker_boundaries = [position for position in grid_positions if position < value_left - 1]
                marker_right = marker_boundaries[1] if len(marker_boundaries) > 1 else value_left
                return centers, value_left, marker_right

    fallback_centers = sorted((word[0] + word[2]) / 2 for word in number_words)
    fallback_left = fallback_centers[0] - 20 if fallback_centers else None
    return fallback_centers, fallback_left, fallback_left


def cell_span_for_word(word, value_centers, page_lines):
    x, y = word_center(word)
    verticals = vertical_grid_positions_at_y(page_lines, y)
    if len(verticals) < 2:
        return None
    left_candidates = [position for position in verticals if position <= x]
    right_candidates = [position for position in verticals if position >= x]
    if not left_candidates or not right_candidates:
        return None
    left = max(left_candidates)
    right = min(right_candidates)
    if right - left < 4:
        return None
    indices = value_indices_for_span(left, right, value_centers)
    if not indices:
        return None
    return indices[0], indices[-1]


def header_cell_assignments_from_grid(row, value_centers, description_max_x, page_lines):
    cell_words = {}
    for word in sorted(row, key=lambda candidate: (candidate[1], candidate[0])):
        if word[2] < description_max_x:
            continue
        span = cell_span_for_word(word, value_centers, page_lines)
        if span is None:
            continue
        cell_words.setdefault(span, []).append(word)
    assignments = [[] for _center in value_centers]
    for span, words in sorted(cell_words.items(), key=lambda item: (item[0][0], item[0][1])):
        text = normalize_for_compare(
            " ".join(word[4] for word in sorted(words, key=lambda candidate: (candidate[1], candidate[0])))
        )
        if not text:
            continue
        for idx in range(span[0], span[1] + 1):
            assignments[idx].append(text)
    return assignments


def path_runs(paths):
    if not paths:
        return []
    runs = []
    start = 0
    current = tuple(paths[0])
    for idx, path in enumerate(paths[1:], start=1):
        path_tuple = tuple(path)
        if path_tuple == current:
            continue
        runs.append((start, idx - 1))
        start = idx
        current = path_tuple
    runs.append((start, len(paths) - 1))
    return runs


def group_bbox(group):
    return (
        min(word[0] for word in group),
        min(word[1] for word in group),
        max(word[2] for word in group),
        max(word[3] for word in group),
    )


def assign_header_groups_to_centers_with_paths(groups, value_centers, paths):
    assignments = [[] for _center in value_centers]
    if not groups:
        return assignments
    value_bounds = column_bounds_from_centers(value_centers)
    group_meta = []
    for group in groups:
        text = header_group_text(group)
        if not text:
            continue
        x0, _y0, x1, _y1 = group_bbox(group)
        group_meta.append({"group": group, "text": text, "x0": x0, "x1": x1, "center": (x0 + x1) / 2})
    if not group_meta:
        return assignments
    for start_idx, end_idx in path_runs(paths):
        parent_left = value_bounds[start_idx]
        parent_right = value_bounds[end_idx + 1]
        relevant = [
            item for item in group_meta
            if (
                parent_left - 8 <= item["center"] <= parent_right + 8
                or (item["x1"] >= parent_left and item["x0"] <= parent_right)
            )
        ]
        if not relevant:
            continue
        if len(relevant) == 1:
            item = relevant[0]
            parent_center = sum(value_centers[start_idx:end_idx + 1]) / (end_idx - start_idx + 1)
            parent_width = max(parent_right - parent_left, 1)
            if start_idx == end_idx or abs(item["center"] - parent_center) <= parent_width * 0.18:
                target_indices = range(start_idx, end_idx + 1)
            else:
                target_indices = [
                    min(range(start_idx, end_idx + 1), key=lambda idx: abs(value_centers[idx] - item["center"]))
                ]
            for center_idx in target_indices:
                assignments[center_idx].append(item["text"])
            continue

        relevant = sorted(relevant, key=lambda item: item["center"])
        boundaries = [
            (left["center"] + right["center"]) / 2
            for left, right in zip(relevant, relevant[1:])
        ]
        for center_idx in range(start_idx, end_idx + 1):
            group_idx = 0
            while group_idx < len(boundaries) and value_centers[center_idx] >= boundaries[group_idx]:
                group_idx += 1
            assignments[center_idx].append(relevant[group_idx]["text"])
    return assignments


def compact_header_path(parts):
    compacted = []
    for part in parts:
        part = normalize_for_compare(part)
        if not part or part in compacted[-1:]:
            continue
        if compacted and compacted[-1].endswith("-"):
            compacted[-1] = "{}{}".format(compacted[-1][:-1], part)
            continue
        if compacted and compacted[-1].endswith("von"):
            compacted[-1] = "{} {}".format(compacted[-1], part)
            continue
        if compacted and compacted[-1].endswith(","):
            compacted[-1] = "{} {}".format(compacted[-1], part)
            continue
        compacted.append(part)
    return compacted


def detect_row_header_label(header_rows, description_max_x):
    candidates = []
    for row in header_rows:
        left_text = normalize_for_compare(
            " ".join(
                word[4]
                for word in sorted(row, key=lambda candidate: (candidate[1], candidate[0]))
                if 45 <= word[0] < description_max_x
            )
        )
        if left_text and not re.fullmatch(r"\d+(?:\s+\d+)*", left_text):
            candidates.append(left_text)
    return candidates[-1] if candidates else "Zeile"


def header_assignment_is_full_span(labels_by_center):
    nonempty = [tuple(labels) for labels in labels_by_center if labels]
    return len(nonempty) == len(labels_by_center) and len(set(nonempty)) == 1


def trim_leading_full_span_header_rows(header_assignments):
    first_split_idx = None
    for idx, labels_by_center in enumerate(header_assignments):
        if any(labels_by_center) and not header_assignment_is_full_span(labels_by_center):
            first_split_idx = idx
            break
    if first_split_idx is None:
        return header_assignments
    keep_from = 0
    for idx in range(first_split_idx - 1, -1, -1):
        if header_assignment_is_full_span(header_assignments[idx]):
            keep_from = idx
            break
    return header_assignments[keep_from:]


def derive_nested_symbol_columns(
    page_rows,
    page_start_idx,
    number_row_idx,
    value_centers,
    description_max_x,
    title,
    page_lines=None,
):
    header_rows = page_rows[page_start_idx:number_row_idx]
    row_label = detect_row_header_label(header_rows, description_max_x)
    paths = [[] for _center in value_centers]
    normalized_title = normalize_for_compare(title or "")
    title_identity = normalize_header_identity(title or "")
    prepared_assignments = []

    for row in header_rows:
        text = positioned_row_text(row)
        if not text or PAGE_HEADER_RE.match(text) or is_table_label_row(row):
            continue
        text_identity = normalize_header_identity(text)
        if normalized_title and (
            normalize_for_compare(text) == normalized_title
            or (title_identity and text_identity == title_identity)
            or (title_identity and len(text_identity) >= 12 and text_identity in title_identity)
            or (title_identity and len(title_identity) >= 12 and title_identity in text_identity)
        ):
            continue
        if normalize_for_compare(text) == row_label:
            continue
        if page_lines:
            labels_by_center = header_cell_assignments_from_grid(
                row,
                value_centers,
                description_max_x,
                page_lines,
            )
            if not any(labels_by_center):
                continue
            prepared_assignments.append(labels_by_center)
        else:
            groups = group_header_row_words(row, description_max_x)
            labels_by_center = assign_header_groups_to_centers_with_paths(
                groups,
                value_centers,
                paths,
            )
            for center_idx, labels in enumerate(labels_by_center):
                paths[center_idx].extend(labels)

    if page_lines:
        for labels_by_center in trim_leading_full_span_header_rows(prepared_assignments):
            for center_idx, labels in enumerate(labels_by_center):
                paths[center_idx].extend(labels)

    number_words = [
        word for word in page_rows[number_row_idx]
        if word[0] > 145 and re.match(r"^\d+$", word[4])
    ]
    number_centers = [((word[0] + word[2]) / 2, word[4]) for word in number_words]
    for center_idx, center in enumerate(value_centers):
        if number_centers:
            _nearest_center, number_label = min(number_centers, key=lambda item: abs(item[0] - center))
            paths[center_idx].append(number_label)

    value_columns = []
    for idx, path in enumerate(paths, start=1):
        compacted = compact_header_path(path)
        if compacted:
            value_columns.append(", ".join(compacted))
        else:
            value_columns.append("Wert {}".format(idx))

    row_number_key = "{} Nummer".format(row_label)
    row_label_key = row_label
    return row_number_key, row_label_key, value_columns


def assign_nested_table_values(row_obj, row, value_centers, value_columns, page_lines, value_left):
    cell_words = {}
    for word in sorted(row, key=lambda candidate: (candidate[1], candidate[0])):
        if word[2] < value_left:
            continue
        span = cell_span_for_word(word, value_centers, page_lines) if page_lines else None
        if span is None:
            center = (word[0] + word[2]) / 2
            idx = min(range(len(value_centers)), key=lambda i: abs(value_centers[i] - center))
            if idx >= len(value_columns) or abs(value_centers[idx] - center) > 28:
                continue
            span = (idx, idx)
        cell_words.setdefault(span, []).append(word)

    for span, words in sorted(cell_words.items(), key=lambda item: (item[0][0], item[0][1])):
        text = normalize_for_compare(
            " ".join(word[4] for word in sorted(words, key=lambda candidate: (candidate[1], candidate[0])))
        )
        if not text:
            continue
        for idx in range(span[0], span[1] + 1):
            if idx >= len(value_columns):
                continue
            column = value_columns[idx]
            if row_obj[column]:
                row_obj[column] = "{} {}".format(row_obj[column], text).strip()
            else:
                row_obj[column] = text


def new_symbol_table_row(number, row_number_key, row_label_key, value_columns, page_number):
    row = {
        row_number_key: number,
        row_label_key: "",
    }
    for column in value_columns:
        row[column] = ""
    return row, page_range_from_page(page_number)


def parse_geometric_symbol_table(table, block, fitz_word_pages, fitz_page_lines=None):
    table_text = "\n".join(block.get("lines", [])) if block.get("lines") else block.get("text", "")
    if not fitz_word_pages:
        return None
    line_pages = [page for page in block.get("line_pages", []) if page is not None]
    if not line_pages:
        return None

    label = table.get("label") or block.get("label") or "Tabelle"
    title = parse_table_title_from_text(table_text, table.get("title"))
    table_number_match = re.search(r"\d+[a-z]?", label)
    table_number = table_number_match.group(0) if table_number_match else None
    first_page_idx = min(line_pages)

    rows = []
    row_page_ranges = []
    current = None
    current_page_range = None

    for page_idx in sorted(set(line_pages)):
        if page_idx < 0 or page_idx >= len(fitz_word_pages):
            continue
        page_rows = group_positioned_words_into_rows(fitz_word_pages[page_idx])
        target_label_rows = []
        different_label_rows = []
        for idx, row in enumerate(page_rows):
            text = positioned_row_text(row)
            label_match = re.match(r"^(?:Fortsetzung\s+)?Tabelle\s+(\d+[a-z]?):", text)
            if not label_match:
                continue
            if table_number is not None and label_match.group(1) == table_number:
                target_label_rows.append(idx)
            else:
                different_label_rows.append(idx)

        page_start_idx = 0
        if page_idx == first_page_idx and target_label_rows:
            page_start_idx = target_label_rows[0] + 1
        page_end_idx = next(
            (idx for idx in different_label_rows if idx > page_start_idx),
            len(page_rows),
        )
        for idx in range(page_start_idx, page_end_idx):
            if row_starts_symbol_table_footnote(page_rows[idx]):
                page_end_idx = idx
                break

        data_start_idx = None
        number_row_idx = None
        for idx in range(page_start_idx, page_end_idx):
            if nested_column_number_tokens(page_rows[idx]):
                number_row_idx = idx
                data_start_idx = idx + 1
                break
        if data_start_idx is None:
            continue

        page_lines = []
        if fitz_page_lines and page_idx < len(fitz_page_lines):
            page_lines = fitz_page_lines[page_idx]
        value_centers, value_left, marker_right = infer_nested_value_layout(
            page_rows,
            data_start_idx,
            page_end_idx,
            number_row_idx,
            page_lines,
        )
        if len(value_centers) < 3 or value_left is None:
            continue
        description_max_x = value_left
        data_rows = [
            row for row in page_rows[data_start_idx:page_end_idx]
            if (
                row
                and is_nested_table_row_marker(sorted(row, key=lambda word: word[0])[0][4])
                and sorted(row, key=lambda word: word[0])[0][0] < marker_right + 2
                and row_inside_value_grid(row, value_left, value_centers, page_lines)
            )
        ]
        if len(data_rows) < 1:
            continue
        row_number_key, row_label_key, value_columns = derive_nested_symbol_columns(
            page_rows,
            page_start_idx,
            number_row_idx,
            value_centers,
            description_max_x,
            title,
            page_lines,
        )
        for row in page_rows[data_start_idx:page_end_idx]:
            if PAGE_HEADER_RE.match(positioned_row_text(row)) or row_starts_symbol_table_footnote(row):
                continue
            if not row_inside_value_grid(row, value_left, value_centers, page_lines):
                if current is not None:
                    break
                continue
            words = sorted(row, key=lambda word: word[0])
            if not words:
                continue
            first_word = words[0]
            starts_new = first_word[0] < marker_right + 2 and is_nested_table_row_marker(first_word[4])
            if starts_new:
                if current is not None:
                    current[row_label_key] = normalize_for_compare(current[row_label_key])
                    rows.append(current)
                    row_page_ranges.append(current_page_range)
                current, current_page_range = new_symbol_table_row(
                    first_word[4],
                    row_number_key,
                    row_label_key,
                    value_columns,
                    page_idx + 1,
                )
                number_right = first_word[2]
                description_words = [
                    word[4] for word in sorted(words, key=lambda candidate: (candidate[1], candidate[0]))
                    if number_right < word[0] < description_max_x
                ]
            else:
                if current is None:
                    continue
                description_words = [
                    word[4] for word in sorted(words, key=lambda candidate: (candidate[1], candidate[0]))
                    if word[0] < description_max_x
                ]
                current_page_range = merge_page_ranges([current_page_range, page_range_from_page(page_idx + 1)])
            if description_words:
                current[row_label_key] = normalize_for_compare(
                    "{} {}".format(current[row_label_key], " ".join(description_words))
                )
            assign_nested_table_values(
                current,
                row,
                value_centers,
                value_columns,
                page_lines,
                value_left,
            )

    if current is not None:
        current[row_label_key] = normalize_for_compare(current[row_label_key])
        rows.append(current)
        row_page_ranges.append(current_page_range)

    if not rows:
        return None

    columns = [row_number_key, row_label_key] + value_columns
    header_text = " | ".join(columns)
    return {
        "label": label,
        "title": title,
        "columns": columns,
        "column_header_text": header_text,
        "rows": rows,
        "row_page_ranges": row_page_ranges,
        "notes": table.get("notes", []),
        "note_page_ranges": table.get("note_page_ranges", []),
        "sections": [
            {
                "section_label": None,
                "columns": columns,
                "column_header_text": header_text,
                "rows": rows,
                "row_page_ranges": row_page_ranges,
                "notes": [],
                "note_page_ranges": [],
            }
        ],
    }


def parse_geometric_material_table(table, block, fitz_word_pages, fitz_page_lines=None):
    if not fitz_word_pages:
        return None
    line_pages = [page for page in block.get("line_pages", []) if page is not None]
    if not line_pages:
        return None

    label = table.get("label") or block.get("label") or "Tabelle"
    title = table.get("title")
    table_number_match = re.search(r"\d+[a-z]?", label)
    table_number = table_number_match.group(0) if table_number_match else None
    first_page_idx = min(line_pages)
    sections = []

    for page_idx in sorted(set(line_pages)):
        if page_idx < 0 or page_idx >= len(fitz_word_pages):
            continue
        page_rows = group_positioned_words_into_rows(fitz_word_pages[page_idx])
        page_lines = []
        if fitz_page_lines and page_idx < len(fitz_page_lines):
            page_lines = fitz_page_lines[page_idx]
        target_label_rows = []
        different_label_rows = []
        for idx, row in enumerate(page_rows):
            text = positioned_row_text(row)
            label_match = re.match(r"^(?:Fortsetzung\s+)?Tabelle\s+(\d+[a-z]?):", text)
            if not label_match:
                continue
            if table_number is not None and label_match.group(1) == table_number:
                target_label_rows.append(idx)
            else:
                different_label_rows.append(idx)
        page_start_idx = 0
        if page_idx == first_page_idx and target_label_rows:
            page_start_idx = target_label_rows[0] + 1
        page_end_idx = next(
            (idx for idx in different_label_rows if idx > page_start_idx),
            len(page_rows),
        )
        consumed_until = page_start_idx
        for row_idx, row in enumerate(page_rows):
            if row_idx < page_start_idx or row_idx >= page_end_idx:
                continue
            if row_idx < consumed_until:
                continue
            header = None
            data_start_idx = None
            if row_idx + 1 < page_end_idx and row_looks_like_wide_key_header(page_rows[row_idx + 1], page_lines):
                if not is_material_class_header_row(row, page_rows[row_idx + 1]):
                    continue
                header = material_header_columns(row, page_rows[row_idx + 1])
                data_start_idx = row_idx + 2
            elif row_looks_like_wide_key_header(row, page_lines):
                data_start_idx = row_idx + 1
                continuation_row = None
                dim_center = row_dim_center(row)
                if (
                    row_idx + 1 < page_end_idx
                    and is_material_header_continuation(page_rows[row_idx + 1], dim_center)
                ):
                    continuation_row = page_rows[row_idx + 1]
                    data_start_idx = row_idx + 2
                header = material_header_columns(row, continuation_row=continuation_row)
            if header is None:
                continue
            columns, centers = header
            end_idx = page_end_idx
            for next_idx in range(data_start_idx, len(page_rows)):
                if next_idx >= page_end_idx:
                    end_idx = page_end_idx
                    break
                next_text = positioned_row_text(page_rows[next_idx])
                if re.match(r"^(?:Fortsetzung\s+)?Tabelle\s+\d+[a-z]?:", next_text):
                    next_number_match = re.search(r"Tabelle\s+(\d+[a-z]?)", next_text)
                    next_number = next_number_match.group(1) if next_number_match else None
                    if next_number != table_number:
                        end_idx = next_idx
                        break
                if material_table_stop_row(page_rows[next_idx], centers, page_lines):
                    end_idx = next_idx
                    break

            rows, row_page_ranges = parse_material_panel_rows(
                page_rows,
                data_start_idx,
                end_idx,
                columns,
                centers,
                page_idx + 1,
                page_lines,
            )
            if rows:
                section_label = " ".join(columns[2:])
                sections.append(
                    {
                        "section_label": section_label,
                        "columns": columns,
                        "column_header_text": " | ".join(columns),
                        "rows": rows,
                        "row_page_ranges": row_page_ranges,
                        "notes": [],
                        "note_page_ranges": [],
                    }
                )
                consumed_until = end_idx

    if not sections:
        return None

    all_columns = []
    seen_columns = set()
    for section in sections:
        for column in section["columns"]:
            if column not in seen_columns:
                seen_columns.add(column)
                all_columns.append(column)
    merged_rows, merged_page_ranges = merge_material_panel_sections(sections, all_columns)
    merged_section = {
        "section_label": None,
        "columns": all_columns,
        "column_header_text": "; ".join(section["column_header_text"] for section in sections),
        "rows": merged_rows,
        "row_page_ranges": merged_page_ranges,
        "notes": [],
        "note_page_ranges": [],
    }
    return {
        "label": label,
        "title": title,
        "columns": all_columns,
        "column_header_text": merged_section["column_header_text"],
        "rows": merged_rows,
        "row_page_ranges": merged_page_ranges,
        "notes": table.get("notes", []),
        "note_page_ranges": table.get("note_page_ranges", []),
        "sections": [merged_section],
    }


def parse_table_block(table_text, line_pages=None, label_override=None):
    """Parse a coarse table block into header metadata and row strings.

    PDF text extraction does not preserve grid geometry reliably. This parser
    keeps rows as conservative line strings but makes the inferred column header
    explicit in every row chunk.
    """
    raw_lines = table_text.splitlines()
    if line_pages is None or len(line_pages) != len(raw_lines):
        line_pages = [None] * len(raw_lines)
    line_items = [
        (line.strip(), page_idx + 1 if page_idx is not None else None)
        for line, page_idx in zip(raw_lines, line_pages)
        if line.strip()
    ]
    lines = [line for line, _page_number in line_items]
    if not lines:
        return {
            "label": None,
            "title": None,
            "columns": [],
            "column_header_text": "",
            "rows": [],
            "row_page_ranges": [],
            "notes": [],
            "note_page_ranges": [],
            "sections": [],
        }

    label = label_override or lines[0]
    title_lines = []
    header_search_start = 0 if label_override else 1
    if not label_override:
        heading_match = re.match(r"^(Tabelle\s+\d+[a-z]?|Anhang\s+\d+)\s*:?\s*(.*)$", label)
        if heading_match:
            label = heading_match.group(1)
            if heading_match.group(2).strip():
                title_lines.append(heading_match.group(2).strip())
    header_index = None
    for idx, line in enumerate(lines[header_search_start:], start=header_search_start):
        if looks_like_table_header_line(line) and has_following_table_rows(lines, idx):
            header_index = idx
            break
        title_lines.append(line)

    if header_index is None:
        header_index = header_search_start if len(lines) > header_search_start else 0
        title_lines = []

    header_text = lines[header_index] if header_index < len(lines) else ""
    header_end_index = header_index
    if looks_like_table_header_line(header_text):
        header_text, header_end_index = collect_multiline_table_header(lines, header_index)
    raw_row_items = line_items[header_end_index + 1 :]

    sections = [
        {
            "section_label": None,
            "columns": infer_table_columns(header_text),
            "column_header_text": header_text,
            "rows": [],
            "row_page_ranges": [],
            "notes": [],
            "note_page_ranges": [],
        }
    ]
    current_section = sections[0]
    in_notes = False
    for line, page_number in raw_row_items:
        normalized = normalize_for_compare(line)
        if normalized == normalize_for_compare(current_section["column_header_text"]):
            continue
        if is_table_section_header(normalized):
            current_section = {
                "section_label": normalized,
                "columns": infer_table_columns(normalized),
                "column_header_text": normalized,
                "rows": [],
                "row_page_ranges": [],
                "notes": [],
                "note_page_ranges": [],
            }
            sections.append(current_section)
            in_notes = False
            continue
        if is_table_note_start(line):
            in_notes = True
        if in_notes:
            current_section["notes"].append(line)
            current_section["note_page_ranges"].append(page_range_from_page(page_number))
            continue
        current_section["rows"].append(line)
        current_section["row_page_ranges"].append(page_range_from_page(page_number))

    row_lines = []
    row_page_ranges = []
    note_lines = []
    note_page_ranges = []
    for section in sections:
        num_columns = len(section.get("columns", []))
        parsed_rows = [split_row_into_cells(r, num_columns) for r in section["rows"]]
        section["rows"] = parsed_rows
        row_lines.extend(section["rows"])
        row_page_ranges.extend(section["row_page_ranges"])
        note_lines.extend(section["notes"])
        note_page_ranges.extend(section["note_page_ranges"])
    return {
        "label": label,
        "title": " ".join(title_lines).strip() or None,
        "columns": infer_table_columns(header_text),
        "column_header_text": header_text,
        "rows": row_lines,
        "row_page_ranges": row_page_ranges,
        "notes": note_lines,
        "note_page_ranges": note_page_ranges,
        "sections": sections,
    }


def add_table_from_block(
    structural_units,
    chunks,
    doc_id,
    doc_key,
    document_global_key,
    annex_label,
    annex_unit_id,
    annex_unit_obj,
    parent_citation,
    unit_global_key,
    pages,
    block,
    confidence=0.70,
    fitz_word_pages=None,
    fitz_page_lines=None,
):
    table_text = "\n".join(block.get("lines", [])) if block.get("lines") else block["text"]
    table = parse_table_block(
        table_text,
        block.get("line_pages"),
        label_override=block.get("label") if block.get("is_implicit_table") else None,
    )
    table = default_table_parser_registry().parse_first(
        TableParseContext(
            table=table,
            block=block,
            fitz_word_pages=fitz_word_pages,
            fitz_page_lines=fitz_page_lines,
        )
    )
    page_range = block.get("page_range") or annex_unit_obj["page_range"]
    table_label = table["label"] or block["label"] or "Tabelle"
    table_global_key = chunk_slug(unit_global_key, table_label)
    table_citation = "{} {}".format(parent_citation, table_label)
    sections = table.get("sections") or [
        {
            "section_label": None,
            "columns": table["columns"],
            "column_header_text": table["column_header_text"],
            "rows": table["rows"],
            "row_page_ranges": table.get("row_page_ranges", []),
            "notes": table["notes"],
            "note_page_ranges": table.get("note_page_ranges", []),
        }
    ]
    labeled_sections = [section for section in sections if section.get("section_label")]
    if SPLIT_TABLE_SECTIONS_AS_UNITS and len(labeled_sections) > 1:
        unit_specs = []
        for section in sections:
            section_label = section.get("section_label")
            section_table_label = "{} {}".format(table_label, section_label) if section_label else table_label
            section_global_key = chunk_slug(table_global_key, section_label)
            section_citation = "{} {}".format(table_citation, section_label) if section_label else table_citation
            unit_specs.append(
                {
                    "label": section_table_label,
                    "global_key": section_global_key,
                    "citation": section_citation,
                    "sections": [section],
                    "columns": section["columns"],
                    "column_header_text": section["column_header_text"],
                    "table_sections": [section_label] if section_label else [],
                }
            )
    else:
        unit_specs = [
            {
                "label": table_label,
                "global_key": table_global_key,
                "citation": table_citation,
                "sections": sections,
                "columns": table["columns"],
                "column_header_text": table["column_header_text"],
                "table_sections": [section.get("section_label") for section in sections if section.get("section_label")],
            }
        ]

    for spec in unit_specs:
        table_unit_id = "unit_{}".format(spec["global_key"])
        if table_unit_id not in annex_unit_obj["child_unit_ids"]:
            annex_unit_obj["child_unit_ids"].append(table_unit_id)

        unit_page_range = merge_page_ranges(
            [
                page_range
                for section in spec["sections"]
                for page_range in (
                    section.get("row_page_ranges", []) + section.get("note_page_ranges", [])
                )
            ]
        ) or page_range
        unit_row_count = sum(len(section.get("rows", [])) for section in spec["sections"])
        unit_note_count = sum(len(section.get("notes", [])) for section in spec["sections"])

        table_unit = {
            "unit_id": table_unit_id,
            "global_key": spec["global_key"],
            "legal_citation": spec["citation"],
            "display_name": "{}{}".format(
                spec["citation"],
                " {}".format(table["title"]) if table.get("title") else "",
            ),
            "document_id": doc_id,
            "document_key": doc_key,
            "document_global_key": document_global_key,
            "unit_type": "table",
            "label": spec["label"],
            "number": slugify(spec["label"].replace("Tabelle", "").replace("Anhang", "")).replace("_", ""),
            "title": table.get("title"),
            "breadcrumbs": [document_global_key, annex_label, spec["label"]],
            "parent_unit_id": annex_unit_id,
            "child_unit_ids": [],
            "page_range": unit_page_range,
            "columns": spec["columns"],
            "column_header_text": spec["column_header_text"],
            "row_count": unit_row_count,
            "note_count": unit_note_count,
            "table_sections": spec["table_sections"],
            "parser_name": table.get("parser_name"),
            "confidence": confidence,
            "review_status": "pending",
            "is_uncertain": False,
            "uncertainty_reason": None,
        }
        structural_units.append(table_unit)

        all_rows = [
            row
            for section in spec["sections"]
            for row in section.get("rows", [])
        ]
        all_page_ranges = [
            page_range
            for section in spec["sections"]
            for page_range in (
                section.get("row_page_ranges", []) + section.get("note_page_ranges", [])
            )
        ]
        chunk_page_range = merge_page_ranges(all_page_ranges) or unit_page_range
        chunk_page_id = pages[chunk_page_range["start"] - 1]["page_id"]
        chunk_global_key = chunk_slug(spec["global_key"], "rows")
        chunk_id = "chunk_{}".format(chunk_global_key)
        chunk_text = table_text.strip()
        chunk_obj = {
            "chunk_id": chunk_id,
            "global_key": chunk_global_key,
            "legal_citation": spec["citation"],
            "display_name": spec["citation"],
            "chunk_type": "table_rows",
            "unit_id": table_unit_id,
            "document_global_key": document_global_key,
            "parent_chunk_id": None,
            "child_chunk_ids": [],
            "label": "rows",
            "number": None,
            "sequence": 1,
            "page_id": chunk_page_id,
            "page_range": chunk_page_range,
            "columns": spec["columns"],
            "column_header_text": spec["column_header_text"],
            "parser_name": table.get("parser_name"),
            "table_section": "; ".join(spec["table_sections"]) if spec["table_sections"] else None,
            "rows": all_rows,
            "text": chunk_text,
            "text_sha256": sha256_str(chunk_text),
            "confidence": confidence,
            "review_status": "pending",
        }
        if unit_row_count:
            chunk_obj["row_range"] = {"start": 1, "end": unit_row_count}
        chunks.append(chunk_obj)


# ---------------------------------------------------------------------------
# AVV waste code detection
# ---------------------------------------------------------------------------

def detect_waste_codes(page_text):
    """Return list of (waste_code_line, full_text) for waste code lines."""
    results = []
    lines = page_text.splitlines()
    for line in lines:
        m = AVV_WASTE_RE.match(line.strip())
        if m:
            waste_code = m.group(1).strip()
            waste_text = m.group(2).strip()
            results.append((waste_code, waste_text, line.strip()))
    return results


def make_issue(
    doc_id,
    target_unit_id,
    target_chunk_id,
    issue_type,
    severity,
    description,
    evidence,
    corrected_value=None,
    page_range=None,
):
    issue_id = "issue_{}".format(make_id(doc_id, issue_type, evidence, description))
    issue = {
        "issue_id": issue_id,
        "document_id": doc_id,
        "target_unit_id": target_unit_id,
        "target_chunk_id": target_chunk_id,
        "issue_type": issue_type,
        "severity": severity,
        "description": description,
        "evidence": evidence,
        "corrected_value": corrected_value,
        "review_status": "open",
    }
    if page_range is not None:
        issue["page_range"] = page_range
    return issue


def detect_source_text_anomalies(doc_id, pages, rule_set=None):
    """Flag likely source/PDF text defects without silently rewriting the law."""
    issues = []
    seen = set()
    rule_set = rule_set or RuleSet()
    patterns = [
        (
            re.compile(rule["pattern"]),
            rule["issue_type"],
            rule["description"],
            rule.get("severity", "warning"),
        )
        for rule in rule_set.source_text_anomaly_patterns
        if rule.get("pattern") and rule.get("issue_type") and rule.get("description")
    ]
    for page in pages:
        for raw_line in page.get("text", "").splitlines():
            line = normalize_for_compare(raw_line)
            for regex, issue_type, description, severity in patterns:
                if regex.search(line):
                    key = (page.get("page_number"), issue_type, line)
                    if key in seen:
                        continue
                    seen.add(key)
                    issues.append(
                        make_issue(
                            doc_id,
                            None,
                            None,
                            issue_type,
                            severity,
                            "{} Page {}.".format(description, page.get("page_number")),
                            line,
                            None,
                            {"start": page.get("page_number"), "end": page.get("page_number")},
                        )
                    )
    return issues


def write_page_files(pages, doc_key, output_base_dir, pages_root_dir):
    if output_base_dir is None:
        output_base_dir = os.getcwd()
    if pages_root_dir is None:
        pages_root_dir = os.path.join(output_base_dir, "pages")
    doc_pages_dir = os.path.join(pages_root_dir, doc_key)
    os.makedirs(doc_pages_dir, exist_ok=True)

    page_refs = []
    for page in pages:
        page_number = int(page["page_number"])
        filename = "page_{:03d}.json".format(page_number)
        page_path = os.path.join(doc_pages_dir, filename)
        with open(page_path, "w", encoding="utf-8") as f:
            json.dump(page, f, indent=2, ensure_ascii=False)
        page_refs.append(
            {
                "page_id": page["page_id"],
                "page_number": page_number,
                "pdf_page_index": page["pdf_page_index"],
                "path": relative_path(page_path, output_base_dir),
                "text_sha256": page["text_sha256"],
                "text_extraction_backend": page.get("text_extraction_backend"),
            }
        )
    return page_refs


# ---------------------------------------------------------------------------
# Document extraction
# ---------------------------------------------------------------------------

def extract_document(pdf_path, output_base_dir=None, pages_root_dir=None, rule_set=None):
    raw, reader = read_pdf(pdf_path)
    doc_sha = sha256_bytes(raw)
    title, date_enacted, full_citation, canonical_citation, doc_metadata = extract_meta(reader, pdf_path)
    fitz_page_texts = extract_fitz_page_texts(pdf_path)
    fitz_word_pages = extract_fitz_page_words(pdf_path)
    fitz_page_lines = extract_fitz_page_lines(pdf_path)

    pages = []
    all_waste_codes = []
    page_text_map = {}
    backend_counts = {"pypdf": 0, "pymupdf": 0}
    backend_comparison = []

    for idx, page in enumerate(reader.pages):
        pypdf_text = (page.extract_text() or "").replace("\x00", "")
        fitz_text = fitz_page_texts[idx] if idx < len(fitz_page_texts) else ""
        text, backend = choose_page_text(pypdf_text, fitz_text)
        backend_counts[backend] = backend_counts.get(backend, 0) + 1
        if fitz_text:
            pypdf_norm = normalize_for_compare(pypdf_text)
            fitz_norm = normalize_for_compare(fitz_text)
            if pypdf_norm != fitz_norm:
                backend_comparison.append(
                    {
                        "page_number": idx + 1,
                        "pypdf_chars": len(pypdf_norm),
                        "pymupdf_chars": len(fitz_norm),
                        "selected_backend": backend,
                    }
                )
        text_sha = sha256_str(text)
        page_id = "pg_{}_{}".format(make_id(pdf_path, str(idx)), str(idx).zfill(3))
        page_obj = {
            "page_id": page_id,
            "page_number": idx + 1,
            "pdf_page_index": idx,
            "text": text,
            "text_sha256": text_sha,
            "text_extraction_backend": backend,
        }
        if fitz_text:
            page_obj["backend_text_sha256"] = {
                "pypdf": sha256_str(pypdf_text),
                "pymupdf": sha256_str(fitz_text),
            }
        pages.append(page_obj)
        page_text_map[idx] = text

        wcodes = detect_waste_codes(text)
        for wc, wt, full_line in wcodes:
            all_waste_codes.append((idx, wc, wt, full_line))

    all_paras = detect_paras_across_pages(page_text_map)
    all_annexes = detect_annexes_across_pages(page_text_map)

    doc_id = "doc_{}".format(make_id(pdf_path))
    source_pdf = os.path.basename(pdf_path)
    doc_key = doc_key_from_metadata(source_pdf, canonical_citation, doc_metadata)
    document_global_key = document_global_key_from_metadata(source_pdf, title, canonical_citation, doc_metadata)
    citation_prefix = doc_metadata.get("abbreviation") or canonical_citation or doc_key
    page_refs = write_page_files(pages, doc_key, output_base_dir, pages_root_dir)
    structural_units = []
    chunks = []
    issues = []
    issues.extend(detect_source_text_anomalies(doc_id, pages, rule_set))

    # Process paragraph units
    for para_idx, para in enumerate(all_paras):
        page_num = para["start_page"]
        end_page_num = para["end_page"]
        label = para["label"]
        title_text = para.get("title")
        number = paragraph_number(label)
        para_text = para["text"]
        unit_global_key = unit_slug(document_global_key, "para", number)
        unit_id = "unit_{}".format(unit_global_key)
        unit_citation = legal_citation(citation_prefix, "paragraph", label, title_text)
        page_id = pages[page_num]["page_id"]

        subsections = split_into_subsections(para_text)
        subsection_count = len(subsections)

        unit_obj = {
            "unit_id": unit_id,
            "global_key": unit_global_key,
            "legal_citation": unit_citation,
            "display_name": "{}{}".format(unit_citation, " {}".format(title_text) if title_text else ""),
            "document_id": doc_id,
            "document_key": doc_key,
            "document_global_key": document_global_key,
            "unit_type": "paragraph",
            "label": label,
            "number": number,
            "title": title_text,
            "breadcrumbs": [document_global_key, label],
            "parent_unit_id": None,
            "child_unit_ids": [],
            "page_range": {"start": page_num + 1, "end": end_page_num + 1},
            "text": para_text,
            "text_sha256": sha256_str(para_text),
            "confidence": 0.95,
            "review_status": "pending",
            "is_uncertain": False,
            "uncertainty_reason": None,
        }
        structural_units.append(unit_obj)

        for sub_idx, (sub_num, sub_text) in enumerate(subsections):
            if sub_num:
                chunk_type = "subsection"
                chunk_global_key = chunk_slug(unit_global_key, "abs", sub_num)
                chunk_label = "Abs. {}".format(sub_num)
            else:
                chunk_type = "paragraph_text"
                chunk_global_key = chunk_slug(unit_global_key, "text")
                chunk_label = label
            chunk_id = "chunk_{}".format(chunk_global_key)
            chunk_citation = make_subsection_citation(unit_citation, sub_num)
            chunk_obj = {
                "chunk_id": chunk_id,
                "global_key": chunk_global_key,
                "legal_citation": chunk_citation,
                "display_name": chunk_citation,
                "chunk_type": chunk_type,
                "unit_id": unit_id,
                "document_global_key": document_global_key,
                "parent_chunk_id": None,
                "child_chunk_ids": [],
                "label": chunk_label,
                "number": sub_num,
                "sequence": sub_idx + 1,
                "page_id": page_id,
                "page_range": {"start": page_num + 1, "end": end_page_num + 1},
                "text": sub_text,
                "text_sha256": sha256_str(sub_text),
                "evidence_text": para_text,
                "confidence": 0.9 if subsection_count > 1 else 1.0,
                "review_status": "pending",
            }
            chunks.append(chunk_obj)

    # Process Anlage units
    for annex_idx, annex in enumerate(all_annexes):
        page_num = annex["start_page"]
        end_page_num = annex["end_page"]
        label = annex["label"]
        title_text = annex.get("title")
        number = label.replace("Anlage", "").strip()
        annex_text = annex["text"]
        unit_global_key = unit_slug(document_global_key, "anlage", number)
        unit_id = "unit_{}".format(unit_global_key)
        unit_citation = legal_citation(citation_prefix, "annex", label, title_text)
        page_id = pages[page_num]["page_id"]

        unit_obj = {
            "unit_id": unit_id,
            "global_key": unit_global_key,
            "legal_citation": unit_citation,
            "display_name": "{}{}".format(unit_citation, " {}".format(title_text) if title_text else ""),
            "document_id": doc_id,
            "document_key": doc_key,
            "document_global_key": document_global_key,
            "unit_type": "annex",
            "label": label,
            "number": number,
            "title": title_text,
            "breadcrumbs": [document_global_key, label],
            "parent_unit_id": None,
            "child_unit_ids": [],
            "page_range": {"start": page_num + 1, "end": end_page_num + 1},
            "text": annex_text,
            "text_sha256": sha256_str(annex_text),
            "confidence": 0.85,
            "review_status": "pending",
            "is_uncertain": False,
            "uncertainty_reason": None,
        }
        structural_units.append(unit_obj)

        annex_chunks = split_annex_into_chunks(annex)
        for chunk_idx, annex_chunk in enumerate(annex_chunks):
            if annex_chunk["chunk_type"] == "table_block":
                add_table_from_block(
                    structural_units,
                    chunks,
                    doc_id,
                    doc_key,
                    document_global_key,
                    label,
                    unit_id,
                    unit_obj,
                    unit_citation,
                    unit_global_key,
                    pages,
                    annex_chunk,
                    0.70,
                    fitz_word_pages,
                    fitz_page_lines,
                )
                continue

            label_part = annex_chunk["label"] or "text"
            chunk_global_key = chunk_slug(unit_global_key, label_part)
            chunk_id = "chunk_{}".format(chunk_global_key)
            if annex_chunk["label"]:
                chunk_citation = "{} {}".format(unit_citation, annex_chunk["label"])
            else:
                chunk_citation = unit_citation
            chunk_text = annex_chunk["text"]
            chunk_obj = {
                "chunk_id": chunk_id,
                "global_key": chunk_global_key,
                "legal_citation": chunk_citation,
                "display_name": chunk_citation,
                "chunk_type": annex_chunk["chunk_type"],
                "unit_id": unit_id,
                "document_global_key": document_global_key,
                "parent_chunk_id": None,
                "child_chunk_ids": [],
                "label": annex_chunk["label"],
                "number": None,
                "sequence": chunk_idx + 1,
                "page_id": page_id,
                "page_range": annex_chunk.get("page_range", {"start": page_num + 1, "end": end_page_num + 1}),
                "text": chunk_text,
                "text_sha256": sha256_str(chunk_text),
                "evidence_text": annex_text,
                "confidence": 0.75,
                "review_status": "pending",
            }
            chunks.append(chunk_obj)

    # Process waste code units
    for wc_idx, (page_num, waste_code, waste_text, full_line) in enumerate(all_waste_codes):
        unit_global_key = chunk_slug(document_global_key, "waste_code", waste_code)
        unit_id = "unit_{}".format(unit_global_key)
        page_id = pages[page_num]["page_id"]

        unit_obj = {
            "unit_id": unit_id,
            "global_key": unit_global_key,
            "legal_citation": "{} waste code {}".format(citation_prefix, waste_code),
            "display_name": "{} waste code {}".format(citation_prefix, waste_code),
            "document_id": doc_id,
            "document_key": doc_key,
            "document_global_key": document_global_key,
            "unit_type": "waste_code",
            "label": waste_code,
            "number": waste_code,
            "title": waste_text,
            "breadcrumbs": [document_global_key, "waste_code", waste_code],
            "parent_unit_id": None,
            "child_unit_ids": [],
            "page_range": {"start": page_num + 1, "end": page_num + 1},
            "text": full_line,
            "text_sha256": sha256_str(full_line),
            "confidence": 0.90,
            "review_status": "pending",
            "is_uncertain": False,
            "uncertainty_reason": None,
        }
        structural_units.append(unit_obj)

        chunk_global_key = chunk_slug(unit_global_key, "text")
        chunk_id = "chunk_{}".format(chunk_global_key)
        chunk_obj = {
            "chunk_id": chunk_id,
            "global_key": chunk_global_key,
            "legal_citation": "{} waste code {}".format(citation_prefix, waste_code),
            "display_name": "{} waste code {}".format(citation_prefix, waste_code),
            "chunk_type": "waste_code_entry",
            "unit_id": unit_id,
            "document_global_key": document_global_key,
            "parent_chunk_id": None,
            "child_chunk_ids": [],
            "label": waste_code,
            "number": waste_code,
            "sequence": wc_idx + 1,
            "page_id": page_id,
            "page_range": {"start": page_num + 1, "end": page_num + 1},
            "text": full_line,
            "text_sha256": sha256_str(full_line),
            "evidence_text": full_line,
            "confidence": 0.90,
            "review_status": "pending",
        }
        chunks.append(chunk_obj)

    # Check for low paragraph count issue
    para_unit_count = sum(1 for u in structural_units if u["unit_type"] == "paragraph")
    if para_unit_count < 3:
        issue_id = "issue_{}_low_para".format(make_id(doc_id, "low_para"))
        issue_obj = {
            "issue_id": issue_id,
            "document_id": doc_id,
            "target_unit_id": None,
            "target_chunk_id": None,
            "issue_type": "low_paragraph_count",
            "severity": "warning",
            "description": "Document has fewer than 3 paragraph units (found {}).".format(para_unit_count),
            "evidence": "Only {} paragraph headings detected in PDF.".format(para_unit_count),
            "corrected_value": None,
            "review_status": "open",
        }
        issues.append(issue_obj)

    doc_obj = {
        "document_id": doc_id,
        "source_pdf": source_pdf,
        "sha256": doc_sha,
        "title": title,
        "full_citation": full_citation,
        "canonical_citation": canonical_citation,
        "date_enacted": date_enacted,
        "document_key": doc_key,
        "document_global_key": document_global_key,
        "global_key": document_global_key,
        "citation_prefix": citation_prefix,
        "abbreviation": doc_metadata.get("abbreviation"),
        "pages": page_refs,
        "page_refs": page_refs,
        "structural_units": structural_units,
        "chunks": chunks,
        "metadata": {
            "pdf_pages": len(reader.pages),
            "extractor": "extract_normtext",
            "pypdf_version": getattr(PdfReader, "__version__", "unknown"),
            "secondary_pdf_backend": "pymupdf" if fitz is not None else None,
            "text_backend_counts": backend_counts,
            "backend_comparison": backend_comparison,
            **doc_metadata,
        },
    }

    return doc_obj, issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract structured text from German legal PDFs.")
    parser.add_argument("--output", required=True, help="Output JSON path.")
    parser.add_argument(
        "--pages-dir",
        help="Directory for per-page JSON files. Defaults to <output-dir>/pages.",
    )
    parser.add_argument(
        "--rules-dir",
        help="Optional directory with JSON rule files for source-text anomaly checks.",
    )
    parser.add_argument("pdfs", nargs="+", help="PDF files to process.")
    args = parser.parse_args()

    output_path = args.output
    output_base_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_base_dir, exist_ok=True)
    pages_root_dir = args.pages_dir or os.path.join(output_base_dir, "pages")
    rule_set = load_rules(args.rules_dir)

    all_documents = []
    all_issues = []

    for pdf_path in args.pdfs:
        print("Processing: {}".format(pdf_path))
        doc_obj, issues = extract_document(pdf_path, output_base_dir, pages_root_dir, rule_set)
        all_documents.append(doc_obj)
        all_issues.extend(issues)

    output = {
        "schema_version": "1.0.0-draft",
        "phase": "normtext",
        "documents": all_documents,
        "review_decisions": [],
        "extraction_issues": all_issues,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print("Wrote {} documents to {}".format(len(all_documents), output_path))


if __name__ == "__main__":
    main()
