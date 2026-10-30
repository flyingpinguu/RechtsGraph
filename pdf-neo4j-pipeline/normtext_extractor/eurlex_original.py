"""Fallback adapters for original EUR-Lex acts outside the consolidated corpus.

Original acts are preferably parsed from Formex through
``eurlex_formex``.  This module keeps older PDF-only acts and occasional XHTML
fallbacks inside the same canonical Document / StructuralUnit / Chunk model.
"""

from __future__ import annotations

import bisect
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from xml.etree import ElementTree as ET

from .eurlex_formex import (
    DocumentBuilder,
    eu_citation_aliases,
    file_sha256,
    normalize_text,
    sha256_text,
    slugify,
)


EURLEX_ORIGINAL_EXTRACTOR_VERSION = "1.1.0"
ARTICLE_LINE_RE = re.compile(r"^\s*(?:Artikel|Article)\s+([0-9]+[a-z]?)\s*$", re.IGNORECASE)
ANNEX_LINE_RE = re.compile(
    r"^\s*(?:ANHANG|ANLAGE|ANNEX)\s*([IVXLCDM]+|[0-9]+[A-Z]?)?\s*$",
    re.IGNORECASE,
)
ACT_METADATA = {
    "R": {"descriptor_label": "regulation", "legal_value": "REG"},
    "L": {"descriptor_label": "directive", "legal_value": "DIR"},
}


def _document_context(record: Mapping[str, Any]) -> Dict[str, str]:
    base_celex = str(record.get("base_celex") or "").upper()
    if not re.fullmatch(r"3\d{4}[A-Z]\d{4}", base_celex):
        raise ValueError("invalid original EUR-Lex base CELEX: {!r}".format(base_celex))
    descriptor = str(record.get("descriptor") or base_celex[5]).upper()
    return {
        "base_celex": base_celex,
        "descriptor": descriptor,
        "document_id": "doc_eu_{}".format(slugify(base_celex)),
        "document_key": base_celex,
        "document_global_key": "celex_{}".format(slugify(base_celex)),
        "citation_prefix": base_celex,
    }


def _metadata(
    context: Mapping[str, str],
    record: Mapping[str, Any],
    *,
    source_format: str,
    source_hash: str,
    article_count: int,
    annex_count: int,
    page_count: int,
) -> Dict[str, Any]:
    aliases = eu_citation_aliases(context["base_celex"], context["descriptor"])
    act_metadata = ACT_METADATA.get(
        context["descriptor"],
        {"descriptor_label": "legal_act", "legal_value": context["descriptor"]},
    )
    return {
        "source_format": source_format,
        "extractor": "eurlex_original",
        "eurlex_original_extractor_version": EURLEX_ORIGINAL_EXTRACTOR_VERSION,
        "short_title": record.get("title") or context["base_celex"],
        "base_celex": context["base_celex"],
        "consolidated_celex": context["base_celex"],
        "consolidation_date": None,
        "descriptor": context["descriptor"],
        "descriptor_label": record.get("descriptor_label") or act_metadata["descriptor_label"],
        "legal_value": record.get("legal_value") or act_metadata["legal_value"],
        "legal_status": "unknown",
        "in_force": None,
        "citation_aliases": aliases,
        "citation_alias_keys": "|{}|".format(
            "|".join(sorted({slugify(alias) for alias in aliases if alias}))
        ),
        "source_url": record.get("download_url"),
        "source_cellar_uri": record.get("source_cellar_uri"),
        "source_file_path": record.get("local_path"),
        "source_file_sha256": source_hash,
        "article_count": article_count,
        "annex_count": annex_count,
        "table_count": 0,
        "pdf_pages": page_count,
        "pdf_alignment_status": "source_pdf" if source_format == "eurlex_pdf_original" else "not_applicable",
    }


def _payload(
    document: Mapping[str, Any],
    issues: List[Dict[str, Any]],
    record: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": "1.0.0-draft",
        "phase": "normtext",
        "extractor": {
            "name": "eurlex_original",
            "version": EURLEX_ORIGINAL_EXTRACTOR_VERSION,
        },
        "source_manifest_entry": dict(record),
        "documents": [dict(document)],
        "review_decisions": [],
        "extraction_issues": issues,
    }


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _class_tokens(element: ET.Element) -> set[str]:
    return {token for token in str(element.get("class") or "").split() if token}


