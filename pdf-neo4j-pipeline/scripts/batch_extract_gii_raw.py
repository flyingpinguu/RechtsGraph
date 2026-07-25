#!/usr/bin/env python3
"""Batch-extract raw normtext JSONs for downloaded Gesetze-im-Internet PDFs.

This runner keeps one raw JSON per source PDF so full-corpus runs are resumable
and problem documents can be inspected independently.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_MANIFEST = REPO_ROOT / "gesetze_im_internet_pdfs" / "manifest.json"
DEFAULT_PDF_DIR = REPO_ROOT / "gesetze_im_internet_pdfs"
DEFAULT_OUT_DIR = PROJECT_ROOT / "output" / "gii_full"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def manifest_key(entry: dict[str, Any]) -> str:
    return Path(entry["relative_file_path"]).stem


def raw_output_path(out_dir: Path, entry: dict[str, Any]) -> Path:
    return out_dir / "raw" / entry["category"] / "{}_raw.json".format(manifest_key(entry))


def pages_dir(out_dir: Path, entry: dict[str, Any]) -> Path:
    return out_dir / "pages" / entry["category"] / manifest_key(entry)


def select_entries(
    manifest: dict[str, Any],
    offset: int,
    limit: int | None,
) -> list[dict[str, Any]]:
    entries = [
        entry
        for entry in manifest.get("entries", [])
        if entry.get("status") in {"ok", "ok_existing"} and entry.get("relative_file_path")
    ]
    if offset:
        entries = entries[offset:]
    if limit is not None:
        entries = entries[:limit]
    return entries


def extract_one(task: dict[str, Any]) -> dict[str, Any]:
    # Import inside workers so process startup remains clean.
    sys.path.insert(0, str(PROJECT_ROOT))
    from normtext_extractor.pipeline import extract_document, load_rules  # noqa: WPS433

    entry = task["entry"]
    pdf_path = Path(task["pdf_dir"]) / entry["relative_file_path"]
    out_dir = Path(task["out_dir"])
    output_path = raw_output_path(out_dir, entry)
    page_root = pages_dir(out_dir, entry)
    output_base_dir = out_dir
    started = time.time()

    if task["resume"] and output_path.exists():
        return {
            "status": "skipped_existing",
            "manifest_key": manifest_key(entry),
            "category": entry["category"],
            "relative_file_path": entry["relative_file_path"],
            "output_path": str(output_path),
            "duration_seconds": 0,
        }

    try:
        rule_set = load_rules(task.get("rules_dir"))
        doc_obj, issues = extract_document(
            str(pdf_path),
            output_base_dir=str(output_base_dir),
            pages_root_dir=str(page_root),
            rule_set=rule_set,
        )
        payload = {
            "schema_version": "1.0.0-draft",
            "phase": "normtext",
            "source_manifest_entry": entry,
            "documents": [doc_obj],
            "review_decisions": [],
            "extraction_issues": issues,
        }
        write_json(output_path, payload)
        return {
            "status": "ok",
            "manifest_key": manifest_key(entry),
            "category": entry["category"],
            "relative_file_path": entry["relative_file_path"],
            "document_key": doc_obj.get("document_key"),
            "document_global_key": doc_obj.get("document_global_key"),
            "output_path": str(output_path),
            "pages_dir": str(page_root),
            "structural_units": len(doc_obj.get("structural_units") or []),
            "chunks": len(doc_obj.get("chunks") or []),
            "issues": len(issues),
            "duration_seconds": round(time.time() - started, 3),
        }
    except Exception as exc:  # keep full corpus run moving
        return {
            "status": "error",
            "manifest_key": manifest_key(entry),
            "category": entry.get("category"),
            "relative_file_path": entry.get("relative_file_path"),
            "output_path": str(output_path),
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "duration_seconds": round(time.time() - started, 3),
        }


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    ok_rows = [row for row in results if row["status"] in {"ok", "skipped_existing"}]
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "total_results": len(results),
        "status_counts": counts,
        "structural_units": sum(int(row.get("structural_units") or 0) for row in ok_rows),
        "chunks": sum(int(row.get("chunks") or 0) for row in ok_rows),
        "issues": sum(int(row.get("issues") or 0) for row in ok_rows),
    }


def write_report(path: Path, summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    lines = [
        "# GII Full Raw Extraction Report",
        "",
        f"- Generated: `{summary['generated_at']}`",
        f"- Total results: `{summary['total_results']}`",
        "",
        "## Status Counts",
        "",
    ]
    for status, count in sorted(summary["status_counts"].items()):
        lines.append(f"- `{status}`: `{count}`")
    lines.extend(
        [
            "",
            "## Totals",
            "",
            f"- Structural units: `{summary['structural_units']}`",
            f"- Chunks: `{summary['chunks']}`",
            f"- Extraction issues: `{summary['issues']}`",
            "",
            "## Errors",
            "",
        ]
    )
    errors = [row for row in results if row["status"] == "error"]
    if not errors:
        lines.append("No process errors.")
    for row in errors[:200]:
        lines.append(
            "- `{}` `{}`: {}".format(
                row.get("manifest_key"),
                row.get("relative_file_path"),
                row.get("error"),
            )
        )
    if len(errors) > 200:
        lines.append(f"- ... {len(errors) - 200} more")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--rules-dir", type=str, default=None)
    parser.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    manifest = load_json(args.manifest)
    entries = select_entries(manifest, args.offset, args.limit)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.out_dir / "logs" / "raw_extraction_results.jsonl"
    summary_path = args.out_dir / "logs" / "raw_extraction_summary.json"
    report_path = args.out_dir / "validation" / "raw_extraction_process_report.md"

    print(f"selected_entries={len(entries)}")
    print(f"workers={args.workers}")
    print(f"out_dir={args.out_dir}")
    print(f"log={log_path}")

    tasks = [
        {
            "entry": entry,
            "pdf_dir": str(args.pdf_dir),
            "out_dir": str(args.out_dir),
            "rules_dir": args.rules_dir,
            "resume": args.resume,
        }
        for entry in entries
    ]
    results: list[dict[str, Any]] = []
    completed = 0
    started = time.time()

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(extract_one, task): task for task in tasks}
        for future in as_completed(future_map):
            row = future.result()
            results.append(row)
            append_jsonl(log_path, row)
            completed += 1
            if completed <= 20 or completed % 25 == 0 or row["status"] == "error":
                elapsed = time.time() - started
                rate = completed / elapsed if elapsed else 0
                print(
                    "done {}/{} status={} key={} units={} chunks={} rate={:.2f}/s".format(
                        completed,
                        len(entries),
                        row.get("status"),
                        row.get("manifest_key"),
                        row.get("structural_units", ""),
                        row.get("chunks", ""),
                        rate,
                    ),
                    flush=True,
                )

    summary = summarize(results)
    write_json(summary_path, summary)
    write_report(report_path, summary, results)
    print(f"summary={summary_path}")
    print(f"report={report_path}")
    return 0 if not summary["status_counts"].get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
