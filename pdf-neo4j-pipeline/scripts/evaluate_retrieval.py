#!/usr/bin/env python3
"""Evaluate lexical retrieval against explicitly reviewed reference evidence.

The input is JSON, either a list of cases or ``{"cases": [...]}``.  Each case
has an ``id``, ``query``, optional ``corpus``/``document_ref``, and
``reference_status`` (``reviewed`` or ``unreviewed``).  A reviewed case may
name one reference mode: ``relevant_passage_ids`` or ``relevant_citations``.
Cases without gold evidence are searched and counted for latency, but are
excluded from evidence metrics.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rechtsgraph_mcp.index import RetrievalIndex  # noqa: E402


MAX_K = 50


class CaseValidationError(ValueError):
    """Raised when a case file cannot support an auditable evaluation."""


def _citation_key(value: str) -> str:
    """Compare citation labels without erasing punctuation or number boundaries."""

    return " ".join(value.casefold().split())


def _citation_exists(index: RetrievalIndex, citation: str) -> bool:
    """Resolve a reviewed passage citation directly from read-only index metadata.

    This deliberately does not call FTS: whether a reference exists must not
    depend on the retriever or an arbitrary ranked-result cutoff.
    """

    key = _citation_key(citation)
    index.connection.create_function("citation_key", 1, lambda value: _citation_key(value or ""))
    row = index.connection.execute(
        """SELECT 1 FROM passages AS p
           WHERE citation_key(p.legal_citation) = ?
           LIMIT 1""",
        (key,),
    ).fetchone()
    return row is not None


def _case_list(payload: Any) -> list[dict[str, Any]]:
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise CaseValidationError("case file must be a JSON list or an object with a cases list")
    return cases


def _string_list(value: Any, name: str, case_id: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise CaseValidationError("case {!r}: {} must be a list of non-empty strings".format(case_id, name))
    values = [item.strip() for item in value]
    if len(set(values)) != len(values):
        raise CaseValidationError("case {!r}: {} must not contain duplicates".format(case_id, name))
    return values


def validate_cases(payload: Any, index: RetrievalIndex) -> list[dict[str, Any]]:
    """Validate case shape and passage identifiers through the public index API."""

    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_case in _case_list(payload):
        if not isinstance(raw_case, dict):
            raise CaseValidationError("every case must be an object")
        case_id, query, status = raw_case.get("id"), raw_case.get("query"), raw_case.get("reference_status")
        if not isinstance(case_id, str) or not case_id.strip():
            raise CaseValidationError("case id must be a non-empty string")
        case_id = case_id.strip()
        if case_id in seen_ids:
            raise CaseValidationError("case ids must be unique: {!r}".format(case_id))
        seen_ids.add(case_id)
        if not isinstance(query, str) or not query.strip():
            raise CaseValidationError("case {!r}: query must be a non-empty string".format(case_id))
        if status not in {"reviewed", "unreviewed"}:
            raise CaseValidationError("case {!r}: reference_status must be reviewed or unreviewed".format(case_id))
        for name in ("corpus", "document_ref"):
            if name in raw_case and (not isinstance(raw_case[name], str) or not raw_case[name].strip()):
                raise CaseValidationError("case {!r}: {} must be a non-empty string when supplied".format(case_id, name))
        has_passages = "relevant_passage_ids" in raw_case
        has_citations = "relevant_citations" in raw_case
        if has_passages and has_citations:
            raise CaseValidationError("case {!r}: use passage IDs or citations, not both".format(case_id))
        gold_mode: str | None
        gold: list[str]
        if has_passages:
            gold_mode, gold = "passage", _string_list(raw_case["relevant_passage_ids"], "relevant_passage_ids", case_id)
            # get_context is the public read-only way to resolve stored passage IDs.
            if status == "reviewed":
                for passage_id in gold:
                    if not index.get_context([passage_id], before=0, after=0)["passages"]:
                        raise CaseValidationError("case {!r}: unknown relevant passage ID {!r}".format(case_id, passage_id))
        elif has_citations:
            gold_mode, gold = "citation", _string_list(raw_case["relevant_citations"], "relevant_citations", case_id)
            if len({_citation_key(citation) for citation in gold}) != len(gold):
                raise CaseValidationError("case {!r}: relevant_citations must not contain normalized duplicates".format(case_id))
            if status == "reviewed":
                for citation in gold:
                    if not _citation_exists(index, citation):
                        raise CaseValidationError("case {!r}: unknown relevant citation {!r}".format(case_id, citation))
        else:
            gold_mode, gold = None, []
        validated.append({
            "id": case_id, "query": query.strip(), "reference_status": status,
            "corpus": raw_case.get("corpus", None), "document_ref": raw_case.get("document_ref", None),
            "gold_mode": gold_mode, "gold": gold,
        })
    return validated


def _matched_gold(case: dict[str, Any], results: Iterable[dict[str, Any]]) -> list[str]:
    if case["gold_mode"] == "passage":
        returned = {row["passage_id"] for row in results}
        return [item for item in case["gold"] if item in returned]
    if case["gold_mode"] == "citation":
        returned = {
            _citation_key(str(row.get("legal_citation", "")))
            for row in results
        }
        return [item for item in case["gold"] if _citation_key(item) in returned]
    return []


def evaluate(index: RetrievalIndex, cases: list[dict[str, Any]], k: int) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    latencies_ms: list[float] = []
    for case in cases:
        if case["reference_status"] == "unreviewed":
            records.append({**case, "skipped": True})
            continue
        started = time.perf_counter()
        results = index.search_corpus(case["query"], corpus=case["corpus"], document_ref=case["document_ref"], limit=k)
        latency_ms = (time.perf_counter() - started) * 1000
        latencies_ms.append(latency_ms)
        matched = _matched_gold(case, results)
        ranks = [
            position for position, row in enumerate(results, start=1)
            if (row["passage_id"] in set(case["gold"]) if case["gold_mode"] == "passage"
                else _citation_key(str(row.get("legal_citation", ""))) in {_citation_key(item) for item in case["gold"]})
        ]
        records.append({**case, "skipped": False, "latency_ms": latency_ms,
                        "results": results, "matched": matched, "first_rank": min(ranks) if ranks else None})

    scored = [record for record in records if not record["skipped"] and record["gold"]]
    total_gold = sum(len(record["gold"]) for record in scored)
    found_gold = sum(len(record["matched"]) for record in scored)
    reviewed = [record for record in records if not record["skipped"]]
    return {
        "k": k, "records": records,
        "scope": "Passage-retrieval evidence only; these metrics do not measure legal correctness or answer accuracy.",
        "counts": {
            "cases_loaded": len(cases), "reviewed_cases_searched": len(reviewed),
            "unreviewed_cases_skipped": sum(record["skipped"] for record in records),
            "reviewed_cases_with_gold": len(scored),
            "reviewed_cases_without_gold": len(reviewed) - len(scored),
            "reference_units": total_gold,
        },
        "metrics": {
            "recall_at_k": (found_gold / total_gold) if total_gold else None,
            "mean_reciprocal_rank": (sum(1 / record["first_rank"] if record["first_rank"] else 0 for record in scored) / len(scored)) if scored else None,
            "all_required_evidence_found": (sum(set(record["matched"]) == set(record["gold"]) for record in scored) / len(scored)) if scored else None,
            "latency_ms_mean": (sum(latencies_ms) / len(latencies_ms)) if latencies_ms else None,
            "latency_ms_max": max(latencies_ms) if latencies_ms else None,
        },
    }


def _number(value: float | None) -> str:
    return "n/a" if value is None else "{:.3f}".format(value)


def render_report(result: dict[str, Any], index_path: Path) -> str:
    counts, metrics = result["counts"], result["metrics"]
    lines = [
        "# Retrieval evaluation",
        "", "- Index: `{}`".format(index_path), "- k: `{}`".format(result["k"]), "",
        "## Counts", "", "| Measure | Value |", "| --- | ---: |",
    ]
    lines.extend("| {} | {} |".format(name.replace("_", " "), value) for name, value in counts.items())
    lines.extend(["", "## Metrics", "", "| Metric | Value |", "| --- | ---: |",
                  "| Recall@k | {} |".format(_number(metrics["recall_at_k"])),
                  "| Mean reciprocal rank | {} |".format(_number(metrics["mean_reciprocal_rank"])),
                  "| All required evidence found | {} |".format(_number(metrics["all_required_evidence_found"])),
                  "| Mean retrieval latency (ms) | {} |".format(_number(metrics["latency_ms_mean"])),
                  "| Max retrieval latency (ms) | {} |".format(_number(metrics["latency_ms_max"])),
                  "", "## Per reviewed case", "", "| Case | Gold mode | Found / required | First rank | Latency (ms) |",
                  "| --- | --- | ---: | ---: | ---: |"])
    for record in result["records"]:
        if record["skipped"]:
            continue
        required = len(record["gold"])
        lines.append("| {} | {} | {} / {} | {} | {} |".format(
            record["id"], record["gold_mode"] or "none", len(record["matched"]), required,
            record["first_rank"] or "n/a", _number(record["latency_ms"])))
    lines.extend(["", "Unreviewed cases were excluded from every metric. Reviewed cases without reference evidence are excluded from Recall@k, reciprocal-rank, and all-required-evidence denominators.",
                  "", "These are passage-retrieval measures only: finding a reviewed passage or citation does not establish legal correctness, interpretation, completeness, or a correct answer to a legal question.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path, help="Existing SQLite retrieval index (opened read-only).")
    parser.add_argument("--cases", required=True, type=Path, help="JSON evaluation cases.")
    parser.add_argument("--output", required=True, type=Path, help="Markdown report to write.")
    parser.add_argument("--k", type=int, default=10, help="Results per query (1-{}, default: 10).".format(MAX_K))
    args = parser.parse_args()
    if not 1 <= args.k <= MAX_K:
        parser.error("--k must be between 1 and {}".format(MAX_K))
    try:
        payload = json.loads(args.cases.read_text(encoding="utf-8"))
        with RetrievalIndex(args.index) as index:
            cases = validate_cases(payload, index)
            result = evaluate(index, cases, args.k)
        index_stat = args.index.stat()
        result["reproducibility"] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fixture_description": payload.get("description") if isinstance(payload, dict) else None,
            "index": {"path": str(args.index.resolve()), "size_bytes": index_stat.st_size,
                      "modified_at": datetime.fromtimestamp(index_stat.st_mtime, timezone.utc).isoformat()},
            "cases": {"path": str(args.cases.resolve()),
                      "sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest()},
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.suffix.casefold() == ".json":
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            args.output.write_text(render_report(result, args.index), encoding="utf-8")
    except (OSError, json.JSONDecodeError, CaseValidationError, ValueError) as error:
        parser.error(str(error))
    print("report: {}".format(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
