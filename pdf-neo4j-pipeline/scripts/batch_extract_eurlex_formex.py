#!/usr/bin/env python3
"""Batch-extract the standard EUR-Lex corpus into canonical raw JSON.

The Cellar register is produced by ``download_cellar_consolidated.py``.  This
batch selects its consolidated German Formex manifestations and, by default,
also exact referenced EU regulations and directives from
``external_documents``.  Original acts prefer Formex and use explicit
XHTML/PDF fallback adapters where the Publications Office offers no structured
package.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


TOOL_NAME = "batch_extract_eurlex_formex"
TOOL_VERSION = "1.1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_CORPUS_DIR = REPO_ROOT / "eurlex_consolidated_de"
DEFAULT_REGISTER = DEFAULT_CORPUS_DIR / "register.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output" / "eurlex_formex_rl"
DEFAULT_EXTERNAL_CORPUS_DIR = REPO_ROOT / "external_documents"
DEFAULT_EXTERNAL_REGISTER = DEFAULT_EXTERNAL_CORPUS_DIR / "register.sqlite"
DEFAULT_DESCRIPTORS = ("R", "L")
EXTERNAL_MEDIA_FORMATS = {
    "application/zip": "fmx4_zip",
    "application/pdf": "pdf",
    "application/xhtml+xml": "xhtml",
    "text/html": "html",
}
EXTRACTOR_VERSIONS = {
    "eurlex_formex": "1.1.0",
    "eurlex_original": "1.1.0",
}
EXTERNAL_ACT_METADATA = {
    "R": {"descriptor_label": "regulation", "legal_value": "REG"},
    "L": {"descriptor_label": "directive", "legal_value": "DIR"},
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("{} must contain a JSON object".format(path))
    return value


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name), suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _safe_source_path(corpus_dir: Path, local_path: str) -> Path:
    root = corpus_dir.resolve()
    candidate = (root / local_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("source path escapes corpus: {!r}".format(local_path)) from exc
    return candidate


def select_records(
    register: Mapping[str, Any],
    descriptors: Iterable[str],
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    allowed = {value.upper() for value in descriptors}
    documents = register.get("documents")
    if not isinstance(documents, list):
        raise ValueError("Cellar register has no documents list")
    selected = [
        dict(record)
        for record in documents
        if isinstance(record, dict)
        and record.get("status") == "downloaded"
        and record.get("manifestation_format") == "fmx4_zip"
        and str(record.get("descriptor") or "").upper() in allowed
    ]
    selected.sort(
        key=lambda record: (
            str(record.get("descriptor") or ""),
            str(record.get("base_celex") or ""),
            str(record.get("consolidation_date") or ""),
        )
    )
    if limit is not None:
        selected = selected[:limit]
    return selected


def select_external_legal_act_records(
    register_path: Path,
    corpus_dir: Path,
    descriptors: Iterable[str],
) -> List[Dict[str, Any]]:
    """Select exact, locally validated referenced EU regulations/directives."""

    allowed = {
        value.upper()
        for value in descriptors
        if value.upper() in EXTERNAL_ACT_METADATA
    }
    if not allowed or not register_path.is_file():
        return []
    connection = sqlite3.connect(str(register_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT d.id, d.title, d.short_title, d.corpus_status,
                   f.local_path, f.media_type, f.sha256, f.download_url,
                   f.final_url, f.retrieved_at
            FROM documents AS d
            JOIN files AS f ON f.document_id = d.id
            WHERE d.corpus_status = 'downloaded_original_eu_text'
              AND f.validation_status = 'valid'
              AND EXISTS (
                SELECT 1
                FROM graph_references AS g
                WHERE g.document_id = d.id
                  AND g.resolution_status = 'exact_celex_downloaded'
              )
            """
        ).fetchall()
    finally:
        connection.close()

    priority = {"fmx4_zip": 0, "xhtml": 1, "html": 2, "pdf": 3}
    selected_by_celex: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        celex = str(row["short_title"] or "").upper()
        match = re.fullmatch(r"3\d{4}([A-Z])\d{4}", celex)
        if not match or match.group(1) not in allowed:
            continue
        descriptor = match.group(1)
        act_metadata = EXTERNAL_ACT_METADATA[descriptor]
        media_type = str(row["media_type"] or "").casefold()
        manifestation_format = EXTERNAL_MEDIA_FORMATS.get(media_type)
        if not manifestation_format:
            continue
        relative = str(row["local_path"] or "")
        if not relative:
            continue
        source_path = _safe_source_path(corpus_dir, relative)
        if not source_path.is_file():
            continue
        record = {
            "base_celex": celex,
            "consolidated_celex": celex,
            "consolidation_date": None,
            "descriptor": descriptor,
            "descriptor_label": act_metadata["descriptor_label"],
            "legal_value": act_metadata["legal_value"],
            "language": "de",
            "status": "downloaded",
            "manifestation_format": manifestation_format,
            "local_path": relative,
            "download_url": row["download_url"] or row["final_url"],
            "title": row["title"] or celex,
            "sha256": row["sha256"],
            "retrieved_at": row["retrieved_at"],
            "media_type": media_type,
            "source_collection": "external_referenced_original",
            "_source_root": str(corpus_dir.resolve()),
        }
        previous = selected_by_celex.get(celex)
        if previous is None or priority[manifestation_format] < priority[
            str(previous["manifestation_format"])
        ]:
            selected_by_celex[celex] = record
    return sorted(selected_by_celex.values(), key=lambda record: record["base_celex"])


