"""GII XML ingestion into the canonical normtext raw-document contract."""

from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

from normtext_extractor.gii_xml_tables import (
    element_text,
    merge_continuation_tables,
    normalize_inline_text,
    parse_cals_table,
)


PARAGRAPH_LABEL_RE = re.compile(r"^§+\s*(\d+[a-z]?)\b", re.IGNORECASE)
ARTICLE_LABEL_RE = re.compile(r"^(?:Art\.?|Artikel)\s*([0-9]+[a-z]?)\b", re.IGNORECASE)
ANNEX_LABEL_RE = re.compile(r"^(Anlage|Anhang)\s*(.*)$", re.IGNORECASE)
SUBSECTION_LABEL_RE = re.compile(r"^\s*\((\d+[a-z]?)\)\s*")
TABLE_CUE_RE = re.compile(
    r"^\s*(?P<continuation>Fortsetzung\s+)?Tabelle\s+"
    r"(?P<number>\d+[a-z]?)\s*:?\s*(?P<title>.*)$",
    re.IGNORECASE | re.DOTALL,
)
TOC_LABELS = {"inhaltsübersicht", "inhaltsverzeichnis"}
FORMULA_LABELS = {
    "eingangsformel": "preamble",
    "präambel": "preamble",
    "preambel": "preamble",
    "schlussformel": "final_formula",
}
DIVISION_RANKS = {
    "buch": 1,
    "teil": 2,
    "kapitel": 3,
    "abschnitt": 4,
    "unterabschnitt": 5,
    "titel": 6,
    "untertitel": 7,
}
FOOTNOTE_PATHS = (
    "./textdaten/text/Footnotes/Footnote",
    "./textdaten/fussnoten/Footnotes/Footnote",
    "./textdaten/fussnoten/Content/P",
    "./textdaten/fussnoten/Revision/P",
)
FOOTNOTE_TOKEN_RE = re.compile(r"\[([^\[\]\r\n]+)\]")
REPEALED_RE = re.compile(r"\b(?:weggefallen|aufgehoben|außer\s+kraft)\b", re.IGNORECASE)

