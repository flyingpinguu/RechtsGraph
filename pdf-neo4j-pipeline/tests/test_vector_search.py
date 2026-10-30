import io
import json
import sqlite3
from dataclasses import replace

import pytest

from rechtsgraph_mcp.embeddings import (
    CachedEmbedder, EmbeddingSettings, OpenRouterEmbedder, validate_vector,
)
from rechtsgraph_mcp.index import RetrievalIndex, build_index
from rechtsgraph_mcp.vector_search import (
    Neo4jVectorIndex, hydrate, input_hash, reciprocal_rank_fusion, search,
)
from tests.test_retrieval_index import _raw_document, _write_manifest


@pytest.fixture(autouse=True)
def no_local_credentials(monkeypatch):
    monkeypatch.setenv("RECHTSGRAPH_LOAD_ENV", "0")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("RECHTSGRAPH_SEARCH_MODE", raising=False)


class FakeEmbedder:
    def __init__(self, settings):
        self.settings = settings
        self.calls = []

    def encode(self, texts, purpose="document"):
        self.calls.append((texts, purpose))
        return [[1.0, 0.0] for _ in texts]


def test_cache_reuses_inputs_and_separates_models_and_query_purpose(tmp_path):
    settings = EmbeddingSettings(model="test/a", dimensions=2, cache_path=tmp_path / "cache")
    fake = FakeEmbedder(settings)
    cache = CachedEmbedder(fake)
    assert cache.encode(["same", "same"]) == [[1, 0], [1, 0]]
    cache.encode(["same"])
    assert len(fake.calls) == 1
    cache.encode(["same"], purpose="query")
    assert len(fake.calls) == 2
    cache.close()
    other = FakeEmbedder(replace(settings, model="test/b"))
    cache = CachedEmbedder(other)
    cache.encode(["same"])
    assert len(other.calls) == 1
    cache.close()
    assert settings.profile != replace(settings, query_instruction="different").profile


@pytest.mark.parametrize("vector", [[0, 0], [float("nan"), 1], [1], [True, 1], ["x", 1]])
def test_invalid_vectors_are_rejected(vector):
    with pytest.raises(ValueError):
        validate_vector(vector, 2)


