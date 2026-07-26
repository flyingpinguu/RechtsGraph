import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
PDF_DIR = REPO_ROOT / "abfall_pdfs"

DOCS = {
    "ersatzbaustoffv": "06_ErsatzbaustoffV.pdf",
    "krwg": "01_KrWG.pdf",
    "depv": "07_DepV.pdf",
    "efbv": "14_EfbV.pdf",
}


def run_cmd(args):
    result = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def regression_outputs(tmp_path_factory):
    root = tmp_path_factory.mktemp("normtext_regression")
    raw_dir = root / "raw"
    graph_dir = root / "content_graphs"
    pages_dir = root / "pages"
    merged_dir = root / "merged"
    neo4j_dir = root / "neo4j"
    for directory in (raw_dir, graph_dir, pages_dir, merged_dir, neo4j_dir):
        directory.mkdir(parents=True, exist_ok=True)

    raw_paths = {}
    graph_paths = {}
    for key, pdf_name in DOCS.items():
        raw_path = raw_dir / f"{key}_raw.json"
        graph_path = graph_dir / f"{key}_content_graph.json"
        run_cmd(
            [
                sys.executable,
                "scripts/extract_normtext.py",
                "--output",
                str(raw_path),
                "--pages-dir",
                str(pages_dir),
                str(PDF_DIR / pdf_name),
            ]
        )
        run_cmd(
            [
                sys.executable,
                "scripts/extract_content_nodes.py",
                "--input",
                str(raw_path),
                "--output",
                str(graph_path),
            ]
        )
        raw_paths[key] = raw_path
        graph_paths[key] = graph_path

    return {
        "root": root,
        "raw": raw_paths,
        "graphs": graph_paths,
        "merged": merged_dir,
        "neo4j": neo4j_dir,
    }


def table_chunks(payload):
    for document in payload.get("documents", []):
        for chunk in document.get("chunks", []):
            if chunk.get("chunk_type") == "table_rows":
                yield chunk


def matching_tables(payload, needle):
    return [
        chunk
        for chunk in table_chunks(payload)
        if needle in (chunk.get("legal_citation") or "")
    ]


def row_marker(row):
    if isinstance(row, dict):
        first_key = next(iter(row))
        return str(row.get(first_key) or "")
    if isinstance(row, list) and row:
        return str(row[0] or "")
    return ""


def assert_unique_ids(graph):
    for key in ("nodes", "relationships"):
        ids = [item["id"] for item in graph[key]]
        duplicates = [item_id for item_id, count in Counter(ids).items() if count > 1]
        assert not duplicates, f"duplicate {key}: {duplicates[:10]}"


def test_ersatzbaustoffv_table_regressions(regression_outputs):
    payload = load_json(regression_outputs["raw"]["ersatzbaustoffv"])
    assert all(chunk.get("parser_name") for chunk in table_chunks(payload))

    table_1 = matching_tables(payload, "Anlage 1 Tabelle 1")
    assert len(table_1) == 1
    # The PDF-only fallback can expose the first 11-column visual panel or,
    # if its geometry heuristic improves, the full 20-column logical table.
    # The XML-primary CALS regression asserts the authoritative horizontal
    # continuation merge separately.
    assert len(table_1[0]["columns"]) in {11, 20}
    rows = table_1[0]["rows"]
    # Depending on the PyMuPDF geometry stream, the legacy fallback either
    # removes both repeated panel-header rows or retains both.  It must not
    # lose any of the 18 data rows; XML owns the exact 18-row semantic shape.
    assert len(rows) in {18, 20}
    if len(rows) == 20:
        assert rows[0] == rows[-2]

    table_2 = matching_tables(payload, "Anlage 1 Tabelle 2")
    assert len(table_2) == 1
    assert table_2[0]["parser_name"] == "grid"
    assert table_2[0]["columns"] == ["Parameter", "Dimension", "GS-0", "GS-1", "GS-2", "GS-3"]
    assert len(table_2[0]["rows"]) == 11

    anlage_5_table_1 = matching_tables(payload, "Anlage 5 Tabelle 1")
    assert len(anlage_5_table_1) == 1
    assert anlage_5_table_1[0]["parser_name"] == "grid"
    assert anlage_5_table_1[0]["columns"] == [
        "Parameter",
        "Dimension",
        "Bewertungs- relevanter Bereich",
        "Norm",
        "Normbezeichnung",
    ]
    assert len(anlage_5_table_1[0]["rows"]) >= 50
    assert any(row.get("Parameter") == "Atrazin" for row in anlage_5_table_1[0]["rows"])
    assert any(row.get("Parameter") == "Bromacil" for row in anlage_5_table_1[0]["rows"])
    assert any(row.get("Parameter") == "Diuron" for row in anlage_5_table_1[0]["rows"])
    assert any(row.get("Parameter") == "Simazin" for row in anlage_5_table_1[0]["rows"])
    assert any(row.get("Parameter") == "Dimefuron" for row in anlage_5_table_1[0]["rows"])
    assert not [
        chunk
        for chunk in table_chunks(payload)
        if "Anlage 7" in (chunk.get("legal_citation") or "")
        or "Anlage 8" in (chunk.get("legal_citation") or "")
    ]

    anlage_2 = matching_tables(payload, "Anlage 2 Tabelle")
    assert len(anlage_2) == 27
    assert {len(chunk["columns"]) for chunk in anlage_2} == {11}

    anlage_3 = matching_tables(payload, "Anlage 3 Tabelle")
    assert len(anlage_3) == 13
    assert {len(chunk["columns"]) for chunk in anlage_3} == {11}
    assert {len(chunk["rows"]) for chunk in anlage_3} == {26}
    assert all(row_marker(chunk["rows"][0]) == "B1" for chunk in anlage_3)
    assert all(row_marker(chunk["rows"][-1]) == "B26" for chunk in anlage_3)


def test_content_graphs_have_no_duplicate_ids(regression_outputs):
    for graph_path in regression_outputs["graphs"].values():
        assert_unique_ids(load_json(graph_path))


def test_four_document_merge_reference_and_cypher_workflow(regression_outputs):
    merged_content = regression_outputs["merged"] / "merged_content_graph.json"
    merged_refs = regression_outputs["merged"] / "merged_content_graph_with_refs.json"
    cypher_path = regression_outputs["neo4j"] / "merged_import_with_refs.cypher"

    run_cmd(
        [
            sys.executable,
            "scripts/merge_content_graphs.py",
            "--input",
            *[str(path) for path in regression_outputs["graphs"].values()],
            "--output",
            str(merged_content),
        ]
    )
    run_cmd(
        [
            sys.executable,
            "scripts/extract_reference_relations.py",
            "--input",
            str(merged_content),
            "--output",
            str(merged_refs),
        ]
    )
    run_cmd(
        [
            sys.executable,
            "scripts/export_neo4j_cypher.py",
            "--input",
            str(merged_refs),
            "--output",
            str(cypher_path),
        ]
    )

    graph = load_json(merged_refs)
    assert graph["counts"]["nodes"] >= 1000
    assert graph["counts"]["relationships_by_type"]["REFERS_TO"] >= 2500
    assert_unique_ids(graph)
    assert cypher_path.exists()
    assert cypher_path.stat().st_size > 1_000_000
