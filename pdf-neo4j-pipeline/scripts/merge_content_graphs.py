#!/usr/bin/env python3
"""Merge multiple content graph JSON files before reference extraction."""

import argparse
import datetime as dt
import json
import os
from typing import Any, Dict, Iterable, List, Tuple


SCHEMA_VERSION = "0.1.0"


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def dedupe_by_id(items: Iterable[Dict[str, Any]], item_type: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    conflicts: List[str] = []
    for item in items:
        item_id = item.get("id")
        if not item_id:
            conflicts.append("{} without id".format(item_type))
            continue
        existing = by_id.get(item_id)
        if existing is not None and existing != item:
            conflicts.append("conflicting {} id {}".format(item_type, item_id))
            continue
        by_id[item_id] = item
    return list(by_id.values()), conflicts


def sort_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        nodes,
        key=lambda node: (
            ",".join(node.get("labels") or []),
            (node.get("properties") or {}).get("document_global_key") or "",
            (node.get("properties") or {}).get("global_key") or "",
            node.get("id") or "",
        ),
    )


def sort_relationships(relationships: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        relationships,
        key=lambda rel: (
            rel.get("type") or "",
            rel.get("start_node_id") or "",
            rel.get("end_node_id") or "",
            rel.get("id") or "",
        ),
    )


def count_nodes_by_label(nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for node in nodes:
        for label in node.get("labels") or []:
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def count_relationships_by_type(relationships: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for rel in relationships:
        rel_type = rel.get("type") or "unknown"
        counts[rel_type] = counts.get(rel_type, 0) + 1
    return dict(sorted(counts.items()))


def document_keys(nodes: List[Dict[str, Any]]) -> List[str]:
    keys = []
    for node in nodes:
        if "Document" not in (node.get("labels") or []):
            continue
        props = node.get("properties") or {}
        keys.append(props.get("document_global_key") or props.get("document_key") or node.get("id"))
    return sorted(key for key in keys if key)


def merge_graphs(input_paths: List[str]) -> Dict[str, Any]:
    source_graphs = []
    all_nodes: List[Dict[str, Any]] = []
    all_relationships: List[Dict[str, Any]] = []
    conflicts: List[str] = []

    for path in input_paths:
        graph = load_json(path)
        source_graphs.append(path)
        all_nodes.extend(graph.get("nodes") or [])
        all_relationships.extend(graph.get("relationships") or [])

    nodes, node_conflicts = dedupe_by_id(all_nodes, "node")
    relationships, rel_conflicts = dedupe_by_id(all_relationships, "relationship")
    conflicts.extend(node_conflicts)
    conflicts.extend(rel_conflicts)
    if conflicts:
        raise ValueError("Merge conflicts: {}".format("; ".join(conflicts[:20])))

    nodes = sort_nodes(nodes)
    relationships = sort_relationships(relationships)
    return {
        "schema_version": SCHEMA_VERSION,
        "phase": "merged_content_nodes",
        "source_content_graphs": source_graphs,
        "document_global_keys": document_keys(nodes),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "counts": {
            "documents": len(document_keys(nodes)),
            "nodes": len(nodes),
            "relationships": len(relationships),
            "nodes_by_label": count_nodes_by_label(nodes),
            "relationships_by_type": count_relationships_by_type(relationships),
        },
        "nodes": nodes,
        "relationships": relationships,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", nargs="+", required=True, help="Content graph JSON files")
    parser.add_argument("--output", required=True, help="Merged graph JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    merged = merge_graphs(args.input)
    write_json(args.output, merged)
    print(
        "Merged {documents} documents into {nodes} nodes and {relationships} relationships at {output}".format(
            documents=merged["counts"]["documents"],
            nodes=merged["counts"]["nodes"],
            relationships=merged["counts"]["relationships"],
            output=args.output,
        )
    )


if __name__ == "__main__":
    main()
