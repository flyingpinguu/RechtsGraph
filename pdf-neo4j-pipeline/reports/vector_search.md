# Reproducible vector and hybrid retrieval

The existing SQLite FTS5/BM25 search and canonical passages remain available.
An optional OpenRouter embedding adapter adds a **derived** Neo4j vector index.
No legal parser or source document is changed. Vectors are stored on separate
`RechtsGraphVector` projection nodes, linked to existing graph chunks through
`EMBEDS` where the canonical graph ID exists. Search results are hydrated from
SQLite with their existing source metadata, citations and context IDs.

## Setup and bounded first build

From `pdf-neo4j-pipeline`, use the existing `.venv-mcp`. No additional packages
are required. Copy `.env.example` to `.env` if no local environment file exists,
and set `OPENROUTER_API_KEY`. Existing process environment overrides `.env`.
Never put a key in command arguments, reports or committed files.

```sh
# Offline plan: no provider calls and no graph writes.
.venv-mcp/bin/python scripts/build_vector_index.py

# Default core corpus, at most 20,000 passages, budget at most $1 per invocation.
.venv-mcp/bin/python scripts/build_vector_index.py --execute --budget-usd 1

# Smaller explicit selection; rerunning reuses unchanged graph vectors/cache.
.venv-mcp/bin/python scripts/build_vector_index.py \
  --document KSchG --execute --budget-usd 0.05
```

The default selection is BGB, KSchG, BetrVG, StGB, ZPO, HGB, GG, GDPR and the
AI Act, resolved against the local corpus. This is **partial coverage**. The
remaining corpus continues to use lexical search. Semantic searches are limited
to indexed passages, and hybrid searches combine those with corpus-wide BM25.
Passage caps and selected document references are saved in the build report.

The default model is `qwen/qwen3-embedding-8b`, with 1,024 dimensions. Qwen's
documented instruction format is used for queries only. The input for documents
is citation, document title and exact passage text. Model, dimensions, query
instruction and input-format version form the profile hash. Different profiles
use different Neo4j labels/indexes and cache keys; they cannot silently mix.
An alternative model must support the configured output dimension. Set
`RECHTSGRAPH_EMBEDDING_QUERY_INSTRUCTION` explicitly for model-specific prompts.

## Budget, cache and restart behavior

The provider request caps input pricing at $0.04 per million tokens. Before each
batch, the builder reserves a conservative UTF-8 byte-based token upper bound
plus framing allowance, reserving all three possible HTTP attempts. Cached batches are conservatively included in that
reservation, but do not make paid calls. A run stops before exceeding its
configured reservation. Provider-reported actual cost and tokens are recorded
separately. No answer-generation or judge calls are part of indexing.

Each successful embedding batch is cached in local SQLite before graph writes;
each graph write is transactional. Restart with the same selection to resume.
Previously completed passages are skipped, changed inputs are re-embedded.
`--workers 3` allows three embedding batches concurrently; budget reservations
happen before scheduling and graph writes remain serial. Network retries are
bounded. The budget applies to each invocation, not all
future searches or repeated invocations. Never schedule unrestricted rebuilds.

Build reports are under `output/embeddings/` (ignored by Git), including the
selection SHA-256, profile, scope, counts, state and API usage. There is no
claim that an OpenRouter model slug pins immutable provider weights.

## Search

The existing MCP `search_corpus` gains a `mode` argument:

- `lexical`: original BM25, no embedding API calls.
- `semantic`: Neo4j approximate nearest-neighbor retrieval, explicit failure if
  its index or API is unavailable.
- `hybrid`: reciprocal rank fusion of BM25 and semantic ranks (`1/(60+rank)`);
  equal fused scores prefer semantic rank rather than opaque passage IDs;
  visible lexical fallback on vector/API failure. Fusion does not guarantee
  better ranking: common-word BM25 hits can still compete with relevant vectors.
- `auto`: `RECHTSGRAPH_SEARCH_MODE`, default lexical; the example config selects
  hybrid. Restart the MCP host/server after code or environment changes.

Query embeddings are cached by exact text and profile. The first uncached
semantic/hybrid query is sent to OpenRouter; repeated exact queries reuse it.
`get_service_status(component="vectors")` returns index readiness and scope.
Search also returns effective mode, warnings and vector coverage.

The installed Neo4j version uses `db.index.vector.queryNodes`. Corpus/document
filters apply after an oversampled approximate candidate search, so restrictive
filters can return fewer hits and do not guarantee exact scoped recall.
Embeddings whose input no longer matches canonical text are excluded. Deleted
canonical passages are excluded during hydration; they are not automatically
deleted from old derived graph indexes. Rebuild/cleanup policies can be added
without mutating canonical data.

Similarity/fusion scores are retrieval signals, not calibrated probabilities,
legal correctness, currency or applicability. A semantic hit does not determine
whether a question should be answered. Existing legal-research checks remain.

## Verification

```sh
.venv-mcp/bin/python -m pytest tests/test_vector_search.py \
  tests/test_rechtsgraph_mcp_server.py tests/test_rechtsgraph_mcp_support.py -q

# Compact agent comparison: free plan by default, three tasks / six sessions.
node evals/run_legal_competence_v1.mjs
```

Automated tests use fake embeddings/HTTP and temporary indexes. They cover cache
reuse, model separation, vector validation, rank fusion, stale evidence, visible
fallback and stopping before a paid request when the budget is exhausted.
The compact legal benchmark still requires explicit `--execute` for paid model
calls and independent scoring; index smoke checks do not establish answer quality.

Sources: [OpenRouter embeddings API](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request),
[Qwen model card](https://huggingface.co/Qwen/Qwen3-Embedding-8B),
[Neo4j vector indexes](https://neo4j.com/docs/cypher-manual/current/indexes/search-indexes/vector-indexes/).
