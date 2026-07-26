#!/usr/bin/env python3
"""Extract the explicit PDF-only tail of the official GII XML manifest.

The normal corpus path is the GII XML adapter.  This runner is intentionally
narrow: it selects only downloader-manifest entries reconciled as
``pdf_manifest_only`` that contain no downloaded XML source.  Their successful,
hash-addressed PDF-manifest match is then processed with the deterministic
legacy PDF extractor.

Each source is isolated, verified for path containment and SHA-256 integrity,
and written to its own atomic raw JSON.  A failure never prevents the remaining
fallbacks from running.  Existing raw outputs are reused only after source and
provenance validation with ``--resume``.
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
import shutil
import stat
import sys
import tempfile
import time
import traceback
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


TOOL_NAME = "batch_extract_gii_pdf_fallbacks"
TOOL_VERSION = "1.1.0"
MANIFEST_SCHEMA_VERSION = "1.0"
SOURCE_KIND = "gii_pdf_fallback"
PDF_ONLY_RECONCILIATION_STATUS = "pdf_manifest_only"
SUCCESS_PDF_STATUSES = frozenset({"ok", "ok_existing"})
PAGE_CONTENT_HASH_FIELD = "content_sha256"
RULES_CONFIGURATION_SCHEMA_VERSION = "1.0"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_MANIFEST = REPO_ROOT / "gesetze_im_internet_xml" / "manifest.json"
DEFAULT_PDF_DIR = REPO_ROOT / "gesetze_im_internet_pdfs"
DEFAULT_OUT_DIR = PROJECT_ROOT / "output" / "gii_pdf_fallbacks"


class BatchSourceError(RuntimeError):
    """Raised when batch-level configuration or input is unusable."""


@dataclass(frozen=True)
class BatchConfig:
    manifest_path: Path = DEFAULT_MANIFEST
    pdf_dir: Path = DEFAULT_PDF_DIR
    output_dir: Path = DEFAULT_OUT_DIR
    rules_dir: Optional[Path] = None
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


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def effective_rules_configuration(rules_dir: Optional[Any]) -> Dict[str, Any]:
    """Fingerprint exactly the rule files and mode consumed by ``load_rules``."""

    if not rules_dir:
        configuration = {
            "schema_version": RULES_CONFIGURATION_SCHEMA_VERSION,
            "mode": "built_in_defaults",
            "directory": None,
            "files": [],
        }
        return {
            **configuration,
            "sha256": canonical_json_sha256(configuration),
        }

    root = Path(rules_dir).resolve()
    if not root.is_dir():
        raise BatchSourceError("rules directory not found: {}".format(root))
    try:
        filenames = sorted(
            child.name for child in root.iterdir() if child.name.endswith(".json")
        )
    except OSError as exc:
        raise BatchSourceError(
            "cannot inspect rules directory {}: {}".format(root, exc)
        ) from exc

    files = []
    for filename in filenames:
        rule_path = root / filename
        try:
            mode = rule_path.stat().st_mode
            if not stat.S_ISREG(mode):
                raise BatchSourceError(
                    "rule path is not a regular file: {}".format(rule_path)
                )
            content = rule_path.read_bytes()
        except BatchSourceError:
            raise
        except OSError as exc:
            raise BatchSourceError(
                "cannot read rule file {}: {}".format(rule_path, exc)
            ) from exc
        files.append(
            {
                "path": filename,
                "bytes": len(content),
                "sha256": sha256_bytes(content),
            }
        )

    configuration = {
        "schema_version": RULES_CONFIGURATION_SCHEMA_VERSION,
        "mode": "external_directory",
        "directory": str(root),
        "files": files,
    }
    return {
        **configuration,
        "sha256": canonical_json_sha256(configuration),
    }


def load_json_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchSourceError("cannot read JSON object {}: {}".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise BatchSourceError("{} must contain a JSON object".format(path))
    return value


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably replace a JSON file without exposing partial content."""

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
        try:
            directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
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


def _has_xml_source(entry: Mapping[str, Any]) -> bool:
    """Whether a downloader entry exposes any concrete XML artifact."""

    for key in (
        "relative_archive_path",
        "relative_file_path",
        "archive_sha256",
        "xml_sha256",
    ):
        if entry.get(key):
            return True
    xml_files = entry.get("xml_files")
    return isinstance(xml_files, list) and bool(xml_files)


