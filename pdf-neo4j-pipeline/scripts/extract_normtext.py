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
    r"^(?:Abs\.|Absatz|Satz|Nummer|Nr\.|Buchstabe|Buchst\.|§|und|oder|,)",
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
    r"Ein Service des Bundesministeriums der Justiz sowie des Bundesamts für|"
    r"Justiz\s+.+www\.gesetze-im-internet\.de|"
    r"-\s*Seite\s+\d+\s+von\s+\d+\s*-)$"
)
SPLIT_TABLE_SECTIONS_AS_UNITS = False


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
    return match


def match_annex_heading(stripped):
    match = ANNEX_RE.match(stripped)
    if not match:
        return None
    tail = (match.group(2) or "").strip()
    if tail and ANNEX_REFERENCE_TAIL_RE.match(tail):
        return None
    if tail and tail[0].islower() and not tail.startswith("zu "):
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


def filtered_document_lines(page_text_map):
    """Return document lines with the table of contents removed."""
    stream = document_line_stream(page_text_map)
    filtered = []
    in_toc = False
    for index, (page_idx, line) in enumerate(stream):
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

    for page_idx, line in filtered_document_lines(page_text_map):
        stripped = clean_line(line)
        if match_annex_heading(stripped):
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

    for page_idx, line in filtered_document_lines(page_text_map):
        stripped = clean_line(line)
        m = match_annex_heading(stripped)
        if m:
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
    first = normalize_for_compare(lines[index])
    if not first.startswith("Parameter "):
        return False
    lookahead = normalize_for_compare(" ".join(lines[index : index + 6]))
    if first.startswith("Parameter Dimension"):
        return "Bewertungs" in lookahead and "Norm Normbezeichnung" in lookahead
    if first.startswith("Parameter Dim."):
        return "Bestimmungsbereich" in lookahead and "Überschreitung" in lookahead
    return False


def collect_multiline_table_header(lines, header_index):
    header_parts = [lines[header_index]]
    header_end_index = header_index
    first = normalize_for_compare(lines[header_index])
    if first.startswith("Parameter Dimension"):
        for idx in range(header_index + 1, min(len(lines), header_index + 6)):
            header_parts.append(lines[idx])
            header_end_index = idx
            if "Normbezeichnung" in normalize_for_compare(lines[idx]):
                break
    elif first.startswith("Parameter Dim."):
        for idx in range(header_index + 1, min(len(lines), header_index + 4)):
            header_parts.append(lines[idx])
            header_end_index = idx
            combined = normalize_for_compare(" ".join(header_parts))
            if "Überschreitung" in combined and "%" in combined:
                break
    return normalize_for_compare(" ".join(header_parts)), header_end_index


def infer_table_columns(header_text):
    header = normalize_for_compare(header_text)
    if not header:
        return []
    if header.startswith("Parameter Dimension") and "Norm Normbezeichnung" in header:
        return ["Parameter", "Dimension", "Bewertungsrelevanter Bereich", "Norm", "Normbezeichnung"]
    if header.startswith("Parameter Dim.") and "Überschreitung" in header:
        return ["Parameter", "Dim.", "Bestimmungsbereich", "zulässige Überschreitung in %"]
    if " Konzentration " in header:
        before, after = header.split(" Konzentration ", 1)
        if before.strip() in ("Anorganische Stoffe", "Organische Stoffe"):
            before = "Stoff/Parameter"
        return [before.strip(), "Konzentration {}".format(after).strip()]
    if header.startswith("Untersuchungsparameter "):
        return ["Untersuchungsparameter", "Verfahrenshinweise", "Norm", "Ausgabe der Norm"]
    parts = [part.strip() for part in re.split(r"\s{2,}", header) if part.strip()]
    return parts or [header]


def concentration_section_label(header_text):
    header = normalize_for_compare(header_text)
    if " Konzentration " not in header:
        return None
    before = header.split(" Konzentration ", 1)[0].strip()
    if before in ("Anorganische Stoffe", "Organische Stoffe"):
        return before
    return None


def is_table_section_header(line):
    normalized = normalize_for_compare(line)
    if " Konzentration " not in normalized:
        return False
    if re.search(r"\d+(?:[,.]\d+)?", normalized):
        return False
    return concentration_section_label(normalized) is not None