def select_external_regulation_records(
    register_path: Path,
    corpus_dir: Path,
    descriptors: Iterable[str],
) -> List[Dict[str, Any]]:
    """Compatibility wrapper retaining the former regulation-only selector."""

    requested = {value.upper() for value in descriptors}
    return select_external_legal_act_records(
        register_path,
        corpus_dir,
        ["R"] if "R" in requested else [],
    )


def combined_records(
    cellar_register: Mapping[str, Any],
    *,
    corpus_dir: Path,
    external_register: Path,
    external_corpus_dir: Path,
    descriptors: Iterable[str],
    include_external_legal_acts: bool,
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    records = select_records(cellar_register, descriptors, None)
    for record in records:
        record["source_collection"] = "cellar_consolidated"
        record["_source_root"] = str(corpus_dir.resolve())
    if include_external_legal_acts:
        records.extend(
            select_external_legal_act_records(
                external_register,
                external_corpus_dir,
                descriptors,
            )
        )
    records.sort(
        key=lambda record: (
            str(record.get("descriptor") or ""),
            str(record.get("base_celex") or ""),
            str(record.get("consolidation_date") or ""),
            str(record.get("source_collection") or ""),
        )
    )
    deduplicated: List[Dict[str, Any]] = []
    seen = set()
    for record in records:
        key = (
            str(record.get("base_celex") or ""),
            str(record.get("consolidated_celex") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(record)
    if limit is not None:
        return deduplicated[:limit]
    return deduplicated


def output_path(output_dir: Path, record: Mapping[str, Any]) -> Path:
    descriptor = str(record.get("descriptor") or "_")
    celex = str(record.get("consolidated_celex") or record.get("base_celex") or "unknown")
    return output_dir / "raw" / descriptor / "{}_raw.json".format(celex)


def _summary(payload: Mapping[str, Any], path: Path, output_dir: Path) -> Dict[str, Any]:
    document = (payload.get("documents") or [{}])[0]
    metadata = document.get("metadata") or {}
    return {
        "document_id": document.get("document_id"),
        "document_key": document.get("document_key"),
        "document_global_key": document.get("document_global_key"),
        "structural_units": len(document.get("structural_units") or []),
        "chunks": len(document.get("chunks") or []),
        "articles": metadata.get("xml_article_count", metadata.get("article_count", 0)),
        "annexes": metadata.get("xml_annex_count", metadata.get("annex_count", 0)),
        "tables": metadata.get("xml_table_count", metadata.get("table_count", 0)),
        "issues": len(payload.get("extraction_issues") or []),
        "output_path": path.resolve().relative_to(output_dir.resolve()).as_posix(),
        "output_bytes": path.stat().st_size,
        "output_sha256": file_sha256(path),
    }


def _valid_resume(
    path: Path,
    source_path: Path,
    record: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    try:
        payload = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    extractor = payload.get("extractor") or {}
    if extractor.get("version") != EXTRACTOR_VERSIONS.get(extractor.get("name")):
        return None
    source = payload.get("source_manifest_entry") or {}
    if source.get("consolidated_celex") != record.get("consolidated_celex"):
        return None
    document = (payload.get("documents") or [{}])[0]
    metadata = document.get("metadata") or {}
    recorded_hash = metadata.get("source_package_sha256") or metadata.get("source_file_sha256")
    if recorded_hash != file_sha256(source_path):
        return None
    return payload


def extract_one(task: Mapping[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    record = dict(task["record"])
    source_path = Path(task["source_path"])
    destination = Path(task["output_path"])
    output_dir = Path(task["output_dir"])
    result = {
        "selection_index": task["selection_index"],
        "base_celex": record.get("base_celex"),
        "consolidated_celex": record.get("consolidated_celex"),
        "descriptor": record.get("descriptor"),
        "source_collection": record.get("source_collection"),
        "manifestation_format": record.get("manifestation_format"),
        "source_path": record.get("local_path"),
    }
    try:
        if destination.exists() and not task["force"]:
            if not task["resume"]:
                raise FileExistsError(
                    "{} exists; use --resume or --force".format(destination)
                )
            payload = _valid_resume(destination, source_path, record)
            if payload is not None:
                result.update({"status": "skipped_existing", **_summary(payload, destination, output_dir)})
                result["duration_seconds"] = round(time.monotonic() - started, 3)
                return result

        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        sys.path.insert(0, str(PROJECT_ROOT))
        public_record = {key: value for key, value in record.items() if not key.startswith("_")}
        if record.get("manifestation_format") == "fmx4_zip":
            from normtext_extractor.eurlex_formex import (  # noqa: WPS433
                EURLEX_FORMEX_EXTRACTOR_VERSION,
                extract_package,
            )

            if EURLEX_FORMEX_EXTRACTOR_VERSION != EXTRACTOR_VERSIONS["eurlex_formex"]:
                raise RuntimeError("unexpected Formex extractor version")
            payload = extract_package(source_path, public_record)
        else:
            from normtext_extractor.eurlex_original import (  # noqa: WPS433
                EURLEX_ORIGINAL_EXTRACTOR_VERSION,
                extract_source,
            )

            if EURLEX_ORIGINAL_EXTRACTOR_VERSION != EXTRACTOR_VERSIONS["eurlex_original"]:
                raise RuntimeError("unexpected original EUR-Lex extractor version")
            payload = extract_source(source_path, public_record)
        payload["batch_extractor"] = {"name": TOOL_NAME, "version": TOOL_VERSION}
        atomic_write_json(destination, payload)
        result.update({"status": "ok", **_summary(payload, destination, output_dir)})
    except BaseException as exc:
        result.update(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(limit=20),
            }
        )
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def build_manifest(
    *,
    register_path: Path,
    external_register_path: Path,
    corpus_dir: Path,
    external_corpus_dir: Path,
    output_dir: Path,
    descriptors: Sequence[str],
    workers: int,
    results: Sequence[Mapping[str, Any]],
    started_at: str,
) -> Dict[str, Any]:
    status_counts = Counter(str(result.get("status") or "unknown") for result in results)
    descriptor_counts = Counter(str(result.get("descriptor") or "unknown") for result in results)
    source_counts = Counter(str(result.get("source_collection") or "unknown") for result in results)
    format_counts = Counter(str(result.get("manifestation_format") or "unknown") for result in results)
    successful = [result for result in results if result.get("status") in {"ok", "skipped_existing"}]
    return {
        "schema_version": "1.0",
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "started_at": started_at,
        "completed_at": utc_now(),
        "register_path": str(register_path),
        "external_register_path": str(external_register_path),
        "corpus_dir": str(corpus_dir),
        "external_corpus_dir": str(external_corpus_dir),
        "output_dir": str(output_dir),
        "descriptors": list(descriptors),
        "workers": workers,
        "summary": {
            "selected": len(results),
            "successful": len(successful),
            "status_counts": dict(sorted(status_counts.items())),
            "descriptor_counts": dict(sorted(descriptor_counts.items())),
            "source_counts": dict(sorted(source_counts.items())),
            "manifestation_format_counts": dict(sorted(format_counts.items())),
            "structural_units": sum(int(item.get("structural_units") or 0) for item in successful),
            "chunks": sum(int(item.get("chunks") or 0) for item in successful),
            "articles": sum(int(item.get("articles") or 0) for item in successful),
            "annexes": sum(int(item.get("annexes") or 0) for item in successful),
            "tables": sum(int(item.get("tables") or 0) for item in successful),
            "issues": sum(int(item.get("issues") or 0) for item in successful),
            "output_bytes": sum(int(item.get("output_bytes") or 0) for item in successful),
        },
        "results": list(results),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", type=Path, default=DEFAULT_REGISTER)
    parser.add_argument("--corpus-dir", type=Path, default=DEFAULT_CORPUS_DIR)
    parser.add_argument("--external-register", type=Path, default=DEFAULT_EXTERNAL_REGISTER)
    parser.add_argument("--external-corpus-dir", type=Path, default=DEFAULT_EXTERNAL_CORPUS_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--descriptors", nargs="+", default=list(DEFAULT_DESCRIPTORS))
    parser.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--no-external-legal-acts",
        action="store_true",
        help="Exclude exact referenced regulations/directives from external_documents.",
    )
    parser.add_argument(
        "--no-external-regulations",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be positive")
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit cannot be negative")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = args.output_dir / ".batch_extract_eurlex_formex.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("another EUR-Lex Formex batch is running") from exc

        started_at = utc_now()
        register = load_json(args.register)
        records = combined_records(
            register,
            corpus_dir=args.corpus_dir,
            external_register=args.external_register,
            external_corpus_dir=args.external_corpus_dir,
            descriptors=args.descriptors,
            include_external_legal_acts=not (
                args.no_external_legal_acts or args.no_external_regulations
            ),
            limit=args.limit,
        )
        tasks = []
        for index, record in enumerate(records):
            local_path = str(record.get("local_path") or "")
            if not local_path:
                continue
            source_root = Path(str(record.get("_source_root") or args.corpus_dir))
            source = _safe_source_path(source_root, local_path)
            tasks.append(
                {
                    "selection_index": index,
                    "record": record,
                    "source_path": str(source),
                    "output_path": str(output_path(args.output_dir, record)),
                    "output_dir": str(args.output_dir),
                    "resume": args.resume,
                    "force": args.force,
                }
            )

        results: List[Dict[str, Any]] = []

        def report_progress(result: Mapping[str, Any]) -> None:
            completed = len(results)
            if (
                completed % 50 == 0
                or completed == len(tasks)
                or result.get("status") == "error"
            ):
                print(
                    "[{}/{}] {} {}".format(
                        completed,
                        len(tasks),
                        result.get("status"),
                        result.get("consolidated_celex"),
                    ),
                    flush=True,
                )

        if args.workers == 1:
            for task in tasks:
                result = extract_one(task)
                results.append(result)
                report_progress(result)
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(extract_one, task): task for task in tasks}
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    report_progress(result)
        results.sort(key=lambda item: int(item.get("selection_index") or 0))
        manifest = build_manifest(
            register_path=args.register,
            external_register_path=args.external_register,
            corpus_dir=args.corpus_dir,
            external_corpus_dir=args.external_corpus_dir,
            output_dir=args.output_dir,
            descriptors=args.descriptors,
            workers=args.workers,
            results=results,
            started_at=started_at,
        )
        atomic_write_json(args.output_dir / "manifest.json", manifest)
        print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))
        if manifest["summary"]["status_counts"].get("error"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
