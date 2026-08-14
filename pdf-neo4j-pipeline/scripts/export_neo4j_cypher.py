#!/usr/bin/env python3
"""Convert content graph JSON into an importable Neo4j Cypher script.

Input is produced by `extract_content_nodes.py`. The output can be executed
with Neo4j Browser or `cypher-shell`.
"""

import argparse
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COMMON_NODE_LABEL = "GraphNode"

COMMON_CONTENT_PROPERTIES = {
    "global_key",
    "display_name",
    "legal_citation",
}

NEO4J_NODE_PROPERTY_ALLOWLIST = {
    "Document": {
        "document_key",
        "document_global_key",
        "global_key",
        "title",
        "short_title",
        "full_title",
        "full_citation",
        "canonical_citation",
        "citation_prefix",
        "abbreviation",
        "source_pdf",
        "sha256",
        "page_count",
        "date_enacted",
        "source_format",
        "extractor",
        "source_xml",
        "source_zip",
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
    },
    "StructuralUnit": COMMON_CONTENT_PROPERTIES | {
        "unit_type",
        "document_key",
        "document_global_key",
        "label",
        "number",
        "title",
        "page_start",
        "page_end",
        "text_sha256",
        "text_char_count",
        "text_preview",
        "column_header_text",
        "row_count",
        "note_count",
        "table_sections",
    },
    "Chunk": COMMON_CONTENT_PROPERTIES | {
        "chunk_type",
        "unit_id",
        "document_global_key",
        "label",
        "number",
        "sequence",
        "page_start",
        "page_end",
        "text",
        "text_sha256",
        "table_section",
        "column_header_text",
        "table_title",
        "table_part_index",
        "table_part_count",
        "table_chunk_token_count",
        "table_chunk_token_encoding",
        "table_chunk_max_tokens",
        "oversized_atomic_row",
        "row_start",
        "row_end",
        "row_count_in_chunk",
        "rows_json",
    },
    "ReferenceTarget": {
        "global_key",
        "status",
        "target_document_key",
        "requested_target_global_key",
        "resolved_target_global_key",
        "target_level",
        "reference_kind",
        "display_name",
        "target_title_key",
        "nearest_resolved_target_id",
    },
}


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def cypher_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def cypher_identifier(value: str) -> str:
    escaped = value.replace("`", "``")
    return "`{}`".format(escaped)


def cypher_key(value: str) -> str:
    if IDENT_RE.match(value):
        return value
    return cypher_identifier(value)


def cypher_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return cypher_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(cypher_value(item) for item in value) + "]"
    if isinstance(value, dict):
        items = []
        for key in sorted(value):
            items.append("{}: {}".format(cypher_key(str(key)), cypher_value(value[key])))
        return "{" + ", ".join(items) + "}"
    return cypher_string(str(value))


def cypher_map(mapping: Dict[str, Any]) -> str:
    return cypher_value(mapping)