# GII packages are normally tiny.  These generous ceilings prevent a corrupt
# or hostile ZIP from exhausting memory while leaving ample room for image-
# heavy annexes.
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_XML_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_COMPRESSION_RATIO = 1_000


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_str(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def make_id(*parts: str) -> str:
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()[:12]


def slugify(value: str) -> str:
    value = (value or "").lower()
    for old, new in {
        "§": "para",
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }.items():
        value = value.replace(old, new)
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _clean_text(value: Optional[str]) -> str:
    return normalize_inline_text(value or "")


def _child_text(parent: Optional[ET.Element], path: str) -> str:
    if parent is None:
        return ""
    element = parent.find(path)
    return element_text(element) if element is not None else ""


def _all_child_text(parent: Optional[ET.Element], path: str) -> List[str]:
    if parent is None:
        return []
    return [
        element_text(element)
        for element in parent.findall(path)
        if element_text(element)
    ]


def _archive_member_is_safe(name: str) -> bool:
    member = Path(name)
    return not member.is_absolute() and ".." not in member.parts


def read_gii_xml_package(path: os.PathLike[str] | str) -> Dict[str, Any]:
    """Read one XML file or GII XML ZIP without extracting untrusted paths."""
    source_path = Path(path)
    package_bytes = source_path.stat().st_size
    if package_bytes > MAX_PACKAGE_BYTES:
        raise ValueError(
            "GII package exceeds {} byte safety limit".format(MAX_PACKAGE_BYTES)
        )
    raw = source_path.read_bytes()
    if zipfile.is_zipfile(io.BytesIO(raw)):
        assets: List[Dict[str, Any]] = []
        xml_candidates: List[Tuple[str, bytes]] = []
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(
                    "GII ZIP exceeds {} member safety limit".format(
                        MAX_ARCHIVE_MEMBERS
                    )
                )
            uncompressed_bytes = sum(info.file_size for info in infos)
            if uncompressed_bytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError(
                    "GII ZIP exceeds {} uncompressed byte safety limit".format(
                        MAX_ARCHIVE_UNCOMPRESSED_BYTES
                    )
                )
            for info in infos:
                if info.is_dir():
                    continue
                if not _archive_member_is_safe(info.filename):
                    raise ValueError("unsafe ZIP member path: {}".format(info.filename))
                if info.flag_bits & 0x1:
                    raise ValueError(
                        "encrypted ZIP member is not supported: {}".format(
                            info.filename
                        )
                    )
                if (
                    info.file_size >= 5 * 1024 * 1024
                    and info.compress_size
                    and info.file_size / info.compress_size
                    > MAX_MEMBER_COMPRESSION_RATIO
                ):
                    raise ValueError(
                        "suspicious ZIP compression ratio: {}".format(info.filename)
                    )
                if (
                    info.filename.lower().endswith(".xml")
                    and info.file_size > MAX_XML_BYTES
                ):
                    raise ValueError(
                        "GII XML exceeds {} byte safety limit".format(MAX_XML_BYTES)
                    )
                member_bytes = archive.read(info)
                item = {
                    "archive_member": info.filename,
                    "bytes": len(member_bytes),
                    "compressed_bytes": info.compress_size,
                    "sha256": sha256_bytes(member_bytes),
                }
                if info.filename.lower().endswith(".xml"):
                    xml_candidates.append((info.filename, member_bytes))
                    item["kind"] = "xml"
                else:
                    item["kind"] = "asset"
                assets.append(item)
        if not xml_candidates:
            raise ValueError("GII ZIP contains no XML document")
        xml_candidates.sort(key=lambda item: len(item[1]), reverse=True)
        xml_name, xml_bytes = xml_candidates[0]
        return {
            "package_path": str(source_path),
            "package_kind": "zip",
            "package_sha256": sha256_bytes(raw),
            "package_bytes": len(raw),
            "xml_name": xml_name,
            "xml_bytes": xml_bytes,
            "xml_sha256": sha256_bytes(xml_bytes),
            "assets": assets,
        }
    if len(raw) > MAX_XML_BYTES:
        raise ValueError(
            "GII XML exceeds {} byte safety limit".format(MAX_XML_BYTES)
        )
    return {
        "package_path": str(source_path),
        "package_kind": "xml",
        "package_sha256": sha256_bytes(raw),
        "package_bytes": len(raw),
        "xml_name": source_path.name,
        "xml_bytes": raw,
        "xml_sha256": sha256_bytes(raw),
        "assets": [
            {
                "archive_member": source_path.name,
                "bytes": len(raw),
                "sha256": sha256_bytes(raw),
                "kind": "xml",
            }
        ],
    }


def _mixed_events(node: ET.Element) -> Iterable[Tuple[str, Any]]:
    """Yield text/table/asset events from mixed content in source order."""
    if node.text:
        yield ("text", node.text)
    for child in node:
        if child.tag == "table":
            yield ("table", child)
        elif child.tag in {"IMG", "FILE"}:
            yield (
                "asset",
                {
                    "kind": child.tag.lower(),
                    "source": child.get("SRC"),
                    "preview": child.get("PREVIEW"),
                    "title": child.get("title") or child.get("alt"),
                    "attributes": dict(child.attrib),
                },
            )
        elif child.tag == "BR":
            yield ("text", "\n")
        elif child.tag == "QuoteL":
            yield ("text", "\u201e")
        elif child.tag == "QuoteR":
            yield ("text", "\u201c")
        elif child.tag == "FnR":
            reference = child.get("ID")
            if reference:
                yield ("text", "[{}]".format(reference))
        elif child.tag == "FnArea":
            # This is the visual footnote-rule area, not another semantic
            # citation site.  Its FnR children repeat already-rendered refs.
            pass
        elif child.tag == "TOC":
            pass
        else:
            yield from _mixed_events(child)
            if child.tag == "DT":
                yield ("text", " ")
            elif child.tag in {"DD", "P", "Title", "Subtitle", "Ident"}:
                yield ("text", "\n")
        if child.tail:
            yield ("text", child.tail)


def _preformatted_text(node: ET.Element) -> str:
    parts: List[str] = []

    def visit(element: ET.Element) -> None:
        if element.text:
            parts.append(element.text)
        for child in element:
            if child.tag == "BR":
                parts.append("\n")
            elif child.tag == "FnArea":
                pass
            elif child.tag == "FnR":
                reference = child.get("ID")
                if reference:
                    parts.append("[{}]".format(reference))
            elif child.tag == "IMG":
                description = child.get("alt") or child.get("title")
                parts.append(
                    description
                    or "[Bild: {}]".format(child.get("SRC") or "ohne Quelle")
                )
            elif child.tag == "FILE":
                parts.append(child.get("title") or child.get("SRC") or "[Datei]")
            else:
                visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(node)
    return "".join(parts).replace("\r\n", "\n").replace("\r", "\n")


def _direct_descendants(node: ET.Element, tag: str) -> List[ET.Element]:
    matches: List[ET.Element] = []

    def visit(parent: ET.Element) -> None:
        for child in parent:
            if child.tag == tag:
                matches.append(child)
            else:
                visit(child)

    visit(node)
    return matches


def _definition_list(dl: ET.Element) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    pending_marker = ""
    for child in dl:
        if child.tag == "DT":
            pending_marker = element_text(child)
            continue
        if child.tag != "DD":
            continue
        nested = [_definition_list(item) for item in _direct_descendants(child, "DL")]
        items.append(
            {
                "marker": pending_marker,
                "text": element_text(child),
                "children": nested,
                "attributes": dict(child.attrib),
            }
        )
        pending_marker = ""
    return {
        "list_type": dl.get("Type"),
        "items": items,
        "attributes": dict(dl.attrib),
    }


def _visible_footnote_marker(footnote: ET.Element) -> str:
    marker = "{}{}{}".format(
        footnote.get("Prefix") or "",
        footnote.get("FnZ") or "",
        footnote.get("Postfix") or "",
    ).strip()
    return "[{}]".format(marker) if marker else ""


def _footnote_marker_map(norm: ET.Element) -> Dict[str, str]:
    markers: Dict[str, str] = {}
    for path in FOOTNOTE_PATHS[:2]:
        for footnote in norm.findall(path):
            footnote_id = footnote.get("ID")
            if not footnote_id:
                continue
            marker = _visible_footnote_marker(footnote)
            if marker:
                markers[footnote_id] = marker
    return markers


def _replace_footnote_references(value: Any, markers: Dict[str, str]) -> Any:
    """Replace rendered internal IDREF tokens throughout a parsed block."""
    if isinstance(value, str):
        return FOOTNOTE_TOKEN_RE.sub(
            lambda match: markers.get(match.group(1), match.group(0)),
            value,
        )
    if isinstance(value, list):
        return [_replace_footnote_references(item, markers) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_footnote_references(item, markers) for item in value)
    if isinstance(value, dict):
        return {
            key: (
                item
                if key == "source_element"
                else _replace_footnote_references(item, markers)
            )
            for key, item in value.items()
        }
    return value


def _source_footnote_ids(node: ET.Element) -> List[str]:
    references: List[str] = []

    def visit(element: ET.Element) -> None:
        for child in element:
            if child.tag in {"FnArea", "Footnotes"}:
                continue
            if child.tag == "FnR":
                footnote_id = child.get("ID")
                if footnote_id and footnote_id not in references:
                    references.append(footnote_id)
            else:
                visit(child)

    visit(node)
    return references


def _blocks_from_mixed(node: ET.Element, source_tag: str) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    text_parts: List[str] = []

    def flush_text() -> None:
        text = normalize_inline_text("".join(text_parts))
        text_parts.clear()
        if text:
            blocks.append({"type": "text", "text": text, "source_tag": source_tag})

    for event_type, value in _mixed_events(node):
        if event_type == "text":
            text_parts.append(value)
            continue
        flush_text()
        if event_type == "table":
            blocks.append(
                {
                    "type": "table",
                    "table": parse_cals_table(value),
                    "source_element": value,
                    "source_tag": source_tag,
                    "footnote_ids": _source_footnote_ids(value),
                }
            )
        elif event_type == "asset":
            blocks.append({"type": "asset", **value, "source_tag": source_tag})
    flush_text()
    first_text = next(
        (block for block in blocks if block.get("type") == "text"),
        None,
    )
    if first_text is not None:
        structured_lists = (
            [_definition_list(node)]
            if node.tag == "DL"
            else [
                _definition_list(item)
                for item in _direct_descendants(node, "DL")
            ]
        )
        if structured_lists:
            first_text["structured_lists"] = structured_lists
        preformatted = (
            [_preformatted_text(node)]
            if node.tag == "pre"
            else [
                _preformatted_text(item)
                for item in _direct_descendants(node, "pre")
            ]
        )
        if preformatted:
            first_text["preformatted_blocks"] = preformatted
    return blocks


def norm_content_blocks(
    norm: ET.Element,
    footnote_markers: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Return source-ordered non-TOC blocks for one GII norm."""
    blocks: List[Dict[str, Any]] = []
    for container_path in ("./textdaten/text",):
        container = norm.find(container_path)
        if container is None:
            continue
        for child in container:
            if child.tag == "TOC" or child.tag == "Footnotes":
                continue
            if child.tag in {"Content", "Revision"}:
                for content_child in child:
                    if content_child.tag == "TOC":
                        continue
                    if content_child.tag == "table":
                        blocks.append(
                            {
                                "type": "table",
                                "table": parse_cals_table(content_child),
                                "source_element": content_child,
                                "source_tag": content_child.tag,
                                "footnote_ids": _source_footnote_ids(content_child),
                            }
                        )
                    elif content_child.tag in {"IMG", "FILE"}:
                        blocks.append(
                            {
                                "type": "asset",
                                "kind": content_child.tag.lower(),
                                "source": content_child.get("SRC"),
                                "preview": content_child.get("PREVIEW"),
                                "title": content_child.get("title")
                                or content_child.get("alt"),
                                "attributes": dict(content_child.attrib),
                                "source_tag": content_child.tag,
                            }
                        )
                    else:
                        blocks.extend(_blocks_from_mixed(content_child, content_child.tag))
            elif child.tag == "table":
                blocks.append(
                    {
                        "type": "table",
                        "table": parse_cals_table(child),
                        "source_element": child,
                        "source_tag": child.tag,
                        "footnote_ids": _source_footnote_ids(child),
                    }
                )
            else:
                blocks.extend(_blocks_from_mixed(child, child.tag))
    if footnote_markers:
        blocks = [
            _replace_footnote_references(block, footnote_markers)
            for block in blocks
        ]
    return blocks


def norm_footnotes(norm: ET.Element) -> List[Dict[str, Any]]:
    footnotes: List[Dict[str, Any]] = []
    marker_map = _footnote_marker_map(norm)
    for root_path in FOOTNOTE_PATHS:
        note_kind = (
            "footnote"
            if root_path.endswith("Footnotes/Footnote")
            else "source_note"
        )
        for element in norm.findall(root_path):
            text = element_text(element)
            if not text:
                continue
            text = _replace_footnote_references(text, marker_map)
            fallback_index = len(footnotes) + 1
            footnotes.append(
                {
                    "footnote_id": element.get("ID")
                    or "{}_{}".format(note_kind, fallback_index),
                    "note_kind": note_kind,
                    "prefix": element.get("Prefix"),
                    "marker": element.get("FnZ"),
                    "postfix": element.get("Postfix"),
                    "visible_marker": _visible_footnote_marker(element),
                    "text": text,
                    "attributes": dict(element.attrib),
                    "preformatted_blocks": [
                        _preformatted_text(item)
                        for item in _direct_descendants(element, "pre")
                    ],
                }
            )
    return footnotes


def norm_metadata(
    norm: ET.Element,
    footnote_markers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    metadata = norm.find("./metadaten")
    division = metadata.find("./gliederungseinheit") if metadata is not None else None
    result = {
        "document_number": norm.get("doknr"),
        "build_date": norm.get("builddate"),
        "jurabk": _all_child_text(metadata, "./jurabk"),
        "official_abbreviation": _child_text(metadata, "./amtabk"),
        "date_enacted": _child_text(metadata, "./ausfertigung-datum"),
        "short_title": _child_text(metadata, "./kurzue"),
        "long_title": _child_text(metadata, "./langue"),
        "label": _child_text(metadata, "./enbez"),
        "title": _child_text(metadata, "./titel"),
        "division_key": _child_text(division, "./gliederungskennzahl"),
        "division_label": _child_text(division, "./gliederungsbez"),
        "division_title": _child_text(division, "./gliederungstitel"),
    }
    if footnote_markers:
        result = _replace_footnote_references(result, footnote_markers)
    return result


def classify_norm(meta: Dict[str, Any], has_content: bool) -> Dict[str, Any]:
    """Classify a norm, giving meaningful enbez precedence over hierarchy data."""
    label = _clean_text(meta.get("label"))
    division_label = _clean_text(meta.get("division_label"))
    title = _clean_text(meta.get("title"))
    if not label and has_content and division_label:
        if PARAGRAPH_LABEL_RE.match(division_label) or ARTICLE_LABEL_RE.match(division_label):
            label = division_label
            title = title or _clean_text(meta.get("division_title"))
    normalized_label = label.lower().rstrip(":")
    if normalized_label in TOC_LABELS:
        return {"kind": "toc", "label": label, "title": title, "number": None}
    paragraph_match = PARAGRAPH_LABEL_RE.match(label)
    if paragraph_match:
        return {
            "kind": "paragraph",
            "label": label,
            "title": title,
            "number": paragraph_match.group(1),
        }
    article_match = ARTICLE_LABEL_RE.match(label)
    if article_match:
        return {
            "kind": "article",
            "label": label,
            "title": title,
            "number": article_match.group(1),
        }
    annex_match = ANNEX_LABEL_RE.match(label)
    if annex_match:
        number = _clean_text(annex_match.group(2)) or "anlage"
        return {
            "kind": "annex" if annex_match.group(1).lower() == "anlage" else "appendix",
            "label": label,
            "title": title,
            "number": number,
        }
    if normalized_label in FORMULA_LABELS:
        return {
            "kind": FORMULA_LABELS[normalized_label],
            "label": label,
            "title": title,
            "number": None,
        }
    if label and has_content:
        return {
            "kind": "provision",
            "label": label,
            "title": title,
            "number": label,
        }
    if division_label:
        article_match = ARTICLE_LABEL_RE.match(division_label)
        if article_match:
            return {
                "kind": "article",
                "label": division_label,
                "title": _clean_text(meta.get("division_title")),
                "number": article_match.group(1),
                "from_division": True,
            }
        paragraph_match = PARAGRAPH_LABEL_RE.match(division_label)
        if paragraph_match:
            return {
                "kind": "paragraph",
                "label": division_label,
                "title": _clean_text(meta.get("division_title")),
                "number": paragraph_match.group(1),
                "from_division": True,
            }
        if division_label.strip() == "-":
            division_label = ""
    if division_label:
        return {
            "kind": "division",
            "label": division_label,
            "title": _clean_text(meta.get("division_title")),
            "number": _clean_text(meta.get("division_key")) or division_label,
            "from_division": True,
        }
    if has_content:
        return {
            "kind": "document_note",
            "label": label or "Dokumenthinweise",
            "title": title,
            "number": None,
        }
    return {"kind": "metadata", "label": label, "title": title, "number": None}


def _division_rank(label: str, key: str) -> int:
    normalized = slugify(label)
    for prefix, rank in DIVISION_RANKS.items():
        if normalized.startswith(prefix):
            return rank
    compact_key = re.sub(r"\D", "", key or "")
    if compact_key and len(compact_key) >= 3:
        groups = [compact_key[index : index + 3] for index in range(0, len(compact_key), 3)]
        nonzero = [group for group in groups if group and set(group) != {"0"}]
        return max(len(nonzero), 1)
    return 1


def _division_label_kind(label: str) -> Optional[str]:
    normalized = slugify(label)
    for prefix in DIVISION_RANKS:
        if normalized.startswith(prefix):
            return prefix
    return None


def _longest_division_parent(
    division_key: str,
    units_by_division_key: Dict[str, str],
) -> Optional[str]:
    key = re.sub(r"\s+", "", division_key or "")
    candidates = [
        (candidate, unit_id)
        for candidate, unit_id in units_by_division_key.items()
        if candidate != key and key.startswith(candidate)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item[0]))[1]


def _unit_key_component(kind: str, label: str, number: Any) -> str:
    if kind == "paragraph":
        return "para_{}".format(slugify(str(number)))
    if kind == "article":
        return "art_{}".format(slugify(str(number)))
    if kind == "annex":
        return "anlage_{}".format(slugify(str(number)))
    if kind == "appendix":
        return "anhang_{}".format(slugify(str(number)))
    if kind == "division":
        return "gliederung_{}".format(slugify(str(number or label)))
    return "{}_{}".format(slugify(kind), slugify(str(number or label)))


def _deduplicate_key(base: str, seen: Dict[str, int]) -> str:
    seen[base] = seen.get(base, 0) + 1
    if seen[base] == 1:
        return base
    return "{}_part_{}".format(base, seen[base])


def _citation(prefix: str, label: str) -> str:
    return "{} {}".format(prefix, label).strip()


def _table_text(columns: Sequence[str], rows: Sequence[Dict[str, Any]]) -> str:
    lines = [" | ".join(columns)]
    for row in rows:
        lines.append(" | ".join(str(row.get(column) or "") for column in columns))
    return "\n".join(lines).strip()


def _document_metadata(root: ET.Element) -> Tuple[ET.Element, Dict[str, Any]]:
    norms = root.findall("./norm")
    if not norms:
        raise ValueError("GII XML contains no norm elements")
    preferred = next(
        (
            norm
            for norm in norms
            if norm.find("./metadaten/langue") is not None
            or norm.find("./metadaten/kurzue") is not None
        ),
        norms[0],
    )
    return preferred, norm_metadata(preferred, _footnote_marker_map(preferred))


def _root_document_number(root: ET.Element, metadata_norm: ET.Element) -> str:
    return root.get("doknr") or metadata_norm.get("doknr") or ""


def _table_cue(text: str) -> Optional[Dict[str, Any]]:
    match = TABLE_CUE_RE.match(text or "")
    if not match:
        return None
    return {
        "label": "Tabelle {}".format(match.group("number")),
        "continuation": bool(match.group("continuation")),
        "title": _clean_text(match.group("title")),
    }


def _attach_child(parent_by_id: Dict[str, Dict[str, Any]], parent_id: Optional[str], child_id: str) -> None:
    if not parent_id:
        return
    parent = parent_by_id.get(parent_id)
    if parent is not None and child_id not in parent["child_unit_ids"]:
        parent["child_unit_ids"].append(child_id)


def _issue(
    document_id: str,
    issue_type: str,
    severity: str,
    description: str,
    evidence: Any,
    unit_id: Optional[str] = None,
    chunk_id: Optional[str] = None,
) -> Dict[str, Any]:
    evidence_text = (
        evidence
        if isinstance(evidence, str)
        else repr(evidence)
    )
    return {
        "issue_id": "issue_{}".format(
            make_id(
                document_id,
                issue_type,
                unit_id or "",
                chunk_id or "",
                evidence_text,
            )
        ),
        "document_id": document_id,
        "unit_id": unit_id,
        "chunk_id": chunk_id,
        "issue_type": issue_type,
        "severity": severity,
        "description": description,
        "evidence": evidence,
        "corrected_value": None,
        "review_status": "open",
    }


def _asset_reference_manifest(
    root: ET.Element,
    package_assets: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    by_name = {
        str(asset.get("archive_member") or ""): asset
        for asset in package_assets
        if asset.get("kind") == "asset"
    }
    by_basename: Dict[str, List[Dict[str, Any]]] = {}
    for name, asset in by_name.items():
        by_basename.setdefault(Path(name).name, []).append(asset)

    references: List[Dict[str, Any]] = []
    missing: List[str] = []
    for element in root.iter():
        if element.tag not in {"IMG", "FILE"}:
            continue
        source = element.get("SRC") or ""
        match = by_name.get(source)
        if match is None:
            basename_matches = by_basename.get(Path(source).name, [])
            if len(basename_matches) == 1:
                match = basename_matches[0]
        reference = {
            "kind": element.tag.lower(),
            "source": source or None,
            "preview": element.get("PREVIEW"),
            "title": element.get("title") or element.get("alt"),
            "attributes": dict(element.attrib),
            "archive_member": match.get("archive_member") if match else None,
            "sha256": match.get("sha256") if match else None,
            "bytes": match.get("bytes") if match else None,
            "status": "available" if match else "missing",
        }
        references.append(reference)
        if source and match is None and source not in missing:
            missing.append(source)
    return references, missing


def extract_document_from_xml(
    source_path: os.PathLike[str] | str,
    source_manifest_entry: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Convert one official GII XML package to the canonical raw document."""
    package = read_gii_xml_package(source_path)
    if b"<!ENTITY" in package["xml_bytes"].upper():
        raise ValueError("GII XML with inline entity declarations is not supported")
    try:
        root = ET.fromstring(package["xml_bytes"])
    except ET.ParseError as exc:
        raise ValueError("invalid GII XML: {}".format(exc)) from exc
    if root.tag != "dokumente":
        raise ValueError("unexpected GII XML root {!r}".format(root.tag))

    metadata_norm, root_meta = _document_metadata(root)
    document_number = _root_document_number(root, metadata_norm)
    abbreviation = (
        root_meta.get("official_abbreviation")
        or (root_meta.get("jurabk") or [""])[-1]
        or (source_manifest_entry or {}).get("title")
        or document_number
    )
    title = root_meta.get("long_title") or root_meta.get("short_title") or abbreviation
    short_title = root_meta.get("short_title") or title
    document_key = slugify(abbreviation) or slugify(document_number)
    document_global_key = slugify(short_title) or document_key
    doc_id = "doc_{}".format(make_id("gii_xml", document_number or package["xml_sha256"]))
    source_pdf = (source_manifest_entry or {}).get("pdf_file")
    source_pdf = source_pdf or (source_manifest_entry or {}).get("relative_file_path")
    source_pdf = source_pdf or (source_manifest_entry or {}).get("pdf_relative_path")
    source_asset_refs, missing_assets = _asset_reference_manifest(
        root,
        package["assets"],
    )
    asset_ref_by_source = {
        reference["source"]: reference
        for reference in source_asset_refs
        if reference.get("source")
    }

    structural_units: List[Dict[str, Any]] = []
    chunks: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    for missing_asset in missing_assets:
        issues.append(
            _issue(
                doc_id,
                "xml_missing_asset",
                "warning",
                "XML references an asset that is absent from the source package.",
                missing_asset,
            )
        )
    unit_by_id: Dict[str, Dict[str, Any]] = {}
    seen_unit_keys: Dict[str, int] = {}
    seen_chunk_keys: Dict[str, int] = {}
    division_stack: List[Tuple[int, str]] = []
    unit_by_division_key: Dict[str, str] = {}

    for norm_index, norm in enumerate(root.findall("./norm")):
        footnote_markers = _footnote_marker_map(norm)
        meta = norm_metadata(norm, footnote_markers)
        blocks = norm_content_blocks(norm, footnote_markers)
        footnotes = norm_footnotes(norm)
        definition_ids = {
            footnote["footnote_id"]
            for footnote in footnotes
            if footnote.get("note_kind") == "footnote"
        }
        referenced_ids = set(_source_footnote_ids(norm))
        missing_definitions = sorted(referenced_ids - definition_ids)
        orphan_definitions = sorted(definition_ids - referenced_ids)
        has_content = bool(blocks or footnotes)
        classification = classify_norm(meta, has_content)
        kind = classification["kind"]
        if kind in {"metadata", "toc"}:
            continue

        if missing_definitions:
            issues.append(
                _issue(
                    doc_id,
                    "xml_missing_footnote_definition",
                    "warning",
                    "One or more FnR references have no matching Footnote definition.",
                    missing_definitions,
                )
            )
        if orphan_definitions:
            issues.append(
                _issue(
                    doc_id,
                    "xml_orphan_footnote_definition",
                    "warning",
                    "One or more Footnote definitions are not referenced by operative text.",
                    orphan_definitions,
                )
            )

        label = classification["label"]
        unit_title = classification["title"]
        number = classification["number"]
        division_key = re.sub(r"\s+", "", meta.get("division_key") or "")
        if kind in {"annex", "appendix", "preamble", "final_formula"}:
            division_stack = []
        hierarchy_conflict: Optional[Dict[str, Any]] = None
        parent_unit_id = division_stack[-1][1] if division_stack and kind != "division" else None
        if division_key and not classification.get("from_division"):
            parent_unit_id = unit_by_division_key.get(division_key) or parent_unit_id
        if classification.get("from_division"):
            parent_unit_id = _longest_division_parent(division_key, unit_by_division_key)
        if kind == "division":
            rank = _division_rank(label, str(number or ""))
            while division_stack and division_stack[-1][0] >= rank:
                division_stack.pop()
            semantic_parent = division_stack[-1][1] if division_stack else None
            numeric_parent = _longest_division_parent(
                division_key,
                unit_by_division_key,
            )
            numeric_parent_unit = unit_by_id.get(numeric_parent or "")
            if (
                numeric_parent_unit
                and numeric_parent_unit.get("unit_type") == "division"
                and _division_label_kind(numeric_parent_unit.get("label") or "")
                == _division_label_kind(label)
                and _division_label_kind(label) is not None
            ):
                hierarchy_conflict = {
                    "division_key": division_key,
                    "label": label,
                    "numeric_parent_unit_id": numeric_parent,
                    "semantic_parent_unit_id": semantic_parent,
                }
                parent_unit_id = semantic_parent
            else:
                parent_unit_id = numeric_parent or semantic_parent

        component = _unit_key_component(kind, label, number)
        key_parent = unit_by_id.get(parent_unit_id or "")
        key_prefix = document_global_key
        if (
            kind == "paragraph"
            and key_parent
            and key_parent.get("unit_type") == "article"
        ):
            key_prefix = key_parent["global_key"]
        unit_global_key = _deduplicate_key(
            "{}_{}".format(key_prefix, component),
            seen_unit_keys,
        )
        unit_id = "unit_{}".format(unit_global_key)
        legal_citation = _citation(abbreviation, label)
        plain_text_parts = [label, unit_title]
        plain_text_parts.extend(block["text"] for block in blocks if block["type"] == "text")
        for block in blocks:
            if block["type"] == "table":
                table = block["table"]
                plain_text_parts.append(_table_text(table.get("columns") or [], table.get("rows") or []))
        unit_text = "\n".join(part for part in plain_text_parts if part).strip()
        breadcrumbs = (
            list(key_parent.get("breadcrumbs") or []) + [label]
            if key_parent
            else [document_global_key, label]
        )
        unit = {
            "unit_id": unit_id,
            "global_key": unit_global_key,
            "legal_citation": legal_citation,
            "display_name": "{}{}".format(
                legal_citation,
                " {}".format(unit_title) if unit_title else "",
            ),
            "document_id": doc_id,
            "document_key": document_key,
            "document_global_key": document_global_key,
            "unit_type": kind,
            "label": label,
            "number": number,
            "title": unit_title or None,
            "breadcrumbs": breadcrumbs,
            "parent_unit_id": parent_unit_id,
            "child_unit_ids": [],
            "sequence": len(structural_units) + 1,
            "source_order": norm_index,
            "page_range": None,
            "text": unit_text,
            "text_sha256": sha256_str(unit_text),
            "confidence": 1.0,
            "review_status": "pending",
            "is_repealed": bool(
                REPEALED_RE.search(
                    "\n".join(
                        value
                        for value in (label, unit_title, unit_text)
                        if value
                    )
                )
            ),
            "is_uncertain": bool(hierarchy_conflict),
            "uncertainty_reason": (
                "XML numeric hierarchy conflicts with semantic heading rank"
                if hierarchy_conflict
                else None
            ),
            "hierarchy_rank": (
                _division_rank(label, str(number or ""))
                if kind == "division"
                else None
            ),
            "source_xml_document_number": meta.get("document_number"),
            "source_xml_build_date": meta.get("build_date"),
            "source_xml_norm_index": norm_index,
            "source_xml_division_key": meta.get("division_key") or None,
        }
        structural_units.append(unit)
        unit_by_id[unit_id] = unit
        _attach_child(unit_by_id, parent_unit_id, unit_id)
        if hierarchy_conflict:
            issues.append(
                _issue(
                    doc_id,
                    "xml_hierarchy_rank_conflict",
                    "warning",
                    "Numeric division ancestry conflicts with semantic heading rank; semantic order was used.",
                    hierarchy_conflict,
                    unit_id=unit_id,
                )
            )
        if kind == "division":
            division_stack.append((_division_rank(label, str(number or "")), unit_id))
        if division_key and classification.get("from_division"):
            unit_by_division_key.setdefault(division_key, unit_id)

        sequence = 0
        pending_table: Optional[Dict[str, Any]] = None
        unnamed_table_counter = 0
        logical_table_by_label: Dict[str, Dict[str, Any]] = {}
        table_records: List[Dict[str, Any]] = []
        text_block_index = 0
        last_subsection_chunk: Optional[Dict[str, Any]] = None

        for block in blocks:
            if block["type"] == "text":
                text = block["text"]
                cue = _table_cue(text)
                if cue:
                    pending_table = cue
                    last_subsection_chunk = None
                    continue
                if pending_table is not None and not pending_table.get("title"):
                    pending_table["title"] = text
                    last_subsection_chunk = None
                text_block_index += 1
                sequence += 1
                subsection_match = SUBSECTION_LABEL_RE.match(text)
                if subsection_match and kind in {"paragraph", "article", "provision"}:
                    sub_number: Any = subsection_match.group(1)
                    chunk_type = "subsection"
                    chunk_label = "Abs. {}".format(sub_number)
                    chunk_citation = "{} Abs. {}".format(legal_citation, sub_number)
                    component = "abs_{}".format(slugify(str(sub_number)))
                else:
                    if (
                        last_subsection_chunk is not None
                        and block.get("source_tag") == "P"
                        and pending_table is None
                        and kind in {"paragraph", "article", "provision"}
                    ):
                        combined_text = "{}\n{}".format(
                            last_subsection_chunk["text"],
                            text,
                        ).strip()
                        last_subsection_chunk["text"] = combined_text
                        last_subsection_chunk["text_sha256"] = sha256_str(combined_text)
                        last_subsection_chunk.setdefault(
                            "source_xml_block_indexes",
                            [last_subsection_chunk["source_xml_block_index"]],
                        ).append(text_block_index)
                        for additive_field in (
                            "structured_lists",
                            "preformatted_blocks",
                        ):
                            if block.get(additive_field):
                                last_subsection_chunk.setdefault(
                                    additive_field,
                                    [],
                                ).extend(block[additive_field])
                        continue
                    sub_number = None
                    chunk_type = "annex_text" if kind in {"annex", "appendix"} else "provision_text"
                    chunk_label = label if text_block_index == 1 else "text_{}".format(text_block_index)
                    chunk_citation = legal_citation
                    component = "text_{}".format(text_block_index)
                chunk_global_key = _deduplicate_key(
                    "{}_{}".format(unit_global_key, component),
                    seen_chunk_keys,
                )
                chunk = {
                    "chunk_id": "chunk_{}".format(chunk_global_key),
                    "global_key": chunk_global_key,
                    "legal_citation": chunk_citation,
                    "display_name": chunk_citation,
                    "chunk_type": chunk_type,
                    "unit_id": unit_id,
                    "document_global_key": document_global_key,
                    "parent_chunk_id": None,
                    "child_chunk_ids": [],
                    "label": chunk_label,
                    "number": sub_number,
                    "sequence": sequence,
                    "source_order": [norm_index, text_block_index],
                    "page_id": None,
                    "page_range": None,
                    "text": text,
                    "text_sha256": sha256_str(text),
                    "evidence_text": unit_text,
                    "confidence": 1.0,
                    "review_status": "pending",
                    "source_xml_norm_index": norm_index,
                    "source_xml_block_index": text_block_index,
                    "source_xml_tag": block.get("source_tag"),
                }
                if block.get("structured_lists"):
                    chunk["structured_lists"] = block["structured_lists"]
                if block.get("preformatted_blocks"):
                    chunk["preformatted_blocks"] = block["preformatted_blocks"]
                chunks.append(chunk)
                last_subsection_chunk = chunk if chunk_type == "subsection" else None
                continue

            if block["type"] == "asset":
                last_subsection_chunk = None
                sequence += 1
                if block["kind"] == "img":
                    asset_text = block.get("title") or "[Bild: {}]".format(
                        block.get("source") or "ohne Quelle"
                    )
                else:
                    asset_text = (
                        block.get("title")
                        or block.get("source")
                        or "[Datei]"
                    )
                resolved_asset = asset_ref_by_source.get(block.get("source"))
                chunk_global_key = _deduplicate_key(
                    "{}_asset_{}".format(unit_global_key, sequence),
                    seen_chunk_keys,
                )
                chunks.append(
                    {
                        "chunk_id": "chunk_{}".format(chunk_global_key),
                        "global_key": chunk_global_key,
                        "legal_citation": legal_citation,
                        "display_name": legal_citation,
                        "chunk_type": "source_asset",
                        "unit_id": unit_id,
                        "document_global_key": document_global_key,
                        "parent_chunk_id": None,
                        "child_chunk_ids": [],
                        "label": block["kind"],
                        "number": None,
                        "sequence": sequence,
                        "source_order": [norm_index, sequence],
                        "page_id": None,
                        "page_range": None,
                        "text": asset_text,
                        "text_sha256": sha256_str(asset_text),
                        "confidence": 1.0,
                        "review_status": "pending",
                        "source_asset": block.get("source"),
                        "source_asset_attributes": block.get("attributes") or {},
                        "source_asset_status": (
                            resolved_asset.get("status")
                            if resolved_asset
                            else "missing"
                        ),
                        "source_asset_sha256": (
                            resolved_asset.get("sha256")
                            if resolved_asset
                            else None
                        ),
                    }
                )
                continue

            if block["type"] != "table":
                continue
            last_subsection_chunk = None
            table = block["table"]
            if pending_table:
                table_label = pending_table["label"]
                table_title = pending_table.get("title") or table.get("title") or ""
                continuation = bool(pending_table.get("continuation"))
            else:
                unnamed_table_counter += 1
                table_label = "Unbezeichnete Tabelle {}".format(unnamed_table_counter)
                table_title = table.get("title") or ""
                continuation = False
            table_key = slugify(table_label)
            if continuation and table_key in logical_table_by_label:
                record = logical_table_by_label[table_key]
                record["table"] = merge_continuation_tables(record["table"], table)
                record["source_table_count"] += 1
                record["footnote_ids"] = sorted(
                    set(record.get("footnote_ids") or [])
                    | set(block.get("footnote_ids") or [])
                )
            else:
                record = {
                    "label": table_label,
                    "title": table_title,
                    "table": table,
                    "source_table_count": 1,
                    "sequence": len(table_records) + 1,
                    "footnote_ids": list(block.get("footnote_ids") or []),
                }
                table_records.append(record)
                if table_key not in logical_table_by_label:
                    logical_table_by_label[table_key] = record
            pending_table = None

        table_context_by_footnote_id: Dict[str, List[Dict[str, str]]] = {}
        for table_index, record in enumerate(table_records, 1):
            table = record["table"]
            table_label = record["label"]
            table_component = "{}_{}".format(slugify(table_label), table_index)
            table_global_key = _deduplicate_key(
                "{}_{}".format(unit_global_key, table_component),
                seen_unit_keys,
            )
            table_unit_id = "unit_{}".format(table_global_key)
            table_citation = "{} {}".format(legal_citation, table_label)
            rows = table.get("rows") or []
            columns = table.get("columns") or []
            text = _table_text(columns, rows)
            table_uncertainty_reasons = []
            if table.get("continuation_unmerged"):
                table_uncertainty_reasons.append(
                    "XML continuation panels could not be merged"
                )
            if table.get("multi_tgroup_layout"):
                table_uncertainty_reasons.append(
                    "XML table contains incompatible tgroup layouts"
                )
            table_unit = {
                "unit_id": table_unit_id,
                "global_key": table_global_key,
                "legal_citation": table_citation,
                "display_name": "{}{}".format(
                    table_citation,
                    " {}".format(record["title"]) if record["title"] else "",
                ),
                "document_id": doc_id,
                "document_key": document_key,
                "document_global_key": document_global_key,
                "unit_type": "table",
                "label": table_label,
                "number": re.sub(r"(?i)^Tabelle\s+", "", table_label),
                "title": record["title"] or None,
                "breadcrumbs": [document_global_key, label, table_label],
                "parent_unit_id": unit_id,
                "child_unit_ids": [],
                "sequence": len(structural_units) + 1,
                "source_order": [norm_index, table_index],
                "page_range": None,
                "text": text,
                "text_sha256": sha256_str(text),
                "columns": columns,
                "column_header_text": " | ".join(columns),
                "row_count": len(rows),
                "note_count": len(record.get("footnote_ids") or []),
                "table_sections": [],
                "parser_name": "gii_xml_cals",
                "confidence": 1.0,
                "review_status": "pending",
                "is_uncertain": bool(table_uncertainty_reasons),
                "uncertainty_reason": (
                    "; ".join(table_uncertainty_reasons)
                    if table_uncertainty_reasons
                    else None
                ),
                "source_xml_norm_index": norm_index,
                "source_xml_table_index": table_index,
                "source_xml_table_count": record["source_table_count"],
                "source_xml_footnote_ids": record.get("footnote_ids") or [],
                "table_data": table,
            }
            structural_units.append(table_unit)
            unit_by_id[table_unit_id] = table_unit
            _attach_child(unit_by_id, unit_id, table_unit_id)
            for footnote_id in record.get("footnote_ids") or []:
                table_context_by_footnote_id.setdefault(footnote_id, []).append(
                    {
                        "unit_id": table_unit_id,
                        "legal_citation": table_citation,
                    }
                )
            chunk_global_key = _deduplicate_key(
                "{}_rows".format(table_global_key),
                seen_chunk_keys,
            )
            chunks.append(
                {
                    "chunk_id": "chunk_{}".format(chunk_global_key),
                    "global_key": chunk_global_key,
                    "legal_citation": table_citation,
                    "display_name": table_citation,
                    "chunk_type": "table_rows",
                    "unit_id": table_unit_id,
                    "document_global_key": document_global_key,
                    "parent_chunk_id": None,
                    "child_chunk_ids": [],
                    "label": "rows",
                    "number": None,
                    "sequence": 1,
                    "source_order": [norm_index, table_index, 0],
                    "page_id": None,
                    "page_range": None,
                    "columns": columns,
                    "column_header_text": " | ".join(columns),
                    "parser_name": "gii_xml_cals",
                    "table_section": None,
                    "rows": rows,
                    "text": text,
                    "text_sha256": sha256_str(text),
                    "confidence": 1.0,
                    "review_status": "pending",
                    "row_range": {"start": 1, "end": len(rows)} if rows else None,
                    "table_data": table,
                }
            )

        for footnote_index, footnote in enumerate(footnotes, 1):
            sequence += 1
            table_contexts = table_context_by_footnote_id.get(
                footnote["footnote_id"],
                [],
            )
            table_context = table_contexts[0] if len(table_contexts) == 1 else None
            note_kind = footnote.get("note_kind") or "footnote"
            note_unit_id = (
                table_context["unit_id"]
                if table_context and note_kind == "footnote"
                else unit_id
            )
            note_citation = (
                table_context["legal_citation"]
                if table_context and note_kind == "footnote"
                else legal_citation
            )
            chunk_type = (
                "table_note"
                if table_context and note_kind == "footnote"
                else note_kind
            )
            footnote_key = _deduplicate_key(
                "{}_{}_{}".format(
                    unit_global_key,
                    slugify(chunk_type),
                    slugify(footnote["footnote_id"]),
                ),
                seen_chunk_keys,
            )
            chunks.append(
                {
                    "chunk_id": "chunk_{}".format(footnote_key),
                    "global_key": footnote_key,
                    "legal_citation": note_citation,
                    "display_name": "{} {}".format(
                        note_citation,
                        "Fußnote" if note_kind == "footnote" else "Quellenhinweis",
                    ),
                    "chunk_type": chunk_type,
                    "unit_id": note_unit_id,
                    "document_global_key": document_global_key,
                    "parent_chunk_id": None,
                    "child_chunk_ids": [],
                    "label": (
                        footnote.get("visible_marker")
                        or footnote.get("marker")
                        or footnote["footnote_id"]
                    ),
                    "number": footnote_index,
                    "sequence": sequence,
                    "source_order": [norm_index, sequence],
                    "page_id": None,
                    "page_range": None,
                    "text": footnote["text"],
                    "text_sha256": sha256_str(footnote["text"]),
                    "confidence": 1.0,
                    "review_status": "pending",
                    "source_xml_footnote_id": footnote["footnote_id"],
                    "source_xml_footnote_attributes": footnote.get("attributes") or {},
                    "source_xml_footnote_marker": footnote.get("visible_marker"),
                    "preformatted_blocks": footnote.get("preformatted_blocks") or [],
                }
            )

    if not structural_units:
        issues.append(
            _issue(
                doc_id,
                "no_structural_units",
                "error",
                "GII XML contains no content-bearing legal units.",
                package["xml_name"],
            )
        )

    status_notes = []
    for stand in metadata_norm.findall("./metadaten/standangabe"):
        note_type = _child_text(stand, "./standtyp")
        comment = _child_text(stand, "./standkommentar")
        if note_type or comment:
            status_notes.append({"type": note_type, "comment": comment})
    fundstellen = []
    for source in metadata_norm.findall("./metadaten/fundstelle"):
        fundstellen.append(
            {
                "type": source.get("typ"),
                "periodical": _child_text(source, "./periodikum"),
                "citation": _child_text(source, "./zitstelle"),
            }
        )
    full_citation = title
    if fundstellen:
        citation_text = ", ".join(
            " ".join(
                part
                for part in (item.get("periodical"), item.get("citation"))
                if part
            )
            for item in fundstellen
        )
        if citation_text:
            full_citation = "{} ({})".format(title, citation_text)

    doc = {
        "document_id": doc_id,
        "source_pdf": source_pdf,
        "source_xml": package["xml_name"],
        "source_zip": (
            package["package_path"]
            if package["package_kind"] == "zip"
            else None
        ),
        "sha256": package["xml_sha256"],
        "title": title,
        "full_citation": full_citation,
        "canonical_citation": abbreviation,
        "date_enacted": root_meta.get("date_enacted") or None,
        "document_key": document_key,
        "document_global_key": document_global_key,
        "global_key": document_global_key,
        "citation_prefix": abbreviation,
        "abbreviation": abbreviation,
        "pages": [],
        "page_refs": [],
        "structural_units": structural_units,
        "chunks": chunks,
        "metadata": {
            "source_format": "gii_xml",
            "extractor": "gii_xml",
            "short_title": short_title,
            "official_abbreviation": root_meta.get("official_abbreviation") or None,
            "jurabk": root_meta.get("jurabk") or [],
            "gii_document_number": document_number,
            "gii_build_date": root.get("builddate"),
            "source_xml_sha256": package["xml_sha256"],
            "source_xml_name": package["xml_name"],
            "source_package_path": package["package_path"],
            "source_package_kind": package["package_kind"],
            "source_package_sha256": package["package_sha256"],
            "source_package_bytes": package["package_bytes"],
            "source_package_members": package["assets"],
            "source_assets": source_asset_refs,
            "source_asset_count": len(source_asset_refs),
            "missing_source_asset_count": len(missing_assets),
            "status_notes": status_notes,
            "official_sources": fundstellen,
            "xml_norm_count": len(root.findall("./norm")),
            "xml_unit_count": len(structural_units),
            "xml_chunk_count": len(chunks),
            "pdf_alignment_status": "not_attempted",
            "pdf_pages": 0,
        },
    }
    return doc, issues


def extract_package(
    source_path: os.PathLike[str] | str,
    source_manifest_entry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the standard top-level normtext payload for one XML package."""
    document, issues = extract_document_from_xml(source_path, source_manifest_entry)
    return {
        "schema_version": "1.0.0-draft",
        "phase": "normtext",
        "source_manifest_entry": source_manifest_entry,
        "documents": [document],
        "review_decisions": [],
        "extraction_issues": issues,
    }