def is_table_note_start(line):
    normalized = normalize_for_compare(line)
    return (
        normalized == "-----"
        or re.match(r"^\d+\)\s", normalized)
        or normalized.startswith("Für Salzbelastung")
        or normalized.startswith("Der pH-Wert")
        or normalized.startswith("nicht überschreiten")
        or normalized.startswith("ISO-Normen")
    )


def is_method_table_header(header_text):
    return normalize_for_compare(header_text).startswith("Untersuchungsparameter ")


def method_row_starts():
    return (
        "pH-Wert",
        "Trockenrückstand",
        "Cyanid, gesamt",
        "Cyanid, leicht freisetzbar",
        "Arsen",
        "Blei",
        "Cadmium",
        "Chrom",
        "Chrom, gesamt",
        "Chromat",
        "Kupfer",
        "Nickel",
        "Zink",
        "Quecksilber",
        "Mineralölkohlenwasserstoffe",
        "Leichtflüchtige",
        "Benzol und Derivate",
        "BTEX",
        "Polycyclische aromatische",
        "PAK, gesamt",
        "Naphthalin",
        "Polychlorierte Biphenyle",
        "PCB, gesamt",
        "TOC",
        "Glühverlust",
        "Elektrische Leitfähigkeit",
        "Gesamttrockenrückstand",
        "für alle Elemente",
    )


def is_method_row_start(line):
    normalized = normalize_for_compare(line)
    for start in method_row_starts():
        if re.match(r"^{}(?:$|[\s:])".format(re.escape(start)), normalized):
            return True
    return False


def split_embedded_method_rows(line):
    """Split PDF extraction joins such as '... 1981Cyanid, leicht ...'."""
    parts = [line]
    for marker in method_row_starts():
        next_parts = []
        for part in parts:
            idx = part.find(marker)
            if idx > 0 and re.search(r"\d{4}$", part[:idx].strip()):
                next_parts.append(part[:idx].strip())
                next_parts.append(part[idx:].strip())
            else:
                next_parts.append(part)
        parts = next_parts
    return parts


def method_row_has_method(row_lines):
    text = normalize_for_compare(" ".join(row_lines))
    method_markers = (
        "DIN ",
        "DIN-",
        "ISO",
        "Merkblatt",
        "Gaschromatographie",
        "AAS",
        "ICP",
        "HPLC",
        "GC-",
        "GC/",
        "Elementaranalyse",
        "Wasserbeschaffenheit",
        "Bodenbeschaffenheit",
        "Deutsche Einheitsverfahren",
    )
    return any(marker in text for marker in method_markers)


def parse_method_table_rows(raw_row_items, header_text):
    rows = []
    row_page_ranges = []
    notes = []
    note_page_ranges = []
    current = []
    current_pages = []
    in_notes = False
    normalized_header = normalize_for_compare(header_text)

    split_lines = []
    for line, page_number in raw_row_items:
        for split_line in split_embedded_method_rows(line):
            split_lines.append((split_line, page_number))

    for line, page_number in split_lines:
        normalized = normalize_for_compare(line)
        if not normalized:
            continue
        if normalized == normalized_header:
            continue
        if is_table_note_start(line):
            in_notes = True
        if in_notes:
            if current:
                rows.append(" ".join(current).strip())
                row_page_ranges.append(merge_page_ranges(current_pages))
                current = []
                current_pages = []
            notes.append(line)
            note_page_ranges.append(page_range_from_page(page_number))
            continue
        if is_method_row_start(line) and current and method_row_has_method(current):
            rows.append(" ".join(current).strip())
            row_page_ranges.append(merge_page_ranges(current_pages))
            current = [line]
            current_pages = [page_range_from_page(page_number)]
            continue
        if current:
            current.append(line)
            current_pages.append(page_range_from_page(page_number))
        else:
            current = [line]
            current_pages = [page_range_from_page(page_number)]

    if current:
        rows.append(" ".join(current).strip())
        row_page_ranges.append(merge_page_ranges(current_pages))
    return rows, row_page_ranges, notes, note_page_ranges
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


