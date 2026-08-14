#!/usr/bin/env python3
"""Export deterministic content nodes and hierarchy relations from raw JSON.

The raw extraction JSON is optimized for review and audit. This script converts
it into a flatter graph payload that can be imported into Neo4j later:

- Document nodes
- StructuralUnit nodes
- Chunk nodes
- hierarchy and sequence relationships

PDF pages remain external evidence files and are represented only by page/range
properties on units and chunks.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional


SCHEMA_VERSION = "0.1.0"

XML_DOCUMENT_METADATA_FIELDS = (
    "source_format",
    "extractor",
    "official_abbreviation",
    "jurabk",
    "gii_document_number",
    "gii_build_date",
    "source_xml_sha256",
    "source_xml_name",
    "source_package_kind",
    "source_package_sha256",
    "pdf_alignment_status",
    "base_celex",
    "consolidated_celex",
    "consolidation_date",
    "descriptor",
    "descriptor_label",
    "legal_value",
    "legal_status",
    "in_force",
    "citation_aliases",
    "citation_alias_keys",
    "source_url",
    "source_cellar_uri",
)

XML_STRUCTURED_FIELDS = {
    "header_matrix",
    "preformatted_blocks",
    "structured_lists",
}


def stable_rel_id(rel_type: str, start_id: str, end_id: str, qualifier: str = "") -> str:
    raw = "|".join([rel_type, start_id, end_id, qualifier])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return "rel_{}".format(digest)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def page_start(obj: Dict[str, Any]) -> Optional[int]:
    page_range = obj.get("page_range") or {}
    return page_range.get("start")


def page_end(obj: Dict[str, Any]) -> Optional[int]:
    page_range = obj.get("page_range") or {}
    return page_range.get("end")


def compact_text(text: Optional[str], max_chars: int) -> Optional[str]:
    if text is None:
        return None
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + " ..."


def clean_value(value: Any) -> Any:
    """Return a Neo4j-friendly scalar/list value where possible.

    Neo4j properties can be scalar values or homogeneous-ish arrays of scalar
    values. Nested table rows therefore need to be stored separately as JSON.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list) and all(
        isinstance(item, (str, int, float, bool)) or item is None for item in value
    ):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def clean_citation_text(citation: Optional[str]) -> str:
    cleaned = re.sub(r"\s+", " ", citation or "").strip()
    return cleaned.strip('"').strip()


def full_title_from_citation(citation: Optional[str]) -> Optional[str]:
    cleaned = clean_citation_text(citation)
    if not cleaned:
        return None
    for pattern in (
        r"\s+in\s+der\s+Fassung\b",
        r"\s+in\s+der\s+im\s+Bundesgesetzblatt\b",
        r",\s+d(?:as|ie|er)\s+zuletzt\b",
        r",\s+zuletzt\b",
        r"\s+vom\s+\d{1,2}\.\s+[A-ZÄÖÜa-zäöüß]+\s+\d{4}\b",
    ):
        match = re.search(pattern, cleaned)
        if match and match.start() > 5:
            return cleaned[: match.start()].strip()
    return cleaned


