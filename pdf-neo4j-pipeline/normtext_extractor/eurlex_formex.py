"""Canonical extraction adapter for consolidated EUR-Lex Formex packages.

The adapter intentionally emits the same ``Document`` / ``StructuralUnit`` /
``Chunk`` contract as the GII XML and legacy PDF extractors.  Formex remains
the immutable source of truth; this module only derives deterministic legal
addresses, retrievable text chunks, and a lossless table representation.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from xml.etree import ElementTree as ET

from .gii_xml_tables import columns_from_header_matrix, normalize_inline_text
from .table_chunking import (
    DEFAULT_ENCODING,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TARGET_TOKENS,
    split_table_rows,
    table_rows_text,
    token_count,
)


EURLEX_FORMEX_EXTRACTOR_VERSION = "1.0.0"
TEXT_CHUNK_MAX_TOKENS = 1800
TEXT_CHUNK_TARGET_TOKENS = 1500
BLOCK_TAGS = {
    "ALINEA",
    "CONSID",
    "DD",
    "DEFINITION",
    "DIVISION",
    "DLIST.ITEM",
    "FINAL",
    "GR.SEQ",
    "ITEM",
    "LIST",
    "NP",
    "P",
    "PARAG",
    "PREAMBLE.FINAL",
    "PREAMBLE.INIT",
    "ROW",
    "TITLE",
    "VISA",
}
STRUCTURAL_SKIP_TAGS = {"ARTICLE", "CONS.ANNEX", "DIVISION", "GR.SEQ", "TBL"}
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
ARTICLE_NUMBER_RE = re.compile(r"(?:Artikel|Article)\s+([0-9]+[a-z]?)", re.IGNORECASE)
ANNEX_NUMBER_RE = re.compile(
    r"(?:ANHANG|ANLAGE|ANNEX)\s+([IVXLCDM]+|[0-9]+[A-Z]?)\b",
    re.IGNORECASE,
)
TABLE_NUMBER_RE = re.compile(r"(?:Tabelle|Table)\s+([A-Za-z0-9.\-]+)", re.IGNORECASE)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes((value or "").encode("utf-8"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("§", " para ").replace("&", " und ")
    text = (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "unbezeichnet"


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "\0".join(str(part or "") for part in parts)
    return "{}_{}".format(prefix, sha256_text(raw)[:16])


def normalize_text(value: str) -> str:
    lines: List[str] = []
    for line in (value or "").replace("\u00a0", " ").splitlines():
        line = WHITESPACE_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def _note_marker(element: ET.Element) -> str:
    reference = element.get("NOTE.REF")
    if reference and not normalize_text("".join(element.itertext())):
        return "[Fußnote {}]".format(reference)
    return ""


def element_text(
    element: Optional[ET.Element],
    *,
    skip_tags: Iterable[str] = (),
) -> str:
    """Render mixed Formex content without flattening embedded structures twice."""

    if element is None:
        return ""
    skipped = set(skip_tags)
    parts: List[str] = []

    def visit(node: ET.Element, *, is_root: bool = False) -> None:
        if not is_root and node.tag in skipped:
            return
        if node.tag == "INCL.ELEMENT":
            source = node.get("FILEREF") or "ohne Dateiname"
            kind = node.get("CONTENT") or "Abbildung"
            parts.append("[{}: {}]".format(kind, source))
        elif node.tag == "NOTE":
            marker = _note_marker(node)
            if marker:
                parts.append(marker)
            else:
                if node.text:
                    parts.append(node.text)
                for child in node:
                    visit(child)
                    if child.tail:
                        parts.append(child.tail)
        elif node.tag == "QUOT.START":
            parts.append(node.text or "„")
        elif node.tag == "QUOT.END":
            parts.append(node.text or "“")
        else:
            if node.text:
                parts.append(node.text)
            for child in node:
                if child.tag not in skipped:
                    visit(child)
                if child.tail:
                    parts.append(child.tail)
        if node.tag in BLOCK_TAGS:
            parts.append("\n")

    visit(element, is_root=True)
    return normalize_text("".join(parts))


def _direct_nested_tables(node: ET.Element) -> List[ET.Element]:
    result: List[ET.Element] = []

    def visit(parent: ET.Element) -> None:
        for child in parent:
            if child.tag == "TBL":
                result.append(child)
            else:
                visit(child)

    visit(node)
    return result


def _cell_assets(cell: ET.Element) -> List[Dict[str, Any]]:
    return [
        {
            "kind": asset.get("CONTENT") or "OTHER",
            "source": asset.get("FILEREF"),
            "media_type": asset.get("TYPE"),
            "attributes": dict(asset.attrib),
        }
        for asset in cell.iter("INCL.ELEMENT")
    ]


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value or ""))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_formex_rows(
    rows: Sequence[ET.Element],
    num_columns: int,
) -> Dict[str, Any]:
    expanded_matrix: List[List[str]] = []
    cells: List[Dict[str, Any]] = []
    active_spans: Dict[int, Dict[str, Any]] = {}

    for row_index, row in enumerate(rows):
        expanded = [""] * num_columns
        occupied: Set[int] = set()
        for column, span in list(active_spans.items()):
            if span["row_end"] < row_index:
                active_spans.pop(column, None)
                continue
            expanded[column] = span["text"]
            occupied.add(column)

        cursor = 0
        for cell in row.findall("./CELL"):
            declared = _positive_int(cell.get("COL"), cursor + 1) - 1
            start = max(0, min(declared, num_columns - 1))
            while start in occupied and start < num_columns - 1:
                start += 1
            col_span = _positive_int(cell.get("COLSPAN"), 1)
            row_span = _positive_int(cell.get("ROWSPAN"), 1)
            end = min(num_columns - 1, start + col_span - 1)
            text = element_text(cell, skip_tags={"TBL"})
            is_header = (
                str(row.get("TYPE") or "").upper() == "HEADER"
                or str(cell.get("TYPE") or "").upper() == "HEADER"
            )
            assets = _cell_assets(cell) if cell.find(".//INCL.ELEMENT") is not None else []
            nested_elements = _direct_nested_tables(cell)
            cell_record = {
                "cell_index": len(cells),
                "text": text,
                "row_start": row_index,
                "row_end": row_index + row_span - 1,
                "row_span": row_span,
                "col_start": start,
                "col_end": end,
                "col_span": end - start + 1,
                "is_header": is_header,
                "attributes": dict(cell.attrib),
                "asset_refs": assets,
                "nested_tables": [
                    parse_formex_table(nested)
                    for nested in nested_elements
                ],
            }
            # Normal cells are already represented exactly once in the row
            # matrix. Keep coordinate records only where they carry additional
            # structure; this prevents half a million redundant cell objects in
            # the largest consolidated classification tables.
            if row_span > 1 or col_span > 1 or is_header or assets or nested_elements:
                cells.append(cell_record)
            for column in range(start, end + 1):
                expanded[column] = text
                occupied.add(column)
                if row_span > 1:
                    active_spans[column] = {
                        "row_end": row_index + row_span - 1,
                        "text": text,
                    }
            cursor = end + 1
        expanded_matrix.append(expanded)

    return {
        "expanded_matrix": expanded_matrix,
        "cells": cells,
    }


def _header_row_count(rows: Sequence[ET.Element], matrix: Sequence[Sequence[str]]) -> int:
    count = 0
    for row in rows:
        row_header = str(row.get("TYPE") or "").upper() == "HEADER"
        cells = row.findall("./CELL")
        cells_header = bool(cells) and all(
            str(cell.get("TYPE") or "").upper() == "HEADER" for cell in cells
        )
        if row_header or cells_header:
            count += 1
        else:
            break

    # Formex frequently marks a full-width caption as HEADER but leaves the
    # following actual column labels untyped. Promote exactly that next row.
    if count and count < len(matrix):
        header = matrix[:count]
        distinct_by_column = {
            tuple(row[column] if column < len(row) else "" for row in header)
            for column in range(len(matrix[0]) if matrix else 0)
        }
        next_nonempty = sum(bool(value) for value in matrix[count])
        if len(distinct_by_column) <= 1 and next_nonempty >= 2:
            count += 1
    return count


def _unique_columns(columns: Sequence[str]) -> List[str]:
    result: List[str] = []
    counts: Dict[str, int] = {}
    for index, raw in enumerate(columns):
        base = normalize_inline_text(raw) or "column_{}".format(index + 1)
        counts[base] = counts.get(base, 0) + 1
        result.append(base if counts[base] == 1 else "{}__{}".format(base, counts[base]))
    return result


def parse_formex_table(table: ET.Element) -> Dict[str, Any]:
    """Parse a Formex ``TBL`` into a span-aware, row-oriented representation."""

    corpus = table.find("./CORPUS")
    rows = corpus.findall("./ROW") if corpus is not None else []
    declared_columns = _positive_int(table.get("COLS"), 1)
    observed_columns = 0
    for row in rows:
        for cell in row.findall("./CELL"):
            start = _positive_int(cell.get("COL"), observed_columns + 1)
            observed_columns = max(
                observed_columns,
                start + _positive_int(cell.get("COLSPAN"), 1) - 1,
            )
    num_columns = max(declared_columns, observed_columns, 1)
    parsed = _parse_formex_rows(rows, num_columns)
    matrix = parsed["expanded_matrix"]
    header_count = _header_row_count(rows, matrix)
    header_matrix = matrix[:header_count]
    body_matrix = matrix[header_count:]
    columns = _unique_columns(columns_from_header_matrix(header_matrix, num_columns))
    mappings = [
        {
            column: normalize_inline_text(row[index] if index < len(row) else "")
            for index, column in enumerate(columns)
        }
        for row in body_matrix
    ]
    title = element_text(table.find("./TITLE"), skip_tags={"TBL"})
    notes = [
        {
            "note_id": note.get("NOTE.ID") or note.get("NUMBER.ORG"),
            "text": element_text(note, skip_tags={"TBL"}),
            "attributes": dict(note.attrib),
        }
        for group in table.findall("./GR.NOTES")
        for note in group.findall("./NOTE")
        if element_text(note, skip_tags={"TBL"})
    ]
    return {
        "num_columns": num_columns,
        "columns": columns,
        "rows": mappings,
        "header_matrix": header_matrix,
        "body_matrix": body_matrix,
        "cells": parsed["cells"],
        "title": title,
        "notes": notes,
        "attributes": dict(table.attrib),
    }


def compact_formex_table(table: Mapping[str, Any]) -> Dict[str, Any]:
    """Return structural metadata without duplicating all retrievable rows.

    The row mappings live on table chunks and the immutable Formex XML remains
    authoritative.  Unit-level metadata therefore only needs headers, span or
    asset-bearing cells, notes, and shape information.
    """

    return {
        "num_columns": table.get("num_columns"),
        "columns": copy.deepcopy(table.get("columns") or []),
        "header_matrix": copy.deepcopy(table.get("header_matrix") or []),
        "row_count": len(table.get("rows") or []),
        "cells": copy.deepcopy(table.get("cells") or []),
        "title": table.get("title") or "",
        "notes": copy.deepcopy(table.get("notes") or []),
        "attributes": copy.deepcopy(table.get("attributes") or {}),
    }


def _content_member(
    archive: zipfile.ZipFile,
    record: Mapping[str, Any],
) -> zipfile.ZipInfo:
    preferred = str(record.get("content_member") or "")
    if preferred:
        try:
            info = archive.getinfo(preferred)
        except KeyError:
            info = None
        if info is not None:
            return info
    candidates = [
        item
        for item in archive.infolist()
        if item.filename.lower().endswith(".xml")
        and not item.filename.lower().endswith(".doc.xml")
    ]
    if not candidates:
        raise ValueError("Formex package has no content XML member")
    return max(candidates, key=lambda item: item.file_size)


def _load_package(
    source_path: os.PathLike[str] | str,
    record: Mapping[str, Any],
) -> Dict[str, Any]:
    path = Path(source_path)
    package_hash = file_sha256(path)
    with zipfile.ZipFile(path) as archive:
        info = _content_member(archive, record)
        xml_bytes = archive.read(info)
        members = [
            {
                "name": item.filename,
                "bytes": item.file_size,
                "compressed_bytes": item.compress_size,
            }
            for item in archive.infolist()
        ]
    root = ET.fromstring(xml_bytes)
    if root.tag != "CONS.ACT":
        raise ValueError("expected CONS.ACT root, got {!r}".format(root.tag))
    return {
        "path": path,
        "package_sha256": package_hash,
        "package_bytes": path.stat().st_size,
        "xml_name": info.filename,
        "xml_bytes": xml_bytes,
        "xml_sha256": sha256_bytes(xml_bytes),
        "members": members,
        "root": root,
    }


def _base_celex_parts(base_celex: str) -> Tuple[str, str, str]:
    match = re.fullmatch(r"3(?P<year>\d{4})(?P<kind>[A-Z])(?P<number>\d{4})", base_celex or "")
    if not match:
        return "", "", ""
    return match.group("year"), match.group("kind"), str(int(match.group("number")))


def eu_citation_aliases(base_celex: str, descriptor: str) -> List[str]:
    """Return common German citation spellings for deterministic resolution."""

    year, _kind, number = _base_celex_parts(base_celex)
    act_type = {"R": "Verordnung", "L": "Richtlinie"}.get(descriptor, "Rechtsakt")
    aliases = {
        base_celex,
        "CELEX:{}".format(base_celex),
        "{} {}/{}".format(act_type, year, number),
        "{} {}/{}".format(act_type, number, year),
    }
    for code in ("EU", "EG", "EWG", "EAG"):
        aliases.update(
            {
                "{} ({}) {}/{}".format(act_type, code, year, number),
                "{} ({}) Nr. {}/{}".format(act_type, code, number, year),
                "{} {}/{}/{}".format(act_type, year, number, code),
                "{} {}/{}/{}".format(act_type, number, year, code),
            }
        )
    return sorted(alias for alias in aliases if year and number)


def _legal_status(info: Mapping[str, Any]) -> Tuple[str, Optional[bool]]:
    status = str(info.get("END") or "unknown").lower()
    if status == "none":
        return status, True
    if status == "repealed":
        return status, False
    return status, None


def _number_from_text(pattern: re.Pattern[str], text: str, fallback: str) -> str:
    match = pattern.search(text or "")
    return match.group(1) if match else fallback


def _paragraph_number(paragraph: ET.Element, fallback: str) -> str:
    label = element_text(paragraph.find("./NO.PARAG"))
    match = re.search(r"([0-9]+[a-z]?)", label, re.IGNORECASE)
    if match:
        return match.group(1)
    identifier = str(paragraph.get("IDENTIFIER") or "")
    if "." in identifier:
        tail = identifier.rsplit(".", 1)[-1]
        if tail.isdigit():
            return str(int(tail))
    return fallback


def _split_long_text(
    text: str,
    *,
    encoding_name: str = DEFAULT_ENCODING,
    max_tokens: int = TEXT_CHUNK_MAX_TOKENS,
    target_tokens: int = TEXT_CHUNK_TARGET_TOKENS,
) -> List[str]:
    text = normalize_text(text)
    if not text or token_count(text, encoding_name) <= max_tokens:
        return [text] if text else []

    blocks: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if token_count(line, encoding_name) <= target_tokens:
            blocks.append(line)
            continue
        sentences = re.split(r"(?<=[.!?;:])\s+(?=[A-ZÄÖÜ0-9(])", line)
        blocks.extend(sentence for sentence in sentences if sentence)

    # Token slicing is the final lossless fallback for a single very long cell
    # or formula description. tiktoken decode preserves the source text.
    from .table_chunking import _encoding  # local import; shared cached encoder

    encoder = _encoding(encoding_name)
    atomic: List[str] = []
    for block in blocks:
        ids = encoder.encode(block)
        if len(ids) <= target_tokens:
            atomic.append(block)
        else:
            atomic.extend(
                normalize_text(encoder.decode(ids[start:start + target_tokens]))
                for start in range(0, len(ids), target_tokens)
            )

    parts: List[str] = []
    current: List[str] = []
    for block in atomic:
        candidate = "\n".join(current + [block])
        if current and token_count(candidate, encoding_name) > target_tokens:
            parts.append("\n".join(current))
            current = [block]
        else:
            current.append(block)
    if current:
        parts.append("\n".join(current))
    return [part for part in parts if part]


@dataclass
class DocumentBuilder:
    document_id: str
    document_key: str
    document_global_key: str
    citation_prefix: str
    units: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    _unit_by_id: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _seen_unit_keys: Set[str] = field(default_factory=set)
    _seen_chunk_keys: Set[str] = field(default_factory=set)
    _source_order: int = 0

    def unique_key(self, base: str, seen: Set[str]) -> str:
        if base not in seen:
            seen.add(base)
            return base
        ordinal = 2
        while "{}_{}".format(base, ordinal) in seen:
            ordinal += 1
        value = "{}_{}".format(base, ordinal)
        seen.add(value)
        return value

    def add_unit(
        self,
        *,
        global_key: str,
        unit_type: str,
        legal_citation: str,
        label: str,
        number: Any,
        title: str,
        text: str,
        parent_unit_id: Optional[str],
        parser_name: str = "eurlex_formex",
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        global_key = self.unique_key(global_key, self._seen_unit_keys)
        unit_id = stable_id("unit", self.document_id, global_key)
        self._source_order += 1
        unit = {
            "unit_id": unit_id,
            "global_key": global_key,
            "legal_citation": legal_citation,
            "display_name": "{}{}".format(
                legal_citation,
                " {}".format(title) if title and title not in legal_citation else "",
            ).strip(),
            "document_id": self.document_id,
            "document_key": self.document_key,
            "document_global_key": self.document_global_key,
            "unit_type": unit_type,
            "label": label,
            "number": number,
            "title": title or None,
            "breadcrumbs": [self.document_global_key, legal_citation],
            "parent_unit_id": parent_unit_id,
            "child_unit_ids": [],
            "sequence": self._source_order,
            "source_order": self._source_order,
            "page_range": None,
            "text": text,
            "text_sha256": sha256_text(text),
            "parser_name": parser_name,
            "confidence": 1.0,
            "review_status": "pending",
            "is_uncertain": False,
            "uncertainty_reason": None,
        }
        if extra:
            unit.update(copy.deepcopy(dict(extra)))
        self.units.append(unit)
        self._unit_by_id[unit_id] = unit
        if parent_unit_id and parent_unit_id in self._unit_by_id:
            self._unit_by_id[parent_unit_id]["child_unit_ids"].append(unit_id)
        return unit

    def add_text_chunks(
        self,
        *,
        unit: Mapping[str, Any],
        base_global_key: str,
        legal_citation: str,
        chunk_type: str,
        label: str,
        number: Any,
        text: str,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        parts = _split_long_text(text)
        result: List[Dict[str, Any]] = []
        for part_index, part in enumerate(parts, 1):
            key_base = base_global_key if part_index == 1 else "{}_part_{}".format(base_global_key, part_index)
            global_key = self.unique_key(key_base, self._seen_chunk_keys)
            self._source_order += 1
            chunk = {
                "chunk_id": stable_id("chunk", self.document_id, global_key),
                "global_key": global_key,
                "legal_citation": legal_citation,
                "display_name": (
                    legal_citation
                    if len(parts) == 1
                    else "{} (Teil {}/{})".format(legal_citation, part_index, len(parts))
                ),
                "chunk_type": chunk_type,
                "unit_id": unit["unit_id"],
                "document_global_key": self.document_global_key,
                "parent_chunk_id": None,
                "child_chunk_ids": [],
                "label": label,
                "number": number,
                "sequence": len(result) + 1,
                "source_order": self._source_order,
                "page_id": None,
                "page_range": None,
                "text": part,
                "text_sha256": sha256_text(part),
                "confidence": 1.0,
                "review_status": "pending",
            }
            if len(parts) > 1:
                chunk["text_part_index"] = part_index
                chunk["text_part_count"] = len(parts)
            if extra:
                chunk.update(copy.deepcopy(dict(extra)))
            self.chunks.append(chunk)
            result.append(chunk)
        return result

    def add_issue(
        self,
        issue_type: str,
        severity: str,
        description: str,
        evidence: Any,
        *,
        unit_id: Optional[str] = None,
        chunk_id: Optional[str] = None,
    ) -> None:
        self.issues.append(
            {
                "issue_id": stable_id("issue", self.document_id, issue_type, evidence),
                "document_id": self.document_id,
                "unit_id": unit_id,
                "chunk_id": chunk_id,
                "issue_type": issue_type,
                "severity": severity,
                "description": description,
                "evidence": evidence,
                "corrected_value": None,
                "review_status": "open",
            }
        )


def _top_level_tables(element: ET.Element) -> List[ET.Element]:
    tables: List[ET.Element] = []

    def walk(node: ET.Element, inside_table: bool = False) -> None:
        for child in node:
            if child.tag == "TBL":
                if not inside_table:
                    tables.append(child)
                continue
            if child.tag in {"ARTICLE", "CONS.ANNEX", "DIVISION", "GR.SEQ"} and child is not element:
                continue
            walk(child, inside_table)

    walk(element)
    return tables


def _table_title_and_number(table: ET.Element, parsed: Mapping[str, Any], ordinal: int) -> Tuple[str, str]:
    title = str(parsed.get("title") or "")
    if not title:
        first_rows = parsed.get("header_matrix") or parsed.get("body_matrix") or []
        if first_rows:
            distinct = []
            for value in first_rows[0]:
                if value and value not in distinct:
                    distinct.append(value)
            if len(distinct) == 1:
                title = distinct[0]
    number = _number_from_text(
        TABLE_NUMBER_RE,
        title,
        str(table.get("NO.SEQ") or ordinal),
    )
    return title, number


def _add_table(
    builder: DocumentBuilder,
    table: ET.Element,
    parent_unit: Mapping[str, Any],
    parent_global_key: str,
    parent_citation: str,
    ordinal: int,
) -> None:
    parsed = parse_formex_table(table)
    title, number = _table_title_and_number(table, parsed, ordinal)
    table_global = "{}_tabelle_{}".format(parent_global_key, slugify(number))
    citation = "{} Tabelle {}".format(parent_citation, number).strip()
    rows = parsed.get("rows") or []
    columns = parsed.get("columns") or []
    table_text = table_rows_text(columns, rows)
    if title:
        table_text = "{}\n{}".format(title, table_text).strip()
    unit_text = table_text
    if token_count(table_text, DEFAULT_ENCODING) > DEFAULT_MAX_TOKENS:
        unit_text = "{}\nSpalten: {}\n[Strukturierte Tabelle mit {} Zeilen]".format(
            title or citation,
            " | ".join(columns),
            len(rows),
        ).strip()
    compact_table = compact_formex_table(parsed)
    unit = builder.add_unit(
        global_key=table_global,
        unit_type="table",
        legal_citation=citation,
        label="Tabelle {}".format(number),
        number=number,
        title=title,
        text=unit_text,
        parent_unit_id=parent_unit["unit_id"],
        parser_name="eurlex_formex_table",
        extra={
            "columns": columns,
            "column_header_text": " | ".join(columns),
            "row_count": len(rows),
            "note_count": len(parsed.get("notes") or []),
            "table_sections": [],
            "table_data": compact_table,
            "source_xml_table_sequence": ordinal,
        },
    )
    parts = split_table_rows(
        table_citation=citation,
        table_title=title,
        columns=columns,
        rows=rows,
        header_matrix=parsed.get("header_matrix") or [],
        encoding_name=DEFAULT_ENCODING,
        max_tokens=DEFAULT_MAX_TOKENS,
        target_tokens=DEFAULT_TARGET_TOKENS,
    )
    for part_index, part in enumerate(parts, 1):
        key_base = (
            "{}_rows_part_{:04d}".format(unit["global_key"], part_index)
            if part["was_split"]
            else "{}_rows".format(unit["global_key"])
        )
        global_key = builder.unique_key(key_base, builder._seen_chunk_keys)
        builder._source_order += 1
        row_start = part.get("row_start")
        row_end = part.get("row_end")
        chunk = {
            "chunk_id": stable_id("chunk", builder.document_id, global_key),
            "global_key": global_key,
            "legal_citation": citation,
            "display_name": (
                "{} Zeilen {}–{}".format(citation, row_start, row_end)
                if part["was_split"]
                else citation
            ),
            "chunk_type": "table_rows",
            "unit_id": unit["unit_id"],
            "document_global_key": builder.document_global_key,
            "parent_chunk_id": None,
            "child_chunk_ids": [],
            "label": "rows {}-{}".format(row_start, row_end) if part["was_split"] else "rows",
            "number": None,
            "sequence": part_index,
            "source_order": builder._source_order,
            "page_id": None,
            "page_range": None,
            "columns": part["columns"],
            "column_header_text": part["column_header_text"],
            "header_matrix": part["header_matrix"],
            "table_title": part["table_title"],
            "table_part_index": part.get("part_index") if part["was_split"] else None,
            "table_part_count": part.get("part_count") if part["was_split"] else None,
            "table_chunk_token_count": part["token_count"],
            "table_chunk_token_encoding": part["token_encoding"],
            "table_chunk_max_tokens": part["max_tokens"],
            "oversized_atomic_row": part["oversized_atomic_row"],
            "parser_name": "eurlex_formex_table",
            "table_section": None,
            "rows": part["rows"],
            "text": part["text"],
            "text_sha256": sha256_text(part["text"]),
            "confidence": 1.0,
            "review_status": "pending",
            "row_range": (
                {"start": row_start, "end": row_end}
                if row_start is not None and row_end is not None
                else None
            ),
        }
        builder.chunks.append(chunk)

    for note_index, note in enumerate(parsed.get("notes") or [], 1):
        note_id = note.get("note_id") or note_index
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_fussnote_{}".format(unit["global_key"], slugify(note_id)),
            legal_citation="{} Fußnote {}".format(citation, note_id),
            chunk_type="footnote",
            label="Fußnote {}".format(note_id),
            number=note_id,
            text=note.get("text") or "",
            extra={"source_xml_note_attributes": note.get("attributes") or {}},
        )


def _article_number(article: ET.Element, fallback: str) -> str:
    title = element_text(article.find("./TI.ART"))
    number = _number_from_text(ARTICLE_NUMBER_RE, title, "")
    if number:
        return number
    identifier = str(article.get("IDENTIFIER") or "")
    if identifier.isdigit():
        return str(int(identifier))
    match = re.search(r"([0-9]+[a-z]?)", identifier, re.IGNORECASE)
    return match.group(1) if match else fallback


def _parse_article(
    builder: DocumentBuilder,
    article: ET.Element,
    parent_unit_id: Optional[str],
    key_prefix: str,
    citation_prefix: str,
    ordinal: int,
) -> Dict[str, Any]:
    number = _article_number(article, str(ordinal))
    label = element_text(article.find("./TI.ART")) or "Artikel {}".format(number)
    subtitle = element_text(article.find("./STI.ART"), skip_tags={"TBL"})
    global_key = "{}_art_{}".format(key_prefix, slugify(number))
    citation = "{} Art. {}".format(citation_prefix, number)
    text = element_text(article, skip_tags={"TBL"})
    unit = builder.add_unit(
        global_key=global_key,
        unit_type="article",
        legal_citation=citation,
        label=label,
        number=number,
        title=subtitle,
        text=text,
        parent_unit_id=parent_unit_id,
        extra={"source_xml_identifier": article.get("IDENTIFIER")},
    )

    content_children = [
        child for child in article if child.tag not in {"TI.ART", "STI.ART"}
    ]
    paragraph_index = 0
    alinea_index = 0
    for child in content_children:
        if child.tag == "PARAG":
            paragraph_index += 1
            paragraph_number = _paragraph_number(child, str(paragraph_index))
            paragraph_text = element_text(child, skip_tags={"TBL"})
            builder.add_text_chunks(
                unit=unit,
                base_global_key="{}_abs_{}".format(unit["global_key"], slugify(paragraph_number)),
                legal_citation="{} Abs. {}".format(citation, paragraph_number),
                chunk_type="subsection",
                label="Abs. {}".format(paragraph_number),
                number=paragraph_number,
                text=paragraph_text,
                extra={"source_xml_identifier": child.get("IDENTIFIER")},
            )
        elif child.tag == "ALINEA":
            alinea_index += 1
            alinea_text = element_text(child, skip_tags={"TBL"})
            builder.add_text_chunks(
                unit=unit,
                base_global_key=(
                    "{}_text".format(unit["global_key"])
                    if alinea_index == 1
                    else "{}_text_{}".format(unit["global_key"], alinea_index)
                ),
                legal_citation=citation,
                chunk_type="article_text",
                label=label,
                number=alinea_index,
                text=alinea_text,
            )
        elif child.tag not in {"DIVISION", "ARTICLE"}:
            child_text = element_text(child, skip_tags={"TBL"})
            if child_text:
                alinea_index += 1
                builder.add_text_chunks(
                    unit=unit,
                    base_global_key="{}_text_{}".format(unit["global_key"], alinea_index),
                    legal_citation=citation,
                    chunk_type="article_text",
                    label=label,
                    number=alinea_index,
                    text=child_text,
                )

    if not any(chunk["unit_id"] == unit["unit_id"] for chunk in builder.chunks):
        fallback = element_text(article, skip_tags={"TI.ART", "STI.ART", "TBL"})
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_text".format(unit["global_key"]),
            legal_citation=citation,
            chunk_type="article_text",
            label=label,
            number=0,
            text=fallback,
        )

    for table_index, table in enumerate(_top_level_tables(article), 1):
        _add_table(builder, table, unit, unit["global_key"], citation, table_index)
    return unit


def _parse_division(
    builder: DocumentBuilder,
    division: ET.Element,
    parent_unit_id: Optional[str],
    key_prefix: str,
    citation_prefix: str,
    ordinal: int,
) -> Dict[str, Any]:
    title = element_text(division.find("./TITLE"), skip_tags={"TBL"})
    label = title or "Gliederung {}".format(ordinal)
    number = _number_from_text(
        re.compile(r"(?:Kapitel|Abschnitt|Titel|Teil)\s+([IVXLCDM0-9A-Z]+)", re.IGNORECASE),
        label,
        str(ordinal),
    )
    global_key = "{}_gliederung_{}".format(key_prefix, slugify(label))
    unit = builder.add_unit(
        global_key=global_key,
        unit_type="division",
        legal_citation="{} {}".format(citation_prefix, label),
        label=label,
        number=number,
        title=title,
        text=title,
        parent_unit_id=parent_unit_id,
    )
    article_index = 0
    division_index = 0
    for child in division:
        if child.tag == "ARTICLE":
            article_index += 1
            _parse_article(
                builder,
                child,
                unit["unit_id"],
                builder.document_global_key,
                citation_prefix,
                article_index,
            )
        elif child.tag == "DIVISION":
            division_index += 1
            _parse_division(
                builder,
                child,
                unit["unit_id"],
                key_prefix,
                citation_prefix,
                division_index,
            )
    return unit


def _parse_preamble(builder: DocumentBuilder, preamble: ET.Element) -> None:
    text = element_text(preamble, skip_tags={"TBL"})
    unit = builder.add_unit(
        global_key="{}_preambel".format(builder.document_global_key),
        unit_type="preamble",
        legal_citation="{} Präambel".format(builder.citation_prefix),
        label="Präambel",
        number=None,
        title="Präambel und Erwägungsgründe",
        text=text,
        parent_unit_id=None,
    )
    init = element_text(preamble.find("./PREAMBLE.INIT"), skip_tags={"TBL"})
    if init:
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_einleitung".format(unit["global_key"]),
            legal_citation=unit["legal_citation"],
            chunk_type="preamble_text",
            label="Einleitung",
            number=0,
            text=init,
        )
    for visa_index, visa in enumerate(preamble.iter("VISA"), 1):
        visa_text = element_text(visa, skip_tags={"TBL"})
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_bezugsvermerk_{}".format(unit["global_key"], visa_index),
            legal_citation="{} Bezugsvermerk {}".format(unit["legal_citation"], visa_index),
            chunk_type="visa",
            label="Bezugsvermerk {}".format(visa_index),
            number=visa_index,
            text=visa_text,
        )
    for recital_index, recital in enumerate(preamble.iter("CONSID"), 1):
        recital_text = element_text(recital, skip_tags={"TBL"})
        number_match = re.match(r"\(?\s*([0-9]+[a-z]?)\s*\)?", recital_text, re.IGNORECASE)
        number = number_match.group(1) if number_match else str(recital_index)
        recital_citation = "{} Erwägungsgrund {}".format(builder.citation_prefix, number)
        recital_unit = builder.add_unit(
            global_key="{}_erwaegungsgrund_{}".format(builder.document_global_key, slugify(number)),
            unit_type="recital",
            legal_citation=recital_citation,
            label="Erwägungsgrund {}".format(number),
            number=number,
            title="",
            text=recital_text,
            parent_unit_id=unit["unit_id"],
        )
        builder.add_text_chunks(
            unit=recital_unit,
            base_global_key=recital_unit["global_key"],
            legal_citation=recital_citation,
            chunk_type="recital",
            label=recital_unit["label"],
            number=number,
            text=recital_text,
        )
    final = element_text(preamble.find("./PREAMBLE.FINAL"), skip_tags={"TBL"})
    if final and final != init:
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_schluss".format(unit["global_key"]),
            legal_citation=unit["legal_citation"],
            chunk_type="preamble_text",
            label="Präambelschluss",
            number=None,
            text=final,
        )
    for table_index, table in enumerate(_top_level_tables(preamble), 1):
        _add_table(builder, table, unit, unit["global_key"], unit["legal_citation"], table_index)


def _parse_annex_sequence(
    builder: DocumentBuilder,
    sequence: ET.Element,
    parent_unit: Mapping[str, Any],
    parent_global_key: str,
    parent_citation: str,
    ordinal: int,
) -> None:
    sequence_number = element_text(sequence.find("./NO.GR.SEQ")) or str(ordinal)
    title = element_text(sequence.find("./TITLE"), skip_tags={"TBL"})
    label = " ".join(value for value in (sequence_number, title) if value).strip()
    global_key = "{}_abschnitt_{}".format(parent_global_key, slugify(label))
    own_text_parts: List[str] = []
    for child in sequence:
        if child.tag in STRUCTURAL_SKIP_TAGS or child.tag in {"NO.GR.SEQ", "TITLE"}:
            continue
        value = element_text(child, skip_tags=STRUCTURAL_SKIP_TAGS)
        if value:
            own_text_parts.append(value)
    own_text = normalize_text("\n".join(own_text_parts))
    unit = builder.add_unit(
        global_key=global_key,
        unit_type="annex_section",
        legal_citation="{} {}".format(parent_citation, sequence_number),
        label=label,
        number=sequence_number,
        title=title,
        text=own_text,
        parent_unit_id=parent_unit["unit_id"],
    )
    if own_text:
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_text".format(unit["global_key"]),
            legal_citation=unit["legal_citation"],
            chunk_type="annex_text",
            label=label,
            number=sequence_number,
            text=own_text,
        )
    sub_index = 0
    article_index = 0
    for child in sequence:
        if child.tag == "GR.SEQ":
            sub_index += 1
            _parse_annex_sequence(
                builder,
                child,
                unit,
                unit["global_key"],
                unit["legal_citation"],
                sub_index,
            )
        elif child.tag == "ARTICLE":
            article_index += 1
            _parse_article(
                builder,
                child,
                unit["unit_id"],
                unit["global_key"],
                parent_citation,
                article_index,
            )
    for table_index, table in enumerate(_top_level_tables(sequence), 1):
        _add_table(builder, table, unit, unit["global_key"], unit["legal_citation"], table_index)


def _parse_annex(
    builder: DocumentBuilder,
    annex: ET.Element,
    parent_unit_id: Optional[str],
    parent_global_key: str,
    parent_citation: str,
    ordinal: int,
) -> Dict[str, Any]:
    title = element_text(annex.find("./TITLE"), skip_tags={"TBL"})
    number = _number_from_text(ANNEX_NUMBER_RE, title, str(ordinal))
    label = title or "Anhang {}".format(number)
    global_key = "{}_anhang_{}".format(parent_global_key, slugify(number))
    citation = "{} Anhang {}".format(parent_citation, number)
    content = annex.find("./CONTENTS")
    text = element_text(content, skip_tags=STRUCTURAL_SKIP_TAGS) if content is not None else ""
    unit = builder.add_unit(
        global_key=global_key,
        unit_type="annex",
        legal_citation=citation,
        label=label,
        number=number,
        title=title,
        text=text,
        parent_unit_id=parent_unit_id,
    )
    if content is not None:
        loose_parts: List[str] = []
        sequence_index = 0
        article_index = 0
        for child in content:
            if child.tag == "GR.SEQ":
                sequence_index += 1
                _parse_annex_sequence(
                    builder,
                    child,
                    unit,
                    unit["global_key"],
                    citation,
                    sequence_index,
                )
            elif child.tag == "ARTICLE":
                article_index += 1
                _parse_article(
                    builder,
                    child,
                    unit["unit_id"],
                    unit["global_key"],
                    citation,
                    article_index,
                )
            elif child.tag not in {"CONS.ANNEX", "TBL", "TOC"}:
                value = element_text(child, skip_tags=STRUCTURAL_SKIP_TAGS)
                if value:
                    loose_parts.append(value)
        loose_text = normalize_text("\n".join(loose_parts))
        if loose_text:
            builder.add_text_chunks(
                unit=unit,
                base_global_key="{}_text".format(unit["global_key"]),
                legal_citation=citation,
                chunk_type="annex_text",
                label=label,
                number=number,
                text=loose_text,
            )
        for table_index, table in enumerate(_top_level_tables(content), 1):
            _add_table(builder, table, unit, unit["global_key"], citation, table_index)

    nested_index = 0
    for child in annex:
        if child.tag == "CONS.ANNEX":
            nested_index += 1
            _parse_annex(
                builder,
                child,
                unit["unit_id"],
                unit["global_key"],
                citation,
                nested_index,
            )
    return unit


def _parse_final(builder: DocumentBuilder, final: ET.Element) -> None:
    text = element_text(final, skip_tags={"TBL"})
    if not text:
        return
    unit = builder.add_unit(
        global_key="{}_schlussformel".format(builder.document_global_key),
        unit_type="final",
        legal_citation="{} Schlussformel".format(builder.citation_prefix),
        label="Schlussformel",
        number=None,
        title="Schlussformel",
        text=text,
        parent_unit_id=None,
    )
    builder.add_text_chunks(
        unit=unit,
        base_global_key="{}_text".format(unit["global_key"]),
        legal_citation=unit["legal_citation"],
        chunk_type="final_text",
        label="Schlussformel",
        number=None,
        text=text,
    )


def extract_document_from_formex(
    source_path: os.PathLike[str] | str,
    register_record: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    package = _load_package(source_path, register_record)
    root: ET.Element = package["root"]
    cons_doc = root.find("./CONS.DOC")
    if cons_doc is None:
        raise ValueError("CONS.ACT package has no CONS.DOC")
    info_element = root.find("./INFO.CONSLEG")
    info = dict(info_element.attrib) if info_element is not None else {}
    info.update(dict(register_record.get("info_consleg") or {}))

    base_celex = str(register_record.get("base_celex") or "")
    consolidated_celex = str(register_record.get("consolidated_celex") or base_celex)
    descriptor = str(register_record.get("descriptor") or "")
    title_xml = element_text(cons_doc.find("./TITLE"), skip_tags={"TBL"})
    title = str(register_record.get("title") or title_xml or consolidated_celex)
    document_global_key = "celex_{}".format(slugify(base_celex))
    document_key = base_celex
    document_id = "doc_eu_{}".format(slugify(consolidated_celex))
    citation_prefix = base_celex
    aliases = eu_citation_aliases(base_celex, descriptor)
    citation_alias_keys = "|{}|".format(
        "|".join(sorted({slugify(alias) for alias in aliases if alias}))
    )
    legal_status, in_force = _legal_status(info)
    builder = DocumentBuilder(
        document_id=document_id,
        document_key=document_key,
        document_global_key=document_global_key,
        citation_prefix=citation_prefix,
    )

    preamble = cons_doc.find("./PREAMBLE")
    if preamble is not None:
        _parse_preamble(builder, preamble)

    enacting = cons_doc.find("./ENACTING.TERMS")
    if enacting is not None:
        article_index = 0
        division_index = 0
        for child in enacting:
            if child.tag == "ARTICLE":
                article_index += 1
                _parse_article(
                    builder,
                    child,
                    None,
                    document_global_key,
                    citation_prefix,
                    article_index,
                )
            elif child.tag == "DIVISION":
                division_index += 1
                _parse_division(
                    builder,
                    child,
                    None,
                    document_global_key,
                    citation_prefix,
                    division_index,
                )

    final = cons_doc.find("./FINAL")
    if final is not None:
        _parse_final(builder, final)

    for annex_index, annex in enumerate(cons_doc.findall("./CONS.ANNEX"), 1):
        _parse_annex(
            builder,
            annex,
            None,
            document_global_key,
            citation_prefix,
            annex_index,
        )

    if not builder.units or not builder.chunks:
        fallback_text = element_text(cons_doc, skip_tags={"TBL"})
        fallback_unit = builder.add_unit(
            global_key="{}_dokumenttext".format(document_global_key),
            unit_type="document_text",
            legal_citation=citation_prefix,
            label="Dokumenttext",
            number=None,
            title=title,
            text=fallback_text,
            parent_unit_id=None,
        )
        builder.add_text_chunks(
            unit=fallback_unit,
            base_global_key="{}_text".format(fallback_unit["global_key"]),
            legal_citation=citation_prefix,
            chunk_type="document_text",
            label="Dokumenttext",
            number=None,
            text=fallback_text,
        )
        builder.add_issue(
            "formex_no_structured_content",
            "warning",
            "Formex contained no modeled operative structure; emitted a document-text fallback.",
            {"content_member": package["xml_name"]},
            unit_id=fallback_unit["unit_id"],
        )

    complete_text = element_text(cons_doc, skip_tags={"TBL"})
    placeholder_count = len(re.findall(r"\bX{3,}\b", complete_text))
    if placeholder_count:
        builder.add_issue(
            "formex_placeholder_text",
            "warning",
            "Consolidated Formex contains explicit placeholder text.",
            {"placeholder_count": placeholder_count},
        )

    source_url = str(register_record.get("download_url") or "") or None
    doc = {
        "document_id": document_id,
        "source_pdf": None,
        "source_xml": package["xml_name"],
        "source_zip": str(register_record.get("local_path") or Path(source_path).name),
        "sha256": package["xml_sha256"],
        "title": title,
        "full_citation": title_xml or title,
        "canonical_citation": base_celex,
        "date_enacted": info.get("START.DATE") or None,
        "document_key": document_key,
        "document_global_key": document_global_key,
        "global_key": document_global_key,
        "citation_prefix": citation_prefix,
        "abbreviation": base_celex,
        "pages": [],
        "page_refs": [],
        "structural_units": builder.units,
        "chunks": builder.chunks,
        "metadata": {
            "source_format": "eurlex_formex",
            "extractor": "eurlex_formex",
            "eurlex_formex_extractor_version": EURLEX_FORMEX_EXTRACTOR_VERSION,
            "short_title": title,
            "base_celex": base_celex,
            "consolidated_celex": consolidated_celex,
            "consolidation_date": register_record.get("consolidation_date"),
            "descriptor": descriptor,
            "descriptor_label": register_record.get("descriptor_label"),
            "legal_value": register_record.get("legal_value") or info.get("LEG.VAL"),
            "legal_status": legal_status,
            "in_force": in_force,
            "citation_aliases": aliases,
            "citation_alias_keys": citation_alias_keys,
            "source_url": source_url,
            "source_cellar_uri": register_record.get("consolidated_cellar_uri"),
            "source_xml_sha256": package["xml_sha256"],
            "source_xml_name": package["xml_name"],
            "source_package_path": str(register_record.get("local_path") or source_path),
            "source_package_kind": "fmx4_zip",
            "source_package_sha256": package["package_sha256"],
            "source_package_bytes": package["package_bytes"],
            "source_package_members": package["members"],
            "info_consleg": info,
            "xml_unit_count": len(builder.units),
            "xml_chunk_count": len(builder.chunks),
            "xml_article_count": sum(unit["unit_type"] == "article" for unit in builder.units),
            "xml_annex_count": sum(unit["unit_type"] == "annex" for unit in builder.units),
            "xml_table_count": sum(unit["unit_type"] == "table" for unit in builder.units),
            "pdf_alignment_status": "not_applicable",
            "pdf_pages": 0,
        },
    }
    return doc, builder.issues


def extract_package(
    source_path: os.PathLike[str] | str,
    register_record: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    record = dict(register_record or {})
    document, issues = extract_document_from_formex(source_path, record)
    return {
        "schema_version": "1.0.0-draft",
        "phase": "normtext",
        "extractor": {
            "name": "eurlex_formex",
            "version": EURLEX_FORMEX_EXTRACTOR_VERSION,
        },
        "source_manifest_entry": record,
        "documents": [document],
        "review_decisions": [],
        "extraction_issues": issues,
    }


__all__ = [
    "EURLEX_FORMEX_EXTRACTOR_VERSION",
    "element_text",
    "eu_citation_aliases",
    "extract_document_from_formex",
    "extract_package",
    "parse_formex_table",
    "compact_formex_table",
]
