#!/usr/bin/env python3
"""Inventory and download German consolidated EU legal acts from Cellar.

The script queries the Cellar knowledge graph descriptor by descriptor so the
10,000-row endpoint limit cannot silently truncate the corpus.  For every base
act it selects the most recent consolidation whose consolidation date is not
later than the requested snapshot date.  German Formex 4 ZIPs are preferred;
XHTML, legacy HTML, and PDF are documented fallbacks when Formex is unavailable.

The output is a rebuildable corpus rather than a hand-curated directory:

* ``register.json`` contains run metadata and one record per base act.
* ``register.jsonl`` contains the same records in streaming-friendly form.
* ``download_results.jsonl`` is an append-only completion log.
* ``packages/<descriptor>/`` contains the downloaded manifestations.

Downloads are staged, size-limited, validated, hashed, and atomically renamed.
The command is safe to resume after interruption.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import tempfile
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


TOOL_NAME = "download_cellar_consolidated"
TOOL_VERSION = "1.0.0"
REGISTER_SCHEMA_VERSION = "1.0"
SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
PUBLICATION_URL = "https://publications.europa.eu/resource/celex/{celex}"
USER_AGENT = "cellar-consolidated-corpus/{} (+research corpus)".format(TOOL_VERSION)
LANGUAGE = "deu"
FORMEX_ACCEPT = "application/zip;mtype=fmx4"
XHTML_ACCEPT = "application/xhtml+xml"
HTML_ACCEPT = "text/html"
PDF_ACCEPT = "application/pdf"
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
CHECKPOINT_EVERY = 50
DIRECT_FORMAT_PRIORITY = {
    "fmx4": 0,
    "xhtml": 1,
    "html": 2,
    "pdfa1a": 3,
    "pdfa1b": 4,
    "pdf": 5,
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_OUT_DIR = REPO_ROOT / "eurlex_consolidated_de"

DESCRIPTOR_LABELS = {
    "A": "opinion",
    "B": "budget",
    "C": "declaration",
    "D": "decision",
    "E": "cfsp_act",
    "F": "police_judicial_cooperation_act",
    "G": "resolution",
    "H": "recommendation",
    "J": "state_aid_non_opposition",
    "K": "ecsc_recommendation",
    "L": "directive",
    "M": "merger_non_opposition",
    "O": "ecb_guideline",
    "Q": "institutional_act",
    "R": "regulation",
    "S": "ecsc_general_decision",
    "X": "other_oj_l_act",
    "Y": "other_oj_c_act",
}


class CorpusError(RuntimeError):
    """Expected discovery, download, or validation failure."""


class ValidationError(CorpusError):
    """Downloaded content is not the requested safe manifestation."""


@dataclass(frozen=True)
class InventoryItem:
    descriptor: str
    descriptor_label: str
    base_cellar_uri: str
    base_celex_uri: str
    base_celex: str
    consolidated_cellar_uri: str
    consolidated_celex: str
    consolidation_date: str

    @property
    def download_url(self) -> str:
        return PUBLICATION_URL.format(celex=quote(self.consolidated_celex, safe="-"))


@dataclass(frozen=True)
class DownloadConfig:
    output_dir: Path
    timeout_seconds: float
    retries: int
    retry_backoff_seconds: float
    max_download_bytes: int
    max_uncompressed_bytes: int
    max_zip_members: int


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=".{}-".format(path.name), suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_text(values: Iterable[str]) -> str:
    return " ".join(" ".join(values).split())


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def fetch_bytes(
    url: str,
    *,
    headers: Mapping[str, str],
    timeout_seconds: float,
    retries: int,
    backoff_seconds: float,
) -> tuple[bytes, str, str]:
    last_error: Optional[BaseException] = None
    for attempt in range(1, retries + 2):
        request = Request(url, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read()
                return (
                    payload,
                    response.geturl(),
                    response.headers.get_content_type(),
                )
        except HTTPError as exc:
            last_error = exc
            if exc.code not in RETRYABLE_STATUSES or attempt > retries:
                raise CorpusError("HTTP {} for {}".format(exc.code, url)) from exc
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            last_error = exc
            if attempt > retries:
                raise CorpusError("request failed for {}: {}".format(url, exc)) from exc
        time.sleep(backoff_seconds * (2 ** (attempt - 1)))
    raise CorpusError("request failed for {}: {}".format(url, last_error))


def sparql_csv(
    query: str,
    *,
    timeout_seconds: float,
    retries: int,
    backoff_seconds: float,
) -> list[dict[str, str]]:
    url = "{}?{}".format(SPARQL_ENDPOINT, urlencode({"query": query}))
    payload, _, _ = fetch_bytes(
        url,
        headers={"Accept": "text/csv"},
        timeout_seconds=timeout_seconds,
        retries=retries,
        backoff_seconds=backoff_seconds,
    )
    text = payload.decode("utf-8-sig")
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def batches(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def descriptor_inventory_query() -> str:
    return """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT ?descriptor (COUNT(DISTINCT ?base) AS ?baseActs)
       (COUNT(DISTINCT ?consolidated) AS ?versions)