def pick_properties(source: Dict[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for field in fields:
        if field in source and source.get(field) is not None:
            props[field] = clean_value(source.get(field))
    return props


def add_xml_provenance_props(props: Dict[str, Any], source: Dict[str, Any]) -> None:
    """Keep additive XML provenance without changing legacy PDF properties."""
    for field, value in source.items():
        if value is None:
            continue
        if (
            field == "source_order"
            or field.startswith("source_xml_")
            or field.startswith("source_asset")
            or field in XML_STRUCTURED_FIELDS
        ):
            props[field] = clean_value(value)


def add_page_range_props(props: Dict[str, Any], source: Dict[str, Any]) -> None:
    start = page_start(source)
    end = page_end(source)
    if start is not None:
        props["page_start"] = start
    if end is not None:
        props["page_end"] = end


def add_row_range_props(props: Dict[str, Any], source: Dict[str, Any]) -> None:
    row_range = source.get("row_range") or {}
    if row_range.get("start") is not None:
        props["row_start"] = row_range["start"]
    if row_range.get("end") is not None:
        props["row_end"] = row_range["end"]


def document_node(document: Dict[str, Any]) -> Dict[str, Any]:
    props = pick_properties(
        document,
        [
            "document_id",
            "document_key",
            "document_global_key",
            "global_key",
            "title",
            "canonical_citation",
            "citation_prefix",
            "abbreviation",
            "full_citation",
            "source_pdf",
            "source_xml",
            "source_zip",
            "sha256",
            "date_enacted",
        ],
    )
    full_title = full_title_from_citation(document.get("full_citation"))
    if full_title:
        props["full_title"] = full_title
    page_refs = document.get("page_refs") or document.get("pages") or []
    props["page_count"] = len(page_refs)
    if document.get("metadata"):
        metadata = document["metadata"]
        if metadata.get("short_title"):
            props["short_title"] = metadata["short_title"]
        for field in XML_DOCUMENT_METADATA_FIELDS:
            if metadata.get(field) is not None:
                props[field] = clean_value(metadata[field])
        if not props.get("citation_alias_keys") and metadata.get("citation_aliases"):
            alias_keys = {
                re.sub(r"[^a-z0-9]+", "_", str(alias).lower()).strip("_")
                for alias in metadata.get("citation_aliases") or []
                if alias
            }
            props["citation_alias_keys"] = "|{}|".format(
                "|".join(sorted(key for key in alias_keys if key))
            )
        props["metadata_json"] = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    return {
        "id": document["document_id"],
        "labels": ["Document"],
        "properties": props,
    }


def unit_node(unit: Dict[str, Any]) -> Dict[str, Any]:
    props = pick_properties(
        unit,
        [
            "unit_id",
            "global_key",
            "document_id",
            "document_key",
            "document_global_key",
            "unit_type",
            "legal_citation",
            "display_name",
            "label",
            "number",
            "title",
            "breadcrumbs",
            "sequence",
            "source_order",
            "parent_unit_id",
            "child_unit_ids",
            "text_sha256",
            "columns",
            "column_header_text",
            "row_count",
            "note_count",
            "table_sections",
            "parser_name",
            "confidence",
            "review_status",
            "is_uncertain",
            "uncertainty_reason",
        ],
    )
    add_xml_provenance_props(props, unit)
    add_page_range_props(props, unit)
    if "text" in unit and unit.get("text") is not None:
        props["text_char_count"] = len(unit.get("text") or "")
        props["text_preview"] = compact_text(unit.get("text"), 500)
    return {
        "id": unit["unit_id"],
        "labels": ["StructuralUnit", "StructuralUnit_{}".format(unit.get("unit_type", "unknown"))],
        "properties": props,
    }


def chunk_node(chunk: Dict[str, Any]) -> Dict[str, Any]:
    props = pick_properties(
        chunk,
        [
            "chunk_id",
            "global_key",
            "unit_id",
            "document_global_key",
            "chunk_type",
            "legal_citation",
            "display_name",
            "label",
            "number",
            "sequence",
            "parent_chunk_id",
            "child_chunk_ids",
            "page_id",
            "text",
            "text_sha256",
            "table_section",
            "columns",
            "column_header_text",
            "table_title",
            "table_part_index",
            "table_part_count",
            "table_chunk_token_count",
            "table_chunk_token_encoding",
            "table_chunk_max_tokens",
            "oversized_atomic_row",
            "parser_name",
            "confidence",
            "review_status",
        ],
    )
    add_xml_provenance_props(props, chunk)
    add_page_range_props(props, chunk)
    add_row_range_props(props, chunk)
    props["text_preview"] = compact_text(chunk.get("text"), 500)
    if "rows" in chunk:
        props["rows_json"] = json.dumps(chunk.get("rows"), ensure_ascii=False, sort_keys=True)
        props["row_count_in_chunk"] = len(chunk.get("rows") or [])
    return {
        "id": chunk["chunk_id"],
        "labels": ["Chunk", "Chunk_{}".format(chunk.get("chunk_type", "unknown"))],
        "properties": props,
    }


def relationship(
    rel_type: str,
    start_id: str,
    end_id: str,
    properties: Optional[Dict[str, Any]] = None,
    qualifier: str = "",
) -> Dict[str, Any]:
    return {
        "id": stable_rel_id(rel_type, start_id, end_id, qualifier),
        "type": rel_type,
        "start_node_id": start_id,
        "end_node_id": end_id,
        "properties": properties or {},
    }


def explicit_order(value: Any) -> tuple:
    """Return a total-order key for scalar or tuple/list source positions."""
    if isinstance(value, (list, tuple)):
        values = value
    elif value is None:
        values = ()
    else:
        values = (value,)
    normalized = []
    for item in values:
        if isinstance(item, bool):
            normalized.append((0, int(item)))
        elif isinstance(item, (int, float)):
            normalized.append((0, item))
        else:
            normalized.append((1, str(item)))
    return tuple(normalized) if normalized else ((2, ""),)


def source_position_key(item: Dict[str, Any]) -> tuple:
    start = page_start(item)
    if start is not None:
        return (
            0,
            start,
            page_end(item) if page_end(item) is not None else start,
            explicit_order(item.get("source_order")),
            explicit_order(item.get("sequence")),
        )
    return (
        1,
        0,
        0,
        explicit_order(item.get("source_order")),
        explicit_order(item.get("sequence")),
    )


def sort_units(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        units,
        key=lambda unit: (
            source_position_key(unit),
            str(unit.get("global_key") or unit.get("unit_id")),
        ),
    )


def sort_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        chunks,
        key=lambda chunk: (
            source_position_key(chunk),
            str(chunk.get("global_key") or chunk.get("chunk_id")),
        ),
    )


def add_next_relationships(
    relationships: List[Dict[str, Any]],
    items: List[Dict[str, Any]],
    id_field: str,
    rel_type: str,
) -> None:
    for idx, (left, right) in enumerate(zip(items, items[1:]), start=1):
        relationships.append(
            relationship(
                rel_type,
                left[id_field],
                right[id_field],
                {"sequence": idx},
                qualifier=str(idx),
            )
        )


def graph_scoped_id(raw_id: Optional[str], document_id: str) -> Optional[str]:
    if not raw_id:
        return raw_id
    if raw_id.startswith("doc_"):
        return raw_id
    suffix = "__{}".format(document_id)
    if raw_id.endswith(suffix):
        return raw_id
    return "{}{}".format(raw_id, suffix)


def graph_scoped_document(document: Dict[str, Any]) -> Dict[str, Any]:
    """Return a graph-export copy whose node ids are unique corpus-wide.

    Raw extraction IDs intentionally follow legal/global names so they are easy
    to inspect. In the full corpus, however, multiple PDFs can contain the same
    legal title and paragraph numbers. Neo4j node ids therefore need a document
    namespace while the legal `global_key` properties stay unchanged for
    reference resolution and citation display.
    """
    document_id = document["document_id"]
    unit_id_map = {
        unit.get("unit_id"): graph_scoped_id(unit.get("unit_id"), document_id)
        for unit in document.get("structural_units") or []
        if unit.get("unit_id")
    }
    chunk_id_map = {
        chunk.get("chunk_id"): graph_scoped_id(chunk.get("chunk_id"), document_id)
        for chunk in document.get("chunks") or []
        if chunk.get("chunk_id")
    }

    scoped = dict(document)
    scoped_units = []
    for unit in document.get("structural_units") or []:
        scoped_unit = dict(unit)
        raw_unit_id = unit.get("unit_id")
        scoped_unit["unit_id"] = unit_id_map.get(raw_unit_id, raw_unit_id)
        parent_unit_id = unit.get("parent_unit_id")
        if parent_unit_id:
            scoped_unit["parent_unit_id"] = unit_id_map.get(parent_unit_id, parent_unit_id)
        child_unit_ids = unit.get("child_unit_ids")
        if isinstance(child_unit_ids, list):
            scoped_unit["child_unit_ids"] = [
                unit_id_map.get(child_id, child_id)
                for child_id in child_unit_ids
            ]
        scoped_units.append(scoped_unit)

    scoped_chunks = []
    for chunk in document.get("chunks") or []:
        scoped_chunk = dict(chunk)
        raw_chunk_id = chunk.get("chunk_id")
        scoped_chunk["chunk_id"] = chunk_id_map.get(raw_chunk_id, raw_chunk_id)
        unit_id = chunk.get("unit_id")
        if unit_id:
            scoped_chunk["unit_id"] = unit_id_map.get(unit_id, unit_id)
        parent_chunk_id = chunk.get("parent_chunk_id")
        if parent_chunk_id:
            scoped_chunk["parent_chunk_id"] = chunk_id_map.get(parent_chunk_id, parent_chunk_id)
        child_chunk_ids = chunk.get("child_chunk_ids")
        if isinstance(child_chunk_ids, list):
            scoped_chunk["child_chunk_ids"] = [
                chunk_id_map.get(child_id, child_id)
                for child_id in child_chunk_ids
            ]
        scoped_chunks.append(scoped_chunk)

    scoped["structural_units"] = scoped_units
    scoped["chunks"] = scoped_chunks
    return scoped


def export_document(document: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    document = graph_scoped_document(document)
    nodes: List[Dict[str, Any]] = [document_node(document)]
    relationships: List[Dict[str, Any]] = []

    units = document.get("structural_units") or []
    chunks = document.get("chunks") or []

    unit_by_id = {unit["unit_id"]: unit for unit in units}
    chunk_by_id = {chunk["chunk_id"]: chunk for chunk in chunks}

    for unit in units:
        nodes.append(unit_node(unit))
        parent_unit_id = unit.get("parent_unit_id")
        if parent_unit_id:
            relationships.append(
                relationship(
                    "CONTAINS_UNIT",
                    parent_unit_id,
                    unit["unit_id"],
                    {"source": "parent_unit_id"},
                )
            )
        else:
            relationships.append(
                relationship(
                    "CONTAINS_UNIT",
                    document["document_id"],
                    unit["unit_id"],
                    {"source": "document_root"},
                )
            )

    for chunk in chunks:
        nodes.append(chunk_node(chunk))
        parent_chunk_id = chunk.get("parent_chunk_id")
        if parent_chunk_id:
            relationships.append(
                relationship(
                    "CONTAINS_CHUNK",
                    parent_chunk_id,
                    chunk["chunk_id"],
                    {"source": "parent_chunk_id"},
                )
            )
        else:
            relationships.append(
                relationship(
                    "CONTAINS_CHUNK",
                    chunk["unit_id"],
                    chunk["chunk_id"],
                    {"source": "unit_id"},
                )
            )

    root_units = [unit for unit in units if not unit.get("parent_unit_id")]
    add_next_relationships(relationships, sort_units(root_units), "unit_id", "NEXT_UNIT")

    children_by_unit: Dict[str, List[Dict[str, Any]]] = {}
    for unit in units:
        parent = unit.get("parent_unit_id")
        if parent:
            children_by_unit.setdefault(parent, []).append(unit)
    for child_units in children_by_unit.values():
        add_next_relationships(relationships, sort_units(child_units), "unit_id", "NEXT_UNIT")

    top_chunks_by_unit: Dict[str, List[Dict[str, Any]]] = {}
    child_chunks_by_chunk: Dict[str, List[Dict[str, Any]]] = {}
    for chunk in chunks:
        parent = chunk.get("parent_chunk_id")
        if parent:
            child_chunks_by_chunk.setdefault(parent, []).append(chunk)
        else:
            top_chunks_by_unit.setdefault(chunk["unit_id"], []).append(chunk)

    for unit_id, unit_chunks in top_chunks_by_unit.items():
        if unit_id in unit_by_id:
            add_next_relationships(relationships, sort_chunks(unit_chunks), "chunk_id", "NEXT_CHUNK")
    for parent_id, child_chunks in child_chunks_by_chunk.items():
        if parent_id in chunk_by_id:
            add_next_relationships(relationships, sort_chunks(child_chunks), "chunk_id", "NEXT_CHUNK")

    return {"nodes": nodes, "relationships": relationships}


def build_graph(raw: Dict[str, Any], source_path: str) -> Dict[str, Any]:
    all_nodes: List[Dict[str, Any]] = []
    all_relationships: List[Dict[str, Any]] = []

    for document in raw.get("documents") or []:
        exported = export_document(document)
        all_nodes.extend(exported["nodes"])
        all_relationships.extend(exported["relationships"])

    counts_by_label: Dict[str, int] = {}
    for node in all_nodes:
        for label in node["labels"]:
            counts_by_label[label] = counts_by_label.get(label, 0) + 1

    counts_by_type: Dict[str, int] = {}
    for rel in all_relationships:
        counts_by_type[rel["type"]] = counts_by_type.get(rel["type"], 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "phase": "content_nodes",
        "source_raw_json": source_path,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "counts": {
            "nodes": len(all_nodes),
            "relationships": len(all_relationships),
            "nodes_by_label": counts_by_label,
            "relationships_by_type": counts_by_type,
        },
        "nodes": all_nodes,
        "relationships": all_relationships,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Raw extraction JSON")
    parser.add_argument("--output", required=True, help="Output graph JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw = load_json(args.input)
    graph = build_graph(raw, args.input)
    write_json(args.output, graph)
    print(
        "Wrote {nodes} nodes and {relationships} relationships to {output}".format(
            nodes=graph["counts"]["nodes"],
            relationships=graph["counts"]["relationships"],
            output=args.output,
        )
    )


if __name__ == "__main__":
    main()
