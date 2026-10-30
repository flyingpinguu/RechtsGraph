import json
from argparse import Namespace

import pytest

from scripts import check_vector_search as smoke
from rechtsgraph_mcp.embeddings import EmbeddingSettings


@pytest.fixture
def setup_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("RECHTSGRAPH_LOAD_ENV", "0")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings = EmbeddingSettings(model="test/model", dimensions=2, cache_path=tmp_path / "cache.sqlite")
    monkeypatch.setattr(smoke.EmbeddingSettings, "from_environment", lambda: settings)
    class Index:
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get_structural_unit(self, document, unit, **kwargs):
            return {"document": {"document_id": document}, "unit": {"unit_id": document+unit},
                    "passages": [{"passage_id": document+unit}], "truncated": False}
    monkeypatch.setattr(smoke, "RetrievalIndex", Index)
    index_path = tmp_path / "canonical.sqlite"
    index_path.touch()
    return Namespace(index=index_path, execute=False, output_prefix=tmp_path / "report")


def test_offline_plan_resolves_three_canonical_targets_without_api_or_writes(setup_smoke, monkeypatch):
    monkeypatch.setattr(smoke, "CachedEmbedder", lambda *args: pytest.fail("No cache creation offline"))
    monkeypatch.setattr(smoke, "search", lambda *args, **kwargs: pytest.fail("No searches offline"))
    report = smoke.run(setup_smoke)
    assert len(report["cases"]) == 3
    assert report["checks"] == []
    assert all(case["expected_passage_ids"] for case in report["cases"])
    assert not setup_smoke.output_prefix.with_suffix(".json").exists()
    assert not (setup_smoke.output_prefix.parent / "cache.sqlite").exists()
    assert report["fixture_sha256"] == smoke.run(setup_smoke)["fixture_sha256"]


def test_live_workflow_reuses_three_query_embeddings_and_reports_true_rank(setup_smoke, monkeypatch):
    setup_smoke.execute = True
    calls = []
    def encode(self, texts, purpose="document"):
        calls.extend((text, purpose) for text in texts)
        return [[1.0, 0.0] for text in texts]
    monkeypatch.setattr(smoke.OpenRouterEmbedder, "encode", encode)
    def search(index, query, *, mode, limit, embedder):
        assert limit == 5
        if mode != "lexical":
            embedder.encode([query], purpose="query")
        case = next(case for case in smoke.CASES if case["query"] == query)
        return {"mode": mode, "results": [{"passage_id": "wrong"},
                {"passage_id": case["document"]+case["unit"]}], "warnings": []}
    monkeypatch.setattr(smoke, "search", search)
    report = smoke.run(setup_smoke)
    assert report["complete"] is True
    assert len(report["checks"]) == 9
    assert len(calls) == 3
    assert all(row["first_expected_rank"] == 2 for row in report["checks"])
    assert all(row["hit_at_5"] for row in report["checks"])
    smoke.run(setup_smoke)
    assert len(calls) == 3, "An identical repeated smoke check uses cached query embeddings"
    saved = json.loads(setup_smoke.output_prefix.with_suffix(".json").read_text())
    assert saved["fixture_sha256"] == report["fixture_sha256"]
    assert "not human-reviewed legal quality" in setup_smoke.output_prefix.with_suffix(".md").read_text()


def test_fallback_and_errors_are_visible_not_semantic_success(setup_smoke, monkeypatch):
    setup_smoke.execute = True
    def search(index, query, *, mode, **kwargs):
        if mode == "semantic":
            raise RuntimeError("Vector index unavailable")
        return {"mode": "lexical_fallback" if mode == "hybrid" else mode,
                "results": [], "warnings": ["offline"]}
    monkeypatch.setattr(smoke, "search", search)
    report = smoke.run(setup_smoke)
    assert report["complete"] is False
    assert len([row for row in report["checks"] if row.get("error")]) == 3
    assert len([row for row in report["checks"] if row.get("effective_mode") == "lexical_fallback"]) == 3
    assert report["api_usage"]["requests"] == 0
