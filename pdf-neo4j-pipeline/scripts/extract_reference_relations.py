#!/usr/bin/env python3
"""Extract deterministic legal reference relations from a content graph JSON.

This pass reads the graph JSON produced by `extract_content_nodes.py` and adds
non-hierarchical reference relations:

- Internal references such as "§§ 3 und 4" or "Anlage 2 Tabelle 1a"
- External long-name references such as "§ 69 Absatz 1 Nummer 8 des
  Kreislaufwirtschaftsgesetzes"
- External abbreviation references such as "§ 21 ElektroG"

The extractor is deliberately conservative. It only handles explicit textual
references and creates placeholder ReferenceTarget nodes when a target is not
present in the current content graph.
"""

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


SCHEMA_VERSION = "0.1.0"
HSPACE = r"[^\S\r\n]"
QUALIFIER_PATTERN = r"(?:Abs\.|Absatz|Satz|Nummern?|Nr\.|Buchstabe|Buchst\.)"
PARA_BODY_PATTERN = (
    r"\d+[a-z]?"
    rf"(?:\s*(?:{QUALIFIER_PATTERN}\s*[A-Za-z0-9]+|"
    rf"(?:und|oder|sowie|,|-|bis)\s*(?:{QUALIFIER_PATTERN}\s*[A-Za-z0-9]+|\d+[a-z]?))){{0,30}}"
)
LAW_NAME_PATTERN = r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9\-–\s]{3,100}?"

PARA_EXTERNAL_LONG_RE = re.compile(
    rf"(?P<mention>§{{1,2}}\s*(?P<body>{PARA_BODY_PATTERN})"
    rf"(?:\s+(?P<article>des|der))?\s+(?P<law>{LAW_NAME_PATTERN}"
    r"(?:gesetzes|verordnung|ordnung|gesetzbuches)))"
)
ARTICLE_EXTERNAL_LONG_RE = re.compile(
    rf"(?P<mention>Artikel\s+(?P<body>{PARA_BODY_PATTERN})"
    rf"\s+(?P<article>des|der)\s+(?P<law>{LAW_NAME_PATTERN}"
    r"(?:gesetzes|verordnung|ordnung|gesetzbuches)))"
)
PARA_EXTERNAL_ABBREV_RE = re.compile(
    rf"(?P<mention>§{{1,2}}\s*(?P<body>{PARA_BODY_PATTERN})"
    r"\s+(?P<abbr>[A-ZÄÖÜ][A-Za-zÄÖÜa-zäöüß0-9]{1,20}(?:G|V|GB|BGB|StGB|VZO))\b)"
)
INTERNAL_PARA_RE = re.compile(
    rf"(?P<mention>§{{1,2}}\s*(?P<body>{PARA_BODY_PATTERN}))"
)
CONTEXTUAL_SUBSECTION_RE = re.compile(
    r"(?P<mention>\b(?:Abs\.|Absatz)\s+(?P<num>\d+[a-z]?)\b)"
)
INTERNAL_ANNEX_RE = re.compile(
    rf"(?P<mention>\b(?P<annex_kind>Anlagen?|Anhang|Anhänge|Anhaenge)\s+(?P<body>\d+[a-z]?"
    rf"(?:{HSPACE}*(?:und|oder|,|-|bis){HSPACE}*\d+[a-z]?)*"
    rf")"
    rf"(?:\s+Tabelle\s+(?P<table_body>\d+[a-z]?"
    rf"(?:{HSPACE}*(?:und|oder|,|-|bis){HSPACE}*(?:Tabelle{HSPACE}+)?\d+[a-z]?)*))?)"
)

QUALIFIER_RE = re.compile(
    r"\b(?P<kind>Abs\.|Absatz\b|Satz\b|Nummern?\b|Nr\.|Buchstabe\b|Buchst\.)\s*(?P<num>[A-Za-z0-9]+)"
)
ABS_QUALIFIER_RE = re.compile(r"\b(?:Abs\.|Absatz)\s*(?P<num>\d+[a-z]?)\b")
LOWER_THAN_ABS_RE = re.compile(r"\b(?:Satz|Nummern?|Nr\.|Buchstabe|Buchst\.)\b")
CONNECTED_NUM_RE = re.compile(
    r"\b(?P<connector>und|oder|sowie|bis)\s*(?:(?:Abs\.|Absatz)\s*)?(?P<num>\d+[a-z]?)\b|"
    r"(?P<comma>,|-)\s*(?:(?:Abs\.|Absatz)\s*)?(?P<comma_num>\d+[a-z]?)\b"
)
MULTI_NUM_RE = re.compile(r"\d+[a-z]?")

LAW_GENITIVE_ENDINGS = (
    ("gesetzbuches", "gesetzbuch"),
    ("gesetzes", "gesetz"),
    ("verordnung", "verordnung"),
    ("ordnung", "ordnung"),
)


def slugify(value: str) -> str:
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


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return "{}_{}".format(prefix, hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])


def normalize_law_name(law_name: str) -> str:
    cleaned = re.sub(r"\s+", " ", law_name or "").strip()
    lower = cleaned.lower()
    for suffix, replacement in LAW_GENITIVE_ENDINGS:
        if lower.endswith(suffix):
            cleaned = cleaned[: -len(suffix)] + replacement
            break
    return slugify(cleaned)


