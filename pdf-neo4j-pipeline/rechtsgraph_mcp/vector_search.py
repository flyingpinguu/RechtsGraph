"""Derived Neo4j vector projection, canonical SQLite hydration and hybrid ranks.

Every model/profile gets its own label and index, avoiding mixed vector spaces.
Text and provenance still come from the read-only canonical retrieval index.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .embeddings import CachedEmbedder, Embedder, EmbeddingSettings, OpenRouterEmbedder, validate_vector
from .index import RetrievalIndex
from .neo4j_store import Neo4jStore


def passage_input(row: dict[str, Any]) -> str:
    return "{}\n{}\n{}".format(row.get("legal_citation") or "", row.get("document_title") or "", row["text"])


def input_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(passage_input(row).encode()).hexdigest()


class Neo4jVectorIndex:
    def __init__(self, settings: EmbeddingSettings, store: Neo4jStore | None = None):
        self.settings = settings
        self.store = store or Neo4jStore()
        self.label = "RGVector_" + settings.profile
        self.index_name = "rg_vector_" + settings.profile

    def initialize(self) -> None:
        self.store._query("CREATE CONSTRAINT rg_vector_key IF NOT EXISTS FOR (v:RechtsGraphVector) REQUIRE v.key IS UNIQUE")
        self.store._query(
            "CREATE VECTOR INDEX {} IF NOT EXISTS FOR (v:{}) ON (v.embedding) "
            "OPTIONS {{indexConfig: {{`vector.dimensions`: {}, `vector.similarity_function`: 'cosine'}}}}".format(
                self.index_name, self.label, self.settings.dimensions))
        self.store._query(
            "MERGE (p:RechtsGraphVectorProfile {profile: $profile}) "
            "SET p.model=$model, p.dimensions=$dimensions, p.query_instruction=$instruction, "
            "p.input_format='citation-title-text-v1'",
            {"profile": self.settings.profile, "model": self.settings.model,
             "dimensions": self.settings.dimensions, "instruction": self.settings.query_instruction})

    def status(self) -> dict[str, Any]:
        rows = self.store._query(
            "SHOW VECTOR INDEXES YIELD name, state, populationPercent WHERE name=$name RETURN state,populationPercent",
            {"name": self.index_name})
        profiles = self.store._query(
            "MATCH (p:RechtsGraphVectorProfile {profile:$profile}) RETURN properties(p) AS metadata",
            {"profile": self.settings.profile})
        count = self.store._query("MATCH (v:{}) RETURN count(v) AS count".format(self.label))[0]["count"]
        return {"available": bool(rows and rows[0]["state"] == "ONLINE" and count),
                "profile": self.settings.profile, "model": self.settings.model,
                "dimensions": self.settings.dimensions, "index": self.index_name,
                "indexed_passages": count, "index_state": rows[0] if rows else None,
                "coverage": profiles[0]["metadata"] if profiles else {}}

    def existing_hashes(self, passage_ids: list[str]) -> dict[str, str]:
        rows = self.store._query(
            "UNWIND $keys AS key MATCH (v:RechtsGraphVector {key:key}) RETURN v.passage_id AS id, v.input_hash AS hash",
            {"keys": [self.settings.profile + ":" + pid for pid in passage_ids]})
        return {row["id"]: row["hash"] for row in rows}

    def write(self, passages: list[dict[str, Any]], vectors: list[list[float]]) -> int:
        if len(passages) != len(vectors):
            raise ValueError("Expected one embedding per passage.")
        data = []
        for row, vector in zip(passages, vectors):
            data.append({"key": self.settings.profile + ":" + row["passage_id"],
                         "passage_id": row["passage_id"], "document_id": row["document_id"],
                         "corpus": row["source_kind"], "chunk_id": row.get("original_chunk_id"),
                         "input_hash": input_hash(row),
                         "embedding": validate_vector(vector, self.settings.dimensions)})
        result = self.store._query(
            "UNWIND $rows AS row MERGE (v:RechtsGraphVector {{key:row.key}}) SET v:{} "
            "SET v.passage_id=row.passage_id, v.document_id=row.document_id, v.corpus=row.corpus, "
            "v.input_hash=row.input_hash, v.embedding=row.embedding, v.profile=$profile "
            "WITH v,row OPTIONAL MATCH (c:GraphNode {{graph_id:row.chunk_id}}) "
            "FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END | MERGE (v)-[:EMBEDS]->(c)) "
            "RETURN count(v) AS written".format(self.label),
            {"rows": data, "profile": self.settings.profile})
        return int(result[0]["written"])

    def progress(self, **values: Any) -> None:
        self.store._query("MATCH (p:RechtsGraphVectorProfile {profile:$profile}) SET p += $values",
                          {"profile": self.settings.profile, "values": values})

    def search(self, vector: list[float], limit: int, corpus: str | None = None,
               document_id: str | None = None) -> list[dict[str, Any]]:
        vector = validate_vector(vector, self.settings.dimensions)
        # Procedure works on the installed Neo4j 2026.04 as well as 5.x.
        # Filters are applied to an oversampled ANN pool; scoped recall is approximate.
        return self.store._query(
            "CALL db.index.vector.queryNodes($index,$candidates,$vector) YIELD node,score "
            "WHERE ($corpus IS NULL OR node.corpus=$corpus) "
            "AND ($document IS NULL OR node.document_id=$document) "
            "RETURN node.passage_id AS passage_id, node.input_hash AS input_hash, score "
            "ORDER BY score DESC, passage_id LIMIT $limit",
            {"index": self.index_name, "vector": vector,
             "candidates": min(1000, max(50, limit * 20)), "limit": limit,
             "corpus": corpus, "document": document_id})


def hydrate(index: RetrievalIndex, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reject embeddings whose exact input changed after indexing."""
    result = []
    for hit in hits:
        row = index.connection.execute(
            "SELECT p.*, d.title AS document_title, d.canonical_citation,d.source_kind,d.source_format, "
            "d.metadata_json AS document_metadata_json FROM passages p JOIN documents d USING(document_id) "
            "WHERE p.passage_id=?", (hit["passage_id"],)).fetchone()
        if row is not None and input_hash(dict(row)) == hit["input_hash"]:
            passage = index._rows([row])[0]
            passage.update(score=hit["score"], retrieval_method="semantic")
            result.append(passage)
    return result