def is_material_class_header_row(row):
    text = positioned_row_text(row)
    class_markers = (
        "RC-",
        "HOS-",
        "SWS-",
        "CUM-",
        "HMVA-",
        "BM-",
        "BG-",
        "GS-",
        "SKG",
        "SKA",
        "SFA",
        "BFA",
        "GKOS",
        "GRS",
    )
    return text.startswith(("MEB ", "BM ", "BG ", "GS ")) and sum(1 for marker in class_markers if marker in text) >= 2


def row_contains_parameter_dim(row):
    text = positioned_row_text(row)
    return "Parameter" in text and re.search(r"\b(?:Dim\.?|Dimension)\b", text)


def row_dim_center(row):
    dim_word = next((word for word in row if word[4].startswith(("Dim", "Dimension"))), None)
    if dim_word is None:
        return None
    return (dim_word[0] + dim_word[2]) / 2


def is_material_header_continuation(row, dim_center):
    if dim_center is None or not row:
        return False
    if any((word[0] + word[2]) / 2 <= dim_center + 8 for word in row):
        return False
    text = positioned_row_text(row)
    return any(marker in text for marker in ("BG-", "BM-", "GS-", "RC-", "SWS-", "HOS-", "HMVA-"))


def material_header_columns(header_row, parameter_dim_row=None, continuation_row=None):
    dim_row = parameter_dim_row or header_row
    parameter_word = next((word for word in dim_row if word[4].startswith("Parameter")), None)
    dim_word = next((word for word in dim_row if word[4].startswith(("Dim", "Dimension"))), None)
    if parameter_word is None or dim_word is None:
        return None
    dim_center = (dim_word[0] + dim_word[2]) / 2
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
    columns = ["Parameter", "Dim."] + material_columns
    centers = [
        (parameter_word[0] + parameter_word[2]) / 2,
        dim_center,
    ] + [
        (word[0] + word[2]) / 2 for word in material_header_words
    ]
    return columns, centers


def material_table_stop_row(row):
    text = positioned_row_text(row)
    if not text:
        return True
    if PAGE_HEADER_RE.match(text):
        return True
    if re.match(r"^(?:Fortsetzung\s+)?Tabelle\s+\d+[a-z]?:", text):
        return True
    if re.match(r"^\d+\s+", text) and any(marker in text for marker in ("Nur ", "Stoffspezifischer", "PAK", "In Gebieten")):
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
    return cells[0] in ("Anorganische Stoffe", "Organische Stoffe") and not any(cells[1:])