def test_openrouter_restores_response_order_and_hides_credentials(monkeypatch):
    settings = EmbeddingSettings(model="test/a", dimensions=2, api_key="never-print-this")
    def respond(request, timeout):
        data = json.loads(request.data)
        assert data["dimensions"] == 2
        assert data["provider"]["max_price"]["prompt"] == 0.04
        assert data["input"][0].startswith("Instruct:")
        return io.BytesIO(json.dumps({"data": [
            {"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 0]},
        ]}).encode())
    monkeypatch.setattr("rechtsgraph_mcp.embeddings.urlopen", respond)
    assert "never-print-this" not in repr(settings)
    assert OpenRouterEmbedder(settings).encode(["a", "b"], "query") == [[1, 0], [0, 1]]


def test_api_rejects_duplicate_or_missing_response_indexes(monkeypatch):
    monkeypatch.setattr("rechtsgraph_mcp.embeddings.urlopen", lambda *a, **k: io.BytesIO(
        json.dumps({"data": [{"index": 0, "embedding": [1, 0]},
                             {"index": 0, "embedding": [1, 0]}]}).encode()))
    with pytest.raises(ValueError, match="indexed vector"):
        OpenRouterEmbedder(EmbeddingSettings(dimensions=2, api_key="fake")).encode(["a", "b"])


def test_rrf_deduplicates_and_rewards_agreement_without_adding_raw_scores():
    lexical = [{"passage_id": "a", "score": -100}, {"passage_id": "b", "score": -1}]
    semantic = [{"passage_id": "b", "score": 0.01}, {"passage_id": "c", "score": 0.99}]
    result = reciprocal_rank_fusion(lexical, semantic, 3)
    assert [row["passage_id"] for row in result] == ["b", "a", "c"]
    assert result[0]["score"] == pytest.approx(1/62 + 1/61)
    tied = reciprocal_rank_fusion([{"passage_id": "a"}], [{"passage_id": "z"}], 2)
    assert [row["passage_id"] for row in tied] == ["z", "a"]


def test_hydration_keeps_canonical_provenance_and_rejects_stale_embeddings(tmp_path):
    path = tmp_path / "index.sqlite"
    build_index(path, [_write_manifest(tmp_path, _raw_document())])
    with RetrievalIndex(path) as index:
        row = index.search_corpus("Wasser")[0]
        hit = {"passage_id": row["passage_id"], "input_hash": input_hash(row), "score": .8}
        valid = hydrate(index, [hit])
        assert valid[0]["text"] == row["text"]
        assert valid[0]["evidence"] == row["evidence"]
        assert hydrate(index, [dict(hit, input_hash="obsolete")]) == []


def test_hybrid_fallback_is_visible_and_semantic_failure_is_not_hidden(tmp_path):
    path = tmp_path / "index.sqlite"
    build_index(path, [_write_manifest(tmp_path, _raw_document())])
    class Offline:
        settings = EmbeddingSettings(dimensions=2)
        def status(self):
            return {"available": False}
    with RetrievalIndex(path) as index:
        result = search(index, "Wasser", mode="hybrid", limit=3, vectors=Offline())
        assert result["mode"] == "lexical_fallback"
        assert result["results"]
        assert "missing" in result["warnings"][0]
        with pytest.raises(RuntimeError):
            search(index, "Wasser", mode="semantic", limit=3, vectors=Offline())


def test_query_model_mismatch_falls_back_without_api_call(tmp_path):
    path = tmp_path / "index.sqlite"
    build_index(path, [_write_manifest(tmp_path, _raw_document())])
    class Online:
        settings = EmbeddingSettings(model="test/a", dimensions=2)
        def status(self):
            return {"available": True}
    embedder = FakeEmbedder(EmbeddingSettings(model="test/b", dimensions=2))
    with RetrievalIndex(path) as index:
        result = search(index, "Wasser", mode="hybrid", limit=3, vectors=Online(), embedder=embedder)
    assert result["mode"] == "lexical_fallback"
    assert not embedder.calls


def test_semantic_scope_normalizes_alias_and_hybrid_recovers_cache_failure(tmp_path):
    path = tmp_path / "index.sqlite"
    build_index(path, [_write_manifest(tmp_path, _raw_document())])
    class Online:
        settings = EmbeddingSettings(model="test/a", dimensions=2)
        corpus = None
        def status(self):
            return {"available": True}
        def search(self, vector, limit, corpus, document):
            self.corpus = corpus
            return []
    vectors = Online()
    embedder = FakeEmbedder(vectors.settings)
    with RetrievalIndex(path) as index:
        search(index, "Wasser", mode="semantic", limit=3, corpus="DE", vectors=vectors, embedder=embedder)
        assert vectors.corpus == "gii"
        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        embedder.encode = locked
        result = search(index, "Wasser", mode="hybrid", limit=3, vectors=vectors, embedder=embedder)
        assert result["mode"] == "lexical_fallback"
        assert result["results"]


def test_vector_writes_are_profile_isolated_and_keep_existing_chunk_links():
    class RecordingStore:
        def _query(self, query, params):
            self.query, self.params = query, params
            return [{"written": len(params["rows"])}]
    store = RecordingStore()
    settings = EmbeddingSettings(dimensions=2)
    vectors = Neo4jVectorIndex(settings, store)
    passage = {"passage_id": "structured:chunk1", "document_id": "doc1", "text": "Evidence",
               "source_kind": "gii", "original_chunk_id": "chunk1"}
    assert vectors.write([passage], [[1, 0]]) == 1
    assert "EMBEDS" in store.query and "OPTIONAL MATCH" in store.query
    assert vectors.label in store.query
    assert store.params["rows"][0]["key"] == settings.profile + ":structured:chunk1"
    assert store.params["rows"][0]["input_hash"] == input_hash(passage)
    with pytest.raises(ValueError):
        vectors.write([passage], [[1, 0, 0]])


def test_budget_stops_before_embedding_call_and_records_partial_scope(tmp_path, monkeypatch):
    from argparse import Namespace
    from scripts import build_vector_index as builder
    path = tmp_path / "index.sqlite"
    build_index(path, [_write_manifest(tmp_path, _raw_document())])
    settings = EmbeddingSettings(dimensions=2, api_key="fake", cache_path=tmp_path / "cache")
    monkeypatch.setattr(builder.EmbeddingSettings, "from_environment", lambda: settings)
    class FakeVectors:
        def __init__(self, *a): pass
        def initialize(self): pass
        def progress(self, **k): pass
        def existing_hashes(self, ids): return {}
    monkeypatch.setattr(builder, "Neo4jVectorIndex", FakeVectors)
    def forbidden(*a, **k):
        pytest.fail("Budget must stop before a paid embedding call")
    monkeypatch.setattr(OpenRouterEmbedder, "encode", forbidden)
    result = builder.run(Namespace(index=path, document=["DemoG"], all_corpus=False,
        max_passages=5, batch_size=2, budget_usd=1e-12, execute=True, report=tmp_path / "report.json"))
    assert result["state"] == "budget_stopped"
    assert result["processed"] == 0
    assert result["api_usage"]["requests"] == 0


def test_parallel_batches_reserve_budget_before_scheduling(tmp_path, monkeypatch):
    from argparse import Namespace
    from scripts import build_vector_index as builder
    path = tmp_path / "index.sqlite"
    raw = _raw_document()
    chunks = raw["documents"][0]["chunks"]
    chunks.append(dict(chunks[0], chunk_id="chunk_three", source_order=3))
    build_index(path, [_write_manifest(tmp_path, raw)])
    with RetrievalIndex(path) as index:
        first_batch = [dict(row) for row in builder.select_passages(index, ["DemoG"], False, 2)]
    budget = builder.conservative_cost(first_batch) * 1.01
    settings = EmbeddingSettings(dimensions=2, api_key="fake", cache_path=tmp_path / "cache")
    monkeypatch.setattr(builder.EmbeddingSettings, "from_environment", lambda: settings)
    class FakeVectors:
        def __init__(self, *a): pass
        def initialize(self): pass
        def progress(self, **k): pass
        def existing_hashes(self, ids): return {}
        def write(self, rows, vectors): return len(rows)
    monkeypatch.setattr(builder, "Neo4jVectorIndex", FakeVectors)
    calls = []
    def encode(self, texts, purpose="document"):
        calls.append(texts)
        return [[1, 0] for _ in texts]
    monkeypatch.setattr(OpenRouterEmbedder, "encode", encode)
    result = builder.run(Namespace(index=path, document=["DemoG"], all_corpus=False,
        max_passages=5, batch_size=2, workers=3, budget_usd=budget,
        execute=True, report=tmp_path / "report.json"))
    assert result["state"] == "budget_stopped"
    assert result["processed"] == result["written"] == 2
    assert len(calls) == 1
    assert result["reserved_upper_cost_usd"] <= budget