WHERE {
  ?consolidated a cdm:act_consolidated ;
    cdm:act_consolidated_based_on_resource_legal ?base .
  ?base owl:sameAs ?baseId .
  FILTER(CONTAINS(STR(?baseId), "/resource/celex/3"))
  BIND(REPLACE(STR(?baseId),
    ".*/celex/3[0-9]{4}([^0-9(]+).*", "$1") AS ?descriptor)
  FILTER(STR(?descriptor) != STR(?baseId))
}
GROUP BY ?descriptor
ORDER BY ?descriptor
""".strip()


def latest_inventory_query(descriptor: str, snapshot_date: str) -> str:
    if not re.fullmatch(r"[A-Z]{1,3}", descriptor):
        raise ValueError("invalid CELEX descriptor: {!r}".format(descriptor))
    pattern = (
        "^http://publications.europa.eu/resource/celex/"
        "3[0-9]{4}" + descriptor + "[0-9]"
    )
    return """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?base ?baseCelex ?consolidated ?consolidatedCelex ?date
WHERE {
  {
    SELECT ?base (MAX(?candidateDate) AS ?latestDate)
    WHERE {
      ?candidate a cdm:act_consolidated ;
        cdm:act_consolidated_based_on_resource_legal ?base ;
        cdm:act_consolidated_date ?candidateDateRaw .
      ?base owl:sameAs ?candidateBaseCelex .
      BIND(xsd:date(STR(?candidateDateRaw)) AS ?candidateDate)
      FILTER(REGEX(STR(?candidateBaseCelex), "%(pattern)s"))
      FILTER(?candidateDate <= "%(snapshot)s"^^xsd:date)
    }
    GROUP BY ?base
  }
  ?consolidated a cdm:act_consolidated ;
    cdm:act_consolidated_based_on_resource_legal ?base ;
    cdm:act_consolidated_date ?dateRaw ;
    cdm:resource_legal_id_celex ?consolidatedCelex .
  ?base owl:sameAs ?baseCelex .
  BIND(xsd:date(STR(?dateRaw)) AS ?date)
  FILTER(REGEX(STR(?baseCelex), "%(pattern)s"))
  FILTER(?date = ?latestDate)
}
ORDER BY ?consolidatedCelex
""".strip() % {"pattern": pattern, "snapshot": snapshot_date}


def discover_inventory(
    *,
    descriptors: Optional[Sequence[str]],
    snapshot_date: str,
    timeout_seconds: float,
    retries: int,
    backoff_seconds: float,
) -> tuple[list[InventoryItem], list[dict[str, Any]]]:
    descriptor_rows = sparql_csv(
        descriptor_inventory_query(),
        timeout_seconds=timeout_seconds,
        retries=retries,
        backoff_seconds=backoff_seconds,
    )
    available = {
        row["descriptor"]: {
            "descriptor": row["descriptor"],
            "descriptor_label": DESCRIPTOR_LABELS.get(row["descriptor"], "unknown"),
            "base_acts_all_dates": int(row.get("baseActs") or 0),
            "consolidated_versions_all_dates": int(row.get("versions") or 0),
        }
        for row in descriptor_rows
        if row.get("descriptor")
    }
    selected = sorted(set(descriptors or available))
    unknown = sorted(set(selected) - set(available))
    if unknown:
        raise CorpusError(
            "requested descriptors have no consolidated acts in Cellar: {}".format(
                ", ".join(unknown)
            )
        )

    inventory: list[InventoryItem] = []
    for descriptor in selected:
        rows = sparql_csv(
            latest_inventory_query(descriptor, snapshot_date),
            timeout_seconds=timeout_seconds,
            retries=retries,
            backoff_seconds=backoff_seconds,
        )
        for row in rows:
            base_celex_uri = row["baseCelex"]
            consolidated_celex = row["consolidatedCelex"]
            item = InventoryItem(
                descriptor=descriptor,
                descriptor_label=DESCRIPTOR_LABELS.get(descriptor, "unknown"),
                base_cellar_uri=row["base"],
                base_celex_uri=base_celex_uri,
                base_celex=unquote(base_celex_uri.rsplit("/", 1)[-1]),
                consolidated_cellar_uri=row["consolidated"],
                consolidated_celex=consolidated_celex,
                consolidation_date=row["date"],
            )
            inventory.append(item)
        available[descriptor]["latest_at_snapshot"] = len(rows)

    deduplicated: dict[str, InventoryItem] = {}
    for item in inventory:
        existing = deduplicated.get(item.base_celex)
        if existing and existing.consolidated_celex != item.consolidated_celex:
            raise CorpusError(
                "multiple latest consolidations for {}: {} and {}".format(
                    item.base_celex,
                    existing.consolidated_celex,
                    item.consolidated_celex,
                )
            )
        deduplicated[item.base_celex] = item
    return (
        sorted(deduplicated.values(), key=lambda item: (item.descriptor, item.base_celex)),
        [available[key] for key in selected],
    )


def direct_manifest_query(work_uris: Sequence[str]) -> str:
    values = " ".join("<{}>".format(uri) for uri in work_uris)
    return """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX purl: <http://purl.org/dc/elements/1.1/>
