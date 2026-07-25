#!/usr/bin/env python3
"""Batch-extract canonical raw JSON from a downloaded GII XML corpus.

The input is the manifest written by ``download_gii_xml.py``.  Only successful
download records are eligible.  Each record is dispatched to its downloaded
ZIP package when possible so referenced assets remain available to the XML
adapter; older manifests that expose only an XML member are also supported.

One raw JSON is written atomically per source record.  Individual extraction
failures are recorded in the aggregate manifest and never stop other records
from being processed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


TOOL_NAME = "batch_extract_gii_xml"
TOOL_VERSION = "1.0.0"
MANIFEST_SCHEMA_VERSION = "1.0"
SUCCESS_SOURCE_STATUSES = frozenset({"ok", "ok_existing"})

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_SOURCE_DIR = REPO_ROOT / "gesetze_im_internet_xml"
DEFAULT_MANIFEST = DEFAULT_SOURCE_DIR / "manifest.json"
DEFAULT_OUT_DIR = PROJECT_ROOT / "output" / "gii_xml"
DEFAULT_PDF_DIR = REPO_ROOT / "gesetze_im_internet_pdfs"


class BatchSourceError(RuntimeError):
    """Raised when the batch-level source or configuration is unusable."""


@dataclass(frozen=True)
class BatchConfig:
    manifest_path: Path = DEFAULT_MANIFEST
    source_dir: Optional[Path] = None
    output_dir: Path = DEFAULT_OUT_DIR
    pdf_dir: Optional[Path] = DEFAULT_PDF_DIR
    align_pdf: bool = True
    workers: int = max(1, min(4, os.cpu_count() or 1))
    limit: Optional[int] = None
    resume: bool = False
    force: bool = False


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchSourceError("cannot read JSON object {}: {}".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise BatchSourceError("{} must contain a JSON object".format(path))
    return value


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably replace ``path`` without exposing a partial JSON document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, str(path))
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_sort_key(indexed_entry: Tuple[int, Mapping[str, Any]]) -> Tuple[Any, ...]:
    index, entry = indexed_entry
    toc_index = _optional_int(entry.get("toc_index"))
    pdf_ordinal = _optional_int(entry.get("pdf_manifest_ordinal"))
    return (
        toc_index is None,
        toc_index if toc_index is not None else sys.maxsize,
        pdf_ordinal is None,
        pdf_ordinal if pdf_ordinal is not None else sys.maxsize,
        str(entry.get("slug") or "").casefold(),
        str(entry.get("item_id") or "").casefold(),
        index,
    )


def select_entries(
    manifest: Mapping[str, Any],
    limit: Optional[int] = None,
) -> List[Tuple[int, Dict[str, Any]]]:
    """Return successful source entries in a stable catalog-oriented order."""

    if limit is not None and limit < 0:
        raise ValueError("limit cannot be negative")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise BatchSourceError("XML corpus manifest has no entries list")
    selected = [
        (index, dict(entry))
        for index, entry in enumerate(entries)
        if isinstance(entry, dict)
        and str(entry.get("status") or "") in SUCCESS_SOURCE_STATUSES
    ]
    selected.sort(key=_entry_sort_key)
    if limit is not None:
        selected = selected[:limit]
    return selected


def _safe_component(value: Any, fallback: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return component or fallback


def entry_identifier(entry: Mapping[str, Any], source_index: int) -> str:
    explicit = _safe_component(entry.get("item_id"), "")
    if explicit:
        return explicit
    source_identity = "\0".join(
        [
            str(entry.get("law_key") or ""),
            str(entry.get("relative_archive_path") or ""),
            str(entry.get("relative_file_path") or ""),
            str(source_index),
        ]
    )
    slug = _safe_component(
        entry.get("slug") or entry.get("xml_document_number"),
        "document",
    )
    return "{}-{}".format(slug, sha256_bytes(source_identity.encode("utf-8"))[:12])


def _successful_pdf_matches(entry: Mapping[str, Any]) -> List[Dict[str, Any]]:
    matches = entry.get("pdf_manifest_matches")
    if not isinstance(matches, list):
        return []
    usable = [
        dict(match)
        for match in matches
        if isinstance(match, dict)
        and match.get("relative_file_path")
        and str(match.get("status") or "ok") in SUCCESS_SOURCE_STATUSES
    ]
    usable.sort(
        key=lambda match: (
            _optional_int(match.get("ordinal")) is None,
            _optional_int(match.get("ordinal"))
            if _optional_int(match.get("ordinal")) is not None
            else sys.maxsize,
            str(match.get("category") or "").casefold(),
            str(match.get("relative_file_path") or "").casefold(),
        )
    )
    return usable


def reconciled_source_manifest_entry(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Adapt the downloader record for the raw-schema compatibility contract.

    ``relative_file_path`` historically denotes a PDF in raw payloads.  The XML
    downloader uses that field for its primary XML member, so the XML value is
    moved to an explicit field.  A legacy-compatible PDF path is restored only
    when the downloader recorded a real PDF-manifest match.
    """

    adapted = dict(entry)
    xml_relative_path = adapted.pop("relative_file_path", None)
    if xml_relative_path:
        adapted["xml_relative_file_path"] = xml_relative_path
        adapted["source_xml"] = xml_relative_path
    archive_relative_path = adapted.get("relative_archive_path")
    if archive_relative_path:
        adapted["xml_package_relative_path"] = archive_relative_path
    adapted["source_kind"] = "gii_xml"

    matches = _successful_pdf_matches(entry)
    if matches:
        match = matches[0]
        pdf_relative_path = str(match["relative_file_path"])
        adapted["pdf_manifest_entry"] = match
        adapted["pdf_file"] = pdf_relative_path
        adapted["pdf_relative_path"] = pdf_relative_path
        adapted["source_pdf"] = pdf_relative_path
        adapted["relative_file_path"] = pdf_relative_path
    else:
        adapted.pop("pdf_file", None)
        adapted.pop("pdf_relative_path", None)
        adapted.pop("source_pdf", None)
    return adapted