def _html_text(element: Optional[ET.Element]) -> str:
    if element is None:
        return ""
    return normalize_text(" ".join(value for value in element.itertext() if value))


def _first_by_class(root: ET.Element, class_name: str) -> Optional[ET.Element]:
    return next((element for element in root.iter() if class_name in _class_tokens(element)), None)


class _LegacyHTMLBlockParser(HTMLParser):
    """Collect paragraph-like blocks from old non-XHTML EUR-Lex pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[str] = []
        self._parts: List[str] = []
        self._block_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.casefold() in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            # Historical EUR-Lex pages sometimes wrap a sequence of <p>
            # elements inside another <p>. Treat a new block as an implicit
            # close, as an HTML browser does, rather than nesting paragraphs.
            if self._parts:
                value = normalize_text(" ".join(self._parts))
                if value:
                    self.blocks.append(value)
            self._parts = []
            self._block_depth = 1
        elif tag.casefold() == "br" and self._block_depth:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() not in {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            return
        if self._block_depth:
            value = normalize_text(" ".join(self._parts))
            if value:
                self.blocks.append(value)
            self._parts = []
            self._block_depth = 0

    def handle_data(self, data: str) -> None:
        if self._block_depth:
            self._parts.append(data)


def _extract_legacy_html(
    path: Path,
    register_record: Mapping[str, Any],
) -> Dict[str, Any]:
    """Parse historical HTML 4 pages that are intentionally not XML."""

    context = _document_context(register_record)
    parser = _LegacyHTMLBlockParser()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    blocks = parser.blocks
    source_hash = file_sha256(path)
    fallback_title = str(register_record.get("title") or context["base_celex"])
    title = next(
        (
            block
            for block in blocks[:12]
            if any(marker in block.upper() for marker in ("RICHTLINIE", "DIRECTIVE", "VERORDNUNG", "REGULATION"))
        ),
        fallback_title,
    )
    builder = DocumentBuilder(
        document_id=context["document_id"],
        document_key=context["document_key"],
        document_global_key=context["document_global_key"],
        citation_prefix=context["citation_prefix"],
    )
    markers: List[Tuple[int, str, str]] = []
    for index, block in enumerate(blocks):
        article_match = ARTICLE_LINE_RE.fullmatch(block)
        annex_match = ANNEX_LINE_RE.fullmatch(block)
        if article_match:
            markers.append((index, "article", article_match.group(1)))
        elif annex_match:
            markers.append((index, "annex", annex_match.group(1) or ""))

    if markers and markers[0][0] > 0:
        preamble_text = "\n\n".join(blocks[: markers[0][0]])
        if preamble_text:
            unit = builder.add_unit(
                global_key="{}_preambel".format(context["document_global_key"]),
                unit_type="preamble",
                legal_citation="{} Präambel".format(context["citation_prefix"]),
                label="Präambel",
                number=None,
                title="Präambel und Erwägungsgründe",
                text=preamble_text,
                parent_unit_id=None,
                parser_name="eurlex_legacy_html",
            )
            builder.add_text_chunks(
                unit=unit,
                base_global_key="{}_text".format(unit["global_key"]),
                legal_citation=unit["legal_citation"],
                chunk_type="preamble_text",
                label="Präambel",
                number=None,
                text=preamble_text,
            )

    article_ordinal = 0
    annex_ordinal = 0
    for marker_index, (start, kind, raw_number) in enumerate(markers):
        end = markers[marker_index + 1][0] if marker_index + 1 < len(markers) else len(blocks)
        text = "\n\n".join(blocks[start + 1 : end]).strip()
        if not text:
            continue
        if kind == "article":
            article_ordinal += 1
            number = raw_number or str(article_ordinal)
            citation = "{} Art. {}".format(context["citation_prefix"], number)
            unit_type, label = "article", "Artikel {}".format(number)
            key = "{}_art_{}".format(context["document_global_key"], slugify(number))
            chunk_type = "article_text"
        else:
            annex_ordinal += 1
            number = raw_number or str(annex_ordinal)
            citation = "{} Anhang {}".format(context["citation_prefix"], number)
            unit_type, label = "annex", "Anhang {}".format(number)
            key = "{}_anhang_{}".format(context["document_global_key"], slugify(number))
            chunk_type = "annex_text"
        unit = builder.add_unit(
            global_key=key,
            unit_type=unit_type,
            legal_citation=citation,
            label=label,
            number=number,
            title="",
            text=text,
            parent_unit_id=None,
            parser_name="eurlex_legacy_html",
        )
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_text".format(unit["global_key"]),
            legal_citation=citation,
            chunk_type=chunk_type,
            label=label,
            number=number,
            text=text,
        )

    complete_text = "\n\n".join(blocks)
    if not builder.units or not builder.chunks:
        _add_fallback_document_text(
            builder,
            context,
            complete_text,
            parser_name="eurlex_legacy_html",
        )
        builder.add_issue(
            "eurlex_legacy_html_no_articles",
            "warning",
            "Legacy EUR-Lex HTML contained no recognized article structure.",
            {"source": str(register_record.get("local_path") or path.name)},
        )
    builder.add_issue(
        "eurlex_legacy_html_fallback",
        "info",
        "Original EUR-Lex act was parsed from a historical HTML 4 page.",
        {"blocks": len(blocks)},
    )
    metadata = _metadata(
        context,
        register_record,
        source_format="eurlex_html_original",
        source_hash=source_hash,
        article_count=sum(unit["unit_type"] == "article" for unit in builder.units),
        annex_count=sum(unit["unit_type"] == "annex" for unit in builder.units),
        page_count=0,
    )
    document = {
        "document_id": context["document_id"],
        "source_pdf": None,
        "source_xml": str(register_record.get("local_path") or path.name),
        "source_zip": None,
        "sha256": source_hash,
        "title": title,
        "full_citation": title,
        "canonical_citation": context["base_celex"],
        "date_enacted": None,
        "document_key": context["document_key"],
        "document_global_key": context["document_global_key"],
        "global_key": context["document_global_key"],
        "citation_prefix": context["citation_prefix"],
        "abbreviation": context["base_celex"],
        "pages": [],
        "page_refs": [],
        "structural_units": builder.units,
        "chunks": builder.chunks,
        "metadata": metadata,
    }
    return _payload(document, builder.issues, register_record)


def _add_fallback_document_text(
    builder: DocumentBuilder,
    context: Mapping[str, str],
    text: str,
    *,
    parser_name: str,
    page_range: Optional[Dict[str, int]] = None,
) -> None:
    unit = builder.add_unit(
        global_key="{}_dokumenttext".format(context["document_global_key"]),
        unit_type="document_text",
        legal_citation=context["citation_prefix"],
        label="Dokumenttext",
        number=None,
        title="Dokumenttext",
        text=text,
        parent_unit_id=None,
        parser_name=parser_name,
        extra={"page_range": page_range} if page_range else None,
    )
    builder.add_text_chunks(
        unit=unit,
        base_global_key="{}_text".format(unit["global_key"]),
        legal_citation=context["citation_prefix"],
        chunk_type="document_text",
        label="Dokumenttext",
        number=None,
        text=text,
        extra={"page_range": page_range} if page_range else None,
    )


def extract_xhtml(
    source_path: Path | str,
    register_record: Mapping[str, Any],
) -> Dict[str, Any]:
    path = Path(source_path)
    context = _document_context(register_record)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return _extract_legacy_html(path, register_record)
    source_hash = file_sha256(path)
    title_node = _first_by_class(root, "eli-main-title")
    title = _html_text(title_node) or str(register_record.get("title") or context["base_celex"])
    builder = DocumentBuilder(
        document_id=context["document_id"],
        document_key=context["document_key"],
        document_global_key=context["document_global_key"],
        citation_prefix=context["citation_prefix"],
    )

    preamble = next(
        (
            element
            for element in root.iter()
            if _local_name(element) == "div" and str(element.get("id") or "").startswith("pbl_")
        ),
        None,
    )
    if preamble is not None and _html_text(preamble):
        text = _html_text(preamble)
        unit = builder.add_unit(
            global_key="{}_preambel".format(context["document_global_key"]),
            unit_type="preamble",
            legal_citation="{} Präambel".format(context["citation_prefix"]),
            label="Präambel",
            number=None,
            title="Präambel und Erwägungsgründe",
            text=text,
            parent_unit_id=None,
            parser_name="eurlex_xhtml",
        )
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_text".format(unit["global_key"]),
            legal_citation=unit["legal_citation"],
            chunk_type="preamble_text",
            label="Präambel",
            number=None,
            text=text,
        )

    article_nodes = [
        element
        for element in root.iter()
        if _local_name(element) == "div"
        and re.fullmatch(r"art_[0-9]+[a-z]?", str(element.get("id") or ""), re.IGNORECASE)
    ]
    for ordinal, article in enumerate(article_nodes, 1):
        heading = next(
            (child for child in article.iter() if "oj-ti-art" in _class_tokens(child)),
            None,
        )
        label = _html_text(heading) or "Artikel {}".format(ordinal)
        match = re.search(r"(?:Artikel|Article)\s+([0-9]+[a-z]?)", label, re.IGNORECASE)
        number = match.group(1) if match else str(ordinal)
        text = _html_text(article)
        citation = "{} Art. {}".format(context["citation_prefix"], number)
        unit = builder.add_unit(
            global_key="{}_art_{}".format(context["document_global_key"], slugify(number)),
            unit_type="article",
            legal_citation=citation,
            label=label,
            number=number,
            title="",
            text=text,
            parent_unit_id=None,
            parser_name="eurlex_xhtml",
            extra={"source_xml_identifier": article.get("id")},
        )
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_text".format(unit["global_key"]),
            legal_citation=citation,
            chunk_type="article_text",
            label=label,
            number=number,
            text=text,
            extra={"source_xml_identifier": article.get("id")},
        )

    annex_nodes = [
        element
        for element in root.iter()
        if _local_name(element) == "div"
        and re.fullmatch(r"anx_[0-9]+[a-z]?", str(element.get("id") or ""), re.IGNORECASE)
    ]
    for ordinal, annex in enumerate(annex_nodes, 1):
        text = _html_text(annex)
        number = str(ordinal)
        citation = "{} Anhang {}".format(context["citation_prefix"], number)
        unit = builder.add_unit(
            global_key="{}_anhang_{}".format(context["document_global_key"], number),
            unit_type="annex",
            legal_citation=citation,
            label="Anhang {}".format(number),
            number=number,
            title="",
            text=text,
            parent_unit_id=None,
            parser_name="eurlex_xhtml",
            extra={"source_xml_identifier": annex.get("id")},
        )
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_text".format(unit["global_key"]),
            legal_citation=citation,
            chunk_type="annex_text",
            label=unit["label"],
            number=number,
            text=text,
        )

    complete_text = _html_text(root)
    if not builder.units or not builder.chunks:
        _add_fallback_document_text(
            builder,
            context,
            complete_text,
            parser_name="eurlex_xhtml",
        )
        builder.add_issue(
            "eurlex_xhtml_no_articles",
            "warning",
            "Original EUR-Lex XHTML contained no recognized article structure.",
            {"source": str(register_record.get("local_path") or path.name)},
        )

    metadata = _metadata(
        context,
        register_record,
        source_format="eurlex_xhtml_original",
        source_hash=source_hash,
        article_count=sum(unit["unit_type"] == "article" for unit in builder.units),
        annex_count=sum(unit["unit_type"] == "annex" for unit in builder.units),
        page_count=0,
    )
    document = {
        "document_id": context["document_id"],
        "source_pdf": None,
        "source_xml": str(register_record.get("local_path") or path.name),
        "source_zip": None,
        "sha256": source_hash,
        "title": title,
        "full_citation": title,
        "canonical_citation": context["base_celex"],
        "date_enacted": None,
        "document_key": context["document_key"],
        "document_global_key": context["document_global_key"],
        "global_key": context["document_global_key"],
        "citation_prefix": context["citation_prefix"],
        "abbreviation": context["base_celex"],
        "pages": [],
        "page_refs": [],
        "structural_units": builder.units,
        "chunks": builder.chunks,
        "metadata": metadata,
    }
    return _payload(document, builder.issues, register_record)


def _read_pdf_pages(path: Path) -> Tuple[List[str], str]:
    try:
        import fitz  # type: ignore

        with fitz.open(path) as document:
            return [page.get_text("text") or "" for page in document], "pymupdf"
    except (ImportError, RuntimeError, ValueError):
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return [(page.extract_text() or "") for page in reader.pages], "pypdf"


def _title_from_pdf(first_page: str, fallback: str) -> str:
    lines = [normalize_text(line) for line in first_page.splitlines()]
    lines = [line for line in lines if line]
    for index, line in enumerate(lines):
        if any(
            marker in line.upper()
            for marker in ("VERORDNUNG", "RICHTLINIE", "REGULATION", "DIRECTIVE")
        ):
            selected = [line]
            for following in lines[index + 1:index + 6]:
                if ARTICLE_LINE_RE.fullmatch(following):
                    break
                selected.append(following)
            return normalize_text(" ".join(selected))
    return fallback


def _page_for_offset(offset: int, page_starts: List[int]) -> int:
    return max(1, bisect.bisect_right(page_starts, offset))


def extract_pdf(
    source_path: Path | str,
    register_record: Mapping[str, Any],
) -> Dict[str, Any]:
    path = Path(source_path)
    context = _document_context(register_record)
    source_hash = file_sha256(path)
    page_texts, backend = _read_pdf_pages(path)
    normalized_pages = [normalize_text(text) for text in page_texts]
    page_starts: List[int] = []
    combined_parts: List[str] = []
    offset = 0
    for text in normalized_pages:
        page_starts.append(offset)
        combined_parts.append(text)
        offset += len(text) + 2
    complete_text = "\n\n".join(combined_parts)
    fallback_title = str(register_record.get("title") or context["base_celex"])
    title = _title_from_pdf(normalized_pages[0] if normalized_pages else "", fallback_title)
    builder = DocumentBuilder(
        document_id=context["document_id"],
        document_key=context["document_key"],
        document_global_key=context["document_global_key"],
        citation_prefix=context["citation_prefix"],
    )

    markers: List[Tuple[int, int, str, str]] = []
    cursor = 0
    for line in complete_text.splitlines(keepends=True):
        article_match = ARTICLE_LINE_RE.fullmatch(line.strip())
        annex_match = ANNEX_LINE_RE.fullmatch(line.strip())
        if article_match:
            markers.append((cursor, cursor + len(line), "article", article_match.group(1)))
        elif annex_match:
            markers.append((cursor, cursor + len(line), "annex", annex_match.group(1) or ""))
        cursor += len(line)

    if markers and markers[0][0] > 0:
        preamble_text = normalize_text(complete_text[: markers[0][0]])
        if preamble_text:
            page_range = {"start": 1, "end": _page_for_offset(markers[0][0], page_starts)}
            unit = builder.add_unit(
                global_key="{}_preambel".format(context["document_global_key"]),
                unit_type="preamble",
                legal_citation="{} Präambel".format(context["citation_prefix"]),
                label="Präambel",
                number=None,
                title="Präambel und Erwägungsgründe",
                text=preamble_text,
                parent_unit_id=None,
                parser_name="eurlex_pdf",
                extra={"page_range": page_range},
            )
            builder.add_text_chunks(
                unit=unit,
                base_global_key="{}_text".format(unit["global_key"]),
                legal_citation=unit["legal_citation"],
                chunk_type="preamble_text",
                label="Präambel",
                number=None,
                text=preamble_text,
                extra={"page_range": page_range},
            )

    article_ordinal = 0
    annex_ordinal = 0
    for marker_index, (start, body_start, kind, raw_number) in enumerate(markers):
        end = markers[marker_index + 1][0] if marker_index + 1 < len(markers) else len(complete_text)
        text = normalize_text(complete_text[body_start:end])
        if not text:
            continue
        page_range = {
            "start": _page_for_offset(start, page_starts),
            "end": _page_for_offset(max(start, end - 1), page_starts),
        }
        if kind == "article":
            article_ordinal += 1
            number = raw_number or str(article_ordinal)
            citation = "{} Art. {}".format(context["citation_prefix"], number)
            unit_type = "article"
            label = "Artikel {}".format(number)
            key = "{}_art_{}".format(context["document_global_key"], slugify(number))
            chunk_type = "article_text"
        else:
            annex_ordinal += 1
            number = raw_number or str(annex_ordinal)
            citation = "{} Anhang {}".format(context["citation_prefix"], number)
            unit_type = "annex"
            label = "Anhang {}".format(number)
            key = "{}_anhang_{}".format(context["document_global_key"], slugify(number))
            chunk_type = "annex_text"
        unit = builder.add_unit(
            global_key=key,
            unit_type=unit_type,
            legal_citation=citation,
            label=label,
            number=number,
            title="",
            text=text,
            parent_unit_id=None,
            parser_name="eurlex_pdf",
            extra={"page_range": page_range},
        )
        builder.add_text_chunks(
            unit=unit,
            base_global_key="{}_text".format(unit["global_key"]),
            legal_citation=citation,
            chunk_type=chunk_type,
            label=label,
            number=number,
            text=text,
            extra={"page_range": page_range, "parser_name": "eurlex_pdf"},
        )

    if not builder.units or not builder.chunks:
        _add_fallback_document_text(
            builder,
            context,
            complete_text,
            parser_name="eurlex_pdf",
            page_range={"start": 1, "end": max(1, len(page_texts))},
        )
        builder.add_issue(
            "eurlex_pdf_no_articles",
            "warning",
            "Original EUR-Lex PDF contained no recognized article headings.",
            {"source": str(register_record.get("local_path") or path.name)},
        )
    builder.add_issue(
        "eurlex_pdf_fallback",
        "info",
        "Original EUR-Lex act was parsed from the official PDF because structured Formex was unavailable.",
        {"pages": len(page_texts), "text_backend": backend},
    )

    page_refs = [
        {
            "page_id": "page_{}_{}".format(slugify(context["base_celex"]), index),
            "page_number": index,
            "pdf_page_index": index - 1,
            "path": None,
            "text_sha256": sha256_text(text),
            "text_extraction_backend": backend,
        }
        for index, text in enumerate(normalized_pages, 1)
    ]
    metadata = _metadata(
        context,
        register_record,
        source_format="eurlex_pdf_original",
        source_hash=source_hash,
        article_count=sum(unit["unit_type"] == "article" for unit in builder.units),
        annex_count=sum(unit["unit_type"] == "annex" for unit in builder.units),
        page_count=len(page_texts),
    )
    metadata["text_extraction_backend"] = backend
    document = {
        "document_id": context["document_id"],
        "source_pdf": str(register_record.get("local_path") or path.name),
        "source_xml": None,
        "source_zip": None,
        "sha256": source_hash,
        "title": title,
        "full_citation": title,
        "canonical_citation": context["base_celex"],
        "date_enacted": None,
        "document_key": context["document_key"],
        "document_global_key": context["document_global_key"],
        "global_key": context["document_global_key"],
        "citation_prefix": context["citation_prefix"],
        "abbreviation": context["base_celex"],
        "pages": page_refs,
        "page_refs": page_refs,
        "structural_units": builder.units,
        "chunks": builder.chunks,
        "metadata": metadata,
    }
    return _payload(document, builder.issues, register_record)


def extract_source(
    source_path: Path | str,
    register_record: Mapping[str, Any],
) -> Dict[str, Any]:
    media_type = str(register_record.get("media_type") or "").casefold()
    suffix = Path(source_path).suffix.casefold()
    if media_type in {"application/xhtml+xml", "text/html"} or suffix in {".xhtml", ".html"}:
        return extract_xhtml(source_path, register_record)
    if media_type == "application/pdf" or suffix == ".pdf":
        return extract_pdf(source_path, register_record)
    raise ValueError("unsupported original EUR-Lex media type: {!r}".format(media_type or suffix))


__all__ = [
    "EURLEX_ORIGINAL_EXTRACTOR_VERSION",
    "extract_pdf",
    "extract_source",
    "extract_xhtml",
]
