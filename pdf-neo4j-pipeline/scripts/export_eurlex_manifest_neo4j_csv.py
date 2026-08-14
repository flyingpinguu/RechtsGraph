#!/usr/bin/env python3
"""Export per-document EUR-Lex graphs to idempotent Neo4j LOAD CSV files.

References are extracted document by document to keep memory bounded. Cross-act
targets initially share deterministic ``ReferenceTarget`` nodes; the generated
Cypher resolves and rewires them after all EU documents have been imported.
The same resolver also closes matching EU targets already present in the German
graph.
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from export_neo4j_csv import (  # noqa: E402
    COMMON_NODE_LABEL,
    labels_for_node,
    node_file_name,
    scalar_properties,
    slugify,
)
from export_neo4j_cypher import (  # noqa: E402
    NEO4J_NODE_PROPERTY_ALLOWLIST,
    cypher_identifier,
    filter_node_properties,
    node_group_for_labels,
    primary_label,
)
from extract_reference_relations import ReferenceExtractor  # noqa: E402


RELATIONSHIP_FIELDS = {
    "CONTAINS_UNIT": ("source",),
    "CONTAINS_CHUNK": ("source",),
    "NEXT_UNIT": ("sequence",),
    "NEXT_CHUNK": ("sequence",),
    "REFERS_TO": (
        "char_end",
        "char_start",
        "extraction_method",
        "mention_text",
        "normalized_reference",
        "reference_id",
        "reference_kind",
        "requested_target_global_key",
        "resolution_method",
        "resolution_status",
        "resolved_target_global_key",
        "source_chunk_id",
        "source_document_id",
        "source_unit_id",
        "target_document_key",
        "target_global_key",
        "target_level",
        "target_title_key",
    ),
}


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _writer(path: Path, fields: Sequence[str]) -> Tuple[Any, csv.DictWriter]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
    writer.writeheader()
    return handle, writer


def _node_fields(labels: Iterable[str]) -> Sequence[str]:
    label = primary_label(labels)
    allowed = NEO4J_NODE_PROPERTY_ALLOWLIST.get(label) or set()
    return ["graph_id", *sorted(allowed), "node_group", "is_content_node"]


def _node_row(node: Mapping[str, Any], labels: Sequence[str]) -> Dict[str, Any]:
    props = {
        **filter_node_properties(labels, node.get("properties") or {}),
        "node_group": node_group_for_labels(labels),
        "is_content_node": node_group_for_labels(labels) == "content_node",
    }
    row = {"graph_id": node["id"]}
    row.update(scalar_properties(props))
    return row


def _rel_row(rel: Mapping[str, Any]) -> Dict[str, Any]:
    row = {
        "rel_id": rel["id"],
        "start_node_id": rel["start_node_id"],
        "end_node_id": rel["end_node_id"],
    }
    row.update(scalar_properties(rel.get("properties") or {}))
    return row


def _write_import_cypher(
    path: Path,
    csv_uri_prefix: str,
    node_files: Sequence[Tuple[Tuple[str, ...], str]],
    relationship_files: Sequence[Tuple[str, str]],
    batch_size: int,
) -> None:
    lines = [
        "// Idempotent incremental import for consolidated EUR-Lex Formex",
        "CREATE CONSTRAINT graphnode_graph_id IF NOT EXISTS",
        "FOR (n:{}) REQUIRE n.graph_id IS UNIQUE;".format(cypher_identifier(COMMON_NODE_LABEL)),
        "",
        "CREATE INDEX graphnode_global_key IF NOT EXISTS",
        "FOR (n:{}) ON (n.global_key);".format(cypher_identifier(COMMON_NODE_LABEL)),
        "",
        "CREATE INDEX reference_target_document_key IF NOT EXISTS",
        "FOR (n:`ReferenceTarget`) ON (n.target_document_key);",
        "",
        "CALL db.awaitIndexes(300);",
        "",
    ]
    for labels, file_name in node_files:
        label_clause = "".join(":{}".format(cypher_identifier(label)) for label in labels)
        lines.extend(
            [
                "LOAD CSV WITH HEADERS FROM '{}' AS row".format(csv_uri_prefix + file_name),
                "CALL (row) {",
                "  MERGE (n{} {{graph_id: row.graph_id}})".format(label_clause),
                "  SET n += row",
                "}} IN TRANSACTIONS OF {} ROWS;".format(batch_size),
                "",
            ]
        )
    for rel_type, file_name in relationship_files:
        lines.extend(
            [
                "LOAD CSV WITH HEADERS FROM '{}' AS row".format(csv_uri_prefix + file_name),
                "CALL (row) {",
                "  MATCH (start:{} {{graph_id: row.start_node_id}})".format(cypher_identifier(COMMON_NODE_LABEL)),
                "  MATCH (end:{} {{graph_id: row.end_node_id}})".format(cypher_identifier(COMMON_NODE_LABEL)),
                "  MERGE (start)-[r:{} {{rel_id: row.rel_id}}]->(end)".format(cypher_identifier(rel_type)),
                "  SET r += row",
                "  REMOVE r.start_node_id, r.end_node_id",
                "}} IN TRANSACTIONS OF {} ROWS;".format(batch_size),
                "",
            ]
        )

    # Resolve the normalized citation key to the imported CELEX document and
    # then to the exact article/annex global key whenever that target exists.
    lines.extend(
        [
            "// Close EU targets from both the German and EU corpora.",
            "MATCH (d:`Document`)",
            "WHERE d.base_celex IS NOT NULL AND d.citation_alias_keys IS NOT NULL",
            "UNWIND split(d.citation_alias_keys, '|') AS alias_key",
            "WITH d, alias_key WHERE alias_key <> ''",
            "CALL (d, alias_key) {",
            "  MATCH (rt:`ReferenceTarget` {target_document_key: alias_key})",
            "  WITH d, rt, coalesce(rt.requested_target_global_key, rt.global_key) AS requested_key",
            "  WITH d, rt, requested_key,",
            "    CASE",
            "      WHEN requested_key STARTS WITH rt.target_document_key",
            "      THEN d.document_global_key + substring(requested_key, size(rt.target_document_key))",
            "      ELSE d.document_global_key",
            "    END AS mapped_key",
            "  OPTIONAL MATCH (exact:`GraphNode` {global_key: mapped_key})",
            "  WHERE NOT exact:`ReferenceTarget`",
            "  WITH rt, requested_key, mapped_key, coalesce(exact, d) AS target",
            "  OPTIONAL MATCH (rt)-[obsolete:`RESOLVES_TO`]->(obsolete_target:`GraphNode`)",
            "  WHERE obsolete_target <> target",
            "  DELETE obsolete",
            "  MERGE (rt)-[resolution:`RESOLVES_TO`]->(target)",
            "  SET rt.status = 'resolved',",
            "      rt.resolved_target_global_key = target.global_key,",
            "      resolution.requested_target_global_key = requested_key,",
            "      resolution.mapped_target_global_key = mapped_key",
            "}} IN TRANSACTIONS OF {} ROWS;".format(batch_size),
            "",
            "// Rewire REFERS_TO to the concrete EU target while retaining the",
            "// ReferenceTarget and RESOLVES_TO edge as an auditable trace.",
            "MATCH (source:`GraphNode`)-[old:`REFERS_TO`]->(rt:`ReferenceTarget`)-[:`RESOLVES_TO`]->(target:`GraphNode`)",
            "WHERE source <> target",
            "CALL (source, old, target) {",
            "  MERGE (source)-[resolved:`REFERS_TO` {rel_id: old.rel_id}]->(target)",
            "  SET resolved += properties(old),",
            "      resolved.resolution_status = 'resolved',",
            "      resolved.resolution_method = CASE",
            "        WHEN target:`Document` THEN 'resolved_document_or_nearest_document'",
            "        ELSE 'resolved_eu_alias_exact'",
            "      END,",
            "      resolved.resolved_target_global_key = target.global_key",
            "  DELETE old",
            "}} IN TRANSACTIONS OF {} ROWS;".format(batch_size),
            "",
            "// Repair stale document-level edges from an earlier import whenever",
            "// the requested article or annex now exists as an exact graph node.",
            "MATCH (source:`GraphNode`)-[old:`REFERS_TO`]->(document:`Document`)",
            "WHERE document.base_celex IS NOT NULL",
            "  AND old.resolution_status = 'resolved'",
            "  AND old.requested_target_global_key IS NOT NULL",
            "  AND old.target_document_key IS NOT NULL",
            "WITH source, old, document,",
            "  CASE",
            "    WHEN old.requested_target_global_key STARTS WITH old.target_document_key",
            "    THEN document.document_global_key + substring(old.requested_target_global_key, size(old.target_document_key))",
            "    ELSE document.document_global_key",
            "  END AS mapped_key",
            "MATCH (exact:`GraphNode` {global_key: mapped_key})",
            "WHERE exact <> document AND NOT exact:`ReferenceTarget`",
            "CALL (source, old, exact) {",
            "  MERGE (source)-[resolved:`REFERS_TO` {rel_id: old.rel_id}]->(exact)",
            "  SET resolved += properties(old),",
            "      resolved.resolution_status = 'resolved',",
            "      resolved.resolution_method = 'resolved_eu_alias_exact',",
            "      resolved.resolved_target_global_key = exact.global_key",
            "  DELETE old",
            "}} IN TRANSACTIONS OF {} ROWS;".format(batch_size),
            "",
            "// A CELEX-shaped ReferenceTarget is still a placeholder, not an",
            "// exact content node. Repair any stale resolved edge to such a node.",
            "MATCH (source:`GraphNode`)-[old:`REFERS_TO`]->(bad:`ReferenceTarget`)",
            "WHERE old.resolution_status = 'resolved'",
            "  AND old.target_document_key IS NOT NULL",
            "MATCH (document:`Document`)",
            "WHERE document.base_celex IS NOT NULL",
            "  AND old.target_document_key IN split(document.citation_alias_keys, '|')",
            "WITH source, old, bad, document,",
            "  CASE",
            "    WHEN old.requested_target_global_key STARTS WITH old.target_document_key",
            "    THEN document.document_global_key + substring(old.requested_target_global_key, size(old.target_document_key))",
            "    ELSE document.document_global_key",
            "  END AS mapped_key",
            "OPTIONAL MATCH (exact:`GraphNode` {global_key: mapped_key})",
            "WHERE NOT exact:`ReferenceTarget`",
            "WITH source, old, bad, document, coalesce(exact, document) AS target",
            "WHERE target <> bad",
            "CALL (source, old, target) {",
            "  MERGE (source)-[resolved:`REFERS_TO` {rel_id: old.rel_id}]->(target)",
            "  SET resolved += properties(old),",
            "      resolved.resolution_status = 'resolved',",
            "      resolved.resolution_method = CASE",
            "        WHEN target:`Document` THEN 'resolved_document_or_nearest_document'",
            "        ELSE 'resolved_eu_alias_exact'",
            "      END,",
            "      resolved.resolved_target_global_key = target.global_key",
            "  DELETE old",
            "}} IN TRANSACTIONS OF {} ROWS;".format(batch_size),
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-manifest", type=Path, required=True)
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--output-cypher", type=Path, required=True)
    parser.add_argument("--csv-uri-prefix", required=True)
    parser.add_argument("--batch-size", type=int, default=10000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = json.loads(args.graph_manifest.read_text(encoding="utf-8"))
    extraction_dir = args.graph_manifest.parent
    results = [
        item
        for item in manifest.get("results") or []
        if item.get("status") in {"ok", "skipped_existing"}
    ]
    args.csv_dir.mkdir(parents=True, exist_ok=True)

    node_handles: Dict[Tuple[str, ...], Any] = {}
    node_writers: Dict[Tuple[str, ...], csv.DictWriter] = {}
    node_names: Dict[Tuple[str, ...], str] = {}
    rel_handles: Dict[str, Any] = {}
    rel_writers: Dict[str, csv.DictWriter] = {}
    rel_names: Dict[str, str] = {}
    node_counts = Counter()
    rel_counts = Counter()
    documents = 0

    try:
        for index, result in enumerate(results, 1):
            graph_path = extraction_dir / result["graph_path"]
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            graph = ReferenceExtractor(graph).extract()
            documents += 1
            for node in graph.get("nodes") or []:
                labels = labels_for_node(node)
                if labels not in node_writers:
                    name = node_file_name(len(node_writers), labels)
                    handle, writer = _writer(args.csv_dir / name, _node_fields(labels))
                    node_handles[labels] = handle
                    node_writers[labels] = writer
                    node_names[labels] = name
                row = _node_row(node, labels)
                node_writers[labels].writerow({key: _csv_value(value) for key, value in row.items()})
                node_counts[primary_label(labels)] += 1
            for rel in graph.get("relationships") or []:
                rel_type = str(rel["type"])
                if rel_type not in rel_writers:
                    name = "relationships_{}.csv".format(slugify(rel_type))
                    fields = [
                        "rel_id",
                        "start_node_id",
                        "end_node_id",
                        *RELATIONSHIP_FIELDS.get(rel_type, ()),
                    ]
                    handle, writer = _writer(args.csv_dir / name, fields)
                    rel_handles[rel_type] = handle
                    rel_writers[rel_type] = writer
                    rel_names[rel_type] = name
                row = _rel_row(rel)
                rel_writers[rel_type].writerow({key: _csv_value(value) for key, value in row.items()})
                rel_counts[rel_type] += 1
            if index % 100 == 0 or index == len(results):
                print("[{}/{}] exported {}".format(index, len(results), result.get("consolidated_celex")), flush=True)
    finally:
        for handle in [*node_handles.values(), *rel_handles.values()]:
            handle.close()

    node_files = sorted((labels, node_names[labels]) for labels in node_names)
    relationship_files = sorted((rel_type, rel_names[rel_type]) for rel_type in rel_names)
    _write_import_cypher(
        args.output_cypher,
        args.csv_uri_prefix,
        node_files,
        relationship_files,
        args.batch_size,
    )
    summary = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_graph_manifest": str(args.graph_manifest),
        "documents": documents,
        "node_rows": sum(node_counts.values()),
        "node_rows_by_primary_label": dict(sorted(node_counts.items())),
        "relationship_rows": sum(rel_counts.values()),
        "relationship_rows_by_type": dict(sorted(rel_counts.items())),
        "node_files": [name for _labels, name in node_files],
        "relationship_files": [name for _type, name in relationship_files],
        "cypher": str(args.output_cypher),
        "content_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(args.csv_dir.glob("*.csv"))
        },
    }
    (args.csv_dir / "export_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in summary.items() if key not in {"content_sha256", "node_files", "relationship_files"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