def _entry_sort_key(indexed_entry: Tuple[int, Mapping[str, Any]]) -> Tuple[Any, ...]:
    index, entry = indexed_entry
    ordinal = _optional_int(entry.get("pdf_manifest_ordinal"))
    category_index = _optional_int(entry.get("category_index"))
    return (
        ordinal is None,
        ordinal if ordinal is not None else sys.maxsize,
        str(entry.get("category") or "").casefold(),
        category_index is None,
        category_index if category_index is not None else sys.maxsize,
        str(entry.get("slug") or "").casefold(),
        str(entry.get("item_id") or "").casefold(),
        index,
    )


def select_entries(
    manifest: Mapping[str, Any],
    limit: Optional[int] = None,
) -> List[Tuple[int, Dict[str, Any]]]:
    """Select only authoritative PDF-only reconciliation records."""

    if limit is not None and limit < 0:
        raise ValueError("limit cannot be negative")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise BatchSourceError("GII downloader manifest has no entries list")
    selected = [
        (index, dict(entry))
        for index, entry in enumerate(entries)
        if isinstance(entry, dict)
        and entry.get("reconciliation_status")
        == PDF_ONLY_RECONCILIATION_STATUS
        and not _has_xml_source(entry)
    ]
    selected.sort(key=_entry_sort_key)
    if limit is not None:
        selected = selected[:limit]
    return selected


def _successful_pdf_matches(entry: Mapping[str, Any]) -> List[Dict[str, Any]]:
    matches = entry.get("pdf_manifest_matches")
    if not isinstance(matches, list):
        return []
    selected = [
        dict(match)
        for match in matches
        if isinstance(match, dict)
        and match.get("relative_file_path")
        and str(match.get("status") or "") in SUCCESS_PDF_STATUSES
    ]
    selected.sort(
        key=lambda match: (
            _optional_int(match.get("ordinal")) is None,
            _optional_int(match.get("ordinal"))
            if _optional_int(match.get("ordinal")) is not None
            else sys.maxsize,
            str(match.get("category") or "").casefold(),
            str(match.get("relative_file_path") or "").casefold(),
        )
    )
    return selected