SELECT DISTINCT ?work ?manifestation ?format ?item ?itemId ?alias
WHERE {
  VALUES ?work { %(values)s }
  ?expression cdm:expression_belongs_to_work ?work ;
    cdm:expression_uses_language ?language .
  ?language purl:identifier "DEU" .
  ?manifestation cdm:manifestation_manifests_expression ?expression ;
    cdm:manifestation_type ?format .
  ?item cdm:item_belongs_to_manifestation ?manifestation .
  OPTIONAL { ?item cdm:item_identifier ?itemId }
  OPTIONAL { ?item owl:sameAs ?alias }
}
ORDER BY ?work ?manifestation ?item
""".strip() % {"values": values}


def discover_direct_manifests(
    items: Sequence[InventoryItem],
    *,
    timeout_seconds: float,
    retries: int,
    backoff_seconds: float,
) -> dict[str, list[dict[str, str]]]:
    work_to_base = {item.consolidated_cellar_uri: item.base_celex for item in items}
    records: dict[str, dict[str, dict[str, str]]] = {}
    work_uris = sorted(work_to_base)
    for group in batches(work_uris, 40):
        rows = sparql_csv(
            direct_manifest_query(group),
            timeout_seconds=timeout_seconds,
            retries=retries,
            backoff_seconds=backoff_seconds,
        )
        for row in rows:
            base_celex = work_to_base.get(row.get("work", ""))
            item_uri = row.get("item")
            manifestation = row.get("manifestation")
            if not base_celex or not item_uri or not manifestation:
                continue
            by_item = records.setdefault(base_celex, {}).setdefault(manifestation, {})
            existing_raw = by_item.get(item_uri)
            existing = json.loads(existing_raw) if existing_raw else None
            candidate = {
                "manifestation": manifestation,
                "format": row.get("format", ""),
                "item": item_uri,
                "item_id": row.get("itemId", ""),
                "alias": row.get("alias", ""),
            }
            if existing is None or (
                "/resource/celex/" in candidate["alias"]
                and "/resource/celex/" not in existing.get("alias", "")
            ):
                by_item[item_uri] = json.dumps(candidate, sort_keys=True)

    selected: dict[str, list[dict[str, str]]] = {}
    for base_celex, manifestations in records.items():
        choices: list[tuple[int, str, list[dict[str, str]]]] = []
        for manifestation, raw_items in manifestations.items():
            manifestation_items = [json.loads(value) for value in raw_items.values()]
            formats = {str(value.get("format") or "").lower() for value in manifestation_items}
            if len(formats) != 1:
                continue
            format_name = next(iter(formats))
            priority = DIRECT_FORMAT_PRIORITY.get(format_name)
            if priority is not None:
                choices.append((priority, manifestation, manifestation_items))
        if choices:
            choices.sort(key=lambda value: (value[0], value[1]))
            selected[base_celex] = sorted(
                choices[0][2], key=lambda value: (value.get("item_id", ""), value["item"])
            )
    return selected


def safe_zip_members(archive: zipfile.ZipFile, config: DownloadConfig) -> list[str]:
    infos = archive.infolist()
    if not infos:
        raise ValidationError("ZIP is empty")
    if len(infos) > config.max_zip_members:
        raise ValidationError("ZIP has too many members: {}".format(len(infos)))
    total = 0
    names: list[str] = []
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
            raise ValidationError("unsafe ZIP member: {!r}".format(info.filename))
        total += info.file_size
        if total > config.max_uncompressed_bytes:
            raise ValidationError("ZIP exceeds uncompressed size limit")
        names.append(info.filename)
    corrupt = archive.testzip()
    if corrupt:
        raise ValidationError("ZIP member failed CRC validation: {}".format(corrupt))
    return names


def extract_formex_metadata(
    archive: zipfile.ZipFile,
    members: Sequence[str],
    item: InventoryItem,
) -> dict[str, Any]:
    xml_members = [name for name in members if name.lower().endswith(".xml")]
    wrapper_members = [name for name in xml_members if name.lower().endswith(".doc.xml")]
    content_members = [name for name in xml_members if name not in wrapper_members]
    if not wrapper_members or not content_members:
        raise ValidationError(
            "Formex ZIP lacks wrapper or content XML: wrappers={} content={}".format(
                len(wrapper_members), len(content_members)
            )
        )

    wrapper_root = ET.fromstring(archive.read(wrapper_members[0]))
    title = ""
    legal_value = ""
    source_celex = ""
    for element in wrapper_root.iter():
        name = local_name(element.tag)
        if name == "LEGAL.VALUE" and not legal_value:
            legal_value = clean_text(element.itertext())
        elif name == "NO.CELEX" and not source_celex:
            source_celex = clean_text(element.itertext())
        elif name == "PAPER" and not title:
            title_element = next(
                (child for child in element.iter() if local_name(child.tag) == "TITLE"),
                None,
            )
            if title_element is not None:
                title = clean_text(title_element.itertext())

    expected_reference = item.consolidated_celex[1:].split("-", 1)[0]
    expected_date = item.consolidation_date.replace("-", "")
    info_consleg: dict[str, str] = {}
    content_root = ""
    with archive.open(content_members[0]) as stream:
        for _, element in ET.iterparse(stream, events=("start",)):
            name = local_name(element.tag)
            if not content_root:
                content_root = name
            if name == "INFO.CONSLEG":
                info_consleg = dict(element.attrib)
                break
    if content_root != "CONS.ACT":
        raise ValidationError("unexpected consolidated XML root: {}".format(content_root))
    actual_reference = info_consleg.get("CONSLEG.REF")
    normalized_expected_reference = re.sub(r"\([^)]*\)$", "", expected_reference)
    reference_matches = actual_reference in {
        expected_reference,
        normalized_expected_reference,
    }
    actual_consolidation_date = info_consleg.get("CONSLEG.DATE") or info_consleg.get(
        "START.DATE"
    )
    return {
        "title": title or None,
        "legal_value": legal_value or None,
        "source_celex": source_celex or None,
        "content_root": content_root,
        "info_consleg": info_consleg,
        "info_consleg_reference_matches_celex": reference_matches,
        "info_consleg_date": actual_consolidation_date,
        "info_consleg_date_matches_celex": actual_consolidation_date == expected_date,
        "zip_member_count": len(members),
        "xml_member_count": len(xml_members),
        "wrapper_member": wrapper_members[0],
        "content_member": content_members[0],
    }


def validate_formex_zip(
    path: Path,
    item: InventoryItem,
    config: DownloadConfig,
) -> dict[str, Any]:
    if not zipfile.is_zipfile(path):
        raise ValidationError("response is not a ZIP file")
    with zipfile.ZipFile(path) as archive:
        members = safe_zip_members(archive, config)
        return extract_formex_metadata(archive, members, item)


def validate_xhtml(path: Path, item: InventoryItem) -> dict[str, Any]:
    with path.open("rb") as stream:
        prefix = stream.read(min(path.stat().st_size, 1024 * 1024))
    text = prefix.decode("utf-8", errors="replace")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    title = clean_text([re.sub(r"<[^>]+>", " ", title_match.group(1))]) if title_match else ""
    if "Konsolidiert" not in title and item.consolidated_celex not in text:
        raise ValidationError("XHTML does not identify a consolidated text")
    return {"title": title or None, "content_root": "html"}


def validate_pdf(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        prefix = stream.read(8)
    if not prefix.startswith(b"%PDF-"):
        raise ValidationError("response is not a PDF file")
    if path.stat().st_size < 512:
        raise ValidationError("PDF response is implausibly small")
    return {"title": None, "content_root": "pdf"}


def stream_download(
    item: InventoryItem,
    *,
    accept: str,
    destination: Path,
    config: DownloadConfig,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    last_error: Optional[BaseException] = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = config.output_dir / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, config.retries + 2):
        temporary = staging_dir / "{}.{}.part".format(
            item.consolidated_celex, hashlib.sha1(accept.encode("utf-8")).hexdigest()[:8]
        )
        try:
            request = Request(
                item.download_url,
                headers={
                    "Accept": accept,
                    "Accept-Language": LANGUAGE,
                    "User-Agent": USER_AGENT,
                },
            )
            started = time.monotonic()
            with urlopen(request, timeout=config.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > config.max_download_bytes:
                    raise ValidationError(
                        "response exceeds download size limit: {}".format(content_length)
                    )
                digest = hashlib.sha256()
                size = 0
                with temporary.open("wb") as stream:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > config.max_download_bytes:
                            raise ValidationError("response exceeds download size limit")
                        digest.update(block)
                        stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                result = {
                    "final_url": response.geturl(),
                    "content_type": response.headers.get("Content-Type"),
                    "size_bytes": size,
                    "sha256": digest.hexdigest(),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            if accept == FORMEX_ACCEPT:
                result.update(validate_formex_zip(temporary, item, config))
                result["manifestation_format"] = "fmx4_zip"
            elif accept == PDF_ACCEPT:
                result.update(validate_pdf(temporary))
                result["manifestation_format"] = "pdf"
            else:
                result.update(validate_xhtml(temporary, item))
                result["manifestation_format"] = (
                    "xhtml" if accept == XHTML_ACCEPT else "html"
                )
            os.replace(temporary, destination)
            attempts.append(
                {"attempt": attempt, "accept": accept, "outcome": "downloaded"}
            )
            return result, attempts
        except HTTPError as exc:
            last_error = exc
            attempts.append(
                {
                    "attempt": attempt,
                    "accept": accept,
                    "outcome": "http_error",
                    "http_status": exc.code,
                }
            )
            if exc.code not in RETRYABLE_STATUSES or attempt > config.retries:
                break
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            last_error = exc
            attempts.append(
                {
                    "attempt": attempt,
                    "accept": accept,
                    "outcome": "network_error",
                    "error": "{}: {}".format(type(exc).__name__, exc),
                }
            )
            if attempt > config.retries:
                break
        except (OSError, zipfile.BadZipFile, ET.ParseError, ValidationError) as exc:
            last_error = exc
            attempts.append(
                {
                    "attempt": attempt,
                    "accept": accept,
                    "outcome": "validation_error",
                    "error": "{}: {}".format(type(exc).__name__, exc),
                }
            )
            break
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        time.sleep(config.retry_backoff_seconds * (2 ** (attempt - 1)))
    raise CorpusError("{}".format(last_error or "download failed"))


def stream_raw_url(
    url: str,
    destination: Path,
    config: DownloadConfig,
) -> dict[str, Any]:
    last_error: Optional[BaseException] = None
    for attempt in range(1, config.retries + 2):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            started = time.monotonic()
            with urlopen(request, timeout=config.timeout_seconds) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > config.max_download_bytes:
                    raise ValidationError(
                        "direct item exceeds download size limit: {}".format(content_length)
                    )
                digest = hashlib.sha256()
                size = 0
                with destination.open("wb") as stream:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > config.max_download_bytes:
                            raise ValidationError("direct item exceeds download size limit")
                        digest.update(block)
                        stream.write(block)
                    stream.flush()
                    os.fsync(stream.fileno())
                return {
                    "item_url": url,
                    "final_url": response.geturl(),
                    "content_type": response.headers.get("Content-Type"),
                    "size_bytes": size,
                    "sha256": digest.hexdigest(),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
        except HTTPError as exc:
            last_error = exc
            if exc.code not in RETRYABLE_STATUSES or attempt > config.retries:
                break
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
            last_error = exc
            if attempt > config.retries:
                break
        except (OSError, ValidationError) as exc:
            last_error = exc
            break
        time.sleep(config.retry_backoff_seconds * (2 ** (attempt - 1)))
    raise CorpusError("direct item download failed for {}: {}".format(url, last_error))


def direct_item_filename(record: Mapping[str, str], index: int) -> str:
    alias = unquote(str(record.get("alias") or ""))
    format_name = str(record.get("format") or "").lower()
    marker = ".DEU.{}.".format(format_name)
    if marker in alias:
        candidate = alias.split(marker, 1)[1]
        if candidate and "/" not in candidate and "\\" not in candidate:
            return candidate
    item_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(record.get("item_id") or ""))
    extension = ".xml" if format_name == "fmx4" else ".bin"
    return (item_id or "DOC_{}".format(index)) + extension


def repair_direct_item(
    item: InventoryItem,
    records: Sequence[Mapping[str, str]],
    config: DownloadConfig,
) -> dict[str, Any]:
    base = {
        **asdict(item),
        "download_url": item.download_url,
        "language": LANGUAGE,
        "retrieved_at": utc_now(),
        "retrieval_method": "direct_cellar_items",
    }
    if not records:
        return {
            **base,
            "status": "unavailable_deu",
            "local_path": None,
            "availability_reason": "no supported German Cellar manifestation items found",
        }
    format_name = str(records[0].get("format") or "").lower()
    manifestation = records[0].get("manifestation")
    staging_root = config.output_dir / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix="direct-", dir=str(staging_root)))
    direct_results: list[dict[str, Any]] = []
    try:
        if format_name == "fmx4":
            relative = Path("packages") / item.descriptor / (
                item.consolidated_celex + ".fmx4.zip"
            )
            destination = config.output_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            member_paths: list[tuple[Path, str]] = []
            used_names: set[str] = set()
            for index, record in enumerate(records, start=1):
                name = direct_item_filename(record, index)
                if name in used_names:
                    stem, suffix = os.path.splitext(name)
                    name = "{}_{}{}".format(stem, index, suffix)
                used_names.add(name)
                local = temporary_dir / "item_{:04d}".format(index)
                result = stream_raw_url(record["item"], local, config)
                result["archive_name"] = name
                direct_results.append(result)
                member_paths.append((local, name))
            if len(member_paths) == 1 and zipfile.is_zipfile(member_paths[0][0]):
                temporary_zip = member_paths[0][0]
            else:
                temporary_zip = temporary_dir / "manifestation.zip"
                with zipfile.ZipFile(
                    temporary_zip,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    for local, name in member_paths:
                        archive.write(local, arcname=name)
            metadata = validate_formex_zip(temporary_zip, item, config)
            size = temporary_zip.stat().st_size
            digest = file_sha256(temporary_zip)
            os.replace(temporary_zip, destination)
            return {
                **base,
                **metadata,
                "status": "downloaded",
                "manifestation_format": "fmx4_zip",
                "local_path": relative.as_posix(),
                "size_bytes": size,
                "sha256": digest,
                "direct_manifestation_uri": manifestation,
                "direct_items": direct_results,
            }

        extension = {
            "xhtml": ".xhtml",
            "html": ".html",
            "pdfa1a": ".pdf",
            "pdfa1b": ".pdf",
            "pdf": ".pdf",
        }.get(format_name)
        if extension is None:
            raise ValidationError("unsupported direct manifestation format: {}".format(format_name))
        relative = Path("packages") / item.descriptor / (item.consolidated_celex + extension)
        destination = config.output_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = temporary_dir / ("manifestation" + extension)
        result = stream_raw_url(records[0]["item"], temporary, config)
        if extension == ".pdf":
            metadata = validate_pdf(temporary)
            manifestation_format = "pdf"
        else:
            metadata = validate_xhtml(temporary, item)
            manifestation_format = format_name
        os.replace(temporary, destination)
        return {
            **base,
            **metadata,
            "status": "downloaded",
            "manifestation_format": manifestation_format,
            "local_path": relative.as_posix(),
            "size_bytes": result["size_bytes"],
            "sha256": result["sha256"],
            "final_url": result["final_url"],
            "content_type": result["content_type"],
            "direct_manifestation_uri": manifestation,
            "direct_items": [result],
        }
    except Exception as exc:
        return {
            **base,
            "status": "failed",
            "local_path": None,
            "error": "{}: {}".format(type(exc).__name__, exc),
            "direct_manifestation_uri": manifestation,
            "direct_items": direct_results,
        }
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def existing_download_record(
    item: InventoryItem,
    previous: Mapping[str, Any],
    config: DownloadConfig,
) -> Optional[dict[str, Any]]:
    relative = previous.get("local_path")
    if previous.get("status") != "downloaded" or not isinstance(relative, str):
        return None
    path = config.output_dir / relative
    if not path.is_file():
        return None
    expected_size = previous.get("size_bytes")
    if isinstance(expected_size, int) and path.stat().st_size != expected_size:
        return None
    expected_sha256 = previous.get("sha256")
    if isinstance(expected_sha256, str) and file_sha256(path) != expected_sha256:
        return None
    return dict(previous)


def download_item(item: InventoryItem, config: DownloadConfig) -> dict[str, Any]:
    base = {
        **asdict(item),
        "download_url": item.download_url,
        "language": LANGUAGE,
        "retrieved_at": utc_now(),
    }
    formex_relative = Path("packages") / item.descriptor / (
        item.consolidated_celex + ".fmx4.zip"
    )
    xhtml_relative = Path("packages") / item.descriptor / (
        item.consolidated_celex + ".xhtml"
    )
    html_relative = Path("packages") / item.descriptor / (
        item.consolidated_celex + ".html"
    )
    pdf_relative = Path("packages") / item.descriptor / (
        item.consolidated_celex + ".pdf"
    )
    all_attempts: list[dict[str, Any]] = []
    try:
        result, attempts = stream_download(
            item,
            accept=FORMEX_ACCEPT,
            destination=config.output_dir / formex_relative,
            config=config,
        )
        all_attempts.extend(attempts)
        return {
            **base,
            **result,
            "status": "downloaded",
            "local_path": formex_relative.as_posix(),
            "attempts": all_attempts,
        }
    except CorpusError as formex_error:
        all_attempts.append(
            {"accept": FORMEX_ACCEPT, "outcome": "failed", "error": str(formex_error)}
        )
    try:
        result, attempts = stream_download(
            item,
            accept=XHTML_ACCEPT,
            destination=config.output_dir / xhtml_relative,
            config=config,
        )
        all_attempts.extend(attempts)
        return {
            **base,
            **result,
            "status": "downloaded",
            "local_path": xhtml_relative.as_posix(),
            "attempts": all_attempts,
        }
    except CorpusError as xhtml_error:
        all_attempts.append(
            {"accept": XHTML_ACCEPT, "outcome": "failed", "error": str(xhtml_error)}
        )
    try:
        result, attempts = stream_download(
            item,
            accept=HTML_ACCEPT,
            destination=config.output_dir / html_relative,
            config=config,
        )
        all_attempts.extend(attempts)
        return {
            **base,
            **result,
            "status": "downloaded",
            "local_path": html_relative.as_posix(),
            "attempts": all_attempts,
        }
    except CorpusError as html_error:
        all_attempts.append(
            {"accept": HTML_ACCEPT, "outcome": "failed", "error": str(html_error)}
        )
    try:
        result, attempts = stream_download(
            item,
            accept=PDF_ACCEPT,
            destination=config.output_dir / pdf_relative,
            config=config,
        )
        all_attempts.extend(attempts)
        return {
            **base,
            **result,
            "status": "downloaded",
            "local_path": pdf_relative.as_posix(),
            "attempts": all_attempts,
        }
    except CorpusError as pdf_error:
        all_attempts.append(
            {"accept": PDF_ACCEPT, "outcome": "failed", "error": str(pdf_error)}
        )
        return {
            **base,
            "status": "failed",
            "local_path": None,
            "attempts": all_attempts,
            "error": str(pdf_error),
        }


def load_previous_register(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != REGISTER_SCHEMA_VERSION:
        raise CorpusError("unsupported register schema in {}".format(path))
    return {
        row["base_celex"]: row
        for row in data.get("documents", [])
        if isinstance(row, dict) and row.get("base_celex")
    }


def register_payload(
    *,
    snapshot_date: str,
    descriptor_summary: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    started_at: str,
    complete: bool,
) -> dict[str, Any]:
    status_counts = Counter(str(row.get("status") or "pending") for row in documents)
    format_counts = Counter(
        str(row.get("manifestation_format"))
        for row in documents
        if row.get("manifestation_format")
    )
    descriptor_counts: dict[str, Counter[str]] = {}
    downloaded_rows = [row for row in documents if row.get("status") == "downloaded"]
    unique_download_sizes: dict[str, int] = {}
    for row in downloaded_rows:
        relative = row.get("local_path")
        if isinstance(relative, str):
            unique_download_sizes[relative] = int(row.get("size_bytes") or 0)
    for row in documents:
        descriptor = str(row.get("descriptor") or "unknown")
        descriptor_counts.setdefault(descriptor, Counter())[str(row.get("status") or "pending")] += 1
    return {
        "schema_version": REGISTER_SCHEMA_VERSION,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION},
        "source": {
            "system": "Cellar",
            "sparql_endpoint": SPARQL_ENDPOINT,
            "publication_url_template": PUBLICATION_URL,
            "language": LANGUAGE,
            "preferred_format": FORMEX_ACCEPT,
            "fallback_formats": [XHTML_ACCEPT, HTML_ACCEPT, PDF_ACCEPT],
        },
        "snapshot_date": snapshot_date,
        "started_at": started_at,
        "updated_at": utc_now(),
        "complete": complete,
        "descriptor_inventory": list(descriptor_summary),
        "summary": {
            "documents": len(documents),
            "status_counts": dict(sorted(status_counts.items())),
            "format_counts": dict(sorted(format_counts.items())),
            "downloaded_bytes": sum(unique_download_sizes.values()),
            "logical_downloaded_bytes": sum(
                int(row.get("size_bytes") or 0) for row in downloaded_rows
            ),
            "unique_downloaded_files": len(unique_download_sizes),
            "by_descriptor": {
                key: dict(sorted(value.items()))
                for key, value in sorted(descriptor_counts.items())
            },
        },
        "documents": list(documents),
    }


def write_register_files(output_dir: Path, payload: Mapping[str, Any]) -> None:
    atomic_write_text(
        output_dir / "register.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    jsonl = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in payload.get("documents", [])
    )
    atomic_write_text(output_dir / "register.jsonl", jsonl)

    summary = payload["summary"]
    lines = [
        "# Cellar consolidated German corpus",
        "",
        "- Snapshot date: `{}`".format(payload["snapshot_date"]),
        "- Complete: `{}`".format(str(payload["complete"]).lower()),
        "- Documents: `{}`".format(summary["documents"]),
        "- Unique downloaded files: `{}`".format(summary["unique_downloaded_files"]),
        "- Downloaded bytes: `{}`".format(summary["downloaded_bytes"]),
        "",
        "## Status",
        "",
    ]
    lines.extend(
        "- {}: `{}`".format(key, value)
        for key, value in summary["status_counts"].items()
    )
    lines.extend(["", "## By descriptor", "", "| Descriptor | Status counts |", "|---|---:|"])
    lines.extend(
        "| {} | `{}` |".format(key, json.dumps(value, sort_keys=True))
        for key, value in summary["by_descriptor"].items()
    )
    atomic_write_text(output_dir / "SUMMARY.md", "\n".join(lines) + "\n")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--snapshot-date", default=dt.date.today().isoformat())
    parser.add_argument(
        "--descriptors",
        nargs="+",
        help="CELEX descriptors to include; default discovers all consolidated sector-3 types",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--direct-items-only",
        action="store_true",
        help="skip normal content negotiation and repair failed register rows via WEMI item URLs",
    )
    parser.add_argument(
        "--skip-direct-item-repair",
        action="store_true",
        help="do not run the direct Cellar item fallback after normal downloads",
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=0.75)
    parser.add_argument("--max-download-mib", type=int, default=250)
    parser.add_argument("--max-uncompressed-mib", type=int, default=1000)
    parser.add_argument("--max-zip-members", type=int, default=20000)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        dt.date.fromisoformat(args.snapshot_date)
    except ValueError as exc:
        raise SystemExit("invalid --snapshot-date: {}".format(args.snapshot_date)) from exc
    if args.workers < 1:
        raise SystemExit("--workers must be positive")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")

    output_dir = args.out_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    print("discovering Cellar inventory for snapshot {}".format(args.snapshot_date), flush=True)
    inventory, descriptor_summary = discover_inventory(
        descriptors=[value.upper() for value in args.descriptors] if args.descriptors else None,
        snapshot_date=args.snapshot_date,
        timeout_seconds=args.timeout_seconds,
        retries=args.retries,
        backoff_seconds=args.retry_backoff_seconds,
    )
    if args.limit:
        inventory = inventory[: args.limit]
    print(
        "inventory={} descriptors={}".format(
            len(inventory), ",".join(sorted({item.descriptor for item in inventory}))
        ),
        flush=True,
    )

    previous = (
        load_previous_register(output_dir / "register.json")
        if args.resume or args.direct_items_only
        else {}
    )
    if args.direct_items_only and not previous:
        raise SystemExit("--direct-items-only requires an existing register.json")
    config = DownloadConfig(
        output_dir=output_dir,
        timeout_seconds=args.timeout_seconds,
        retries=args.retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
        max_download_bytes=args.max_download_mib * 1024 * 1024,
        max_uncompressed_bytes=args.max_uncompressed_mib * 1024 * 1024,
        max_zip_members=args.max_zip_members,
    )

    rows_by_base: dict[str, dict[str, Any]] = {}
    pending: list[InventoryItem] = []
    for item in inventory:
        if args.direct_items_only and item.base_celex in previous:
            rows_by_base[item.base_celex] = dict(previous[item.base_celex])
            continue
        existing = existing_download_record(item, previous.get(item.base_celex, {}), config)
        if existing is not None:
            rows_by_base[item.base_celex] = existing
        else:
            row = {
                **asdict(item),
                "download_url": item.download_url,
                "language": LANGUAGE,
                "status": "pending",
                "local_path": None,
            }
            rows_by_base[item.base_celex] = row
            pending.append(item)

    ordered_rows = lambda: [rows_by_base[item.base_celex] for item in inventory]
    payload = register_payload(
        snapshot_date=args.snapshot_date,
        descriptor_summary=descriptor_summary,
        documents=ordered_rows(),
        started_at=started_at,
        complete=args.inventory_only,
    )
    write_register_files(output_dir, payload)
    if args.inventory_only:
        print("inventory written to {}".format(output_dir / "register.json"))
        return 0

    log_path = output_dir / "download_results.jsonl"
    if not args.direct_items_only:
        print(
            "downloading pending={} reused={}".format(
                len(pending), len(inventory) - len(pending)
            ),
            flush=True,
        )
        completed = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(download_item, item, config): item for item in pending}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    row = future.result()
                except Exception as exc:  # retain a register row for every item
                    row = {
                        **asdict(item),
                        "download_url": item.download_url,
                        "language": LANGUAGE,
                        "status": "failed",
                        "local_path": None,
                        "retrieved_at": utc_now(),
                        "error": "{}: {}".format(type(exc).__name__, exc),
                    }
                rows_by_base[item.base_celex] = row
                append_jsonl(log_path, row)
                completed += 1
                if completed % CHECKPOINT_EVERY == 0 or completed == len(pending):
                    payload = register_payload(
                        snapshot_date=args.snapshot_date,
                        descriptor_summary=descriptor_summary,
                        documents=ordered_rows(),
                        started_at=started_at,
                        complete=False,
                    )
                    write_register_files(output_dir, payload)
                    counts = payload["summary"]["status_counts"]
                    print(
                        "done {}/{} downloaded={} failed={} bytes={}".format(
                            completed,
                            len(pending),
                            counts.get("downloaded", 0),
                            counts.get("failed", 0),
                            payload["summary"]["downloaded_bytes"],
                        ),
                        flush=True,
                    )

    if not args.skip_direct_item_repair:
        failed_items = [
            item
            for item in inventory
            if rows_by_base[item.base_celex].get("status") == "failed"
        ]
        if failed_items:
            print(
                "discovering direct German manifestation items for failed={}".format(
                    len(failed_items)
                ),
                flush=True,
            )
            direct_records = discover_direct_manifests(
                failed_items,
                timeout_seconds=args.timeout_seconds,
                retries=args.retries,
                backoff_seconds=args.retry_backoff_seconds,
            )
            repaired = 0
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(
                        repair_direct_item,
                        item,
                        direct_records.get(item.base_celex, []),
                        config,
                    ): item
                    for item in failed_items
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        row = future.result()
                    except Exception as exc:
                        row = {
                            **asdict(item),
                            "download_url": item.download_url,
                            "language": LANGUAGE,
                            "status": "failed",
                            "local_path": None,
                            "retrieved_at": utc_now(),
                            "retrieval_method": "direct_cellar_items",
                            "error": "{}: {}".format(type(exc).__name__, exc),
                        }
                    rows_by_base[item.base_celex] = row
                    append_jsonl(log_path, row)
                    repaired += 1
                    if repaired % CHECKPOINT_EVERY == 0 or repaired == len(failed_items):
                        payload = register_payload(
                            snapshot_date=args.snapshot_date,
                            descriptor_summary=descriptor_summary,
                            documents=ordered_rows(),
                            started_at=started_at,
                            complete=False,
                        )
                        write_register_files(output_dir, payload)
                        counts = payload["summary"]["status_counts"]
                        print(
                            "direct {}/{} downloaded={} failed={} bytes={}".format(
                                repaired,
                                len(failed_items),
                                counts.get("downloaded", 0),
                                counts.get("failed", 0),
                                payload["summary"]["downloaded_bytes"],
                            ),
                            flush=True,
                        )

    final_payload = register_payload(
        snapshot_date=args.snapshot_date,
        descriptor_summary=descriptor_summary,
        documents=ordered_rows(),
        started_at=started_at,
        complete=True,
    )
    write_register_files(output_dir, final_payload)
    failed = final_payload["summary"]["status_counts"].get("failed", 0)
    print("register={}".format(output_dir / "register.json"))
    print("summary={}".format(output_dir / "SUMMARY.md"))
    print("failed={}".format(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
