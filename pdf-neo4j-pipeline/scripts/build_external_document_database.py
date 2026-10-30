#!/usr/bin/env python3
"""Download and register supplementary documents referenced by RechtsGraph.

The curated catalog controls which authoritative full texts may be downloaded.
The graph inventory contributes demand signals and metadata-only records.  Case
law is retained in the audit table but intentionally excluded from the corpus.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_ROOT = REPO_ROOT / "external_documents"
DEFAULT_CATALOG = DEFAULT_ROOT / "catalog.json"
DEFAULT_INVENTORY = DEFAULT_ROOT / "reference_inventory.json"
DEFAULT_CELLAR_REGISTER = REPO_ROOT / "eurlex_consolidated_de" / "register.json"
USER_AGENT = "RechtsGraph-external-documents/1.0 (+research corpus)"
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}

FAMILY_DOCUMENTS = {
    "adr": "adr_2025",
    "rid": "rid_2025_de",
    "adn": "adn_2025_de",
    "imdg_code": "imdg_code_42_24",
    "icao_technical_instructions": "icao_ti_2025_2026",
    "iata_dgr": "iata_dgr_67",
    "un_model_regulations": "un_model_regulations_rev24_en",
    "un_ghs": "un_ghs_rev11_en",
}
LICENSED_FAMILIES = {"din", "iso", "vdi", "dvgw"}
TECHNICAL_LANDING_PAGES = {
    "din": "https://www.dinmedia.de/",
    "iso": "https://www.iso.org/standards.html",
    "vdi": "https://www.vdi.de/richtlinien",
    "dvgw": "https://www.dvgw-regelwerk.de/",
    "dguv": "https://publikationen.dguv.de/regelwerk/",
    "trgs": "https://www.baua.de/DE/Angebote/Regelwerk/TRGS/TRGS",
    "trbs": "https://www.baua.de/DE/Angebote/Regelwerk/TRBS/TRBS",
    "trba": "https://www.baua.de/DE/Angebote/Regelwerk/TRBA/TRBA",
    "asr": "https://www.baua.de/DE/Angebote/Regelwerk/ASR/ASR",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return "{}_{}".format(prefix, digest)


def display_key(value: str) -> str:
    return " ".join(value.replace("_", " ").split()).strip()


def priority_for_mentions(count: int) -> str:
    if count >= 100:
        return "A"
    if count >= 10:
        return "B"
    return "C"


def safe_local_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("unsafe catalog local_path: {}".format(relative))
    resolved = (root / candidate).resolve()
    if root.resolve() not in resolved.parents:
        raise ValueError("catalog local_path escapes corpus root: {}".format(relative))
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pdf_page_count(path: Path) -> Optional[int]:
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, flags=re.MULTILINE)
    return int(match.group(1)) if match else None


def validate_local_file(path: Path, media_type: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("file is missing")
    size = path.stat().st_size
    if size < 8:
        raise ValueError("file is implausibly small")
    page_count: Optional[int] = None
    if media_type == "application/pdf":
        with path.open("rb") as stream:
            if not stream.read(8).startswith(b"%PDF-"):
                raise ValueError("file is not a PDF")
        page_count = pdf_page_count(path)
    elif media_type == "application/zip":
        if not zipfile.is_zipfile(path):
            raise ValueError("file is not a ZIP archive")
    elif media_type in {"application/xhtml+xml", "text/html"}:
        with path.open("rb") as stream:
            prefix = stream.read(min(size, 64 * 1024)).lstrip().lower()
        if b"<html" not in prefix and not prefix.startswith(b"<?xml"):
            raise ValueError("file is not HTML or XHTML")
    return {
        "size_bytes": size,
        "sha256": file_sha256(path),
        "page_count": page_count,
        "validation_status": "valid",
    }


def validate_formex_file(path: Path) -> dict[str, Any]:
    metadata = validate_local_file(path, "application/zip")
    with zipfile.ZipFile(path) as archive:
        candidates = [
            item
            for item in archive.infolist()
            if item.filename.casefold().endswith(".xml")
            and not item.filename.casefold().endswith(".doc.xml")
        ]
        if not candidates:
            raise ValueError("Formex ZIP has no content XML")
        content = max(candidates, key=lambda item: item.file_size)
        root = ET.fromstring(archive.read(content))
    if root.tag not in {"ACT", "CONS.ACT"}:
        raise ValueError("Formex content root is not ACT or CONS.ACT")
    return {
        **metadata,
        "formex_root": root.tag,
        "content_member": content.filename,
    }


def download_file(
    url: str,
    destination: Path,
    media_type: str,
    *,
    retries: int,
    timeout_seconds: int,
    accept: Optional[str] = None,
    validator: Optional[Any] = None,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 2):
        try:
            request = Request(
                url,
                headers={
                    "Accept": accept or media_type,
                    "Accept-Language": "deu,de;q=0.9,en;q=0.8",
                    "User-Agent": USER_AGENT,
                },
            )
            with urlopen(request, timeout=timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("response exceeds size limit")
                size = 0
                with temporary.open("wb") as stream:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError("response exceeds size limit")
                        stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
            metadata = (
                validator(temporary)
                if validator is not None
                else validate_local_file(temporary, media_type)
            )
            os.replace(temporary, destination)
            return {
                **metadata,
                "download_status": "downloaded",
                "final_url": response.geturl(),
                "retrieved_at": utc_now(),
                "attempts": attempt,
            }
        except HTTPError as exc:
            last_error = exc
            if exc.code not in RETRYABLE_HTTP or attempt > retries:
                break
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            if attempt > retries:
                break
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        time.sleep(min(8, 2 ** (attempt - 1)))
    return {
        "download_status": "failed",
        "validation_status": "failed",
        "error": "{}: {}".format(type(last_error).__name__, last_error),
        "retrieved_at": utc_now(),
    }


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("{} must contain a JSON object".format(path))
    return value


def validate_catalog(catalog: Mapping[str, Any], root: Path) -> None:
    documents = catalog.get("documents")
    if not isinstance(documents, list):
        raise ValueError("catalog documents must be a list")
    ids: set[str] = set()
    local_paths: set[str] = set()
    for document in documents:
        document_id = str(document.get("id") or "")
        if not document_id or document_id in ids:
            raise ValueError("catalog has a missing or duplicate document id: {}".format(document_id))
        ids.add(document_id)
        for file_spec in document.get("files") or []:
            relative = str(file_spec.get("local_path") or "")
            if not relative or relative in local_paths:
                raise ValueError("catalog has a missing or duplicate local_path: {}".format(relative))
            safe_local_path(root, relative)
            local_paths.add(relative)
            if file_spec.get("media_type") not in {"application/pdf", "application/zip"}:
                raise ValueError("unsupported media_type for {}".format(relative))


def make_document_record(document: Mapping[str, Any], source_kind: str) -> dict[str, Any]:
    fields = (
        "id",
        "title",
        "short_title",
        "document_type",
        "category",
        "jurisdiction",
        "authority",
        "edition",
        "language",
        "publication_date",
        "valid_from",
        "valid_to",
        "priority",
        "access_status",
        "landing_url",
        "license_note",
        "notes",
        "parent_document_id",
        "core_location",
    )
    record = {field: document.get(field) for field in fields}
    record.update(
        {
            "source_kind": source_kind,
            "corpus_status": document.get("corpus_status") or "metadata_only",
            "graph_mention_count": int(document.get("graph_mention_count") or 0),
            "graph_source_document_count": int(document.get("graph_source_document_count") or 0),
            "aliases": list(document.get("aliases") or []),
        }
    )
    return record


def catalog_files(
    catalog: Mapping[str, Any],
    root: Path,
    *,
    download: bool,
    retries: int,
    timeout_seconds: int,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    files: list[dict[str, Any]] = []
    specs_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for document in catalog["documents"]:
        document_id = document["id"]
        for file_spec in document.get("files") or []:
            relative = file_spec["local_path"]
            path = safe_local_path(root, relative)
            status: dict[str, Any]
            if path.exists():
                try:
                    status = {
                        **validate_local_file(path, file_spec["media_type"]),
                        "download_status": "already_present",
                    }
                except ValueError as exc:
                    status = {
                        "download_status": "invalid",
                        "validation_status": "failed",
                        "error": str(exc),
                    }
            elif download:
                status = download_file(
                    file_spec["download_url"],
                    path,
                    file_spec["media_type"],
                    retries=retries,
                    timeout_seconds=timeout_seconds,
                )
            else:
                status = {
                    "download_status": "not_requested",
                    "validation_status": "missing",
                }
            record = {
                "document_id": document_id,
                "role": file_spec["role"],
                "local_path": relative,
                "storage_root": "external_documents",
                "download_url": file_spec["download_url"],
                "media_type": file_spec["media_type"],
                "expand_zip": bool(file_spec.get("expand_zip")),
                **status,
            }
            files.append(record)
            specs_by_document[document_id].append(record)
    return files, specs_by_document


def extract_collection_zip(
    root: Path,
    file_record: Mapping[str, Any],
    parent: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if file_record.get("validation_status") != "valid" or not file_record.get("expand_zip"):
        return [], []
    archive_path = safe_local_path(root, str(file_record["local_path"]))
    extraction_root = archive_path.parent / "extracted"
    extraction_root.mkdir(parents=True, exist_ok=True)
    documents: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        for member in sorted(archive.infolist(), key=lambda item: item.filename.casefold()):
            member_path = Path(member.filename)
            if member.is_dir() or member_path.suffix.casefold() != ".pdf":
                continue
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("unsafe ZIP member: {}".format(member.filename))
            destination = (extraction_root / member_path).resolve()
            if extraction_root.resolve() not in destination.parents:
                raise ValueError("ZIP member escapes extraction root: {}".format(member.filename))
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".part")
            with archive.open(member) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
            stem = member_path.stem
            normalized = re.sub(r"[^a-z0-9]+", "_", stem.casefold()).strip("_")
            document_id = "{}_{}".format(parent["id"], normalized)
            if document_id in seen_ids:
                document_id = stable_id(document_id, member.filename)
            seen_ids.add(document_id)
            short_title = stem.replace("-", " ")
            relative = destination.relative_to(root).as_posix()
            metadata = validate_local_file(destination, "application/pdf")
            documents.append(
                make_document_record(
                    {
                        "id": document_id,
                        "title": short_title,
                        "short_title": short_title,
                        "document_type": "official_technical_rule",
                        "category": parent.get("category"),
                        "jurisdiction": parent.get("jurisdiction"),
                        "authority": parent.get("authority"),
                        "language": parent.get("language"),
                        "priority": "C",
                        "access_status": "public_official_document",
                        "landing_url": parent.get("landing_url"),
                        "parent_document_id": parent["id"],
                        "corpus_status": "downloaded",
                        "aliases": [short_title, stem],
                    },
                    "collection_member",
                )
            )
            files.append(
                {
                    "document_id": document_id,
                    "role": "full_text",
                    "local_path": relative,
                    "storage_root": "external_documents",
                    "download_url": None,
                    "media_type": "application/pdf",
                    "expand_zip": False,
                    "download_status": "extracted",
                    **metadata,
                }
            )
    return documents, files


def latest_cellar_records(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    register = load_json(path)
    result: dict[str, dict[str, Any]] = {}
    for row in register.get("documents") or []:
        celex = str(row.get("base_celex") or "").upper()
        if not celex or row.get("status") != "downloaded":
            continue
        previous = result.get(celex)
        if previous is None or str(row.get("consolidation_date") or "") > str(
            previous.get("consolidation_date") or ""
        ):
            result[celex] = row
    return result


def eurlex_url(celex: str) -> str:
    return "https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:{}".format(quote(celex))


def eurlex_pdf_url(celex: str) -> str:
    return "https://eur-lex.europa.eu/legal-content/DE/TXT/PDF/?uri=CELEX:{}".format(quote(celex))


def cellar_celex_url(celex: str) -> str:
    return "https://publications.europa.eu/resource/celex/{}".format(quote(celex))


def alternate_celex_candidates(celex: str) -> list[str]:
    if not re.fullmatch(r"3\d{4}[RLD]\d{4}", celex):
        return []
    alternatives = [
        celex[:5] + descriptor + celex[6:]
        for descriptor in ("R", "L", "D")
        if descriptor != celex[5]
    ]
    alternatives.append("2" + celex[1:5] + "D" + celex[6:])
    return alternatives


def download_missing_eu_documents(
    rows: Iterable[Mapping[str, Any]],
    root: Path,
    *,
    available_cellar_celex: set[str],
    workers: int,
    retries: int,
    timeout_seconds: int,
    include_unregistered: bool = False,
) -> dict[str, dict[str, Any]]:
    rows = list(rows)
    known_cellar_celex = {
        str(row.get("base_celex") or "").upper()
        for row in rows
        if row.get("category") == "eu_already_in_cellar" and row.get("base_celex")
    }
    existing_directory = root / "documents" / "eu_referenced_originals"
    existing_celex = {
        match.group(1).upper()
        for path in existing_directory.glob("3*")
        if (match := re.match(r"(3\d{4}[RLD]\d{4})", path.name, re.IGNORECASE))
    }
    celex_values = sorted(
        {
            str(row.get("base_celex") or "").upper()
            for row in rows
            if row.get("category")
            in {"eu_already_in_cellar", "eu_legal_act_missing_from_cellar_register"}
            and row.get("base_celex")
            and str(row.get("base_celex") or "").upper() not in available_cellar_celex
            and (
                include_unregistered
                or str(row.get("base_celex") or "").upper() in known_cellar_celex
                or str(row.get("base_celex") or "").upper() in existing_celex
            )
        }
    )

    def fetch(celex: str) -> tuple[str, dict[str, Any]]:
        allow_network = include_unregistered or celex in known_cellar_celex
        relative_formex = (
            Path("documents") / "eu_referenced_originals" / (celex + ".fmx4.zip")
        )
        relative_pdf = Path("documents") / "eu_referenced_originals" / (celex + ".pdf")
        relative_xhtml = Path("documents") / "eu_referenced_originals" / (celex + ".xhtml")
        relative_html = Path("documents") / "eu_referenced_originals" / (celex + ".html")
        destination_formex = root / relative_formex
        destination_pdf = root / relative_pdf
        destination_xhtml = root / relative_xhtml
        destination_html = root / relative_html
        resolved_celex = celex
        formex_status: Optional[dict[str, Any]] = None
        if len(celex) > 5 and celex[5] in {"R", "L"}:
            if destination_formex.exists():
                try:
                    formex_status = {
                        **validate_formex_file(destination_formex),
                        "download_status": "already_present",
                    }
                except (ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
                    formex_status = {
                        "download_status": "invalid",
                        "validation_status": "failed",
                        "error": str(exc),
                    }
            elif allow_network:
                formex_status = download_file(
                    cellar_celex_url(celex),
                    destination_formex,
                    "application/zip",
                    retries=retries,
                    timeout_seconds=timeout_seconds,
                    accept="application/zip;mtype=fmx4",
                    validator=validate_formex_file,
                )
        if formex_status and formex_status.get("validation_status") == "valid":
            status = formex_status
            relative = relative_formex
            media_type = "application/zip"
            download_url = cellar_celex_url(celex)
        elif destination_pdf.exists():
            try:
                status = {
                    **validate_local_file(destination_pdf, "application/pdf"),
                    "download_status": "already_present",
                }
            except ValueError as exc:
                status = {
                    "download_status": "invalid",
                    "validation_status": "failed",
                    "error": str(exc),
                }
            relative = relative_pdf
            media_type = "application/pdf"
            download_url = eurlex_pdf_url(celex)
        elif destination_xhtml.exists():
            status = {
                **validate_local_file(destination_xhtml, "application/xhtml+xml"),
                "download_status": "already_present",
            }
            relative = relative_xhtml
            media_type = "application/xhtml+xml"
            download_url = cellar_celex_url(celex)
        elif destination_html.exists():
            status = {
                **validate_local_file(destination_html, "text/html"),
                "download_status": "already_present",
            }
            relative = relative_html
            media_type = "text/html"
            download_url = cellar_celex_url(celex)
        elif allow_network:
            status = download_file(
                eurlex_pdf_url(celex),
                destination_pdf,
                "application/pdf",
                retries=retries,
                timeout_seconds=timeout_seconds,
            )
            relative = relative_pdf
            media_type = "application/pdf"
            download_url = eurlex_pdf_url(celex)
            pdf_error = status.get("error")
            if status.get("validation_status") != "valid":
                status = download_file(
                    cellar_celex_url(celex),
                    destination_xhtml,
                    "application/xhtml+xml",
                    retries=retries,
                    timeout_seconds=timeout_seconds,
                )
                relative = relative_xhtml
                media_type = "application/xhtml+xml"
                download_url = cellar_celex_url(celex)
            if status.get("validation_status") != "valid":
                xhtml_error = status.get("error")
                status = download_file(
                    cellar_celex_url(celex),
                    destination_html,
                    "text/html",
                    retries=retries,
                    timeout_seconds=timeout_seconds,
                )
                relative = relative_html
                media_type = "text/html"
                download_url = cellar_celex_url(celex)
                if status.get("validation_status") != "valid":
                    status["error"] = "PDF: {}; XHTML: {}; HTML: {}".format(
                        pdf_error,
                        xhtml_error,
                        status.get("error"),
                    )
        else:
            status = {
                "download_status": "not_requested",
                "validation_status": "missing",
                "error": "unregistered inferred CELEX was not downloaded automatically",
            }
            relative = relative_pdf
            media_type = "application/pdf"
            download_url = eurlex_pdf_url(celex)
        if status.get("validation_status") != "valid" and allow_network:
            original_error = status.get("error")
            for alternative in alternate_celex_candidates(celex):
                alternative_relative = (
                    Path("documents") / "eu_referenced_originals" / (alternative + ".xhtml")
                )
                alternative_destination = root / alternative_relative
                if alternative_destination.exists():
                    try:
                        alternative_status = {
                            **validate_local_file(
                                alternative_destination,
                                "application/xhtml+xml",
                            ),
                            "download_status": "already_present",
                        }
                    except ValueError as exc:
                        alternative_status = {
                            "download_status": "invalid",
                            "validation_status": "failed",
                            "error": str(exc),
                        }
                else:
                    alternative_status = download_file(
                        cellar_celex_url(alternative),
                        alternative_destination,
                        "application/xhtml+xml",
                        retries=retries,
                        timeout_seconds=timeout_seconds,
                    )
                if alternative_status.get("validation_status") == "valid":
                    status = alternative_status
                    relative = alternative_relative
                    media_type = "application/xhtml+xml"
                    download_url = cellar_celex_url(alternative)
                    resolved_celex = alternative
                    break
            if status.get("validation_status") != "valid":
                status["error"] = "{}; no unique act-type fallback found".format(original_error)
        return celex, {
            "document_id": "eu_{}".format(resolved_celex.casefold()),
            "resolved_celex": resolved_celex,
            "role": "original_legal_text",
            "local_path": relative.as_posix(),
            "storage_root": "external_documents",
            "download_url": download_url,
            "media_type": media_type,
            "expand_zip": False,
            **status,
        }

    results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch, celex): celex for celex in celex_values}
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            celex, status = future.result()
            results[celex] = status
            if index % 50 == 0 or index == len(futures):
                print("EU originals: {}/{}".format(index, len(futures)), flush=True)
    return results


def normalized_alias(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    ascii_value = "".join(character for character in folded if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def build_register(
    catalog: Mapping[str, Any],
    inventory: Mapping[str, Any],
    cellar: Mapping[str, Mapping[str, Any]],
    catalog_file_rows: list[dict[str, Any]],
    files_by_document: Mapping[str, list[dict[str, Any]]],
    collection_documents: list[dict[str, Any]],
    collection_file_rows: list[dict[str, Any]],
    eu_downloads: Mapping[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    documents: dict[str, dict[str, Any]] = {}
    files = [*catalog_file_rows, *collection_file_rows]
    graph_references: list[dict[str, Any]] = []
    technical_citations: list[dict[str, Any]] = []

    for source in catalog["documents"]:
        record = make_document_record(source, "curated")
        statuses = files_by_document.get(source["id"], [])
        valid_count = sum(row.get("validation_status") == "valid" for row in statuses)
        if valid_count:
            if str(source.get("access_status") or "").startswith("paid_"):
                record["corpus_status"] = "metadata_with_public_supplement"
            else:
                record["corpus_status"] = "downloaded"
        elif statuses:
            record["corpus_status"] = "download_failed_or_missing"
        documents[record["id"]] = record

    for record in collection_documents:
        documents[record["id"]] = record

    collection_aliases: dict[str, str] = {}
    for record in collection_documents:
        for alias in [record.get("short_title") or "", *(record.get("aliases") or [])]:
            collection_aliases[normalized_alias(alias)] = record["id"]

    graph_counts: Counter[str] = Counter()
    graph_source_counts: Counter[str] = Counter()

    for row in inventory.get("relationship_targets") or []:
        category = str(row.get("category") or "")
        target_key = str(row.get("target_document_key") or "")
        celex = str(row.get("base_celex") or "").upper() or None
        document_id: Optional[str] = None
        candidate_document_id: Optional[str] = None
        resolution_status = "metadata_only_unresolved"
        exclusion_reason: Optional[str] = None
        if category == "case_law_excluded":
            exclusion_reason = "case_law_excluded_by_scope"
            resolution_status = "excluded_case_law"
        elif category == "de_already_in_gii_corpus":
            core = row.get("core_document") or {}
            core_graph_id = str(core.get("graph_id") or stable_id("doc", target_key))
            document_id = "de_{}".format(core_graph_id)
            if document_id not in documents:
                source_xml = str(core.get("source_xml") or "")
                source_zip = str(core.get("source_zip") or "")
                if source_xml:
                    core_location = "gesetze_im_internet_xml/{}".format(source_xml)
                elif source_zip:
                    core_location = "gesetze_im_internet_xml/{}".format(source_zip)
                else:
                    core_location = (
                        "pdf-neo4j-pipeline/output/gii_full_chunked_resolved_v2/neo4j_csv/"
                        "nodes_009_document.csv#{}".format(core_graph_id)
                    )
                documents[document_id] = make_document_record(
                    {
                        "id": document_id,
                        "title": core.get("title") or display_key(target_key),
                        "short_title": core.get("short_title") or display_key(target_key),
                        "document_type": "german_legal_act",
                        "category": "german_legal_act",
                        "jurisdiction": "DE",
                        "authority": "Bundesministerium der Justiz",
                        "language": "de",
                        "priority": priority_for_mentions(int(row.get("mention_count") or 0)),
                        "access_status": "local_core_copy",
                        "corpus_status": "already_in_core_de_corpus",
                        "core_location": core_location,
                        "aliases": [
                            target_key,
                            str(core.get("document_global_key") or ""),
                            str(core.get("document_key") or ""),
                            str(core.get("global_key") or ""),
                        ],
                        "notes": (
                            "Das Dokument liegt bereits im deutschen Kernkorpus. Offen ist nur "
                            "das konkrete interne Verweisziel; es wird nicht erneut heruntergeladen."
                        ),
                    },
                    "graph_target",
                )
            resolution_status = "exact_german_core_document"
        elif celex:
            downloaded = eu_downloads.get(celex)
            resolved_celex = str(downloaded.get("resolved_celex") or celex) if downloaded else celex
            is_candidate = bool(
                downloaded
                and downloaded.get("validation_status") == "valid"
                and resolved_celex != celex
            )
            if is_candidate:
                candidate_document_id = "eu_{}".format(resolved_celex.casefold())
                if candidate_document_id not in documents:
                    documents[candidate_document_id] = make_document_record(
                        {
                            "id": candidate_document_id,
                            "title": "EU-Dokumentkandidat {}".format(resolved_celex),
                            "short_title": resolved_celex,
                            "document_type": "eu_legal_act_candidate",
                            "category": "eu_legal_act_candidate",
                            "jurisdiction": "EU",
                            "authority": "European Union",
                            "language": "de",
                            "priority": priority_for_mentions(int(row.get("mention_count") or 0)),
                            "access_status": "public_official_document",
                            "landing_url": eurlex_url(resolved_celex),
                            "corpus_status": "downloaded_eu_candidate",
                            "aliases": [resolved_celex],
                            "notes": (
                                "Amtlicher Dokumentkandidat mit gleicher Jahres-/Nummernkombination, "
                                "aber anderem Akttyp oder CELEX-Sektor. Nicht als bestätigtes Verweisziel behandelt."
                            ),
                        },
                        "eu_candidate",
                    )
                    files.append({**downloaded, "document_id": candidate_document_id})
                resolution_status = "candidate_act_type_or_sector_match"

            document_id = "eu_{}".format(celex.casefold())
            if not is_candidate:
                if cellar.get(celex):
                    resolution_status = "exact_celex_core"
                elif downloaded and downloaded.get("validation_status") == "valid":
                    resolution_status = "exact_celex_downloaded"
                else:
                    resolution_status = "exact_celex_unavailable"
            if document_id not in documents:
                cellar_record = cellar.get(celex)
                if cellar_record:
                    corpus_status = "already_in_core_eu_corpus"
                    access_status = "local_core_copy"
                    core_location = "eurlex_consolidated_de/{}".format(cellar_record["local_path"])
                    title = cellar_record.get("title") or celex
                    resolution_status = "exact_celex_core"
                elif downloaded and downloaded.get("validation_status") == "valid" and not is_candidate:
                    corpus_status = "downloaded_original_eu_text"
                    access_status = "public_official_document"
                    core_location = None
                    title = display_key(target_key)
                    downloaded = {**downloaded, "document_id": document_id}
                    files.append(downloaded)
                    resolution_status = "exact_celex_downloaded"
                else:
                    corpus_status = "metadata_only_eu_text"
                    access_status = "public_official_landing"
                    core_location = None
                    title = display_key(target_key)
                    if downloaded and not is_candidate:
                        downloaded = {**downloaded, "document_id": document_id}
                        files.append(downloaded)
                    if not is_candidate:
                        resolution_status = "exact_celex_unavailable"
                documents[document_id] = make_document_record(
                    {
                        "id": document_id,
                        "title": title,
                        "short_title": celex,
                        "document_type": "eu_legal_act",
                        "category": "eu_legal_act",
                        "jurisdiction": "EU",
                        "authority": "European Union",
                        "language": "de",
                        "priority": priority_for_mentions(int(row.get("mention_count") or 0)),
                        "access_status": access_status,
                        "landing_url": eurlex_url(celex),
                        "corpus_status": corpus_status,
                        "core_location": core_location,
                        "aliases": [celex, target_key],
                        "notes": (
                            "Ein amtlicher Kandidat {} wurde gefunden, aber wegen abweichendem "
                            "Akttyp oder CELEX-Sektor nicht automatisch als Ziel bestätigt.".format(
                                resolved_celex
                            )
                            if is_candidate
                            else None
                        ),
                    },
                    "graph_target",
                )
        else:
            document_id = stable_id("graph_target", target_key)
            if document_id not in documents:
                access_status = (
                    "ambiguous_reference"
                    if category == "ambiguous_or_generic"
                    else "unresolved_external_reference"
                )
                documents[document_id] = make_document_record(
                    {
                        "id": document_id,
                        "title": display_key(target_key),
                        "short_title": display_key(target_key),
                        "document_type": category,
                        "category": "graph_external_target",
                        "jurisdiction": None,
                        "authority": None,
                        "language": "de",
                        "priority": priority_for_mentions(int(row.get("mention_count") or 0)),
                        "access_status": access_status,
                        "corpus_status": "metadata_only_unresolved_target",
                        "aliases": [target_key],
                        "notes": "Nicht automatisch heruntergeladen, weil der Verweis nicht eindeutig einer amtlichen Fassung zugeordnet ist.",
                    },
                    "graph_target",
                )
        if document_id:
            graph_counts[document_id] += int(row.get("mention_count") or 0)
            graph_source_counts[document_id] += int(row.get("source_document_count") or 0)
        graph_references.append(
            {
                "target_document_key": target_key,
                "category": category,
                "base_celex": celex,
                "resolved_base_celex": (
                    str(eu_downloads.get(celex, {}).get("resolved_celex") or celex)
                    if celex
                    else None
                ),
                "document_id": document_id,
                "candidate_document_id": candidate_document_id,
                "resolution_status": resolution_status,
                "mention_count": int(row.get("mention_count") or 0),
                "source_document_count": int(row.get("source_document_count") or 0),
                "requested_target_count": int(row.get("requested_target_count") or 0),
                "exclusion_reason": exclusion_reason,
                "samples": row.get("samples") or [],
            }
        )

    technical_counts: Counter[str] = Counter()
    technical_sources: Counter[str] = Counter()
    for row in inventory.get("technical_citations") or []:
        family = str(row.get("family") or "")
        citation = str(row.get("normalized_citation") or "")
        document_id = FAMILY_DOCUMENTS.get(family)
        if family == "dguv":
            number = re.search(r"vorschrift\s+(\d+)", citation)
            if number and "dguv_vorschrift_{}".format(number.group(1)) in documents:
                document_id = "dguv_vorschrift_{}".format(number.group(1))
        elif family == "trgs":
            normalized = normalized_alias(citation)
            match = re.search(r"trgs\s+(\d{3}(?:\s+\d+)?)", normalized)
            if match:
                wanted = "trgs {}".format(match.group(1))
                document_id = collection_aliases.get(wanted)
        if not document_id:
            document_id = stable_id("technical_{}".format(family), citation)
            if document_id not in documents:
                if family in LICENSED_FAMILIES:
                    access_status = "licensed_full_text_metadata_only"
                    notes = "Der Volltext ist lizenz- oder kostenpflichtig und wurde nicht heruntergeladen."
                elif family == "trgs":
                    access_status = "historical_or_repealed_rule_metadata_only"
                    notes = "Die zitierte TRGS ist nicht in der aktuellen BAuA-Sammlung enthalten."
                else:
                    access_status = "metadata_only"
                    notes = None
                original = next(
                    (
                        str(sample.get("citation"))
                        for sample in row.get("samples") or []
                        if sample.get("citation")
                    ),
                    citation,
                )
                documents[document_id] = make_document_record(
                    {
                        "id": document_id,
                        "title": original,
                        "short_title": original,
                        "document_type": "technical_standard_or_rule",
                        "category": family,
                        "jurisdiction": None,
                        "authority": family.upper(),
                        "language": None,
                        "priority": priority_for_mentions(int(row.get("mention_count") or 0)),
                        "access_status": access_status,
                        "landing_url": TECHNICAL_LANDING_PAGES.get(family),
                        "corpus_status": "metadata_only",
                        "aliases": [original, citation],
                        "notes": notes,
                    },
                    "technical_citation",
                )
        technical_counts[document_id] += int(row.get("mention_count") or 0)
        technical_sources[document_id] += int(row.get("source_document_count") or 0)
        technical_citations.append(
            {
                "family": family,
                "normalized_citation": citation,
                "document_id": document_id,
                "mention_count": int(row.get("mention_count") or 0),
                "source_document_count": int(row.get("source_document_count") or 0),
                "samples": row.get("samples") or [],
            }
        )

    for document_id, record in documents.items():
        record["graph_mention_count"] = graph_counts[document_id] + technical_counts[document_id]
        record["graph_source_document_count"] = graph_source_counts[document_id] + technical_sources[document_id]
        if record["graph_mention_count"]:
            inferred = priority_for_mentions(record["graph_mention_count"])
            if {"A": 3, "B": 2, "C": 1}.get(inferred, 0) > {"A": 3, "B": 2, "C": 1}.get(
                record.get("priority"), 0
            ):
                record["priority"] = inferred

    return list(documents.values()), files, graph_references, technical_citations


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)


def write_sqlite(
    path: Path,
    documents: list[dict[str, Any]],
    files: list[dict[str, Any]],
    graph_references: list[dict[str, Any]],
    technical_citations: list[dict[str, Any]],
    generated_at: str,
) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    connection = sqlite3.connect(str(temporary))
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE documents (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              short_title TEXT,
              document_type TEXT,
              category TEXT,
              jurisdiction TEXT,
              authority TEXT,
              edition TEXT,
              language TEXT,
              publication_date TEXT,
              valid_from TEXT,
              valid_to TEXT,
              priority TEXT,
              access_status TEXT,
              corpus_status TEXT,
              source_kind TEXT,
              landing_url TEXT,
              license_note TEXT,
              notes TEXT,
              parent_document_id TEXT REFERENCES documents(id),
              core_location TEXT,
              graph_mention_count INTEGER NOT NULL DEFAULT 0,
              graph_source_document_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE aliases (
              document_id TEXT NOT NULL REFERENCES documents(id),
              alias TEXT NOT NULL,
              normalized_alias TEXT NOT NULL,
              PRIMARY KEY (document_id, normalized_alias)
            );
            CREATE TABLE files (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              document_id TEXT NOT NULL REFERENCES documents(id),
              role TEXT NOT NULL,
              local_path TEXT,
              storage_root TEXT,
              download_url TEXT,
              final_url TEXT,
              media_type TEXT,
              size_bytes INTEGER,
              sha256 TEXT,
              page_count INTEGER,
              download_status TEXT,
              validation_status TEXT,
              retrieved_at TEXT,
              error TEXT
            );
            CREATE TABLE graph_references (
              target_document_key TEXT PRIMARY KEY,
              category TEXT NOT NULL,
              base_celex TEXT,
              resolved_base_celex TEXT,
              document_id TEXT REFERENCES documents(id),
              candidate_document_id TEXT REFERENCES documents(id),
              resolution_status TEXT NOT NULL,
              mention_count INTEGER NOT NULL,
              source_document_count INTEGER NOT NULL,
              requested_target_count INTEGER NOT NULL,
              exclusion_reason TEXT,
              samples_json TEXT NOT NULL
            );
            CREATE TABLE technical_citations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              family TEXT NOT NULL,
              normalized_citation TEXT NOT NULL,
              document_id TEXT REFERENCES documents(id),
              mention_count INTEGER NOT NULL,
              source_document_count INTEGER NOT NULL,
              samples_json TEXT NOT NULL,
              UNIQUE (family, normalized_citation)
            );
            CREATE INDEX documents_priority_idx ON documents(priority);
            CREATE INDEX documents_status_idx ON documents(corpus_status);
            CREATE INDEX graph_references_document_idx ON graph_references(document_id);
            CREATE INDEX technical_citations_document_idx ON technical_citations(document_id);
            """
        )
        connection.execute("INSERT INTO metadata VALUES (?, ?)", ("schema_version", "1.0"))
        connection.execute("INSERT INTO metadata VALUES (?, ?)", ("generated_at", generated_at))
        for record in documents:
            connection.execute(
                """INSERT INTO documents VALUES (
                :id, :title, :short_title, :document_type, :category, :jurisdiction,
                :authority, :edition, :language, :publication_date, :valid_from,
                :valid_to, :priority, :access_status, :corpus_status, :source_kind,
                :landing_url, :license_note, :notes, :parent_document_id,
                :core_location, :graph_mention_count, :graph_source_document_count
                )""",
                record,
            )
            for alias in sorted(set(record.get("aliases") or [])):
                if alias:
                    connection.execute(
                        "INSERT OR IGNORE INTO aliases VALUES (?, ?, ?)",
                        (record["id"], alias, normalized_alias(alias)),
                    )
        for record in files:
            if record["document_id"] not in {row["id"] for row in documents}:
                continue
            connection.execute(
                """INSERT INTO files (
                document_id, role, local_path, storage_root, download_url, final_url,
                media_type, size_bytes, sha256, page_count, download_status,
                validation_status, retrieved_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(
                    record.get(field)
                    for field in (
                        "document_id",
                        "role",
                        "local_path",
                        "storage_root",
                        "download_url",
                        "final_url",
                        "media_type",
                        "size_bytes",
                        "sha256",
                        "page_count",
                        "download_status",
                        "validation_status",
                        "retrieved_at",
                        "error",
                    )
                ),
            )
        for record in graph_references:
            connection.execute(
                "INSERT INTO graph_references VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["target_document_key"],
                    record["category"],
                    record.get("base_celex"),
                    record.get("resolved_base_celex"),
                    record.get("document_id"),
                    record.get("candidate_document_id"),
                    record["resolution_status"],
                    record["mention_count"],
                    record["source_document_count"],
                    record["requested_target_count"],
                    record.get("exclusion_reason"),
                    json.dumps(record.get("samples") or [], ensure_ascii=False),
                ),
            )
        for record in technical_citations:
            connection.execute(
                """INSERT INTO technical_citations (
                family, normalized_citation, document_id, mention_count,
                source_document_count, samples_json
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record["family"],
                    record["normalized_citation"],
                    record.get("document_id"),
                    record["mention_count"],
                    record["source_document_count"],
                    json.dumps(record.get("samples") or [], ensure_ascii=False),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--cellar-register", type=Path, default=DEFAULT_CELLAR_REGISTER)
    parser.add_argument("--download", action="store_true", help="download missing curated public files")
    parser.add_argument(
        "--download-eu-missing",
        action="store_true",
        help="download original German EUR-Lex PDFs for unambiguous CELEX targets missing from the consolidated corpus",
    )
    parser.add_argument(
        "--download-eu-unregistered",
        action="store_true",
        help=(
            "also try inferred CELEX identifiers not present in the Cellar inventory; "
            "by default only Cellar-confirmed missing acts are fetched"
        ),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    catalog = load_json(args.catalog)
    inventory = load_json(args.inventory)
    validate_catalog(catalog, args.root)
    cellar = latest_cellar_records(args.cellar_register)

    catalog_file_rows, files_by_document = catalog_files(
        catalog,
        args.root,
        download=args.download,
        retries=args.retries,
        timeout_seconds=args.timeout_seconds,
    )
    collection_documents: list[dict[str, Any]] = []
    collection_file_rows: list[dict[str, Any]] = []
    catalog_by_id = {row["id"]: row for row in catalog["documents"]}
    for file_record in catalog_file_rows:
        members, member_files = extract_collection_zip(
            args.root,
            file_record,
            catalog_by_id[file_record["document_id"]],
        )
        collection_documents.extend(members)
        collection_file_rows.extend(member_files)

    eu_downloads: dict[str, dict[str, Any]] = {}
    if args.download_eu_missing:
        eu_downloads = download_missing_eu_documents(
            inventory.get("relationship_targets") or [],
            args.root,
            available_cellar_celex=set(cellar),
            workers=args.workers,
            retries=args.retries,
            timeout_seconds=args.timeout_seconds,
            include_unregistered=args.download_eu_unregistered,
        )

    documents, files, graph_references, technical_citations = build_register(
        catalog,
        inventory,
        cellar,
        catalog_file_rows,
        files_by_document,
        collection_documents,
        collection_file_rows,
        eu_downloads,
    )
    documents.sort(key=lambda row: (row.get("priority") or "Z", row["id"]))
    files.sort(key=lambda row: (row["document_id"], row.get("role") or ""))
    graph_references.sort(key=lambda row: (-row["mention_count"], row["target_document_key"]))
    technical_citations.sort(key=lambda row: (-row["mention_count"], row["family"], row["normalized_citation"]))

    generated_at = utc_now()
    document_statuses = Counter(row["corpus_status"] for row in documents)
    access_statuses = Counter(row.get("access_status") or "unknown" for row in documents)
    file_statuses = Counter(row.get("validation_status") or "unknown" for row in files)
    reference_statuses = Counter(row["resolution_status"] for row in graph_references)
    reference_mentions_by_status: Counter[str] = Counter()
    reference_categories = Counter(row["category"] for row in graph_references)
    reference_mentions_by_category: Counter[str] = Counter()
    for row in graph_references:
        reference_mentions_by_status[row["resolution_status"]] += row["mention_count"]
        reference_mentions_by_category[row["category"]] += row["mention_count"]
    technical_families = Counter(row["family"] for row in technical_citations)
    technical_mentions_by_family: Counter[str] = Counter()
    for row in technical_citations:
        technical_mentions_by_family[row["family"]] += row["mention_count"]
    summary = {
        "documents": len(documents),
        "files": len(files),
        "valid_local_files": file_statuses.get("valid", 0),
        "graph_reference_targets": len(graph_references),
        "graph_reference_mentions": sum(row["mention_count"] for row in graph_references),
        "technical_citation_variants": len(technical_citations),
        "technical_citation_mentions": sum(row["mention_count"] for row in technical_citations),
        "excluded_case_law_targets": sum(row["category"] == "case_law_excluded" for row in graph_references),
        "graph_references_by_resolution_status": dict(sorted(reference_statuses.items())),
        "graph_reference_mentions_by_resolution_status": dict(
            sorted(reference_mentions_by_status.items())
        ),
        "graph_references_by_category": dict(sorted(reference_categories.items())),
        "graph_reference_mentions_by_category": dict(
            sorted(reference_mentions_by_category.items())
        ),
        "technical_citations_by_family": dict(sorted(technical_families.items())),
        "technical_citation_mentions_by_family": dict(
            sorted(technical_mentions_by_family.items())
        ),
        "documents_by_corpus_status": dict(sorted(document_statuses.items())),
        "documents_by_access_status": dict(sorted(access_statuses.items())),
        "files_by_validation_status": dict(sorted(file_statuses.items())),
    }
    register = {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "catalog": str(args.catalog.resolve()),
        "inventory": str(args.inventory.resolve()),
        "cellar_register": str(args.cellar_register.resolve()),
        "summary": summary,
        "documents": documents,
        "files": files,
    }
    write_json(args.root / "register.json", register)
    write_jsonl(args.root / "register.jsonl", documents)
    write_json(
        args.root / "download_report.json",
        {
            "schema_version": "1.0",
            "generated_at": generated_at,
            "summary": summary,
            "failed_files": [
                row for row in files if row.get("validation_status") not in {"valid"}
            ],
        },
    )
    write_sqlite(
        args.root / "register.sqlite",
        documents,
        files,
        graph_references,
        technical_citations,
        generated_at,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
