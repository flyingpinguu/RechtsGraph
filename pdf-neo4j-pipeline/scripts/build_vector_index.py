#!/usr/bin/env python3
"""Plan or resume a budgeted OpenRouter -> Neo4j embedding build.

Default: a bounded legal core corpus. No API calls or graph writes without
--execute. Existing canonical text and graph edges are never replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rechtsgraph_mcp.embeddings import CachedEmbedder, EmbeddingSettings, OpenRouterEmbedder
from rechtsgraph_mcp.index import RetrievalIndex
from rechtsgraph_mcp.neo4j_store import Neo4jSettings, Neo4jStore
from rechtsgraph_mcp.vector_search import Neo4jVectorIndex, input_hash, passage_input

ROOT = Path(__file__).resolve().parents[1]
CORE_DOCUMENTS = ["BGB", "KSchG", "BetrVG", "StGB", "ZPO", "HGB", "GG", "32016R0679", "32024R1689"]
# Request routing is capped at the same provider price (USD per million tokens).
MAX_PRICE_PER_MILLION = 0.04


def select_passages(index, documents, all_corpus, max_passages):
    ids = []
    for reference in documents:
        document = index._resolve_document(reference)
        if document is None:
            raise ValueError("Document not found: {}".format(reference))
        ids.append(document["document_id"])
    if not all_corpus and not ids:
        raise ValueError("Select documents or --all-corpus.")
    where = "" if all_corpus else " WHERE p.document_id IN ({})".format(",".join("?" for _ in ids))
    query = ("SELECT p.*,d.title AS document_title,d.source_kind FROM passages p "
             "JOIN documents d USING(document_id)" + where + " ORDER BY p.document_id,p.sort_order,p.passage_id LIMIT ?")
    return index.connection.execute(query, [*(() if all_corpus else ids), max_passages])


def conservative_cost(rows):
    # Byte-level upper bound, not an optimistic chars/4 estimate. Includes framing.
    # Reserve all three possible HTTP attempts as well, including ambiguous errors.
    return 3 * sum(len(passage_input(row).encode()) + 64 for row in rows) * MAX_PRICE_PER_MILLION / 1_000_000


def run(args):
    settings = EmbeddingSettings.from_environment()
    refs = args.document or ([] if args.all_corpus else CORE_DOCUMENTS)
    with RetrievalIndex(args.index) as index:
        # The selected subset is intentionally small enough to plan in memory.
        rows = [dict(row) for row in select_passages(index, refs, args.all_corpus, args.max_passages)]
        total = index.connection.execute("SELECT count(*) FROM passages").fetchone()[0]
    selection_hash = hashlib.sha256("\n".join(row["passage_id"] + ":" + input_hash(row) for row in rows).encode()).hexdigest()
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "model": settings.model,
        "source_index": str(args.index.resolve()),
        "source_index_size": args.index.stat().st_size,
        "source_index_mtime_ns": args.index.stat().st_mtime_ns,
        "dimensions": settings.dimensions, "profile": settings.profile,
        "scope": "bounded_all_corpus" if args.all_corpus else refs,
        "selected_passages": len(rows), "canonical_passages": total,
        "max_passages": args.max_passages, "selection_sha256": selection_hash,
        "estimated_upper_cost_usd": conservative_cost(rows), "budget_usd": args.budget_usd,
        "mode": "execute" if args.execute else "plan", "processed": 0,
        "written": 0, "unchanged": 0, "state": "planned",
    }
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if not args.execute:
        return report
    if not settings.api_key:
        raise ValueError("OPENROUTER_API_KEY is missing.")
    store = Neo4jStore(Neo4jSettings.from_environment())
    # Large batch writes take longer than normal read tools.
    from dataclasses import replace
    store.settings = replace(store.settings, timeout_seconds=60)
    vectors = Neo4jVectorIndex(settings, store)
    vectors.initialize()
    vectors.progress(state="building", scope_json=json.dumps(report["scope"]),
                     selected_passages=len(rows), canonical_passages=total,
                     selection_sha256=selection_hash)
    apis = []
    def encode_batch(pending):
        # Each worker owns its SQLite connection; the main thread owns graph writes.
        api = OpenRouterEmbedder(settings)
        apis.append(api)
        cache = CachedEmbedder(api)
        try:
            return cache.encode([passage_input(row) for row in pending])
        finally:
            cache.close()
    def usage():
        return {"requests": sum(api.usage["requests"] for api in apis),
                "prompt_tokens": sum(api.usage["prompt_tokens"] for api in apis),
                "cost_usd": sum(api.usage["cost_usd"] for api in apis)}
    reserved_cost = 0.0
    try:
        workers = getattr(args, "workers", 1)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            queue = deque()
            start = 0
            stopped = False
            while start < len(rows) or queue:
                while not stopped and start < len(rows) and len(queue) < workers:
                    batch = rows[start:start + args.batch_size]
                    old = vectors.existing_hashes([row["passage_id"] for row in batch])
                    pending = [row for row in batch if old.get(row["passage_id"]) != input_hash(row)]
                    upper = conservative_cost(pending)
                    # Reserve synchronously BEFORE scheduling a paid request.
                    if reserved_cost + upper > args.budget_usd:
                        stopped = True
                        break
                    reserved_cost += upper
                    queue.append((batch, pending, pool.submit(encode_batch, pending) if pending else None))
                    start += len(batch)
                if not queue:
                    break
                batch, pending, future = queue.popleft()
                if future:
                    report["written"] += vectors.write(pending, future.result())
                report["unchanged"] += len(batch) - len(pending)
                report["processed"] += len(batch)
                if (report["processed"] - len(batch)) % (args.batch_size * 10) == 0:
                    print(json.dumps({"processed": report["processed"], "selected": len(rows),
                                      "api_usage": usage()}), flush=True)
            report["state"] = "budget_stopped" if stopped else "complete_selected_scope"
    except BaseException:
        report["state"] = "interrupted"
        raise
    finally:
        report["api_usage"] = usage()
        report["reserved_upper_cost_usd"] = reserved_cost
        vectors.progress(state=report["state"], last_run_processed=report["processed"])
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=ROOT / "output/retrieval/rechtsgraph.sqlite")
    parser.add_argument("--document", action="append", help="Repeat for explicit document references.")
    parser.add_argument("--all-corpus", action="store_true", help="Explicitly select all corpora, bounded by --max-passages.")
    parser.add_argument("--max-passages", type=int, default=20000)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--workers", type=int, default=1, help="1–4 concurrent embedding batches; graph writes remain serial.")
    parser.add_argument("--budget-usd", type=float, default=1.0)
    parser.add_argument("--report", type=Path, default=ROOT / "output/embeddings/build-report.json")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    import math
    if args.max_passages < 1 or not 1 <= args.batch_size <= 256 or not 1 <= args.workers <= 4 or not math.isfinite(args.budget_usd) or args.budget_usd <= 0:
        parser.error("Use positive passage/budget limits and batch size 1–256.")
    if args.all_corpus and args.document:
        parser.error("Choose --all-corpus or --document, not both.")
    try:
        run(args)
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
