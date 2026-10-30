import csv
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_placeholder_references as audit


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def document_row(graph_id, aliases):
    return {
        "graph_id": graph_id,
        "document_key": graph_id,
        "document_global_key": graph_id,
        "global_key": graph_id,
        "citation_alias_keys": "|{}|".format("|".join(aliases)),
    }


def reference_row(index, target):
    chunk_id = "chunk_{}".format(index)
    return {
        "start_node_id": chunk_id,
        "end_node_id": "ref_target_{}".format(target),
        "source_chunk_id": chunk_id,
        "source_unit_id": "unit_{}".format(index),
        "source_document_id": "source_doc",
        "target_document_key": target,
        "requested_target_global_key": "{}_art_1".format(target),
        "target_global_key": "{}_art_1".format(target),
        "char_start": str(index),
        "char_end": str(index + 1),
        "mention_text": target,
    }


def test_audit_can_limit_post_load_resolution_to_an_additional_corpus(tmp_path):
    source_dir = tmp_path / "source"
    resolver_dir = tmp_path / "resolver"
    document_fields = [
        "graph_id",
        "document_key",
        "document_global_key",
        "global_key",
        "citation_alias_keys",
    ]
    write_csv(
        source_dir / "nodes_009_document.csv",
        document_fields,
        [document_row("de_law", ["de_law"])],
    )
    write_csv(
        resolver_dir / "nodes_000_document.csv",
        document_fields,
        [document_row("eu_law", ["eu_alias"])],
    )
    reference_fields = list(reference_row(1, "de_law"))
    write_csv(
        source_dir / "relationships_refers_to.csv",
        reference_fields,
        [
            reference_row(1, "de_law"),
            reference_row(2, "eu_alias"),
            reference_row(3, "missing_law"),
        ],
    )

    summary, _occurrences = audit.analyze(
        source_dir,
        [resolver_dir],
        include_own_aliases=False,
    )

    classes = summary["placeholder_classes"]["chunk"]
    assert classes["corpus_internal_unique_document"] == 1
    assert classes["external_or_unmatched_document"] == 2
    assert summary["estimated_post_load_placeholder_edges"]["chunk"] == 2