def normalize_abbreviation(abbr: str) -> str:
    return slugify(abbr)


def clean_citation_text(citation: Optional[str]) -> str:
    cleaned = re.sub(r"\s+", " ", citation or "").strip()
    return cleaned.strip('"').strip()


def title_aliases_from_citation(citation: Optional[str]) -> Set[str]:
    """Return conservative document title aliases from a full legal citation."""
    cleaned = clean_citation_text(citation)
    aliases: Set[str] = set()
    if not cleaned:
        return aliases
    aliases.add(cleaned)
    cut_patterns = (
        r"\s+in\s+der\s+Fassung\b",
        r"\s+in\s+der\s+im\s+Bundesgesetzblatt\b",
        r",\s+d(?:as|ie|er)\s+zuletzt\b",
        r",\s+zuletzt\b",
        r"\s+vom\s+\d{1,2}\.\s+[A-ZÄÖÜa-zäöüß]+\s+\d{4}\b",
    )
    for pattern in cut_patterns:
        match = re.search(pattern, cleaned)
        if match and match.start() > 5:
            aliases.add(cleaned[: match.start()].strip())
    for match in re.finditer(r"\(([^()]{4,120})\)", cleaned):
        inside = match.group(1).strip()
        if " - " in inside or " – " in inside:
            short_title = re.split(r"\s+[–-]\s+", inside, 1)[0].strip()
            if len(short_title) > 3:
                aliases.add(short_title)
    return aliases


def source_pdf_aliases(source_pdf: Optional[str]) -> Set[str]:
    stem = re.sub(r"\.pdf$", "", source_pdf or "", flags=re.IGNORECASE).strip()
    aliases = {stem} if stem else set()
    stripped = re.sub(r"^\d+[_-]", "", stem)
    if stripped and stripped != stem:
        aliases.add(stripped)
    return aliases


def source_xml_aliases(
    source_xml: Optional[str],
    source_zip: Optional[str] = None,
) -> Set[str]:
    """Return document-like names from XML members and their package paths."""
    aliases: Set[str] = set()
    generic_names = {"xml", "xml.zip", "download", "download.zip"}
    for source in (source_xml, source_zip):
        normalized = re.sub(r"[?#].*$", "", source or "").replace("\\", "/").strip("/")
        if not normalized:
            continue
        parts = [part for part in normalized.split("/") if part]
        candidates = [parts[-1]]
        if len(parts) > 1 and parts[-1].lower() in generic_names:
            candidates.append(parts[-2])
        for candidate in candidates:
            stem = candidate.strip()
            previous = None
            while stem and stem != previous:
                previous = stem
                stem = re.sub(r"\.(?:xml|zip)$", "", stem, flags=re.IGNORECASE)
            if not stem or stem.lower() in {"xml", "download"}:
                continue
            aliases.add(stem)
            stripped = re.sub(r"^\d+[_-]", "", stem)
            if stripped and stripped != stem:
                aliases.add(stripped)
    return aliases


def document_part_from_target_key(global_key: str) -> str:
    for marker in ("_para_", "_art_", "_anlage_", "_anhang_"):
        if marker in global_key:
            return global_key.split(marker, 1)[0]
    return global_key


def token_for_qualifier(kind: str) -> str:
    normalized = kind.replace(".", "").lower()
    if normalized == "abs" or normalized == "absatz":
        return "abs"
    if normalized == "satz":
        return "satz"
    if normalized in ("nummer", "nummern", "nr"):
        return "nr"
    if normalized == "buchstabe" or normalized == "buchst":
        return "buchst"
    return slugify(normalized)


def parse_single_para_body(body: str) -> Tuple[str, List[Tuple[str, str]]]:
    body = re.sub(r"\s+", " ", body or "").strip()
    number_match = re.match(r"(?P<num>\d+[a-z]?)", body)
    if not number_match:
        return "", []
    qualifiers = [
        (token_for_qualifier(match.group("kind")), match.group("num"))
        for match in QUALIFIER_RE.finditer(body)
    ]
    return number_match.group("num"), qualifiers


def split_para_body(body: str) -> List[str]:
    """Return referenced paragraph numbers from a simple §/§§ body."""
    body = re.sub(r"\s+", " ", body or "").strip()
    if QUALIFIER_RE.search(body):
        number, _qualifiers = parse_single_para_body(body)
        return [number] if number else []
    head = re.split(r"\b(?:Abs\.|Absatz|Satz|Nummer|Nr\.|Buchstabe|Buchst\.)\b", body, 1)[0]
    numbers = MULTI_NUM_RE.findall(head)
    return numbers or MULTI_NUM_RE.findall(body[:8])


def split_table_body(body: Optional[str]) -> List[str]:
    if not body:
        return []
    cleaned = re.sub(r"\bTabelle\b", " ", body, flags=re.IGNORECASE)
    return MULTI_NUM_RE.findall(cleaned)


def annex_key_prefix(kind: str) -> str:
    normalized = slugify(kind)
    if normalized.startswith("anhang") or normalized.startswith("anhaenge"):
        return "anhang"
    return "anlage"


