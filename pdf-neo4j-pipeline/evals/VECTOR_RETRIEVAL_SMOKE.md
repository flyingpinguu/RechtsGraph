# Three-query engineering retrieval smoke

From `pdf-neo4j-pipeline`:

```sh
# Resolve canonical target passages and print the fixed plan. No API calls or reports.
.venv-mcp/bin/python scripts/check_vector_search.py

# Run lexical / semantic / hybrid search and save timestamped JSON + Markdown.
.venv-mcp/bin/python scripts/check_vector_search.py --execute
```

The check contains exactly three paraphrases targeting KSchG § 4, BGB § 433
and GDPR Article 17. Expected passage IDs are resolved from the canonical
SQLite structural units before any API requests. It searches globally rather
than giving the retriever the expected document or provision.

For each query and mode, the report records whether any expected passage
occurs among the first five hits, its first rank, returned IDs and citations,
coverage, warnings, and diagnostic latency. Missing expected units abort the
check. Failed semantic search and lexical fallback are recorded explicitly;
exit status is nonzero if a requested mode failed or fell back. A retrieval
miss is a measured result rather than a harness failure.

There is no LLM answer generation or judge. Only the three query embeddings
may incur cost. Semantic and hybrid reuse the same exact query cache; repeated
checks with the same model/profile/query need no additional embedding requests
when the cache remains intact. Neo4j and the embedding projection must already
exist. Profile changes intentionally use a separate cache space.

Reports default to `output/embeddings/retrieval-smoke-TIMESTAMP.json` and `.md`.
Use `--index PATH` for another canonical index, or `--output-prefix PATH` to
choose the report stem (the latter replaces reports with that stem). The JSON
includes fixed queries, expected IDs, fixture hash, embedding model/profile,
index identity and provider usage. Corpus changes or embedding-profile changes
must be considered when comparing reports. Live ANN results may vary.

This is a deliberately tiny **engineering retrieval smoke**, not a held-out,
human-reviewed legal benchmark. It checks that known target passages can be
retrieved within the indexed subset. Hits do not demonstrate current law,
legal applicability, answer faithfulness or general legal competence. Latency
and provider usage are diagnostics, not quality points.
