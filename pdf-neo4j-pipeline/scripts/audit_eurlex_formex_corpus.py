#!/usr/bin/env python3
"""Audit a completed EUR-Lex Formex extraction against its Cellar register."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_EXTRACTION_DIR = PROJECT_ROOT / "output" / "eurlex_formex_rl"
DEFAULT_REGISTER = REPO_ROOT / "eurlex_consolidated_de" / "register.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def issue(
    findings: list,
    code: str,
    celex: str,
    detail: str,
) -> None:
    findings.append({"code": code, "consolidated_celex": celex, "detail": detail})


def audit(
    extraction_dir: Path,
    register_path: Path,
    verify_hashes: bool,
) -> Dict[str, Any]:
    manifest_path = extraction_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    register = json.loads(register_path.read_text(encoding="utf-8"))
    expected = {
        record["consolidated_celex"]
        for record in register.get("documents") or []
        if record.get("status") == "downloaded"
        and record.get("manifestation_format") == "fmx4_zip"
        and record.get("descriptor") in {"R", "L"}
    }
    results = [
        result
        for result in manifest.get("results") or []
        if result.get("status") in {"ok", "skipped_existing"}
    ]
    actual = {str(result.get("consolidated_celex")) for result in results}
    findings = []
    counts = Counter()
    unit_types = Counter()
    chunk_types = Counter()
    issue_types = Counter()
    legal_statuses = Counter()
    descriptors = Counter()
    placeholder_documents = 0
    oversized_atomic_rows = 0

    for missing in sorted(expected - actual):
        issue(findings, "missing_document", missing, "Expected Formex document has no successful extraction")
    for unexpected in sorted(actual - expected):
        issue(findings, "unexpected_document", unexpected, "Extraction is not in the selected register scope")

    for result in results:
        celex = str(result.get("consolidated_celex") or "")
        path = extraction_dir / result["output_path"]
        if not path.is_file():
            issue(findings, "missing_output", celex, str(path))
            continue
        if verify_hashes and file_sha256(path) != result.get("output_sha256"):
            issue(findings, "output_hash_mismatch", celex, str(path))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            issue(findings, "unreadable_output", celex, str(exc))
            continue
        extractor = payload.get("extractor") or {}
        if extractor.get("name") != "eurlex_formex":
            issue(findings, "wrong_extractor", celex, repr(extractor))
        documents = payload.get("documents") or []
        if len(documents) != 1:
            issue(findings, "wrong_document_count", celex, str(len(documents)))
            continue
        document = documents[0]
        metadata = document.get("metadata") or {}
        if metadata.get("consolidated_celex") != celex:
            issue(findings, "celex_mismatch", celex, repr(metadata.get("consolidated_celex")))
        units = document.get("structural_units") or []
        chunks = document.get("chunks") or []
        if len(units) != int(result.get("structural_units") or 0):
            issue(findings, "unit_count_mismatch", celex, "manifest={} raw={}".format(result.get("structural_units"), len(units)))
        if len(chunks) != int(result.get("chunks") or 0):
            issue(findings, "chunk_count_mismatch", celex, "manifest={} raw={}".format(result.get("chunks"), len(chunks)))

        unit_ids = [unit.get("unit_id") for unit in units]
        chunk_ids = [chunk.get("chunk_id") for chunk in chunks]
        unit_keys = [unit.get("global_key") for unit in units]
        chunk_keys = [chunk.get("global_key") for chunk in chunks]
        if len(set(unit_ids)) != len(unit_ids):
            issue(findings, "duplicate_unit_id", celex, "duplicate unit ids")
        if len(set(chunk_ids)) != len(chunk_ids):
            issue(findings, "duplicate_chunk_id", celex, "duplicate chunk ids")
        if len(set(unit_keys)) != len(unit_keys):
            issue(findings, "duplicate_unit_global_key", celex, "duplicate unit global keys")
        if len(set(chunk_keys)) != len(chunk_keys):
            issue(findings, "duplicate_chunk_global_key", celex, "duplicate chunk global keys")
        unit_id_set = set(unit_ids)
        chunk_id_set = set(chunk_ids)
        for unit in units:
            parent = unit.get("parent_unit_id")
            if parent and parent not in unit_id_set:
                issue(findings, "dangling_parent_unit", celex, "{} -> {}".format(unit.get("unit_id"), parent))
            for child in unit.get("child_unit_ids") or []:
                if child not in unit_id_set:
                    issue(findings, "dangling_child_unit", celex, "{} -> {}".format(unit.get("unit_id"), child))
            unit_types[str(unit.get("unit_type") or "unknown")] += 1
        for chunk in chunks:
            if chunk.get("unit_id") not in unit_id_set:
                issue(findings, "dangling_chunk_unit", celex, "{} -> {}".format(chunk.get("chunk_id"), chunk.get("unit_id")))
            parent = chunk.get("parent_chunk_id")
            if parent and parent not in chunk_id_set:
                issue(findings, "dangling_parent_chunk", celex, "{} -> {}".format(chunk.get("chunk_id"), parent))
            if not str(chunk.get("text") or "").strip():
                issue(findings, "empty_chunk", celex, str(chunk.get("chunk_id")))
            token_value = chunk.get("table_chunk_token_count")
            token_limit = chunk.get("table_chunk_max_tokens")
            if token_value is not None and token_limit is not None and int(token_value) > int(token_limit):
                if chunk.get("oversized_atomic_row"):
                    oversized_atomic_rows += 1
                else:
                    issue(findings, "table_chunk_over_limit", celex, "{} > {} ({})".format(token_value, token_limit, chunk.get("chunk_id")))
            chunk_types[str(chunk.get("chunk_type") or "unknown")] += 1
        for extraction_issue in payload.get("extraction_issues") or []:
            issue_type = str(extraction_issue.get("issue_type") or "unknown")
            issue_types[issue_type] += 1
            if issue_type == "formex_placeholder_text":
                placeholder_documents += 1
        legal_statuses[str(metadata.get("legal_status") or "unknown")] += 1
        descriptors[str(metadata.get("descriptor") or "unknown")] += 1
        counts["documents"] += 1
        counts["units"] += len(units)
        counts["chunks"] += len(chunks)

    hard_finding_codes = {
        "missing_document",
        "unexpected_document",
        "missing_output",
        "output_hash_mismatch",
        "unreadable_output",
        "wrong_extractor",
        "wrong_document_count",
        "celex_mismatch",
        "unit_count_mismatch",
        "chunk_count_mismatch",
        "duplicate_unit_id",
        "duplicate_chunk_id",
        "duplicate_unit_global_key",
        "duplicate_chunk_global_key",
        "dangling_parent_unit",
        "dangling_child_unit",
        "dangling_chunk_unit",
        "dangling_parent_chunk",
        "empty_chunk",
        "table_chunk_over_limit",
    }
    hard_findings = [item for item in findings if item["code"] in hard_finding_codes]
    return {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_manifest": str(manifest_path),
        "source_register": str(register_path),
        "verify_hashes": verify_hashes,
        "passed": not hard_findings,
        "counts": dict(counts),
        "expected_documents": len(expected),
        "descriptors": dict(sorted(descriptors.items())),
        "legal_statuses": dict(sorted(legal_statuses.items())),
        "unit_types": dict(sorted(unit_types.items())),
        "chunk_types": dict(sorted(chunk_types.items())),
        "extraction_issue_types": dict(sorted(issue_types.items())),
        "placeholder_documents": placeholder_documents,
        "oversized_atomic_table_rows": oversized_atomic_rows,
        "hard_finding_count": len(hard_findings),
        "finding_count": len(findings),
        "findings": findings,
    }


def markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# EUR-Lex Formex corpus audit",
        "",
        "- Result: **{}**".format("PASS" if report.get("passed") else "FAIL"),
        "- Documents: {} / {}".format((report.get("counts") or {}).get("documents", 0), report.get("expected_documents", 0)),
        "- Structural units: {}".format((report.get("counts") or {}).get("units", 0)),
        "- Chunks: {}".format((report.get("counts") or {}).get("chunks", 0)),
        "- Hard findings: {}".format(report.get("hard_finding_count", 0)),
        "- Placeholder documents: {}".format(report.get("placeholder_documents", 0)),
        "- Oversized atomic table rows: {}".format(report.get("oversized_atomic_table_rows", 0)),
        "",
        "## Legal status",
        "",
    ]
    for key, value in (report.get("legal_statuses") or {}).items():
        lines.append("- {}: {}".format(key, value))
    lines.extend(["", "## Modeled unit types", ""])
    for key, value in (report.get("unit_types") or {}).items():
        lines.append("- {}: {}".format(key, value))
    lines.extend(["", "## Extraction warnings", ""])
    if report.get("extraction_issue_types"):
        for key, value in report["extraction_issue_types"].items():
            lines.append("- {}: {}".format(key, value))
    else:
        lines.append("- none")
    if report.get("findings"):
        lines.extend(["", "## Findings", ""])
        for finding in report["findings"][:200]:
            lines.append("- `{}` {}: {}".format(finding["code"], finding["consolidated_celex"], finding["detail"]))
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction-dir", type=Path, default=DEFAULT_EXTRACTION_DIR)
    parser.add_argument("--register", type=Path, default=DEFAULT_REGISTER)
    parser.add_argument("--skip-hashes", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = audit(args.extraction_dir, args.register, not args.skip_hashes)
    (args.extraction_dir / "audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.extraction_dir / "audit.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("passed", "counts", "expected_documents", "hard_finding_count", "placeholder_documents", "oversized_atomic_table_rows")}, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
