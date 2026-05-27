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
from typing import Any, Dict, Iterable, List, Optional


SCHEMA_VERSION = "0.1.0"


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


def pick_properties(source: Dict[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for field in fields:
        if field in source and source.get(field) is not None:
            props[field] = clean_value(source.get(field))
    return props


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
            "title",
            "canonical_citation",
            "citation_prefix",
            "abbreviation",
            "full_citation",
            "source_pdf",
            "sha256",
            "date_enacted",
        ],
    )
    page_refs = document.get("page_refs") or document.get("pages") or []
    props["page_count"] = len(page_refs)
    if document.get("metadata"):
        props["metadata_json"] = json.dumps(document["metadata"], ensure_ascii=False, sort_keys=True)
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
            "unit_type",
            "legal_citation",
            "display_name",
            "label",
            "number",
            "title",
            "breadcrumbs",
            "parent_unit_id",
            "child_unit_ids",
            "text_sha256",
            "columns",
            "column_header_text",
            "row_count",
            "note_count",
            "table_sections",
            "confidence",
            "review_status",
            "is_uncertain",
            "uncertainty_reason",
        ],
    )
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
            "confidence",
            "review_status",
        ],
    )
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


def sort_units(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        units,
        key=lambda unit: (
            page_start(unit) if page_start(unit) is not None else 10**9,
            page_end(unit) if page_end(unit) is not None else 10**9,
            str(unit.get("global_key") or unit.get("unit_id")),
        ),
    )


def sort_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        chunks,
        key=lambda chunk: (
            page_start(chunk) if page_start(chunk) is not None else 10**9,
            int(chunk.get("sequence") or 0),
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


def export_document(document: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
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