def parse_material_panel_rows(page_rows, start_idx, end_idx, columns, centers, page_number):
    boundaries = [-float("inf")]
    for left, right in zip(centers, centers[1:]):
        boundaries.append((left + right) / 2)
    boundaries.append(float("inf"))

    parsed_rows = []
    row_page_ranges = []
    current = None
    current_page_range = None
    for row in page_rows[start_idx:end_idx]:
        if material_table_stop_row(row) or is_material_class_header_row(row) or row_contains_parameter_dim(row):
            break
        if positioned_row_text(row) in ("Anorganische Stoffe", "Organische Stoffe"):
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
            key = (
                normalize_for_compare(row.get("Parameter", "")),
                normalize_for_compare(row.get("Dim.", "")),
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


GROUNDWATER_COVER_COLUMNS = [
    "Eigenschaft der Grundwasserdeckschicht, außerhalb von Wasserschutzbereichen, ungünstig, 1",
    "Eigenschaft der Grundwasserdeckschicht, außerhalb von Wasserschutzbereichen, günstig, Sand, 2",
    "Eigenschaft der Grundwasserdeckschicht, außerhalb von Wasserschutzbereichen, günstig, Lehm, Schluff, Ton, 3",
    "Eigenschaft der Grundwasserdeckschicht, innerhalb von Wasserschutzbereichen, günstig, WSG III A, HSG III, Sand, 4",
    "Eigenschaft der Grundwasserdeckschicht, innerhalb von Wasserschutzbereichen, günstig, WSG III A, HSG III, Lehm, Schluff, Ton, 4",
    "Eigenschaft der Grundwasserdeckschicht, innerhalb von Wasserschutzbereichen, günstig, WSG III B, HSG IV, Sand, 5",
    "Eigenschaft der Grundwasserdeckschicht, innerhalb von Wasserschutzbereichen, günstig, WSG III B, HSG IV, Lehm, Schluff, Ton, 5",
    "Eigenschaft der Grundwasserdeckschicht, innerhalb von Wasserschutzbereichen, günstig, Wasservorranggebiete, Sand, 6",
    "Eigenschaft der Grundwasserdeckschicht, innerhalb von Wasserschutzbereichen, günstig, Wasservorranggebiete, Lehm, Schluff, Ton, 6",
]


def parse_table_title_from_text(table_text, fallback):
    first_line = next((line.strip() for line in table_text.splitlines() if line.strip()), "")
    match = re.match(r"^(Tabelle\s+\d+[a-z]?)\s*:?\s*(.*)$", first_line)
    if match and match.group(2).strip():
        return match.group(2).strip()
    return fallback


def is_groundwater_installation_table_text(table_text):
    normalized = normalize_for_compare(table_text)
    return (
        "Eigenschaft der Grundwasserdeckschicht" in normalized
        and "Einbauweise" in normalized
        and "Wasserschutzbereichen" in normalized
    )


def is_table_label_row(row):
    text = positioned_row_text(row)
    return bool(re.match(r"^(?:Fortsetzung\s+)?Tabelle\s+\d+[a-z]?:", text))


def row_starts_groundwater_footnote(row):
    words = sorted(row, key=lambda word: word[0])
    if len(words) < 2:
        return False
    first = words[0][4]
    rest = normalize_for_compare(" ".join(word[4] for word in words[1:5]))
    return bool(re.match(r"^\d+$", first)) and rest.startswith(("Zulässig", "Zugelassen", "Nicht zugelassen"))


def row_is_groundwater_column_number_row(row):
    tokens = [word[4] for word in row if word[0] > 145]
    return tokens == ["1", "2", "3", "4", "5", "6"]


def nested_column_number_tokens(row):
    tokens = [word[4] for word in row if word[0] > 145 and re.match(r"^\d+$", word[4])]
    if len(tokens) < 3:
        return []
    expected = [str(idx) for idx in range(1, len(tokens) + 1)]
    return tokens if tokens == expected else []


def row_groundwater_values(row, min_x=145):
    value_words = [
        word for word in row
        if word[0] > min_x and re.match(r"^(?:[+–-](?:\d+)?|[KM])$", word[4])
    ]
    return value_words


def infer_groundwater_value_centers(page_rows, start_idx, end_idx):
    for row in page_rows[start_idx:end_idx]:
        value_words = row_groundwater_values(row, 145)
        if len(value_words) >= 8:
            return sorted((word[0] + word[2]) / 2 for word in value_words[:9])
    return [221.7, 265.9, 304.9, 344.0, 383.0, 422.0, 461.0, 501.0, 543.0]


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


def assign_header_groups_to_centers(groups, value_centers):
    assignments = [[] for _center in value_centers]
    if not groups:
        return assignments
    group_centers = [
        sum((word[0] + word[2]) / 2 for word in group) / len(group)
        for group in groups
    ]
    boundaries = [-float("inf")]
    for left, right in zip(group_centers, group_centers[1:]):
        boundaries.append((left + right) / 2)
    boundaries.append(float("inf"))
    for group_idx, group in enumerate(groups):
        text = header_group_text(group)
        if not text:
            continue
        for center_idx, center in enumerate(value_centers):
            if boundaries[group_idx] <= center < boundaries[group_idx + 1]:
                assignments[center_idx].append(text)
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
    if "Einbauweise" in candidates:
        return "Einbauweise"
    return candidates[-1] if candidates else "Zeile"


def derive_nested_symbol_columns(page_rows, page_start_idx, number_row_idx, value_centers, description_max_x, title):
    header_rows = page_rows[page_start_idx:number_row_idx]
    row_label = detect_row_header_label(header_rows, description_max_x)
    paths = [[] for _center in value_centers]
    normalized_title = normalize_for_compare(title or "")

    for row in header_rows:
        text = positioned_row_text(row)
        if not text or PAGE_HEADER_RE.match(text) or is_table_label_row(row):
            continue
        if normalized_title and normalize_for_compare(text) == normalized_title:
            continue
        if normalize_for_compare(text) == row_label:
            continue
        groups = group_header_row_words(row, description_max_x)
        for center_idx, labels in enumerate(assign_header_groups_to_centers(groups, value_centers)):
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

    if row_label == "Einbauweise":
        row_number_key = "Einbauweise Nummer"
        row_label_key = "Einbauweise"
    else:
        row_number_key = "{} Nummer".format(row_label)
        row_label_key = row_label
    return row_number_key, row_label_key, value_columns


def assign_symbol_values(row_obj, value_words, value_centers, value_columns):
    for word in value_words:
        center = (word[0] + word[2]) / 2
        idx = min(range(len(value_centers)), key=lambda i: abs(value_centers[i] - center))
        if idx >= len(value_columns) or abs(value_centers[idx] - center) > 28:
            continue
        column = value_columns[idx]
        if row_obj[column]:
            row_obj[column] = "{} {}".format(row_obj[column], word[4]).strip()
        else:
            row_obj[column] = word[4]


def new_symbol_table_row(number, row_number_key, row_label_key, value_columns, page_number):
    row = {
        row_number_key: number,
        row_label_key: "",
    }
    for column in value_columns:
        row[column] = ""
    return row, page_range_from_page(page_number)


def parse_geometric_groundwater_table(table, block, fitz_word_pages):
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
            if row_starts_groundwater_footnote(page_rows[idx]):
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

        value_centers = infer_groundwater_value_centers(page_rows, data_start_idx, page_end_idx)
        symbol_rows = [
            row for row in page_rows[data_start_idx:page_end_idx]
            if len(row_groundwater_values(row, 145)) >= 5
        ]
        if len(symbol_rows) < 1:
            continue
        description_max_x = value_centers[0] - 20
        row_number_key, row_label_key, value_columns = derive_nested_symbol_columns(
            page_rows,
            page_start_idx,
            number_row_idx,
            value_centers,
            description_max_x,
            title,
        )
        if is_groundwater_installation_table_text(table_text):
            row_number_key = "Einbauweise Nummer"
            row_label_key = "Einbauweise"
            value_columns = GROUNDWATER_COVER_COLUMNS
        for row in page_rows[data_start_idx:page_end_idx]:
            if PAGE_HEADER_RE.match(positioned_row_text(row)) or row_starts_groundwater_footnote(row):
                continue
            words = sorted(row, key=lambda word: word[0])
            if not words:
                continue
            first_word = words[0]
            starts_new = first_word[0] < 66 and re.match(r"^\d+$", first_word[4])
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
                description_words = [
                    word[4] for word in sorted(words, key=lambda candidate: (candidate[1], candidate[0]))
                    if 66 <= word[0] < description_max_x
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
            assign_symbol_values(
                current,
                row_groundwater_values(row, description_max_x),
                value_centers,
                value_columns,
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


def parse_geometric_material_table(table, block, fitz_word_pages):
    if not fitz_word_pages:
        return None
    title = table.get("title") or ""
    if "Materialwerte" not in title:
        return None
    line_pages = [page for page in block.get("line_pages", []) if page is not None]
    if not line_pages:
        return None

    label = table.get("label") or block.get("label") or "Tabelle"
    table_number_match = re.search(r"\d+[a-z]?", label)
    table_number = table_number_match.group(0) if table_number_match else None
    first_page_idx = min(line_pages)
    sections = []

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
        for row_idx, row in enumerate(page_rows):
            if row_idx < page_start_idx or row_idx >= page_end_idx:
                continue
            header = None
            data_start_idx = None
            if is_material_class_header_row(row):
                if row_idx + 1 >= len(page_rows) or not row_contains_parameter_dim(page_rows[row_idx + 1]):
                    continue
                header = material_header_columns(row, page_rows[row_idx + 1])
                data_start_idx = row_idx + 2
            elif row_contains_parameter_dim(row):
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
                if re.match(r"^\d+\s+", next_text) and any(
                    marker in next_text for marker in ("Nur ", "Stoffspezifischer", "PAK", "In Gebieten")
                ):
                    end_idx = next_idx
                    break

            rows, row_page_ranges = parse_material_panel_rows(
                page_rows,
                data_start_idx,
                end_idx,
                columns,
                centers,
                page_idx + 1,
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
    header_keywords = ("Konzentration", "Verfahrenshinweise", "Parameter", "Norm", "Ausgabe")
    for idx, line in enumerate(lines[header_search_start:], start=header_search_start):
        if any(keyword in line for keyword in header_keywords):
            header_index = idx
            break
        title_lines.append(line)

    if header_index is None:
        header_index = header_search_start if len(lines) > header_search_start else 0
        title_lines = []

    header_text = lines[header_index] if header_index < len(lines) else ""
    header_end_index = header_index
    if header_text.startswith("Parameter "):
        header_text, header_end_index = collect_multiline_table_header(lines, header_index)
    raw_row_items = line_items[header_end_index + 1 :]
    if is_method_table_header(header_text):
        row_lines, row_page_ranges, note_lines, note_page_ranges = parse_method_table_rows(
            raw_row_items,
            header_text,
        )
        columns = infer_table_columns(header_text)
        num_columns = len(columns)
        parsed_rows = [split_row_into_cells(r, num_columns) for r in row_lines]
        section = {
            "section_label": None,
            "columns": columns,
            "column_header_text": header_text,
            "rows": parsed_rows,
            "row_page_ranges": row_page_ranges,
            "notes": note_lines,
            "note_page_ranges": note_page_ranges,
        }
        return {
            "label": label,
            "title": " ".join(title_lines).strip() or None,
            "columns": infer_table_columns(header_text),
            "column_header_text": header_text,
            "rows": row_lines,
            "row_page_ranges": row_page_ranges,
            "notes": note_lines,
            "note_page_ranges": note_page_ranges,
            "sections": [section],
        }

    sections = [
        {
            "section_label": concentration_section_label(header_text),
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
                "section_label": concentration_section_label(normalized),
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
        if " Konzentration " in normalized and not re.search(r"\d+(?:[,.]\d+)?", normalized):
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
):
    table_text = "\n".join(block.get("lines", [])) if block.get("lines") else block["text"]
    table = parse_table_block(
        table_text,
        block.get("line_pages"),
        label_override=block.get("label") if block.get("is_implicit_table") else None,
    )
    geometric_table = parse_geometric_groundwater_table(table, block, fitz_word_pages)
    if geometric_table is None:
        geometric_table = parse_geometric_material_table(table, block, fitz_word_pages)
    if geometric_table is not None:
        table = geometric_table
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


def detect_source_text_anomalies(doc_id, pages):
    """Flag likely source/PDF text defects without silently rewriting the law."""
    issues = []
    seen = set()
    patterns = [
        (
            re.compile(r"Das Bundesministerium .+ die Zahl der .+ und$"),
            "possible_missing_verb_in_source_text",
            "Line looks grammatically incomplete in the extracted source text. "
            "Do not auto-correct norm text; verify against another authoritative source.",
        ),
    ]
    for page in pages:
        for raw_line in page.get("text", "").splitlines():
            line = normalize_for_compare(raw_line)
            for regex, issue_type, description in patterns:
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
                            "warning",
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

def extract_document(pdf_path, output_base_dir=None, pages_root_dir=None):
    raw, reader = read_pdf(pdf_path)
    doc_sha = sha256_bytes(raw)
    title, date_enacted, full_citation, canonical_citation, doc_metadata = extract_meta(reader, pdf_path)
    fitz_page_texts = extract_fitz_page_texts(pdf_path)
    fitz_word_pages = extract_fitz_page_words(pdf_path)

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
    issues.extend(detect_source_text_anomalies(doc_id, pages))

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
    parser.add_argument("pdfs", nargs="+", help="PDF files to process.")
    args = parser.parse_args()

    output_path = args.output
    output_base_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_base_dir, exist_ok=True)
    pages_root_dir = args.pages_dir or os.path.join(output_base_dir, "pages")

    all_documents = []
    all_issues = []

    for pdf_path in args.pdfs:
        print("Processing: {}".format(pdf_path))
        doc_obj, issues = extract_document(pdf_path, output_base_dir, pages_root_dir)
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