def _safe_source_path(source_dir: Path, relative_path: str) -> Path:
    pure_path = PurePosixPath(relative_path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise ValueError("unsafe source manifest path {!r}".format(relative_path))
    root = source_dir.resolve()
    candidate = (root / Path(*pure_path.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "source manifest path escapes source directory: {!r}".format(relative_path)
        ) from exc
    return candidate


def source_package(
    entry: Mapping[str, Any],
    source_dir: Path,
) -> Tuple[Path, str, Optional[str]]:
    archive_path = str(entry.get("relative_archive_path") or "").strip()
    if archive_path:
        return (
            _safe_source_path(source_dir, archive_path),
            archive_path,
            str(entry.get("archive_sha256") or entry.get("sha256") or "") or None,
        )
    xml_path = str(entry.get("relative_file_path") or "").strip()
    if not xml_path:
        raise ValueError("source entry has no archive or XML relative path")
    return (
        _safe_source_path(source_dir, xml_path),
        xml_path,
        str(entry.get("xml_sha256") or "") or None,
    )


def output_path_for(
    output_dir: Path,
    entry: Mapping[str, Any],
    source_index: int,
) -> Path:
    matches = _successful_pdf_matches(entry)
    matched_category = matches[0].get("category") if matches else None
    category = _safe_component(
        entry.get("category") or matched_category,
        "_catalog",
    )
    identifier = entry_identifier(entry, source_index)
    return output_dir / "raw" / category / "{}_raw.json".format(identifier)


def _relative_output_path(output_dir: Path, output_path: Path) -> str:
    return output_path.resolve().relative_to(output_dir.resolve()).as_posix()


def _output_summary(
    payload: Mapping[str, Any],
    output_dir: Path,
    output_path: Path,
) -> Dict[str, Any]:
    documents = payload.get("documents")
    document = documents[0] if isinstance(documents, list) and documents else {}
    if not isinstance(document, dict):
        document = {}
    issues = payload.get("extraction_issues")
    return {
        "document_id": document.get("document_id"),
        "document_key": document.get("document_key"),
        "document_global_key": document.get("document_global_key"),
        "structural_units": len(document.get("structural_units") or []),
        "chunks": len(document.get("chunks") or []),
        "issues": len(issues) if isinstance(issues, list) else 0,
        "output_path": _relative_output_path(output_dir, output_path),
        "output_bytes": output_path.stat().st_size,
        "output_sha256": file_sha256(output_path),
    }


def validate_resumable_output(
    output_path: Path,
    entry: Mapping[str, Any],
    expected_package_sha256: Optional[str],
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, None, "existing output is unreadable: {}".format(exc)
    if not isinstance(payload, dict):
        return False, None, "existing output is not a JSON object"
    documents = payload.get("documents")
    if not isinstance(documents, list) or len(documents) != 1:
        return False, None, "existing output does not contain exactly one document"
    source_entry = payload.get("source_manifest_entry")
    if not isinstance(source_entry, dict):
        return False, None, "existing output lacks source_manifest_entry"
    expected_item_id = entry.get("item_id")
    if expected_item_id and source_entry.get("item_id") != expected_item_id:
        return False, None, "existing output belongs to another manifest entry"
    if expected_package_sha256:
        metadata = documents[0].get("metadata") if isinstance(documents[0], dict) else None
        actual_hash = (
            metadata.get("source_package_sha256")
            if isinstance(metadata, dict)
            else None
        )
        if actual_hash != expected_package_sha256:
            return False, None, "existing output source package hash changed"
    return True, payload, None


def _base_result(task: Mapping[str, Any]) -> Dict[str, Any]:
    entry = task["entry"]
    return {
        "selection_index": task["selection_index"],
        "source_index": task["source_index"],
        "item_id": entry_identifier(entry, task["source_index"]),
        "slug": entry.get("slug"),
        "title": entry.get("title"),
        "source_status": entry.get("status"),
        "source_relative_path": task["source_relative_path"],
        "source_package_sha256": task.get("source_package_sha256"),
        "pdf_relative_path": task.get("pdf_relative_path"),
    }


def _alignment_issue(
    document_id: str,
    issue_type: str,
    description: str,
    evidence: Any,
) -> Dict[str, Any]:
    return {
        "issue_id": "issue_{}".format(
            sha256_bytes(
                "{}\0{}\0{}".format(
                    document_id,
                    issue_type,
                    evidence,
                ).encode("utf-8")
            )[:12]
        ),
        "document_id": document_id,
        "unit_id": None,
        "chunk_id": None,
        "issue_type": issue_type,
        "severity": "warning",
        "description": description,
        "evidence": evidence,
        "corrected_value": None,
        "review_status": "open",
    }


def extract_one(task: Dict[str, Any]) -> Dict[str, Any]:
    """Run one isolated XML extraction task; always return a status record."""

    started = time.monotonic()
    entry = task["entry"]
    source_path = Path(task["source_path"])
    output_dir = Path(task["output_dir"])
    output_path = Path(task["output_path"])
    result = _base_result(task)

    try:
        if output_path.exists() and not task["force"]:
            if not task["resume"]:
                raise FileExistsError(
                    "{} already exists; use --resume or --force".format(output_path)
                )
            valid, payload, validation_error = validate_resumable_output(
                output_path,
                entry,
                task.get("source_package_sha256"),
            )
            if valid and payload is not None:
                result.update(
                    {
                        "status": "skipped_existing",
                        "resume_validation": "verified",
                        **_output_summary(payload, output_dir, output_path),
                    }
                )
                result["duration_seconds"] = round(time.monotonic() - started, 3)
                return result
            result["resume_validation"] = validation_error

        if not source_path.is_file():
            raise FileNotFoundError("source package does not exist: {}".format(source_path))

        # Import inside workers so process startup does not load the PDF pipeline.
        sys.path.insert(0, str(PROJECT_ROOT))
        from normtext_extractor.gii_xml import extract_package  # noqa: WPS433

        source_manifest_entry = reconciled_source_manifest_entry(entry)
        payload = extract_package(source_path, source_manifest_entry)
        if not isinstance(payload, dict):
            raise TypeError("XML extractor returned a non-object payload")
        documents = payload.get("documents")
        if not isinstance(documents, list) or len(documents) != 1:
            raise ValueError("XML extractor did not return exactly one document")
        expected_hash = task.get("source_package_sha256")
        metadata = (
            documents[0].get("metadata")
            if isinstance(documents[0], dict)
            else None
        )
        actual_hash = (
            metadata.get("source_package_sha256")
            if isinstance(metadata, dict)
            else None
        )
        if expected_hash and actual_hash != expected_hash:
            raise ValueError(
                "source package hash differs from the downloader manifest"
            )

        document = documents[0]
        pdf_path_value = task.get("pdf_path")
        pdf_relative_path = task.get("pdf_relative_path")
        if task.get("align_pdf") and pdf_relative_path:
            if pdf_path_value and Path(pdf_path_value).is_file():
                try:
                    from normtext_extractor.gii_pdf_alignment import (  # noqa: WPS433
                        align_document_with_pdf,
                    )

                    aligned_document, alignment_issues = align_document_with_pdf(
                        document,
                        Path(pdf_path_value),
                    )
                    payload["documents"][0] = aligned_document
                    payload.setdefault("extraction_issues", []).extend(
                        alignment_issues
                    )
                    document = aligned_document
                except Exception as alignment_exc:
                    document.setdefault("metadata", {})[
                        "pdf_alignment_status"
                    ] = "error"
                    payload.setdefault("extraction_issues", []).append(
                        _alignment_issue(
                            str(document.get("document_id") or ""),
                            "pdf_alignment_failed",
                            "Secondary PDF page alignment failed; XML extraction remains usable.",
                            {
                                "pdf_relative_path": pdf_relative_path,
                                "error_type": type(alignment_exc).__name__,
                                "error": str(alignment_exc),
                            },
                        )
                    )
            else:
                document.setdefault("metadata", {})[
                    "pdf_alignment_status"
                ] = "unavailable"
                payload.setdefault("extraction_issues", []).append(
                    _alignment_issue(
                        str(document.get("document_id") or ""),
                        "pdf_alignment_source_missing",
                        "Matched secondary PDF is missing; XML extraction remains usable.",
                        pdf_relative_path,
                    )
                )
        elif task.get("align_pdf"):
            document.setdefault("metadata", {})[
                "pdf_alignment_status"
            ] = "unavailable"
            document["metadata"]["pdf_alignment_reason"] = (
                "no_pdf_manifest_match"
            )

        atomic_write_json(output_path, payload)
        result.update(
            {
                "status": "ok",
                "pdf_alignment_status": document.get("metadata", {}).get(
                    "pdf_alignment_status"
                ),
                "pdf_pages": document.get("metadata", {}).get("pdf_pages", 0),
                **_output_summary(payload, output_dir, output_path),
            }
        )
    except Exception as exc:  # one source must never abort the corpus batch
        result.update(
            {
                "status": "error",
                "output_path": _relative_output_path(output_dir, output_path),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def _task_for(
    config: BatchConfig,
    source_dir: Path,
    selection_index: int,
    source_index: int,
    entry: Dict[str, Any],
) -> Dict[str, Any]:
    output_path = output_path_for(config.output_dir, entry, source_index)
    try:
        package_path, package_relative_path, package_sha256 = source_package(
            entry,
            source_dir,
        )
    except Exception as exc:
        # Keep path validation/source selection failures document-local.
        package_path = source_dir / "__invalid_source__"
        package_relative_path = str(
            entry.get("relative_archive_path")
            or entry.get("relative_file_path")
            or ""
        )
        package_sha256 = None
        source_setup_error = "{}: {}".format(type(exc).__name__, exc)
    else:
        source_setup_error = None
    pdf_relative_path: Optional[str] = None
    pdf_path: Optional[Path] = None
    pdf_matches = _successful_pdf_matches(entry)
    if config.align_pdf and pdf_matches:
        pdf_relative_path = str(pdf_matches[0]["relative_file_path"])
        if config.pdf_dir is not None:
            try:
                pdf_path = _safe_source_path(
                    config.pdf_dir,
                    pdf_relative_path,
                )
            except Exception:
                # PDF evidence is optional and must never invalidate XML input.
                pdf_path = None
    return {
        "entry": entry,
        "selection_index": selection_index,
        "source_index": source_index,
        "source_path": str(package_path),
        "source_relative_path": package_relative_path,
        "source_package_sha256": package_sha256,
        "source_setup_error": source_setup_error,
        "align_pdf": config.align_pdf,
        "pdf_path": str(pdf_path) if pdf_path is not None else None,
        "pdf_relative_path": pdf_relative_path,
        "output_dir": str(config.output_dir),
        "output_path": str(output_path),
        "resume": config.resume,
        "force": config.force,
    }


def _setup_error_result(task: Mapping[str, Any]) -> Dict[str, Any]:
    output_dir = Path(task["output_dir"])
    output_path = Path(task["output_path"])
    return {
        **_base_result(task),
        "status": "error",
        "output_path": _relative_output_path(output_dir, output_path),
        "error_type": "SourcePathError",
        "error": task["source_setup_error"],
        "duration_seconds": 0,
    }


def _worker_crash_result(task: Mapping[str, Any], exc: BaseException) -> Dict[str, Any]:
    output_dir = Path(task["output_dir"])
    output_path = Path(task["output_path"])
    return {
        **_base_result(task),
        "status": "error",
        "output_path": _relative_output_path(output_dir, output_path),
        "error_type": type(exc).__name__,
        "error": "worker failed before returning a result: {}".format(exc),
        "traceback": traceback.format_exc(),
        "duration_seconds": 0,
    }


def execute_tasks(
    tasks: Sequence[Dict[str, Any]],
    workers: int,
) -> List[Dict[str, Any]]:
    """Execute every task and return results in deterministic source order."""

    immediate = [
        _setup_error_result(task)
        for task in tasks
        if task.get("source_setup_error")
    ]
    runnable = [task for task in tasks if not task.get("source_setup_error")]
    results = list(immediate)

    if workers == 1:
        for task in runnable:
            results.append(extract_one(task))
    elif runnable:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(extract_one, task): task for task in runnable}
            for future in as_completed(future_map):
                task = future_map[future]
                try:
                    results.append(future.result())
                except BaseException as exc:
                    results.append(_worker_crash_result(task, exc))

    results.sort(
        key=lambda row: (
            _optional_int(row.get("selection_index"))
            if _optional_int(row.get("selection_index")) is not None
            else sys.maxsize,
            str(row.get("item_id") or ""),
        )
    )
    return results


def build_batch_manifest(
    config: BatchConfig,
    source_dir: Path,
    source_manifest: Mapping[str, Any],
    source_manifest_sha256: str,
    eligible_count: int,
    results: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    status_counts = dict(
        sorted(Counter(str(row.get("status") or "unknown") for row in results).items())
    )
    errors = [
        {
            "item_id": row.get("item_id"),
            "source_relative_path": row.get("source_relative_path"),
            "error_type": row.get("error_type"),
            "error": row.get("error"),
        }
        for row in results
        if row.get("status") == "error"
    ]
    script_path = Path(__file__).resolve()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source": "Gesetze-im-Internet XML raw extraction",
        "updated_at": utc_now(),
        "tool": {
            "name": TOOL_NAME,
            "version": TOOL_VERSION,
            "script": str(script_path),
            "script_sha256": file_sha256(script_path),
            "python": platform.python_version(),
        },
        "source_manifest": {
            "path": str(config.manifest_path.resolve()),
            "sha256": source_manifest_sha256,
            "schema_version": source_manifest.get("schema_version"),
            "updated_at": source_manifest.get("updated_at"),
            "source_dir": str(source_dir.resolve()),
            "eligible_entries": eligible_count,
        },
        "run": {
            "workers": config.workers,
            "align_pdf": config.align_pdf,
            "pdf_dir": (
                str(config.pdf_dir.resolve())
                if config.pdf_dir is not None
                else None
            ),
            "limit": config.limit,
            "resume": config.resume,
            "force": config.force,
            "selected_entries": len(results),
        },
        "status_counts": status_counts,
        "errors": errors,
        "entries": [dict(row) for row in results],
    }


def _run_locked(config: BatchConfig) -> Dict[str, Any]:
    if config.workers < 1:
        raise ValueError("workers must be at least 1")
    if config.limit is not None and config.limit < 0:
        raise ValueError("limit cannot be negative")
    if config.resume and config.force:
        raise ValueError("resume and force are mutually exclusive")

    aggregate_path = config.output_dir / "manifest.json"
    if aggregate_path.exists() and not config.resume and not config.force:
        raise BatchSourceError(
            "{} already exists; pass --resume to verify/reuse raw outputs or "
            "--force to regenerate them".format(aggregate_path)
        )

    source_manifest = load_json_object(config.manifest_path)
    all_eligible = select_entries(source_manifest)
    selected = all_eligible if config.limit is None else all_eligible[: config.limit]
    source_dir = (config.source_dir or config.manifest_path.parent).resolve()
    tasks = [
        _task_for(config, source_dir, selection_index, source_index, entry)
        for selection_index, (source_index, entry) in enumerate(selected)
    ]

    results = execute_tasks(tasks, config.workers)
    manifest = build_batch_manifest(
        config,
        source_dir,
        source_manifest,
        file_sha256(config.manifest_path),
        len(all_eligible),
        results,
    )
    atomic_write_json(aggregate_path, manifest)
    return manifest


def run(config: BatchConfig) -> Dict[str, Any]:
    """Run one batch under an exclusive output-directory lock."""

    config.output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.output_dir / ".batch_extract_gii_xml.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BatchSourceError(
                "another XML batch extractor is already using {}".format(
                    config.output_dir
                )
            ) from exc
        try:
            return _run_locked(config)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Root for manifest-relative ZIP/XML paths (default: manifest directory).",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=DEFAULT_PDF_DIR,
        help="Root for matched PDF paths (default: %(default)s).",
    )
    parser.add_argument(
        "--no-pdf-alignment",
        action="store_true",
        help="extract XML without adding secondary PDF page evidence",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(4, os.cpu_count() or 1)),
    )
    parser.add_argument("--limit", type=int, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--force", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = BatchConfig(
        manifest_path=args.manifest,
        source_dir=args.source_dir,
        output_dir=args.out_dir,
        pdf_dir=args.pdf_dir,
        align_pdf=not args.no_pdf_alignment,
        workers=args.workers,
        limit=args.limit,
        resume=args.resume,
        force=args.force,
    )
    try:
        manifest = run(config)
    except (BatchSourceError, OSError, ValueError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 2

    print("manifest={}".format(config.output_dir / "manifest.json"))
    print("selected_entries={}".format(manifest["run"]["selected_entries"]))
    for status, count in manifest["status_counts"].items():
        print("{}={}".format(status, count))
    return 1 if manifest["status_counts"].get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