def _safe_component(value: Any, fallback: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return component or fallback


def entry_identifier(entry: Mapping[str, Any], source_index: int) -> str:
    explicit = _safe_component(entry.get("item_id"), "")
    if explicit:
        return explicit
    identity = "\0".join(
        [
            str(entry.get("law_key") or ""),
            str(entry.get("slug") or ""),
            str(entry.get("pdf_manifest_ordinal") or ""),
            str(source_index),
        ]
    )
    slug = _safe_component(entry.get("slug") or entry.get("title"), "document")
    return "{}-{}".format(slug, sha256_bytes(identity.encode("utf-8"))[:12])


def _safe_source_path(source_dir: Path, relative_path: str) -> Path:
    if not relative_path or "\\" in relative_path or "\x00" in relative_path:
        raise ValueError("unsafe PDF manifest path {!r}".format(relative_path))
    pure_path = PurePosixPath(relative_path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise ValueError("unsafe PDF manifest path {!r}".format(relative_path))
    root = source_dir.resolve()
    candidate = (root / Path(*pure_path.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "PDF manifest path escapes PDF directory: {!r}".format(relative_path)
        ) from exc
    return candidate


def fallback_source_manifest_entry(
    entry: Mapping[str, Any],
    pdf_match: Mapping[str, Any],
) -> Dict[str, Any]:
    """Make PDF fallback provenance explicit in the canonical raw payload."""

    adapted = dict(entry)
    relative_path = str(pdf_match["relative_file_path"])
    adapted.update(
        {
            "source_kind": SOURCE_KIND,
            "source_format": "pdf",
            "source_role": "authoritative_fallback",
            "fallback_reason": "official_gii_xml_unavailable",
            "gii_xml_reconciliation_status": PDF_ONLY_RECONCILIATION_STATUS,
            "pdf_manifest_entry": dict(pdf_match),
            "pdf_file": relative_path,
            "pdf_relative_path": relative_path,
            "source_pdf": relative_path,
            "relative_file_path": relative_path,
        }
    )
    return adapted


def output_path_for(
    output_dir: Path,
    entry: Mapping[str, Any],
    pdf_match: Mapping[str, Any],
    source_index: int,
) -> Path:
    category = _safe_component(
        pdf_match.get("category") or entry.get("category"),
        "_catalog",
    )
    return (
        output_dir
        / "raw"
        / category
        / "{}_raw.json".format(entry_identifier(entry, source_index))
    )


def pages_dir_for(
    output_dir: Path,
    entry: Mapping[str, Any],
    pdf_match: Mapping[str, Any],
    source_index: int,
) -> Path:
    category = _safe_component(
        pdf_match.get("category") or entry.get("category"),
        "_catalog",
    )
    return output_dir / "pages" / category / entry_identifier(entry, source_index)


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
        "pages": len(document.get("page_refs") or document.get("pages") or []),
        "structural_units": len(document.get("structural_units") or []),
        "chunks": len(document.get("chunks") or []),
        "issues": len(issues) if isinstance(issues, list) else 0,
        "output_path": _relative_output_path(output_dir, output_path),
        "output_bytes": output_path.stat().st_size,
        "output_sha256": file_sha256(output_path),
    }


def _page_content_sha256(page: Mapping[str, Any]) -> str:
    content = dict(page)
    content.pop(PAGE_CONTENT_HASH_FIELD, None)
    return canonical_json_sha256(content)


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _page_artifact_path(
    output_base_dir: Path,
    output_root: Path,
    relative_path: Any,
) -> Path:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("page ref lacks a valid relative path")
    path = Path(relative_path)
    if path.is_absolute():
        raise ValueError("page ref path must be relative: {!r}".format(relative_path))
    root = output_root.resolve()
    resolved = (output_base_dir / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            "page ref path escapes output root: {!r}".format(relative_path)
        ) from exc
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise ValueError(
            "page artifact does not exist: {} ({})".format(resolved, exc)
        ) from exc
    if not stat.S_ISREG(mode):
        raise ValueError("page artifact is not a regular file: {}".format(resolved))
    return resolved


def _read_page_artifact(path: Path) -> Dict[str, Any]:
    try:
        page = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("page artifact is unreadable: {} ({})".format(path, exc)) from exc
    if not isinstance(page, dict):
        raise ValueError("page artifact is not a JSON object: {}".format(path))
    return page


def _normalized_generated_page(
    page_ref: Mapping[str, Any],
    page: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    normalized_ref = dict(page_ref)
    normalized_page = dict(page)
    page_id = normalized_ref.get("page_id")
    page_number = normalized_ref.get("page_number")
    if not isinstance(page_id, str) or not page_id:
        raise ValueError("generated page ref lacks page_id")
    if (
        not isinstance(page_number, int)
        or isinstance(page_number, bool)
        or page_number < 1
    ):
        raise ValueError("generated page ref lacks a valid page_number")
    if normalized_page.get("page_id") != page_id:
        raise ValueError("generated page artifact page_id does not match its ref")
    if normalized_page.get("page_number") != page_number:
        raise ValueError("generated page artifact page_number does not match its ref")

    text = normalized_page.get("text")
    if not isinstance(text, str):
        raise ValueError("generated page artifact lacks text")
    text_sha256 = sha256_bytes(text.encode("utf-8"))
    for owner, stored_hash in (
        ("page ref", normalized_ref.get("text_sha256")),
        ("page artifact", normalized_page.get("text_sha256")),
    ):
        if stored_hash is not None and stored_hash != text_sha256:
            raise ValueError("{} text_sha256 is inconsistent".format(owner))
    normalized_ref["text_sha256"] = text_sha256
    normalized_page["text_sha256"] = text_sha256

    content_sha256 = _page_content_sha256(normalized_page)
    stored_content_sha256 = normalized_page.get(PAGE_CONTENT_HASH_FIELD)
    if (
        stored_content_sha256 is not None
        and stored_content_sha256 != content_sha256
    ):
        raise ValueError("page artifact content_sha256 is inconsistent")
    normalized_page[PAGE_CONTENT_HASH_FIELD] = content_sha256
    normalized_ref[PAGE_CONTENT_HASH_FIELD] = content_sha256
    return normalized_ref, normalized_page


def _validate_published_page_refs(
    document: Mapping[str, Any],
    output_path: Path,
    output_root: Path,
) -> None:
    page_refs = document.get("page_refs")
    if not isinstance(page_refs, list):
        raise ValueError("existing document lacks a page_refs list")
    seen_paths = set()
    seen_page_ids = set()
    seen_page_numbers = set()
    for index, page_ref in enumerate(page_refs):
        if not isinstance(page_ref, dict):
            raise ValueError("page ref {} is not a JSON object".format(index))
        page_id = page_ref.get("page_id")
        page_number = page_ref.get("page_number")
        if not isinstance(page_id, str) or not page_id:
            raise ValueError("page ref {} lacks page_id".format(index))
        if (
            not isinstance(page_number, int)
            or isinstance(page_number, bool)
            or page_number < 1
        ):
            raise ValueError("page ref {} lacks a valid page_number".format(index))
        page_path = _page_artifact_path(
            output_path.parent,
            output_root,
            page_ref.get("path"),
        )
        if page_path in seen_paths:
            raise ValueError("duplicate page artifact path: {}".format(page_path))
        if page_id in seen_page_ids:
            raise ValueError("duplicate page_id: {}".format(page_id))
        if page_number in seen_page_numbers:
            raise ValueError("duplicate page_number: {}".format(page_number))
        seen_paths.add(page_path)
        seen_page_ids.add(page_id)
        seen_page_numbers.add(page_number)

        page = _read_page_artifact(page_path)
        if page.get("page_id") != page_id:
            raise ValueError(
                "page artifact page_id does not match ref: {}".format(page_path)
            )
        if page.get("page_number") != page_number:
            raise ValueError(
                "page artifact page_number does not match ref: {}".format(page_path)
            )
        text = page.get("text")
        if not isinstance(text, str):
            raise ValueError("page artifact lacks text: {}".format(page_path))
        text_sha256 = sha256_bytes(text.encode("utf-8"))
        ref_text_sha256 = page_ref.get("text_sha256")
        page_text_sha256 = page.get("text_sha256")
        if not _valid_sha256(ref_text_sha256) or ref_text_sha256 != text_sha256:
            raise ValueError(
                "page ref text_sha256 does not match content: {}".format(page_path)
            )
        if not _valid_sha256(page_text_sha256) or page_text_sha256 != text_sha256:
            raise ValueError(
                "page artifact text_sha256 does not match content: {}".format(page_path)
            )
        content_sha256 = _page_content_sha256(page)
        ref_content_sha256 = page_ref.get(PAGE_CONTENT_HASH_FIELD)
        page_content_sha256 = page.get(PAGE_CONTENT_HASH_FIELD)
        if (
            not _valid_sha256(ref_content_sha256)
            or ref_content_sha256 != content_sha256
        ):
            raise ValueError(
                "page ref content_sha256 does not match artifact: {}".format(page_path)
            )
        if (
            not _valid_sha256(page_content_sha256)
            or page_content_sha256 != content_sha256
        ):
            raise ValueError(
                "page artifact content_sha256 does not match content: {}".format(
                    page_path
                )
            )


def _prepare_generated_page_artifacts(
    document: Dict[str, Any],
    output_path: Path,
    output_root: Path,
    staging_root: Path,
) -> List[Dict[str, Any]]:
    page_refs = document.get("page_refs")
    if not isinstance(page_refs, list):
        raise ValueError("PDF extractor returned no page_refs list")
    prepared = []
    normalized_refs = []
    staging_root = staging_root.resolve()
    seen_paths = set()
    for index, page_ref in enumerate(page_refs):
        if not isinstance(page_ref, dict):
            raise ValueError("generated page ref {} is not an object".format(index))
        page_path = _page_artifact_path(
            output_path.parent,
            output_root,
            page_ref.get("path"),
        )
        try:
            relative_page_path = page_path.relative_to(staging_root)
        except ValueError as exc:
            raise ValueError(
                "generated page artifact is outside its staging directory"
            ) from exc
        if relative_page_path in seen_paths:
            raise ValueError(
                "generated duplicate page artifact: {}".format(relative_page_path)
            )
        seen_paths.add(relative_page_path)
        page = _read_page_artifact(page_path)
        normalized_ref, normalized_page = _normalized_generated_page(page_ref, page)
        atomic_write_json(page_path, normalized_page)
        normalized_refs.append(normalized_ref)
        prepared.append(
            {
                "relative_path": relative_page_path.as_posix(),
                "page_id": normalized_ref["page_id"],
                "page_number": normalized_ref["page_number"],
            }
        )
    document["page_refs"] = normalized_refs
    document["pages"] = normalized_refs
    return prepared


def _publish_page_generation(
    document: Dict[str, Any],
    prepared: Sequence[Mapping[str, Any]],
    staging_root: Path,
    output_base_dir: Path,
) -> Path:
    generation_dir = staging_root.with_name(staging_root.name.lstrip("."))
    if generation_dir.exists():
        raise FileExistsError("page generation already exists: {}".format(generation_dir))
    os.replace(str(staging_root), str(generation_dir))
    page_refs = document["page_refs"]
    for page_ref, prepared_page in zip(page_refs, prepared):
        final_path = generation_dir / str(prepared_page["relative_path"])
        page_ref["path"] = Path(
            os.path.relpath(str(final_path), str(output_base_dir))
        ).as_posix()
    document["pages"] = page_refs
    return generation_dir


def _is_page_generation_name(name: str) -> bool:
    return re.fullmatch(r"\.?generation-[a-z0-9_]{8}", name) is not None


def _referenced_page_generation_names(
    output_path: Path,
    item_pages_root: Path,
) -> set:
    if not output_path.is_file():
        return set()
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict) or payload.get("source_kind") != SOURCE_KIND:
        return set()
    documents = payload.get("documents")
    if not isinstance(documents, list) or len(documents) != 1:
        return set()
    document = documents[0]
    page_refs = document.get("page_refs") if isinstance(document, dict) else None
    if not isinstance(page_refs, list):
        return set()

    root = item_pages_root.resolve()
    referenced = set()
    for page_ref in page_refs:
        relative_path = page_ref.get("path") if isinstance(page_ref, dict) else None
        if not isinstance(relative_path, str) or not relative_path:
            continue
        path = Path(relative_path)
        if path.is_absolute():
            continue
        resolved = (output_path.parent / path).resolve()
        try:
            item_relative_path = resolved.relative_to(root)
        except ValueError:
            continue
        if (
            item_relative_path.parts
            and _is_page_generation_name(item_relative_path.parts[0])
        ):
            referenced.add(item_relative_path.parts[0])
    return referenced


def _cleanup_unreferenced_page_generations(
    item_pages_root: Path,
    output_root: Path,
    output_path: Path,
) -> None:
    root = item_pages_root.resolve()
    try:
        root.relative_to(output_root.resolve())
    except ValueError as exc:
        raise ValueError(
            "item pages directory escapes output root: {}".format(item_pages_root)
        ) from exc
    root.mkdir(parents=True, exist_ok=True)
    referenced = _referenced_page_generation_names(output_path, root)
    for child in root.iterdir():
        if child.name in referenced or not _is_page_generation_name(child.name):
            continue
        try:
            child_mode = child.lstat().st_mode
        except OSError:
            continue
        if not stat.S_ISDIR(child_mode):
            continue
        shutil.rmtree(child)


def validate_resumable_output(
    output_path: Path,
    entry: Mapping[str, Any],
    pdf_relative_path: str,
    expected_pdf_sha256: str,
    output_root: Optional[Path] = None,
    expected_rules_configuration: Optional[Mapping[str, Any]] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, None, "existing output is unreadable: {}".format(exc)
    if not isinstance(payload, dict):
        return False, None, "existing output is not a JSON object"
    if payload.get("source_kind") != SOURCE_KIND:
        return False, None, "existing output is not a GII PDF fallback"
    documents = payload.get("documents")
    if not isinstance(documents, list) or len(documents) != 1:
        return False, None, "existing output does not contain exactly one document"
    source_entry = payload.get("source_manifest_entry")
    if not isinstance(source_entry, dict):
        return False, None, "existing output lacks source_manifest_entry"
    expected_item_id = entry.get("item_id")
    if expected_item_id and source_entry.get("item_id") != expected_item_id:
        return False, None, "existing output belongs to another manifest entry"
    if source_entry.get("source_kind") != SOURCE_KIND:
        return False, None, "existing output lacks PDF-fallback provenance"
    if source_entry.get("pdf_relative_path") != pdf_relative_path:
        return False, None, "existing output used a different PDF source path"
    document = documents[0]
    metadata = document.get("metadata") if isinstance(document, dict) else None
    if not isinstance(metadata, dict):
        return False, None, "existing output lacks document metadata"
    if metadata.get("source_kind") != SOURCE_KIND:
        return False, None, "existing document lacks PDF-fallback provenance"
    if metadata.get("pdf_fallback_adapter_version") != TOOL_VERSION:
        return False, None, "existing output used an obsolete fallback adapter"
    if metadata.get("source_pdf_relative_path") != pdf_relative_path:
        return False, None, "existing document used a different PDF source path"
    if metadata.get("source_pdf_manifest_sha256") != expected_pdf_sha256:
        return False, None, "existing document used different PDF manifest provenance"
    if metadata.get("source_pdf_sha256") != expected_pdf_sha256:
        return False, None, "existing document was extracted from different PDF bytes"
    if document.get("sha256") != expected_pdf_sha256:
        return False, None, "existing document content hash differs from the manifest"
    if expected_rules_configuration is None:
        return False, None, "expected rules configuration is unavailable"
    stored_rules_configuration = metadata.get("rules_configuration")
    if not isinstance(stored_rules_configuration, dict):
        return False, None, "existing document lacks rules configuration provenance"
    if (
        stored_rules_configuration.get("mode")
        != expected_rules_configuration.get("mode")
        or stored_rules_configuration.get("sha256")
        != expected_rules_configuration.get("sha256")
    ):
        return False, None, "existing document used a different rules configuration"
    try:
        _validate_published_page_refs(
            document,
            output_path,
            output_root or output_path.parents[2],
        )
    except (OSError, ValueError) as exc:
        return False, None, "existing page artifacts failed validation: {}".format(exc)
    return True, payload, None


def _page_text(
    output_base_dir: Path,
    page_ref: Mapping[str, Any],
) -> str:
    relative_path = page_ref.get("path")
    if not isinstance(relative_path, str) or not relative_path:
        return ""
    page_path = (output_base_dir / relative_path).resolve()
    try:
        page_path.relative_to(output_base_dir.parents[1].resolve())
    except ValueError:
        return ""
    try:
        page = json.loads(page_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(page.get("text") or "") if isinstance(page, dict) else ""


def ensure_document_body_fallback(
    document: Dict[str, Any],
    output_base_dir: Path,
) -> bool:
    """Retain unsectioned PDF text as one explicit, uncertain graph unit."""

    if document.get("structural_units") or document.get("chunks"):
        return False
    page_refs = document.get("page_refs") or document.get("pages") or []
    if not isinstance(page_refs, list):
        page_refs = []
    texts = [
        _page_text(output_base_dir, page_ref)
        for page_ref in page_refs
        if isinstance(page_ref, dict)
    ]
    text = "\n\n".join(part.strip() for part in texts if part.strip())
    if not text:
        return False

    document_id = str(document.get("document_id") or "")
    document_key = str(document.get("document_key") or "")
    document_global_key = str(
        document.get("document_global_key") or document_key or document_id
    )
    citation_prefix = str(
        document.get("citation_prefix")
        or document.get("abbreviation")
        or document.get("title")
        or document_global_key
    )
    global_key = "{}_document_body".format(document_global_key)
    unit_id = "unit_{}".format(global_key)
    chunk_global_key = "{}_text".format(global_key)
    page_numbers = [
        page_ref.get("page_number")
        for page_ref in page_refs
        if isinstance(page_ref, dict)
        and isinstance(page_ref.get("page_number"), int)
    ]
    page_range = {
        "start": min(page_numbers) if page_numbers else 1,
        "end": max(page_numbers) if page_numbers else 1,
    }
    page_id = next(
        (
            page_ref.get("page_id")
            for page_ref in page_refs
            if isinstance(page_ref, dict) and page_ref.get("page_id")
        ),
        None,
    )
    text_sha256 = sha256_bytes(text.encode("utf-8"))
    uncertainty = (
        "The PDF contains text, but the deterministic legacy splitter found "
        "no paragraph or annex boundary."
    )
    document["structural_units"] = [
        {
            "unit_id": unit_id,
            "global_key": global_key,
            "legal_citation": "{} Dokumenttext".format(citation_prefix),
            "display_name": "{} Dokumenttext".format(citation_prefix),
            "document_id": document_id,
            "document_key": document_key,
            "document_global_key": document_global_key,
            "unit_type": "document_body",
            "label": "Dokumenttext",
            "number": None,
            "title": document.get("title"),
            "breadcrumbs": [document_global_key, "Dokumenttext"],
            "parent_unit_id": None,
            "child_unit_ids": [],
            "page_range": page_range,
            "text": text,
            "text_sha256": text_sha256,
            "confidence": 0.65,
            "review_status": "pending",
            "is_uncertain": True,
            "uncertainty_reason": uncertainty,
        }
    ]
    document["chunks"] = [
        {
            "chunk_id": "chunk_{}".format(chunk_global_key),
            "global_key": chunk_global_key,
            "legal_citation": "{} Dokumenttext".format(citation_prefix),
            "display_name": "{} Dokumenttext".format(citation_prefix),
            "chunk_type": "document_text",
            "unit_id": unit_id,
            "document_global_key": document_global_key,
            "parent_chunk_id": None,
            "child_chunk_ids": [],
            "label": "Dokumenttext",
            "number": 0,
            "sequence": 1,
            "page_id": page_id,
            "page_range": page_range,
            "text": text,
            "text_sha256": text_sha256,
            "evidence_text": text,
            "confidence": 0.65,
            "review_status": "pending",
        }
    ]
    document.setdefault("metadata", {})["fallback_content_mode"] = (
        "whole_document_text"
    )
    return True


def _base_result(task: Mapping[str, Any]) -> Dict[str, Any]:
    entry = task["entry"]
    return {
        "selection_index": task["selection_index"],
        "source_index": task["source_index"],
        "item_id": entry_identifier(entry, task["source_index"]),
        "slug": entry.get("slug"),
        "title": entry.get("title"),
        "source_kind": SOURCE_KIND,
        "reconciliation_status": entry.get("reconciliation_status"),
        "source_relative_path": task.get("pdf_relative_path"),
        "source_pdf_sha256": task.get("expected_pdf_sha256"),
    }


def _verified_task_rules_configuration(
    task: Mapping[str, Any],
) -> Dict[str, Any]:
    expected = task.get("rules_configuration")
    if not isinstance(expected, dict):
        raise ValueError("task lacks a rules configuration fingerprint")
    current = effective_rules_configuration(task.get("rules_dir"))
    if (
        current.get("mode") != expected.get("mode")
        or current.get("sha256") != expected.get("sha256")
    ):
        raise ValueError("rules configuration changed during the batch")
    return current


def extract_one(task: Dict[str, Any]) -> Dict[str, Any]:
    """Extract one fallback PDF and always return a document-local result."""

    started = time.monotonic()
    entry = task["entry"]
    pdf_match = task["pdf_match"]
    pdf_path = Path(task["pdf_path"])
    output_dir = Path(task["output_dir"])
    output_path = Path(task["output_path"])
    result = _base_result(task)
    staging_root: Optional[Path] = None
    uncommitted_generation: Optional[Path] = None

    try:
        expected_sha256 = str(task.get("expected_pdf_sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("PDF manifest entry lacks a valid SHA-256")
        if not pdf_path.is_file():
            raise FileNotFoundError("fallback PDF does not exist: {}".format(pdf_path))
        current_sha256 = file_sha256(pdf_path)
        if current_sha256 != expected_sha256:
            raise ValueError("PDF hash differs from the downloader manifest")
        rules_configuration = _verified_task_rules_configuration(task)
        final_pages_root = Path(task["pages_dir"])
        _cleanup_unreferenced_page_generations(
            final_pages_root,
            output_dir,
            output_path,
        )

        if output_path.exists() and not task["force"]:
            if not task["resume"]:
                raise FileExistsError(
                    "{} already exists; use --resume or --force".format(output_path)
                )
            valid, payload, validation_error = validate_resumable_output(
                output_path,
                entry,
                str(task["pdf_relative_path"]),
                expected_sha256,
                output_dir,
                rules_configuration,
            )
            if valid and payload is not None:
                _verified_task_rules_configuration(task)
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

        sys.path.insert(0, str(PROJECT_ROOT))
        from normtext_extractor.pipeline import (  # noqa: WPS433
            extract_document,
            load_rules,
        )

        rule_set = load_rules(task.get("rules_dir"))
        _verified_task_rules_configuration(task)
        staging_root = Path(
            tempfile.mkdtemp(prefix=".generation-", dir=str(final_pages_root))
        )
        document, issues = extract_document(
            str(pdf_path),
            # Page refs in raw JSON are relative to the raw JSON's directory,
            # matching the validator and downstream path contract.
            output_base_dir=str(output_path.parent),
            pages_root_dir=str(staging_root),
            rule_set=rule_set,
        )
        if not isinstance(document, dict):
            raise TypeError("PDF extractor returned a non-object document")
        if document.get("sha256") != expected_sha256:
            raise ValueError("PDF changed while it was being extracted")
        prepared_pages = _prepare_generated_page_artifacts(
            document,
            output_path,
            output_dir,
            staging_root,
        )
        used_body_fallback = ensure_document_body_fallback(
            document,
            output_path.parent,
        )

        source_entry = fallback_source_manifest_entry(entry, pdf_match)
        metadata = document.setdefault("metadata", {})
        metadata.update(
            {
                "source_kind": SOURCE_KIND,
                "source_format": "pdf",
                "source_role": "authoritative_fallback",
                "fallback_reason": "official_gii_xml_unavailable",
                "gii_xml_reconciliation_status": PDF_ONLY_RECONCILIATION_STATUS,
                "source_pdf_relative_path": task["pdf_relative_path"],
                "source_pdf_sha256": expected_sha256,
                "source_pdf_manifest_sha256": expected_sha256,
                "pdf_fallback_adapter_version": TOOL_VERSION,
                "rules_configuration": dict(rules_configuration),
            }
        )
        document["source_kind"] = SOURCE_KIND
        document["source_pdf"] = task["pdf_relative_path"]
        payload = {
            "schema_version": "1.0.0-draft",
            "phase": "normtext",
            "source_kind": SOURCE_KIND,
            "source_manifest_entry": source_entry,
            "documents": [document],
            "review_decisions": [],
            "extraction_issues": issues if isinstance(issues, list) else [],
        }
        _verified_task_rules_configuration(task)
        uncommitted_generation = _publish_page_generation(
            document,
            prepared_pages,
            staging_root,
            output_path.parent,
        )
        _validate_published_page_refs(document, output_path, output_dir)
        atomic_write_json(output_path, payload)
        uncommitted_generation = None
        _cleanup_unreferenced_page_generations(
            final_pages_root,
            output_dir,
            output_path,
        )
        result.update(
            {
                "status": "ok",
                "fallback_content_mode": (
                    "whole_document_text" if used_body_fallback else None
                ),
                **_output_summary(payload, output_dir, output_path),
            }
        )
    except Exception as exc:
        result.update(
            {
                "status": "error",
                "output_path": _relative_output_path(output_dir, output_path),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        if staging_root is not None and staging_root.exists():
            shutil.rmtree(staging_root)
        if (
            uncommitted_generation is not None
            and uncommitted_generation.exists()
        ):
            shutil.rmtree(uncommitted_generation)
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def _task_for(
    config: BatchConfig,
    selection_index: int,
    source_index: int,
    entry: Dict[str, Any],
    rules_configuration: Mapping[str, Any],
) -> Dict[str, Any]:
    matches = _successful_pdf_matches(entry)
    if not matches:
        pdf_match: Dict[str, Any] = {}
        relative_path = ""
        expected_sha256 = ""
        pdf_path = config.pdf_dir / "__invalid_source__"
        setup_error = "ValueError: PDF-only entry has no successful PDF manifest match"
    else:
        pdf_match = matches[0]
        relative_path = str(pdf_match.get("relative_file_path") or "")
        expected_sha256 = str(pdf_match.get("sha256") or "").strip().lower()
        try:
            pdf_path = _safe_source_path(config.pdf_dir, relative_path)
        except Exception as exc:
            pdf_path = config.pdf_dir / "__invalid_source__"
            setup_error = "{}: {}".format(type(exc).__name__, exc)
        else:
            setup_error = None
    output_path = output_path_for(
        config.output_dir,
        entry,
        pdf_match,
        source_index,
    )
    pages_dir = pages_dir_for(
        config.output_dir,
        entry,
        pdf_match,
        source_index,
    )
    return {
        "entry": entry,
        "pdf_match": pdf_match,
        "selection_index": selection_index,
        "source_index": source_index,
        "pdf_path": str(pdf_path),
        "pdf_relative_path": relative_path,
        "expected_pdf_sha256": expected_sha256,
        "source_setup_error": setup_error,
        "output_dir": str(config.output_dir),
        "output_path": str(output_path),
        "pages_dir": str(pages_dir),
        "rules_dir": str(config.rules_dir.resolve()) if config.rules_dir else None,
        "rules_configuration": dict(rules_configuration),
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
    """Execute all tasks and restore deterministic manifest order."""

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
    source_manifest: Mapping[str, Any],
    source_manifest_sha256: str,
    eligible_count: int,
    results: Sequence[Mapping[str, Any]],
    rules_configuration: Mapping[str, Any],
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
        "source": "Gesetze-im-Internet PDF-only fallback extraction",
        "source_kind": SOURCE_KIND,
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
            "eligible_pdf_only_entries": eligible_count,
        },
        "run": {
            "pdf_dir": str(config.pdf_dir.resolve()),
            "workers": config.workers,
            "rules_dir": (
                str(config.rules_dir.resolve()) if config.rules_dir else None
            ),
            "rules_configuration": dict(rules_configuration),
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
    rules_configuration = effective_rules_configuration(config.rules_dir)
    all_eligible = select_entries(source_manifest)
    selected = all_eligible if config.limit is None else all_eligible[: config.limit]
    tasks = [
        _task_for(
            config,
            selection_index,
            source_index,
            entry,
            rules_configuration,
        )
        for selection_index, (source_index, entry) in enumerate(selected)
    ]
    results = execute_tasks(tasks, config.workers)
    manifest = build_batch_manifest(
        config,
        source_manifest,
        file_sha256(config.manifest_path),
        len(all_eligible),
        results,
        rules_configuration,
    )
    atomic_write_json(aggregate_path, manifest)
    return manifest


def run(config: BatchConfig) -> Dict[str, Any]:
    """Run under an exclusive output-directory lock."""

    config.output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.output_dir / ".batch_extract_gii_pdf_fallbacks.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BatchSourceError(
                "another PDF fallback batch is already using {}".format(
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
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--rules-dir", type=Path, default=None)
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
        pdf_dir=args.pdf_dir,
        output_dir=args.out_dir,
        rules_dir=args.rules_dir,
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
