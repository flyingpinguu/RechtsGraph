#!/usr/bin/env python3
"""Compare placeholder references in two Neo4j CSV exports.

The audit mirrors the deterministic document-alias stage of the generated
EUR-Lex import. It reports raw placeholder edges separately from the subset
that can be closed to exactly one corpus document after all documents exist.
Chunk-level counts represent extracted mentions; unit/document counts are
roll-ups of those mentions.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Set, Tuple


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-csv-dir", type=Path, required=True)
    parser.add_argument("--candidate-csv-dir", type=Path, required=True)
    parser.add_argument(
        "--baseline-resolver-csv-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional document CSV export whose aliases are available after loading",
    )
    parser.add_argument(
        "--candidate-resolver-csv-dir",
        type=Path,
        action="append",
        default=[],
        help="Additional document CSV export whose aliases are available after loading",
    )
    parser.add_argument(
        "--baseline-resolver-only",
        action="store_true",
        help="Do not treat aliases from the baseline's own document CSV as a post-load resolver",
    )
    parser.add_argument(
        "--candidate-resolver-only",
        action="store_true",
        help="Do not treat aliases from the candidate's own document CSV as a post-load resolver",
    )
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    return parser.parse_args()


def document_aliases(csv_dir: Path) -> Dict[str, Set[str]]:
    document_files = sorted(csv_dir.glob("nodes_*_document.csv"))
    if len(document_files) != 1:
        raise ValueError(
            "expected one document CSV in {}, found {}".format(
                csv_dir, len(document_files)
            )
        )
    path = document_files[0]
    aliases: Dict[str, Set[str]] = defaultdict(set)
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            document_id = row.get("graph_id") or ""
            for alias in (row.get("citation_alias_keys") or "").split("|"):
                if alias:
                    aliases[alias].add(document_id)
            for field in ("document_key", "document_global_key", "global_key"):
                alias = row.get(field) or ""
                if alias:
                    aliases[alias].add(document_id)
    return aliases


def reference_level(row: Mapping[str, str]) -> str:
    start_id = row.get("start_node_id") or ""
    if start_id == row.get("source_chunk_id"):
        return "chunk"
    if start_id == row.get("source_unit_id"):
        return "unit"
    if start_id == row.get("source_document_id"):
        return "document"
    return "other"


def resolution_class(row: Mapping[str, str], aliases: Mapping[str, Set[str]]) -> str:
    if not (row.get("end_node_id") or "").startswith("ref_target_"):
        return "already_concrete"
    candidates = aliases.get(row.get("target_document_key") or "", set())
    if len(candidates) == 1:
        return "corpus_internal_unique_document"
    if len(candidates) > 1:
        return "ambiguous_document_alias"
    return "external_or_unmatched_document"


def occurrence_key(row: Mapping[str, str]) -> Tuple[str, str, str, str]:
    return (
        row.get("source_chunk_id") or "",
        row.get("char_start") or "",
        row.get("char_end") or "",
        row.get("mention_text") or "",
    )


def analyze(
    csv_dir: Path,
    resolver_csv_dirs: Iterable[Path] = (),
    include_own_aliases: bool = True,
) -> Tuple[Dict[str, Any], Dict[Tuple[str, str, str, str], Set[str]]]:
    aliases = document_aliases(csv_dir) if include_own_aliases else {}
    resolver_dirs = list(resolver_csv_dirs)
    for resolver_dir in resolver_dirs:
        for alias, document_ids in document_aliases(resolver_dir).items():
            aliases.setdefault(alias, set()).update(document_ids)
    path = csv_dir / "relationships_refers_to.csv"
    total = Counter()
    placeholders = Counter()
    classes = Counter()
    top_unmatched = Counter()
    unique_targets: MutableMapping[str, Set[str]] = defaultdict(set)
    occurrences: MutableMapping[Tuple[str, str, str, str], Set[str]] = defaultdict(set)

    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            level = reference_level(row)
            total[level] += 1
            if level == "chunk":
                occurrences[occurrence_key(row)].add(
                    row.get("requested_target_global_key") or row.get("target_global_key") or ""
                )
            cls = resolution_class(row, aliases)
            if cls == "already_concrete":
                continue
            placeholders[level] += 1
            classes[(level, cls)] += 1
            target_key = row.get("target_document_key") or ""
            unique_targets[level].add(target_key)
            if level == "chunk" and cls == "external_or_unmatched_document":
                top_unmatched[target_key] += 1

    levels = ("chunk", "unit", "document", "other")
    summary = {
        "csv_dir": str(csv_dir),
        "resolver_csv_dirs": [str(path) for path in resolver_dirs],
        "own_document_aliases_included": include_own_aliases,
        "reference_edges": {level: total[level] for level in levels},
        "raw_placeholder_edges": {level: placeholders[level] for level in levels},
        "unique_placeholder_document_keys": {
            level: len(unique_targets[level]) for level in levels
        },
        "placeholder_classes": {
            level: {
                cls: classes[(level, cls)]
                for cls in (
                    "corpus_internal_unique_document",
                    "ambiguous_document_alias",
                    "external_or_unmatched_document",
                )
            }
            for level in levels
        },
        "estimated_post_load_placeholder_edges": {
            level: classes[(level, "ambiguous_document_alias")]
            + classes[(level, "external_or_unmatched_document")]
            for level in levels
        },
        "top_unmatched_chunk_targets": top_unmatched.most_common(25),
    }
    return summary, dict(occurrences)


def write_markdown(
    path: Path,
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    changed_occurrences: int,
) -> None:
    lines = [
        "# Placeholder reference audit",
        "",
        "Chunk-level values are extracted citation mentions. Unit and document values are roll-up edges.",
        "The post-load estimate counts only aliases that identify exactly one corpus document.",
        "",
        "| Metric | Baseline | Candidate | Delta |",
        "|---|---:|---:|---:|",
    ]
    for label, field in (
        ("Chunk reference mentions", "reference_edges"),
        ("Raw chunk placeholder mentions", "raw_placeholder_edges"),
        ("Post-load chunk placeholders", "estimated_post_load_placeholder_edges"),
        ("All raw placeholder edges", "raw_placeholder_edges"),
        ("All post-load placeholder edges", "estimated_post_load_placeholder_edges"),
    ):
        level = "chunk" if "Chunk" in label or "chunk" in label else None
        if level:
            before_value = baseline[field][level]
            after_value = candidate[field][level]
        else:
            before_value = sum(baseline[field].values())
            after_value = sum(candidate[field].values())
        lines.append(
            "| {} | {:,} | {:,} | {:+,} |".format(
                label, before_value, after_value, after_value - before_value
            )
        )

    lines.extend(
        [
            "",
            "## Candidate placeholder classes",
            "",
            "| Class | Chunk mentions | All edges |",
            "|---|---:|---:|",
        ]
    )
    candidate_classes = candidate["placeholder_classes"]
    for label, key in (
        ("Corpus-internal, unique document", "corpus_internal_unique_document"),
        ("Ambiguous document alias", "ambiguous_document_alias"),
        ("External or unmatched document", "external_or_unmatched_document"),
    ):
        all_edges = sum(values[key] for values in candidate_classes.values())
        lines.append(
            "| {} | {:,} | {:,} |".format(
                label, candidate_classes["chunk"][key], all_edges
            )
        )

    lines.extend(
        [
            "",
            "## Changed extraction targets",
            "",
            "{:,.0f} chunk-level citation occurrences changed their requested target set.".format(
                changed_occurrences
            ),
            "",
            "## Most frequent remaining unmatched targets",
            "",
            "| Target key | Chunk mentions |",
            "|---|---:|",
        ]
    )
    for key, count in candidate["top_unmatched_chunk_targets"][:15]:
        lines.append("| `{}` | {:,} |".format(key or "(empty)", count))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    baseline, baseline_occurrences = analyze(
        args.baseline_csv_dir,
        args.baseline_resolver_csv_dir,
        not args.baseline_resolver_only,
    )
    candidate, candidate_occurrences = analyze(
        args.candidate_csv_dir,
        args.candidate_resolver_csv_dir,
        not args.candidate_resolver_only,
    )
    all_occurrences: Iterable[Tuple[str, str, str, str]] = (
        set(baseline_occurrences) | set(candidate_occurrences)
    )
    changed_occurrences = sum(
        baseline_occurrences.get(key, set()) != candidate_occurrences.get(key, set())
        for key in all_occurrences
    )
    result = {
        "baseline": baseline,
        "candidate": candidate,
        "changed_chunk_occurrences": changed_occurrences,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_markdown(args.output_md, baseline, candidate, changed_occurrences)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