def reciprocal_rank_fusion(lexical: list[dict[str, Any]], semantic: list[dict[str, Any]],
                           limit: int) -> list[dict[str, Any]]:
    """Fuse ranks, never add incomparable BM25 and cosine scores."""
    scores: dict[str, float] = {}
    passages: dict[str, dict[str, Any]] = {}
    for rows in (lexical, semantic):
        seen = set()
        for rank, row in enumerate(rows, 1):
            pid = row["passage_id"]
            if pid in seen:
                continue
            seen.add(pid)
            scores[pid] = scores.get(pid, 0) + 1 / (60 + rank)
            passages.setdefault(pid, dict(row))
    # Equal fused scores otherwise depend arbitrarily on opaque passage IDs.
    semantic_ranks = {row["passage_id"]: rank for rank, row in enumerate(semantic, 1)}
    ordered = sorted(scores, key=lambda pid: (-scores[pid], semantic_ranks.get(pid, float("inf")), pid))[:limit]
    return [dict(passages[pid], score=scores[pid], retrieval_method="hybrid") for pid in ordered]


def search(index: RetrievalIndex, query: str, *, mode: str, limit: int,
           corpus: str | None = None, document_ref: str | None = None,
           embedder: Embedder | None = None, vectors: Neo4jVectorIndex | None = None) -> dict[str, Any]:
    if mode not in {"lexical", "semantic", "hybrid"}:
        raise ValueError("Search mode must be lexical, semantic or hybrid.")
    if not query.strip():
        return {"results": [], "mode": mode, "warnings": []}
    candidate_limit = limit if mode == "lexical" else min(50, limit * 3)
    lexical = (index.search_corpus(query, corpus=corpus, document_ref=document_ref, limit=candidate_limit)
               if mode != "semantic" else [])
    if mode == "lexical":
        return {"results": lexical, "mode": mode, "warnings": []}
    document_id = None
    if document_ref:
        doc = index._resolve_document(document_ref, corpus=corpus)
        if not doc:
            return {"results": [], "mode": mode, "warnings": ["document_not_found"]}
        document_id = doc["document_id"]
    settings = vectors.settings if vectors else EmbeddingSettings.from_environment()
    vectors = vectors or Neo4jVectorIndex(settings)
    owned_embedder = None
    try:
        coverage = vectors.status()
        if not coverage["available"]:
            raise RuntimeError("Vector index is missing, empty or offline; run scripts/build_vector_index.py --execute.")
        if embedder is None:
            owned_embedder = CachedEmbedder(OpenRouterEmbedder(settings))
            embedder = owned_embedder
        if embedder.settings.profile != settings.profile:
            raise ValueError("Query embedder and vector index profiles differ.")
        vector = embedder.encode([query], purpose="query")[0]
        aliases = {"de": "gii", "german": "gii", "eu": "eurlex", "external": "external_pdf"}
        normalized_corpus = aliases.get(corpus.casefold(), corpus.casefold()) if corpus else None
        hits = vectors.search(vector, min(50, limit * 3), normalized_corpus, document_id)
        semantic = hydrate(index, hits)
        results = reciprocal_rank_fusion(lexical, semantic, limit) if mode == "hybrid" else semantic[:limit]
        warnings = ["Approximate similarity is not legal applicability or an answer-confidence score."]
        if corpus or document_id:
            warnings.append("Scope filters apply after approximate candidate search; scoped recall is not guaranteed.")
        if len(semantic) < len(hits):
            warnings.append("Stale or missing canonical passages were excluded; rebuild vector index.")
        metadata = coverage.get("coverage") or {}
        total = metadata.get("canonical_passages")
        count = coverage.get("indexed_passages", 0)
        if total and count < total:
            warnings.append("Partial vector coverage; remaining passages are available through lexical search.")
        brief_coverage = {
            "profile": settings.profile, "indexed_passages": count,
            "canonical_passages": total, "build_state": metadata.get("state"),
            # This is a last-build selection, not a claim that previous vectors were deleted.
            "last_build_scope": json.loads(metadata.get("scope_json", "null")),
        }
        return {"results": results, "mode": mode, "coverage": brief_coverage, "warnings": warnings}
    except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
        if mode == "semantic":
            raise
        return {"results": lexical[:limit], "mode": "lexical_fallback", "warnings": [str(exc)]}
    finally:
        if owned_embedder:
            owned_embedder.close()
