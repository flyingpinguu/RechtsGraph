#!/usr/bin/env python3
"""Export a content graph JSON to Neo4j LOAD CSV files and import Cypher."""

import argparse
import csv
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Tuple

from export_neo4j_cypher import (
    COMMON_NODE_LABEL,
    cypher_identifier,
    filter_node_properties,
    node_group_for_labels,
)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value or "").strip("_").lower()
    return value or "item"


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def scalar_properties(props: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in props.items()
        if not key.endswith("_json")
        and (value is None or isinstance(value, (str, int, float, bool)))
    }


def write_csv(path: str, rows: List[Dict[str, Any]], preferred: Iterable[str]) -> List[str]:
    keys = list(preferred)
    seen = set(keys)
    for row in rows:
        for key in sorted(row):
            if key not in seen:
                keys.append(key)
                seen.add(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in keys})
    return keys


def labels_for_node(node: Dict[str, Any]) -> Tuple[str, ...]:
    labels = {COMMON_NODE_LABEL}
    labels.update(label for label in (node.get("labels") or []) if label != "ContentNode")
    return tuple(sorted(labels))


def build_node_rows(nodes: List[Dict[str, Any]]) -> Dict[Tuple[str, ...], List[Dict[str, Any]]]:
    grouped: Dict[Tuple[str, ...], List[Dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        labels = labels_for_node(node)
        props = {
            **filter_node_properties(labels, node.get("properties") or {}),
            "node_group": node_group_for_labels(labels),
            "is_content_node": node_group_for_labels(labels) == "content_node",
        }
        row = {"graph_id": node["id"]}
        row.update(scalar_properties(props))
        grouped[labels].append(row)
    return grouped


def build_relationship_rows(relationships: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rel in relationships:
        row = {
            "rel_id": rel["id"],
            "start_node_id": rel["start_node_id"],
            "end_node_id": rel["end_node_id"],
        }
        row.update(scalar_properties(rel.get("properties") or {}))
        grouped[rel["type"]].append(row)
    return grouped


def node_file_name(index: int, labels: Tuple[str, ...]) -> str:
    label_part = "_".join(slugify(label) for label in labels if label != COMMON_NODE_LABEL)
    return "nodes_{:03d}_{}.csv".format(index, label_part or "graphnode")


def relationship_file_name(rel_type: str) -> str:
    return "relationships_{}.csv".format(slugify(rel_type))


def write_import_cypher(
    path: str,
    csv_uri_prefix: str,
    node_files: List[Tuple[Tuple[str, ...], str]],
    relationship_files: List[Tuple[str, str]],
    batch_size: int,
) -> None:
    lines: List[str] = []
    lines.append("CREATE CONSTRAINT graphnode_graph_id IF NOT EXISTS")
    lines.append("FOR (n:{}) REQUIRE n.graph_id IS UNIQUE;".format(cypher_identifier(COMMON_NODE_LABEL)))
    lines.append("")

    for labels, file_name in node_files:
        label_clause = "".join(":{}".format(cypher_identifier(label)) for label in labels)
        lines.append("LOAD CSV WITH HEADERS FROM '{}' AS row".format(csv_uri_prefix + file_name))
        lines.append("CALL (row) {")
        lines.append("  CREATE (n{} {{graph_id: row.graph_id}})".format(label_clause))
        lines.append("  SET n += row")
        lines.append("}} IN TRANSACTIONS OF {} ROWS;".format(batch_size))
        lines.append("")

    for rel_type, file_name in relationship_files:
        lines.append("LOAD CSV WITH HEADERS FROM '{}' AS row".format(csv_uri_prefix + file_name))
        lines.append("CALL (row) {")
        lines.append("  MATCH (start:{} {{graph_id: row.start_node_id}})".format(cypher_identifier(COMMON_NODE_LABEL)))
        lines.append("  MATCH (end:{} {{graph_id: row.end_node_id}})".format(cypher_identifier(COMMON_NODE_LABEL)))
        lines.append("  CREATE (start)-[r:{}]->(end)".format(cypher_identifier(rel_type)))
        lines.append("  SET r += row, r.rel_id = row.rel_id")
        lines.append("  REMOVE r.start_node_id, r.end_node_id")
        lines.append("}} IN TRANSACTIONS OF {} ROWS;".format(batch_size))
        lines.append("")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Content graph JSON")
    parser.add_argument("--csv-dir", required=True, help="Directory under Neo4j import dir for CSV files")
    parser.add_argument("--output-cypher", required=True, help="Generated LOAD CSV Cypher script")
    parser.add_argument("--csv-uri-prefix", required=True, help="Neo4j file URI prefix, e.g. file:///gii_full_csv/")
    parser.add_argument("--batch-size", type=int, default=5000, help="LOAD CSV transaction batch size")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    graph = load_json(args.input)
    os.makedirs(args.csv_dir, exist_ok=True)

    node_files: List[Tuple[Tuple[str, ...], str]] = []
    for index, (labels, rows) in enumerate(sorted(build_node_rows(graph.get("nodes") or []).items())):
        file_name = node_file_name(index, labels)
        write_csv(os.path.join(args.csv_dir, file_name), rows, ["graph_id"])
        node_files.append((labels, file_name))

    relationship_files: List[Tuple[str, str]] = []
    for rel_type, rows in sorted(build_relationship_rows(graph.get("relationships") or []).items()):
        file_name = relationship_file_name(rel_type)
        write_csv(os.path.join(args.csv_dir, file_name), rows, ["rel_id", "start_node_id", "end_node_id"])
        relationship_files.append((rel_type, file_name))

    write_import_cypher(
        args.output_cypher,
        args.csv_uri_prefix,
        node_files,
        relationship_files,
        args.batch_size,
    )
    print(
        "Wrote {} node CSV groups, {} relationship CSV groups, and {}".format(
            len(node_files),
            len(relationship_files),
            args.output_cypher,
        )
    )


if __name__ == "__main__":
    main()