def chunks(items: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def label_clause(labels: Iterable[str]) -> str:
    label_set = [label for label in labels if label != "ContentNode"]
    return "".join(":{}".format(cypher_identifier(label)) for label in label_set)


def node_group_for_labels(labels: Iterable[str]) -> str:
    label_set = set(labels)
    if label_set & {"Document", "StructuralUnit", "Chunk"}:
        return "content_node"
    if "ReferenceTarget" in label_set:
        return "reference_target"
    return "graph_node"


def primary_label(labels: Iterable[str]) -> str:
    label_set = set(labels)
    for label in ("Document", "StructuralUnit", "Chunk", "ReferenceTarget"):
        if label in label_set:
            return label
    return next(iter(labels), "")


def filter_node_properties(labels: Iterable[str], props: Dict[str, Any]) -> Dict[str, Any]:
    label = primary_label(labels)
    allowed = NEO4J_NODE_PROPERTY_ALLOWLIST.get(label)
    if allowed is None:
        return dict(props)
    return {key: value for key, value in props.items() if key in allowed}


def constraint_name_for_label(label: str) -> str:
    return "{}_graph_id".format(slugify_identifier(label))


def slugify_identifier(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value or "")
    value = value.strip("_").lower()
    if not value or value[0].isdigit():
        value = "label_{}".format(value)
    return value


def emit_node_batch(lines: List[str], labels: Tuple[str, ...], rows: List[Dict[str, Any]]) -> None:
    safe_rows = [
        {
            "id": row["id"],
            "properties": {
                **filter_node_properties(labels, row.get("properties") or {}),
                "node_group": node_group_for_labels(labels),
                "is_content_node": node_group_for_labels(labels) == "content_node",
            },
        }
        for row in rows
    ]
    lines.append("UNWIND {} AS row".format(cypher_value(safe_rows)))
    lines.append("CREATE (n{} {{graph_id: row.id}})".format(label_clause(labels)))
    lines.append("SET n += row.properties, n.graph_id = row.id;")
    lines.append("")


def emit_relationship_batch(lines: List[str], rel_type: str, rows: List[Dict[str, Any]]) -> None:
    safe_rows = [
        {
            "id": row["id"],
            "start_node_id": row["start_node_id"],
            "end_node_id": row["end_node_id"],
            "properties": row.get("properties") or {},
        }
        for row in rows
    ]
    lines.append("UNWIND {} AS row".format(cypher_value(safe_rows)))
    lines.append("MATCH (start:{} {{graph_id: row.start_node_id}})".format(cypher_identifier(COMMON_NODE_LABEL)))
    lines.append("MATCH (end:{} {{graph_id: row.end_node_id}})".format(cypher_identifier(COMMON_NODE_LABEL)))
    lines.append("CREATE (start)-[r:{}]->(end)".format(cypher_identifier(rel_type)))
    lines.append("SET r = row.properties, r.rel_id = row.id;")
    lines.append("")


def build_cypher(graph: Dict[str, Any], batch_size: int) -> str:
    lines: List[str] = []
    counts = graph.get("counts") or {}
    lines.append("// Generated Neo4j import for content graph")
    lines.append("// Source raw JSON: {}".format(graph.get("source_raw_json", "")))
    lines.append("// Nodes: {}; Relationships: {}".format(
        counts.get("nodes", len(graph.get("nodes") or [])),
        counts.get("relationships", len(graph.get("relationships") or [])),
    ))
    lines.append("")
    nodes_by_labels: Dict[Tuple[str, ...], List[Dict[str, Any]]] = defaultdict(list)
    for node in graph.get("nodes") or []:
        labels = tuple(
            sorted(
                {COMMON_NODE_LABEL}
                | {label for label in (node.get("labels") or []) if label != "ContentNode"}
            )
        )
        nodes_by_labels[labels].append(node)

    constraint_labels = [COMMON_NODE_LABEL]
    for label in constraint_labels:
        lines.append("CREATE CONSTRAINT {} IF NOT EXISTS".format(cypher_identifier(constraint_name_for_label(label))))
        lines.append("FOR (n:{}) REQUIRE n.graph_id IS UNIQUE;".format(cypher_identifier(label)))
        lines.append("")

    for labels in sorted(nodes_by_labels):
        for batch in chunks(nodes_by_labels[labels], batch_size):
            emit_node_batch(lines, labels, batch)

    relationships_by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rel in graph.get("relationships") or []:
        relationships_by_type[rel["type"]].append(rel)

    for rel_type in sorted(relationships_by_type):
        for batch in chunks(relationships_by_type[rel_type], batch_size):
            emit_relationship_batch(lines, rel_type, batch)

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Content graph JSON")
    parser.add_argument("--output", required=True, help="Cypher output file")
    parser.add_argument("--batch-size", type=int, default=100, help="Rows per UNWIND batch")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    graph = load_json(args.input)
    cypher = build_cypher(graph, args.batch_size)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(cypher)
        fh.write("\n")
    print("Wrote Cypher import script to {}".format(args.output))


if __name__ == "__main__":
    main()
