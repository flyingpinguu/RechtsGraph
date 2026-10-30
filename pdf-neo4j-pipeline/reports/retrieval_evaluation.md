# Offline retrieval evaluation

`scripts/evaluate_retrieval.py` evaluates an already-built SQLite retrieval index without rebuilding a corpus, creating embeddings, or making model calls. It uses `RetrievalIndex.search_corpus` only for the measured retrieval operation.

## Case file

Supply JSON as a list, or as an object with a `cases` list. Each case needs a unique `id`, a non-empty `query`, and `reference_status` set to `reviewed` or `unreviewed`. `corpus` and `document_ref` are optional retrieval filters.

A reviewed case can name exactly one evidence mode:

```json
{
  "id": "example-reviewed-case",
  "query": "exact wording used to retrieve the reviewed evidence",
  "corpus": "gii",
  "document_ref": "ExampleG",
  "reference_status": "reviewed",
  "relevant_passage_ids": ["structured:known_chunk_id"]
}
```

Use `relevant_citations` instead when the review records passage-level legal citations rather than passage IDs. Document-level references are unsupported. Do not include both fields in one case. A reviewed case may omit both fields when it is useful to measure request latency or coverage, but it has no gold evidence and is excluded from evidence-metric denominators. Passage IDs are resolved through the read-only index API; legal citations are resolved directly from read-only passage metadata, never from ranked FTS results. Citation comparison folds case and whitespace while retaining punctuation and number boundaries. Normalized duplicate citations within one case fail fast.

`unreviewed` cases are never searched or scored. This report intentionally does not ship a claimed reviewed legal gold corpus: review cases must be created and checked by a qualified reviewer for the chosen corpus and task.

## Run

```bash
cd pdf-neo4j-pipeline
python scripts/evaluate_retrieval.py \
  --index output/retrieval/rechtsgraph.sqlite \
  --cases path/to/reviewed_cases.json \
  --output reports/retrieval_evaluation_result.md \
  --k 10
```

Run `python scripts/evaluate_retrieval.py --help` for CLI details. `--k` is bounded to 1–50.

An output ending in `.json` contains the same metrics and per-case retrieval records plus the fixture description, generation time, resolved index path, index size and mtime, and a SHA-256 of the case file. The index is not hashed, avoiding an unnecessary full read of a large corpus file.

## What the output means

For reviewed cases with gold evidence, the Markdown report records Recall@k over required evidence units, mean reciprocal rank of the first matching unit, and the share of cases where every required unit appeared in the top `k`. It also reports mean and maximum retrieval latency for reviewed searches, and counts loaded, skipped, and goldless cases.

These are passage-retrieval measurements. A retrieved reviewed passage or citation does not establish legal correctness, a legally sound interpretation, complete authority, or a correct answer to a legal question.
