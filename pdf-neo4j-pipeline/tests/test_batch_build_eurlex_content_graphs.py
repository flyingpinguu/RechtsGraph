import json
from pathlib import Path

from scripts.batch_build_eurlex_content_graphs import build_one


def _task(raw_path: Path, graph_path: Path) -> dict:
    return {
        "selection_index": 0,
        "base_celex": "32024L0001",
        "consolidated_celex": "32024L0001",
        "descriptor": "L",
        "source_collection": "external_referenced_original",
        "manifestation_format": "xhtml",
        "raw_path": str(raw_path),
        "raw_relative_path": "raw/L/32024L0001_raw.json",
        "graph_path": str(graph_path),
        "graph_relative_path": "content_graph/L/32024L0001_content_graph.json",
        "resume": True,
        "force": False,
    }


def test_resume_rebuilds_content_graph_when_raw_json_changes(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.json"
    graph_path = tmp_path / "content_graph.json"
    task = _task(raw_path, graph_path)

    raw_path.write_text(json.dumps({"documents": [], "revision": 1}), encoding="utf-8")
    first = build_one(task)
    assert first["status"] == "ok"
    first_hash = first["source_raw_sha256"]

    unchanged = build_one(task)
    assert unchanged["status"] == "skipped_existing"
    assert unchanged["source_raw_sha256"] == first_hash

    raw_path.write_text(json.dumps({"documents": [], "revision": 2}), encoding="utf-8")
    changed = build_one(task)
    assert changed["status"] == "ok"
    assert changed["source_raw_sha256"] != first_hash
    assert json.loads(graph_path.read_text(encoding="utf-8"))[
        "source_raw_sha256"
    ] == changed["source_raw_sha256"]