def document_key_from_unit_global_key(global_key: str) -> str:
    for marker in ("_para_", "_anlage_", "_anhang_", "_art_"):
        if marker in global_key:
            return global_key.split(marker, 1)[0]
    return global_key.split("_", 1)[0] if "_" in global_key else global_key


def article_context_from_unit_global_key(global_key: str) -> Optional[str]:
    match = re.match(r"^(?P<context>.+_art_[^_]+)_para_[^_]+(?:_|$)", global_key or "")
    return match.group("context") if match else None


def expand_number_range(start: str, end: str) -> List[str]:
    if not (start.isdigit() and end.isdigit()):
        return [end]
    left = int(start)
    right = int(end)
    if right <= left or right - left > 25:
        return [end]
    return [str(number) for number in range(left + 1, right + 1)]


def append_unique(values: List[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def subsection_numbers_for_body(body: str) -> List[str]:
    body = re.sub(r"\s+", " ", body or "").strip()
    matches = list(ABS_QUALIFIER_RE.finditer(body))
    numbers: List[str] = []
    for idx, match in enumerate(matches):
        current = match.group("num")
        append_unique(numbers, current)
        next_abs_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        lower_match = LOWER_THAN_ABS_RE.search(body, match.end(), next_abs_start)
        segment_end = lower_match.start() if lower_match else next_abs_start
        segment = body[match.end():segment_end]
        last_num = current
        for continuation in CONNECTED_NUM_RE.finditer(segment):
            connector = continuation.group("connector") or continuation.group("comma")
            number = continuation.group("num") or continuation.group("comma_num")
            if connector in ("bis", "-"):
                for expanded in expand_number_range(last_num, number):
                    append_unique(numbers, expanded)
                last_num = number
                continue
            append_unique(numbers, number)
            last_num = number
    return numbers


def global_key_for_para(document_key: str, para_num: str, qualifiers: List[Tuple[str, str]]) -> str:
    key = "{}_para_{}".format(document_key, slugify(para_num))
    for kind, num in qualifiers:
        key += "_{}_{}".format(kind, slugify(num))
    return key


def global_keys_for_para_body(document_key: str, prefix: str, body: str) -> List[str]:
    para_num, _qualifiers = parse_single_para_body(body)
    if not para_num:
        return []
    if prefix == "para":
        first_qualifier = QUALIFIER_RE.search(body)
        paragraph_numbers = (
            split_para_body(body[: first_qualifier.start()])
            if first_qualifier
            else split_para_body(body)
        )
        subsection_numbers = subsection_numbers_for_body(body)
        if subsection_numbers:
            target_para_num = paragraph_numbers[-1] if paragraph_numbers else para_num
            plain_para_numbers = paragraph_numbers[:-1]
            keys = [
                "{}_para_{}".format(document_key, slugify(number))
                for number in plain_para_numbers
            ]
            keys.extend(
                "{}_para_{}_abs_{}".format(document_key, slugify(target_para_num), slugify(number))
                for number in subsection_numbers
            )
            return keys
    if QUALIFIER_RE.search(body):
        return ["{}_{}_{}".format(document_key, prefix, slugify(para_num))]
    return [
        "{}_{}_{}".format(document_key, prefix, slugify(number))
        for number in split_para_body(body)
    ]


def target_level_from_global_key(global_key: str) -> str:
    if "_buchst_" in global_key:
        return "letter"
    if "_nr_" in global_key:
        return "number"
    if "_satz_" in global_key:
        return "sentence"
    if "_abs_" in global_key:
        return "subsection"
    if "_tabelle_" in global_key:
        return "table"
    if "_anlage_" in global_key or "_anhang_" in global_key:
        return "annex"
    if "_art_" in global_key:
        return "article"
    if "_para_" in global_key:
        return "paragraph"
    return "document"


def strip_to_nearest_keys(global_key: str) -> Iterable[str]:
    parts = global_key.split("_")
    cut_markers = ("buchst", "nr", "satz", "abs", "tabelle")
    while True:
        found = False
        for marker in cut_markers:
            if marker in parts:
                idx = len(parts) - 1 - parts[::-1].index(marker)
                if idx >= 0:
                    parts = parts[:idx]
                    found = True
                    yield "_".join(parts)
                    break
        if not found:
            break


def modeled_target_key(global_key: str) -> str:
    parts = global_key.split("_")
    for marker in ("buchst", "nr", "satz"):
        if marker in parts:
            return "_".join(parts[: parts.index(marker)])
    return global_key


def text_without_heading(chunk: Dict[str, Any]) -> Tuple[str, int]:
    """Remove a self heading line while preserving reference-rich annex headings."""
    props = chunk["properties"]
    text = props.get("text") or ""
    lines = text.splitlines()
    if not lines:
        return text, 0
    first = lines[0].strip()
    legal = props.get("legal_citation") or ""
    unit_id = props.get("unit_id") or ""
    if "Anlage" in first or "Anhang" in first:
        return text, 0
    if first.startswith("§") and legal.startswith(first.split()[0]):
        offset = len(lines[0]) + (1 if "\n" in text else 0)
        return "\n".join(lines[1:]), offset
    if unit_id and "_para_" in unit_id and first.startswith("§"):
        offset = len(lines[0]) + (1 if "\n" in text else 0)
        return "\n".join(lines[1:]), offset
    return text, 0


def relationship(rel_type: str, start_id: str, end_id: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    rel_id = stable_id(
        "rel",
        rel_type,
        start_id,
        end_id,
        properties.get("mention_text", ""),
        properties.get("char_start", ""),
        properties.get("target_global_key", ""),
    )
    return {
        "id": rel_id,
        "type": rel_type,
        "start_node_id": start_id,
        "end_node_id": end_id,
        "properties": properties,
    }


def reference_target_node(global_key: str, props: Dict[str, Any]) -> Dict[str, Any]:
    node_id = "ref_target_{}".format(slugify(global_key))
    properties = {
        "global_key": global_key,
        "status": props.get("resolution_status", "unresolved"),
        "target_document_key": props.get("target_document_key"),
        "target_level": props.get("target_level"),
        "reference_kind": props.get("reference_kind"),
        "display_name": props.get("normalized_reference") or global_key,
    }
    if props.get("target_title_key"):
        properties["target_title_key"] = props["target_title_key"]
    if props.get("nearest_resolved_target_id"):
        properties["nearest_resolved_target_id"] = props["nearest_resolved_target_id"]
    return {
        "id": node_id,
        "labels": ["ReferenceTarget"],
        "properties": {k: v for k, v in properties.items() if v is not None},
    }


class ReferenceExtractor:
    def __init__(self, graph: Dict[str, Any]):
        self.graph = graph
        self.nodes_by_id = {node["id"]: node for node in graph.get("nodes") or []}
        self.existing_node_ids = set(self.nodes_by_id)
        self.global_to_node_id: Dict[str, str] = {}
        self.document_by_id: Dict[str, Dict[str, Any]] = {}
        self.document_key_by_id: Dict[str, str] = {}
        self.document_alias_to_global_key: Dict[str, str] = {}
        self._document_alias_candidates: Dict[str, Set[Tuple[str, str]]] = {}
        self.unit_by_id: Dict[str, Dict[str, Any]] = {}
        self.chunk_by_id: Dict[str, Dict[str, Any]] = {}
        self.chunks_by_unit_id: Dict[str, List[Dict[str, Any]]] = {}
        self.reference_targets: Dict[str, Dict[str, Any]] = {}
        self.relationship_keys = {
            (rel["type"], rel["start_node_id"], rel["end_node_id"], rel["id"])
            for rel in graph.get("relationships") or []
        }
        self.seen_reference_keys = set()
        self._index_graph()

    def register_document_alias(self, alias: Optional[str], document_global_key: str, document_id: str) -> None:
        alias_key = slugify(alias or "")
        if not alias_key or not document_global_key:
            return
        self._document_alias_candidates.setdefault(alias_key, set()).add((document_global_key, document_id))

    def _index_graph(self) -> None:
        for node in self.graph.get("nodes") or []:
            props = node.get("properties") or {}
            labels = set(node.get("labels") or [])
            global_key = props.get("global_key")
            if global_key:
                self.global_to_node_id[global_key] = node["id"]
            if "Document" in labels:
                self.document_by_id[node["id"]] = node
                document_global_key = props.get("document_global_key") or props.get("global_key") or props.get("document_key")
                if props.get("document_key"):
                    self.document_key_by_id[node["id"]] = props["document_key"]
                    self.global_to_node_id[props["document_key"]] = node["id"]
                if document_global_key:
                    self.register_document_alias(document_global_key, document_global_key, node["id"])
                for alias_field in (
                    "document_key",
                    "document_global_key",
                    "global_key",
                    "canonical_citation",
                    "abbreviation",
                    "title",
                    "short_title",
                    "full_title",
                    "full_citation",
                    "source_pdf",
                    "source_xml",
                    "source_zip",
                ):
                    alias = props.get(alias_field)
                    if alias:
                        self.register_document_alias(alias, document_global_key, node["id"])
                for alias in source_pdf_aliases(props.get("source_pdf")):
                    self.register_document_alias(alias, document_global_key, node["id"])
                for alias in source_xml_aliases(
                    props.get("source_xml"),
                    props.get("source_zip"),
                ):
                    self.register_document_alias(alias, document_global_key, node["id"])
                for alias in title_aliases_from_citation(props.get("full_citation")):
                    self.register_document_alias(alias, document_global_key, node["id"])
            if "StructuralUnit" in labels:
                self.unit_by_id[node["id"]] = node
            if "Chunk" in labels:
                self.chunk_by_id[node["id"]] = node
                unit_id = props.get("unit_id")
                if unit_id:
                    self.chunks_by_unit_id.setdefault(unit_id, []).append(node)

        for unit_chunks in self.chunks_by_unit_id.values():
            unit_chunks.sort(
                key=lambda chunk: (
                    (chunk.get("properties") or {}).get("sequence") or 0,
                    chunk["id"],
                )
            )
        for alias_key, candidates in self._document_alias_candidates.items():
            if len(candidates) != 1:
                continue
            document_global_key, document_id = next(iter(candidates))
            self.document_alias_to_global_key[alias_key] = document_global_key
            self.global_to_node_id.setdefault(alias_key, document_id)

    def document_key_for_chunk(self, chunk: Dict[str, Any]) -> str:
        props = chunk.get("properties") or {}
        unit = self.unit_by_id.get(props.get("unit_id") or "")
        if unit:
            unit_props = unit.get("properties") or {}
            return (
                unit_props.get("document_global_key")
                or document_key_from_unit_global_key(unit_props.get("global_key", ""))
                or unit_props.get("document_key")
                or ""
            )
        global_key = props.get("global_key") or ""
        return global_key.split("_", 1)[0] if "_" in global_key else ""

    def document_aliases_for_chunk(self, chunk: Dict[str, Any]) -> set:
        aliases = set()
        props = chunk.get("properties") or {}
        unit = self.unit_by_id.get(props.get("unit_id") or "")
        if unit:
            unit_props = unit.get("properties") or {}
            if unit_props.get("document_key"):
                aliases.add(slugify(unit_props["document_key"]))
            if unit_props.get("document_global_key"):
                aliases.add(slugify(unit_props["document_global_key"]))
            document = self.document_by_id.get(unit_props.get("document_id") or "")
            if document:
                doc_props = document.get("properties") or {}
                for field in (
                    "document_key",
                    "document_global_key",
                    "global_key",
                    "canonical_citation",
                    "abbreviation",
                    "title",
                    "short_title",
                    "source_xml",
                    "source_zip",
                ):
                    if doc_props.get(field):
                        aliases.add(slugify(doc_props[field]))
                aliases.update(
                    slugify(alias)
                    for alias in source_xml_aliases(
                        doc_props.get("source_xml"),
                        doc_props.get("source_zip"),
                    )
                )
        return aliases

    def source_unit_id_for_chunk(self, chunk: Dict[str, Any]) -> str:
        return (chunk.get("properties") or {}).get("unit_id") or ""

    def source_document_id_for_chunk(self, chunk: Dict[str, Any]) -> Optional[str]:
        unit = self.unit_by_id.get(self.source_unit_id_for_chunk(chunk))
        if unit:
            return (unit.get("properties") or {}).get("document_id")
        return None

    def unit_id_for_node(self, node_id: Optional[str]) -> Optional[str]:
        if not node_id:
            return None
        if node_id in self.unit_by_id:
            return node_id
        chunk = self.chunk_by_id.get(node_id)
        if chunk:
            return (chunk.get("properties") or {}).get("unit_id")
        return None

    def document_id_for_unit(self, unit_id: Optional[str]) -> Optional[str]:
        if not unit_id:
            return None
        unit = self.unit_by_id.get(unit_id)
        if unit:
            return (unit.get("properties") or {}).get("document_id")
        return None

    def document_id_for_node(self, node_id: Optional[str]) -> Optional[str]:
        if not node_id:
            return None
        if node_id in self.document_by_id:
            return node_id
        return self.document_id_for_unit(self.unit_id_for_node(node_id))

    def document_id_for_key(self, key: Optional[str]) -> Optional[str]:
        if not key:
            return None
        node_id = self.global_to_node_id.get(key) or self.global_to_node_id.get(slugify(key))
        if node_id in self.document_by_id:
            return node_id
        return None

    def representative_chunk_id_for_unit(self, unit_id: str) -> Optional[str]:
        chunks = [
            chunk
            for chunk in self.chunks_by_unit_id.get(unit_id, [])
            if not (chunk.get("properties") or {}).get("parent_chunk_id")
        ]
        if not chunks:
            return None
        preferred_types = ("paragraph_text", "annex_text", "waste_code_entry", "table_rows")
        for preferred_type in preferred_types:
            for chunk in chunks:
                if (chunk.get("properties") or {}).get("chunk_type") == preferred_type:
                    return chunk["id"]
        return chunks[0]["id"]

    def target_chunk_ids_for_node(self, node_id: str) -> List[str]:
        if node_id in self.chunk_by_id:
            return [node_id]
        if node_id in self.unit_by_id:
            representative_id = self.representative_chunk_id_for_unit(node_id)
            return [representative_id] if representative_id else []
        if node_id in self.reference_targets or node_id.startswith("ref_target_"):
            return [node_id]
        return []

    def target_unit_id_for_reference(self, target_id: str, nearest_id: Optional[str]) -> Optional[str]:
        return self.unit_id_for_node(target_id) or self.unit_id_for_node(nearest_id)

    def document_reference_target_node(self, target_document_key: str, props: Dict[str, Any]) -> Dict[str, Any]:
        global_key = target_document_key
        node_id = "ref_target_{}".format(slugify(global_key))
        properties = {
            "global_key": global_key,
            "status": "unresolved",
            "target_document_key": target_document_key,
            "target_level": "document",
            "reference_kind": props.get("reference_kind"),
            "display_name": target_document_key,
        }
        if props.get("target_title_key"):
            properties["target_title_key"] = props["target_title_key"]
        return {
            "id": node_id,
            "labels": ["ReferenceTarget"],
            "properties": {k: v for k, v in properties.items() if v is not None},
        }

    def resolve_target(self, target_global_key: str) -> Tuple[str, str, Optional[str]]:
        candidate_keys = [target_global_key]
        nested_para = re.match(
            r"^(?P<document>.+)_art_[^_]+_para_(?P<tail>.+)$",
            target_global_key,
        )
        if nested_para:
            candidate_keys.append(
                "{}_para_{}".format(
                    nested_para.group("document"),
                    nested_para.group("tail"),
                )
            )
        document_part = document_part_from_target_key(target_global_key)
        mapped_document_key = self.document_alias_to_global_key.get(slugify(document_part))
        if mapped_document_key and mapped_document_key != document_part:
            if target_global_key == document_part:
                candidate_keys.append(mapped_document_key)
            elif target_global_key.startswith(document_part):
                candidate_keys.append(mapped_document_key + target_global_key[len(document_part):])
        seen_candidates = set()
        for candidate_key in candidate_keys:
            if candidate_key in seen_candidates:
                continue
            seen_candidates.add(candidate_key)
            exact = self.global_to_node_id.get(candidate_key)
            if exact:
                return exact, "resolved", candidate_key
            for nearest_key in strip_to_nearest_keys(candidate_key):
                nearest = self.global_to_node_id.get(nearest_key)
                if nearest:
                    return nearest, "resolved", nearest_key
        return "ref_target_{}".format(slugify(target_global_key)), "unresolved", None

    def add_reference(
        self,
        source_chunk: Dict[str, Any],
        mention_text: str,
        char_start: int,
        char_end: int,
        target_document_key: str,
        target_global_key: str,
        reference_kind: str,
        normalized_reference: str,
        target_title_key: Optional[str] = None,
    ) -> None:
        target_global_key = modeled_target_key(target_global_key)
        source_id = source_chunk["id"]
        source_unit_id = self.source_unit_id_for_chunk(source_chunk)
        target_id, status, resolved_target_global_key = self.resolve_target(target_global_key)
        effective_target_global_key = resolved_target_global_key or target_global_key

        # Skip pure self references introduced by paragraph/table headings.
        if target_id == source_id or target_id == source_unit_id:
            return

        ref_key = (source_id, effective_target_global_key, mention_text, char_start, char_end)
        if ref_key in self.seen_reference_keys:
            return
        self.seen_reference_keys.add(ref_key)

        props = {
            "reference_id": stable_id("ref", source_id, effective_target_global_key, char_start, char_end, mention_text),
            "source_chunk_id": source_id,
            "source_unit_id": source_unit_id,
            "source_document_id": self.source_document_id_for_chunk(source_chunk),
            "mention_text": mention_text,
            "normalized_reference": normalized_reference,
            "reference_kind": reference_kind,
            "target_document_key": target_document_key,
            "target_global_key": effective_target_global_key,
            "target_title_key": target_title_key,
            "target_level": target_level_from_global_key(effective_target_global_key),
            "resolution_status": status,
            "char_start": char_start,
            "char_end": char_end,
            "extraction_method": "deterministic_regex_v0.1.0",
        }
        props = {k: v for k, v in props.items() if v is not None}

        if target_id not in self.existing_node_ids:
            node = reference_target_node(effective_target_global_key, props)
            self.reference_targets[node["id"]] = node
            self.existing_node_ids.add(node["id"])

        self.add_refers_to_relationships(source_chunk, target_id, None, props)

    def add_relationship_once(self, rel_type: str, start_id: str, end_id: str, props: Dict[str, Any]) -> None:
        if start_id == end_id:
            return
        rel = relationship(rel_type, start_id, end_id, props)
        rel_key = (rel["type"], rel["start_node_id"], rel["end_node_id"], rel["id"])
        if rel_key not in self.relationship_keys:
            self.graph.setdefault("relationships", []).append(rel)
            self.relationship_keys.add(rel_key)

    def add_refers_to_relationships(
        self,
        source_chunk: Dict[str, Any],
        target_id: str,
        nearest_id: Optional[str],
        props: Dict[str, Any],
    ) -> None:
        source_chunk_id = source_chunk["id"]
        source_unit_id = self.source_unit_id_for_chunk(source_chunk)
        source_document_id = self.source_document_id_for_chunk(source_chunk)

        # Chunk level: prefer Chunk -> Chunk. When a target is only represented
        # as a broad StructuralUnit, link to one representative chunk; otherwise
        # references such as "Anlage 8" explode into dozens of parallel edges.
        # The exact broad target is still preserved by the unit-level rollup.
        chunk_targets = self.target_chunk_ids_for_node(target_id)
        for chunk_target_id in chunk_targets:
            if chunk_target_id == source_chunk_id:
                continue
            self.add_relationship_once("REFERS_TO", source_chunk_id, chunk_target_id, props)

        # Unit level: roll the observed chunk reference up to its source unit and
        # the nearest known target unit. For unknown external targets, keep the
        # exact ReferenceTarget as the target.
        target_unit_id = self.target_unit_id_for_reference(target_id, nearest_id)
        if source_unit_id:
            unit_target_id = target_unit_id
            if not unit_target_id and (target_id in self.reference_targets or target_id.startswith("ref_target_")):
                unit_target_id = target_id
            if unit_target_id and unit_target_id != source_unit_id:
                self.add_relationship_once("REFERS_TO", source_unit_id, unit_target_id, props)

        # Document level: connect documents when the target document is known.
        # For unknown external documents, create/use a document-level
        # ReferenceTarget keyed by the normalized title/abbreviation.
        target_document_id = (
            self.document_id_for_node(target_id)
            or self.document_id_for_node(nearest_id)
            or self.document_id_for_key(props.get("target_document_key"))
        )
        document_target_id = target_document_id
        if not document_target_id and props.get("target_document_key"):
            document_target_id = "ref_target_{}".format(slugify(props["target_document_key"]))
            if document_target_id not in self.existing_node_ids:
                node = self.document_reference_target_node(props["target_document_key"], props)
                self.reference_targets[node["id"]] = node
                self.existing_node_ids.add(node["id"])

        if source_document_id and document_target_id and source_document_id != document_target_id:
            self.add_relationship_once("REFERS_TO", source_document_id, document_target_id, props)

    def extract(self) -> Dict[str, Any]:
        for chunk in list(self.chunk_by_id.values()):
            self.extract_from_chunk(chunk)
        if self.reference_targets:
            self.graph.setdefault("nodes", []).extend(self.reference_targets.values())
            self.nodes_by_id.update(self.reference_targets)
        self.validate_reference_levels()
        self.update_counts()
        self.graph["reference_extraction"] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "method": "deterministic_regex_v0.1.0",
            "reference_target_nodes_added": len(self.reference_targets),
            "refers_to_relationships_added": len(
                [rel for rel in self.graph.get("relationships") or [] if rel.get("type") == "REFERS_TO"]
            ),
        }
        return self.graph

    def validate_reference_levels(self) -> None:
        allowed_targets = {
            "Chunk": {"Chunk", "ReferenceTarget"},
            "StructuralUnit": {"StructuralUnit", "ReferenceTarget"},
            "Document": {"Document", "ReferenceTarget"},
        }
        invalid = []
        for rel in self.graph.get("relationships") or []:
            if rel.get("type") != "REFERS_TO":
                continue
            start = self.nodes_by_id.get(rel.get("start_node_id")) or {}
            end = self.nodes_by_id.get(rel.get("end_node_id")) or {}
            start_labels = set(start.get("labels") or [])
            end_labels = set(end.get("labels") or [])
            start_level = next((label for label in allowed_targets if label in start_labels), None)
            end_level = next((label for label in {"Chunk", "StructuralUnit", "Document", "ReferenceTarget"} if label in end_labels), None)
            if not start_level or end_level not in allowed_targets[start_level]:
                invalid.append(
                    "{}:{} -> {}:{}".format(
                        rel.get("id"),
                        rel.get("start_node_id"),
                        rel.get("end_node_id"),
                        sorted(end_labels),
                    )
                )
        if invalid:
            preview = "; ".join(invalid[:10])
            raise ValueError("Invalid REFERS_TO level mapping: {}".format(preview))

    def extract_from_chunk(self, chunk: Dict[str, Any]) -> None:
        text, offset = text_without_heading(chunk)
        if not text:
            return
        occupied: List[Tuple[int, int]] = []
        occupied.extend(self.extract_external_long(chunk, text, offset))
        occupied.extend(self.extract_external_abbrev(chunk, text, offset, occupied))
        occupied.extend(self.extract_internal_annex(chunk, text, offset, occupied))
        occupied.extend(self.extract_internal_para(chunk, text, offset, occupied))
        self.extract_contextual_subsection(chunk, text, offset, occupied)

    def overlaps(self, start: int, end: int, spans: List[Tuple[int, int]]) -> bool:
        return any(start < span_end and end > span_start for span_start, span_end in spans)

    def extract_external_long(self, chunk: Dict[str, Any], text: str, offset: int) -> List[Tuple[int, int]]:
        spans = []
        for pattern, reference_kind, prefix in (
            (PARA_EXTERNAL_LONG_RE, "external_long_name", "para"),
            (ARTICLE_EXTERNAL_LONG_RE, "external_long_name", "art"),
        ):
            for match in pattern.finditer(text):
                law_key = normalize_law_name(match.group("law"))
                body = match.group("body")
                target_keys = global_keys_for_para_body(law_key, prefix, body)
                if not target_keys:
                    continue
                for target_key in target_keys:
                    self.add_reference(
                        chunk,
                        match.group("mention"),
                        offset + match.start(),
                        offset + match.end(),
                        law_key,
                        target_key,
                        reference_kind,
                        "{} {} {}".format(law_key, prefix, body),
                        target_title_key=law_key,
                    )
                spans.append((match.start(), match.end()))
        return spans

    def extract_external_abbrev(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans = []
        for match in PARA_EXTERNAL_ABBREV_RE.finditer(text):
            if self.overlaps(match.start(), match.end(), occupied):
                continue
            abbr_key = normalize_abbreviation(match.group("abbr"))
            if abbr_key in self.document_aliases_for_chunk(chunk):
                continue
            target_document_key = self.document_alias_to_global_key.get(abbr_key, abbr_key)
            body = match.group("body")
            target_keys = global_keys_for_para_body(target_document_key, "para", body)
            if not target_keys:
                continue
            for target_key in target_keys:
                self.add_reference(
                    chunk,
                    match.group("mention"),
                    offset + match.start(),
                    offset + match.end(),
                    target_document_key,
                    target_key,
                    "external_abbreviation",
                    "{} para {}".format(target_document_key, body),
                    target_title_key=None,
                )
            spans.append((match.start(), match.end()))
        return spans

    def extract_internal_annex(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans = []
        doc_key = self.document_key_for_chunk(chunk)
        for match in INTERNAL_ANNEX_RE.finditer(text):
            if self.overlaps(match.start(), match.end(), occupied):
                continue
            numbers = split_para_body(match.group("body"))
            annex_prefix = annex_key_prefix(match.group("annex_kind"))
            for number in numbers:
                table_numbers = split_table_body(match.group("table_body"))
                if table_numbers:
                    for table_number in table_numbers:
                        target_key = "{}_{}_{}_tabelle_{}".format(
                            doc_key,
                            annex_prefix,
                            slugify(number),
                            slugify(table_number),
                        )
                        self.add_reference(
                            chunk,
                            match.group("mention"),
                            offset + match.start(),
                            offset + match.end(),
                            doc_key,
                            target_key,
                            "internal",
                            target_key,
                        )
                else:
                    target_key = "{}_{}_{}".format(doc_key, annex_prefix, slugify(number))
                    self.add_reference(
                        chunk,
                        match.group("mention"),
                        offset + match.start(),
                        offset + match.end(),
                        doc_key,
                        target_key,
                        "internal",
                        target_key,
                    )
            spans.append((match.start(), match.end()))
        return spans

    def extract_contextual_subsection(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans = []
        source_global = (chunk.get("properties") or {}).get("global_key") or ""
        para_match = re.search(r"^(?P<doc>.+?)_para_(?P<num>\d+[a-z]?)", source_global)
        if not para_match:
            return spans
        base_key = "{}_para_{}".format(para_match.group("doc"), para_match.group("num"))
        for match in CONTEXTUAL_SUBSECTION_RE.finditer(text):
            if self.overlaps(match.start(), match.end(), occupied):
                continue
            # Ignore the leading "(2)" style own subsection marker; this regex
            # catches textual "Absatz 1" references, not parenthesized labels.
            before = text[max(0, match.start() - 2):match.start()]
            if "(" in before:
                continue
            target_key = "{}_abs_{}".format(base_key, slugify(match.group("num")))
            self.add_reference(
                chunk,
                match.group("mention"),
                offset + match.start(),
                offset + match.end(),
                para_match.group("doc"),
                target_key,
                "internal",
                target_key,
            )
            spans.append((match.start(), match.end()))
        return spans

    def extract_internal_para(
        self,
        chunk: Dict[str, Any],
        text: str,
        offset: int,
        occupied: List[Tuple[int, int]],
    ) -> List[Tuple[int, int]]:
        spans = []
        doc_key = self.document_key_for_chunk(chunk)
        unit = self.unit_by_id.get(self.source_unit_id_for_chunk(chunk)) or {}
        unit_global_key = (unit.get("properties") or {}).get("global_key") or ""
        target_key_prefix = article_context_from_unit_global_key(unit_global_key) or doc_key
        for match in INTERNAL_PARA_RE.finditer(text):
            if self.overlaps(match.start(), match.end(), occupied):
                continue
            body = match.group("body")
            target_keys = global_keys_for_para_body(target_key_prefix, "para", body)
            if not target_keys:
                continue
            for target_key in target_keys:
                self.add_reference(
                    chunk,
                    match.group("mention"),
                    offset + match.start(),
                    offset + match.end(),
                    doc_key,
                    target_key,
                    "internal",
                    target_key,
                )
            spans.append((match.start(), match.end()))
        return spans

    def update_counts(self) -> None:
        counts_by_label: Dict[str, int] = {}
        for node in self.graph.get("nodes") or []:
            for label in node.get("labels") or []:
                counts_by_label[label] = counts_by_label.get(label, 0) + 1
        counts_by_type: Dict[str, int] = {}
        for rel in self.graph.get("relationships") or []:
            counts_by_type[rel["type"]] = counts_by_type.get(rel["type"], 0) + 1
        self.graph["counts"] = {
            "nodes": len(self.graph.get("nodes") or []),
            "relationships": len(self.graph.get("relationships") or []),
            "nodes_by_label": counts_by_label,
            "relationships_by_type": counts_by_type,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Content graph JSON")
    parser.add_argument("--output", required=True, help="Graph JSON with reference relations")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    graph = load_json(args.input)
    updated = ReferenceExtractor(copy.deepcopy(graph)).extract()
    write_json(args.output, updated)
    counts = updated.get("counts") or {}
    refs = counts.get("relationships_by_type", {}).get("REFERS_TO", 0)
    targets = counts.get("nodes_by_label", {}).get("ReferenceTarget", 0)
    print(
        "Wrote {} with {} REFERS_TO relationships and {} ReferenceTarget nodes".format(
            args.output,
            refs,
            targets,
        )
    )


if __name__ == "__main__":
    main()
