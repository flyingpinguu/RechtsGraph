#!/usr/bin/env python3
"""Three-query engineering retrieval smoke; live query embeddings require --execute."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rechtsgraph_mcp.embeddings import CachedEmbedder, EmbeddingSettings, OpenRouterEmbedder
from rechtsgraph_mcp.index import RetrievalIndex
from rechtsgraph_mcp.vector_search import search

ROOT = Path(__file__).resolve().parents[1]
TOP_K = 5
MODES = ("lexical", "semantic", "hybrid")
CASES = (
    {"id": "dismissal_deadline", "document": "KSchG", "unit": "§ 4",
     "query": "Mein Arbeitgeber hat mich entlassen. Wie lange habe ich Zeit, mich dagegen beim Arbeitsgericht zu wehren?"},
    {"id": "purchase_obligations", "document": "BGB", "unit": "§ 433",
     "query": "Was müssen Verkäufer und Käufer nach einem Kaufvertrag jeweils leisten?"},
    {"id": "personal_data_erasure", "document": "32016R0679", "unit": "Art. 17",
     "query": "Unter welchen Voraussetzungen kann ich verlangen, dass eine Firma meine personenbezogenen Daten entfernt?"},
)


def resolve_cases(index):
    resolved = []
    for case in CASES:
        unit = index.get_structural_unit(case["document"], case["unit"],
                                         max_passages=1000, max_chars=1_000_000)
        expected = sorted({row["passage_id"] for row in unit["passages"]})
        if not unit.get("unit") or not expected or unit.get("truncated"):
            raise ValueError("Missing or truncated expected canonical unit: {} {}".format(case["document"], case["unit"]))
        resolved.append(dict(case, document_id=unit["document"]["document_id"],
                             unit_id=unit["unit"]["unit_id"], expected_passage_ids=expected))
    return resolved


def markdown_report(report):
    lines = ["# Engineering retrieval smoke", "",
             "Three fixed paraphrase queries; any expected canonical passage in the first five counts as a hit.",
             "This is not human-reviewed legal quality or answer correctness. Latency is diagnostic only.", "",
             "Model: `{}`; profile: `{}`; fixture SHA-256: `{}`.".format(
                 report["model"], report["profile"], report["fixture_sha256"]), "",
             "| Query | Requested mode | Effective mode | Hit@5 | First expected rank | Latency ms |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in report["checks"]:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            row["case_id"], row["requested_mode"], row.get("effective_mode", "error"),
            row.get("hit_at_5", "n/a"), row.get("first_expected_rank") or "—", row["latency_ms"]))
    for row in report["checks"]:
        if row.get("warnings") or row.get("error"):
            lines.extend(["", "{} / {}: {}".format(row["case_id"], row["requested_mode"],
                row.get("error") or "; ".join(row["warnings"]))])
    lines.extend(["", "JSON contains expected IDs, ranked hits, coverage and provider usage. A lexical fallback is not a semantic/hybrid success.", ""])
    return "\n".join(lines)


def run(args):
    settings = EmbeddingSettings.from_environment()
    with RetrievalIndex(args.index) as index:
        cases = resolve_cases(index)
        fixture_hash = hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        report = {
            "schema_version": 1, "kind": "engineering_retrieval_smoke",
            "created_at": datetime.now(timezone.utc).isoformat(), "execute": args.execute,
            "index_path": str(args.index.resolve()), "index_size_bytes": args.index.stat().st_size,
            "model": settings.model, "dimensions": settings.dimensions, "profile": settings.profile,
            "fixture_sha256": fixture_hash, "top_k": TOP_K, "modes": list(MODES),
            "cases": cases, "checks": [],
            "note": "Three fixed queries, no answer generation or judge. Embedding cache reused; approximate retrieval, partial vector coverage possible. No legal quality assertion.",
        }
        if not args.execute:
            return report
        api = OpenRouterEmbedder(settings)
        cache = CachedEmbedder(api)
        try:
            for case in cases:
                for mode in MODES:
                    started = perf_counter()
                    row = {"case_id": case["id"], "requested_mode": mode}
                    try:
                        result = search(index, case["query"], mode=mode, limit=TOP_K, embedder=cache)
                        hits = result["results"][:TOP_K]
                        ranks = [rank for rank, hit in enumerate(hits, 1)
                                 if hit["passage_id"] in case["expected_passage_ids"]]
                        row.update(effective_mode=result["mode"], hit_at_5=bool(ranks),
                                   first_expected_rank=min(ranks) if ranks else None,
                                   results=[{"rank": rank, "passage_id": hit["passage_id"],
                                             "legal_citation": hit.get("legal_citation"),
                                             "score": hit.get("score")} for rank, hit in enumerate(hits, 1)],
                                   warnings=result.get("warnings", []), coverage=result.get("coverage"))
                    except (RuntimeError, ValueError, OSError, sqlite3.Error) as error:
                        row["error"] = str(error)
                    row["latency_ms"] = round((perf_counter() - started) * 1000, 2)
                    report["checks"].append(row)
        finally:
            cache.close()
        report["api_usage"] = api.usage
        report["complete"] = all(not row.get("error") and row.get("effective_mode") == row["requested_mode"]
                                 for row in report["checks"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    prefix = args.output_prefix or ROOT / "output/embeddings" / ("retrieval-smoke-" + stamp)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    report["report_json"] = str(prefix.with_suffix(".json").resolve())
    report["report_markdown"] = str(prefix.with_suffix(".md").resolve())
    prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    prefix.with_suffix(".md").write_text(markdown_report(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=ROOT / "output/retrieval/rechtsgraph.sqlite")
    parser.add_argument("--execute", action="store_true", help="Allow query embedding API requests; cached queries are reused.")
    parser.add_argument("--output-prefix", type=Path, help="Optional JSON/Markdown output stem; default is a new timestamped report.")
    args = parser.parse_args()
    try:
        report = run(args)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not args.execute or report["complete"] else 1
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
