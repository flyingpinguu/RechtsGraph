#!/usr/bin/env python3
"""Download and validate the Gesetze-im-Internet XML corpus.

The authoritative ``gii-toc.xml`` catalog is preferred.  When the existing PDF
manifest is available, entries are reconciled by their GII law directory so its
category, ordinal, abbreviation, and detail URL remain attached to each XML
record.  Catalog-only entries are deliberately retained, and PDF-only entries
fall back to deriving or discovering an ``xml.zip`` link from their detail page.

Downloads and extraction happen below ``OUT_DIR/.staging``.  A build becomes
visible only after its ZIP and every XML member have been validated and the
whole staging directory has been atomically renamed into a content-addressed
destination.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import time
import unicodedata
import uuid
import zipfile
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree as ET
from xml.parsers import expat


TOOL_NAME = "download_gii_xml"
TOOL_VERSION = "1.0.0"
MANIFEST_SCHEMA_VERSION = "1.0"
MANIFEST_CHECKPOINT_EVERY = 100
DEFAULT_TOC_URL = "https://www.gesetze-im-internet.de/gii-toc.xml"
USER_AGENT = "gii-xml-corpus-downloader/{} (+validated research corpus)".format(TOOL_VERSION)
GII_HOSTS = {"gesetze-im-internet.de", "www.gesetze-im-internet.de"}
GII_ALLOWED_ORIGINS = frozenset({"https://www.gesetze-im-internet.de"})
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
REPLACED_BUILD_RE = re.compile(
    r"^\.(?P<target>[0-9a-f]{64})-replaced-(?P<nonce>[0-9a-f]{32})$"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_PDF_MANIFEST = REPO_ROOT / "gesetze_im_internet_pdfs" / "manifest.json"
DEFAULT_OUT_DIR = REPO_ROOT / "gesetze_im_internet_xml"


class CorpusError(RuntimeError):
    """Base class for expected source, download, or validation failures."""


class SourceError(CorpusError):
    """Raised when an input catalog or manifest is malformed."""


@dataclass(frozen=True)
class DownloadAttempt:
    """Structured outcome for one HTTP attempt within an item download."""

    phase: str
    url: str
    attempt: int
    outcome: str
    http_status: Optional[int] = None
    error_type: Optional[str] = None
    reason: Optional[str] = None
    failure_kind: Optional[str] = None

    def manifest_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "url": self.url,
            "attempt": self.attempt,
            "outcome": self.outcome,
            "http_status": self.http_status,
            "error_type": self.error_type,
            "reason": self.reason,
            "failure_kind": self.failure_kind,
        }


class DownloadError(CorpusError):
    """Raised with structured metadata when a remote object cannot be downloaded."""

    def __init__(
        self,
        message: str,
        *,
        url: Optional[str] = None,
        http_status: Optional[int] = None,
        http_attempts: Optional[int] = None,
        attempts: Sequence[DownloadAttempt] = (),
    ) -> None:
        super().__init__(message)
        self.url = url
        self.http_status = http_status
        self.http_attempts = http_attempts
        self.attempts = tuple(attempts)

    def terminal_attempt(self, phase: str) -> Optional[DownloadAttempt]:
        return next(
            (
                attempt
                for attempt in reversed(self.attempts)
                if attempt.phase == phase
            ),
            None,
        )


class ValidationError(CorpusError):
    """Raised when a ZIP or XML payload is unsafe or invalid."""


@dataclass(frozen=True)
class TocEntry:
    index: int
    title: str
    xml_url: str


@dataclass(frozen=True)
class CorpusItem:
    item_id: str
    law_key: str
    title: str
    xml_url: Optional[str]
    detail_url: Optional[str]
    reconciliation_status: str
    toc_index: Optional[int] = None
    toc_title: Optional[str] = None
    pdf_manifest_ordinal: Optional[int] = None
    pdf_title: Optional[str] = None
    category: Optional[str] = None
    category_index: Optional[int] = None
    pdf_manifest_matches: Tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class HTTPConfig:
    timeout_seconds: float = 45.0
    retries: int = 3
    retry_backoff_seconds: float = 0.5
    max_download_bytes: int = 100 * 1024 * 1024


@dataclass(frozen=True)
class RunConfig:
    output_dir: Path
    toc_source: Optional[str]
    pdf_manifest: Optional[Path]
    workers: int = 4
    limit: Optional[int] = None
    resume: bool = False
    force: bool = False
    timeout_seconds: float = 45.0
    retries: int = 3
    retry_backoff_seconds: float = 0.5
    max_download_bytes: int = 100 * 1024 * 1024
    max_uncompressed_bytes: int = 500 * 1024 * 1024
    max_zip_members: int = 10000


class LinkParser(HTMLParser):
    """Collect anchors without requiring a third-party HTML parser."""

    def __init__(self) -> None:
        super().__init__()
        self.links: List[Tuple[str, str, Dict[str, str]]] = []
        self._href: Optional[str] = None
        self._attrs: Dict[str, str] = {}
        self._text: List[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        if tag.lower() != "a":
            return
        attr_map = {key.lower(): value or "" for key, value in attrs}
        self._href = attr_map.get("href")
        self._attrs = attr_map
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        text = " ".join("".join(self._text).split())
        self.links.append((self._href, text, dict(self._attrs)))
        self._href = None
        self._attrs = {}
        self._text = []


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def slugify(value: str) -> str:
    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
        "ß": "ss",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return value or "document"


def safe_url(url: str) -> str:
    """Quote non-ASCII URL components while retaining existing percent escapes."""

    parts = urlsplit(url)
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            quote(unquote(parts.path), safe="/%:@"),
            quote(unquote(parts.query), safe="=&?/:@%"),
            "",
        )
    )


def normalize_source_url(url: str, base_url: Optional[str] = None) -> str:
    """Resolve and validate an allowlisted Gesetze-im-Internet HTTP(S) URL."""

    value = (url or "").strip()
    if base_url:
        value = urljoin(base_url, value)
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SourceError("URL is empty or contains control characters: {!r}".format(url))
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise SourceError("malformed URL {!r}: {}".format(url, exc)) from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise SourceError("expected an absolute HTTP(S) URL, got {!r}".format(url))
    if parts.username is not None or parts.password is not None:
        raise SourceError("URL credentials are not allowed: {!r}".format(url))
    hostname = (parts.hostname or "").lower()
    scheme = parts.scheme.lower()
    if hostname in GII_HOSTS and scheme == "http":
        scheme = "https"
    if hostname in GII_HOSTS:
        hostname = "www.gesetze-im-internet.de"
    host_for_netloc = "[{}]".format(hostname) if ":" in hostname else hostname
    netloc = host_for_netloc
    if port is not None:
        netloc += ":{}".format(port)
    origin = "{}://{}".format(scheme, netloc)
    if origin not in GII_ALLOWED_ORIGINS:
        raise SourceError(
            "URL origin is not allowlisted for Gesetze-im-Internet: {!r}".format(
                origin
            )
        )
    path = re.sub(r"/+", "/", parts.path or "/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def same_origin(left: str, right: str) -> bool:
    left_parts = urlsplit(left)
    right_parts = urlsplit(right)

    def origin(parts: Any) -> Tuple[str, str, int]:
        scheme = parts.scheme.lower()
        default_port = 443 if scheme == "https" else 80
        return scheme, (parts.hostname or "").lower(), parts.port or default_port

    return origin(left_parts) == origin(right_parts)


def law_key_from_url(url: str) -> str:
    """Return a reconciliation key for a detail page or XML ZIP URL."""

    normalized = normalize_source_url(url)
    parts = urlsplit(normalized)
    path = unquote(parts.path)
    lowered = path.lower().rstrip("/")
    if lowered.endswith("/index.html"):
        path = path[: -len("/index.html")]
    elif PurePosixPath(lowered).suffix == ".zip":
        path = str(PurePosixPath(path).parent)
    path = "/" + path.strip("/") + "/"
    return "{}://{}{}".format(parts.scheme.lower(), parts.netloc.lower(), path)


def derive_xml_url(detail_url: str) -> str:
    return normalize_source_url(urljoin(normalize_source_url(detail_url), "xml.zip"))


def derive_detail_url(xml_url: str) -> str:
    return normalize_source_url(urljoin(normalize_source_url(xml_url), "index.html"))


class AllowlistedRedirectHandler(HTTPRedirectHandler):
    """Reject a redirect target before urllib can issue its next request."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> Optional[Request]:
        try:
            validated_url = normalize_source_url(
                new_url,
                base_url=request.full_url,
            )
        except SourceError as exc:
            raise SourceError(
                "refusing redirect from {}: {}".format(request.full_url, exc)
            ) from exc
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            safe_url(validated_url),
        )


class HTTPClient:
    """Small retrying urllib client used by catalog and corpus downloads."""

    def __init__(self, config: HTTPConfig) -> None:
        self.config = config
        self._opener = build_opener(AllowlistedRedirectHandler())

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self.config.retry_backoff_seconds * (2 ** max(0, attempt - 1))
        if delay > 0:
            time.sleep(delay)

    def fetch_bytes(self, url: str) -> Tuple[bytes, str, int]:
        requested_url = normalize_source_url(url)
        last_error: Optional[BaseException] = None
        failed_attempts: List[DownloadAttempt] = []
        total_attempts = self.config.retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                request = Request(
                    safe_url(requested_url),
                    headers={"User-Agent": USER_AGENT},
                )
                with self._opener.open(
                    request,
                    timeout=self.config.timeout_seconds,
                ) as response:
                    final_url = normalize_source_url(response.geturl())
                    content_length = response.headers.get("Content-Length")
                    expected_length = int(content_length) if content_length else None
                    if expected_length and expected_length > self.config.max_download_bytes:
                        raise DownloadError(
                            "response exceeds {} bytes".format(self.config.max_download_bytes)
                        )
                    chunks: List[bytes] = []
                    size = 0
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > self.config.max_download_bytes:
                            raise DownloadError(
                                "response exceeds {} bytes".format(
                                    self.config.max_download_bytes
                                )
                            )
                        chunks.append(block)
                    if expected_length is not None and size != expected_length:
                        raise http.client.IncompleteRead(
                            b"",
                            max(0, expected_length - size),
                        )
                    return b"".join(chunks), final_url, attempt
            except HTTPError as exc:
                last_error = exc
                failed_attempts.append(
                    DownloadAttempt(
                        phase="http_get",
                        url=requested_url,
                        attempt=attempt,
                        outcome="error",
                        http_status=exc.code,
                        error_type=type(exc).__name__,
                        reason=str(exc),
                        failure_kind="http",
                    )
                )
                if exc.code not in RETRYABLE_HTTP_STATUSES or attempt >= total_attempts:
                    break
            except (
                URLError,
                TimeoutError,
                socket.timeout,
                ConnectionError,
                http.client.HTTPException,
                ssl.SSLError,
            ) as exc:
                last_error = exc
                failed_attempts.append(
                    DownloadAttempt(
                        phase="http_get",
                        url=requested_url,
                        attempt=attempt,
                        outcome="error",
                        error_type=type(exc).__name__,
                        reason=str(exc),
                        failure_kind="network",
                    )
                )
                if attempt >= total_attempts:
                    break
            self._sleep_before_retry(attempt)
        raise DownloadError(
            "GET {} failed after {} attempt(s): {}".format(url, attempt, last_error),
            url=requested_url,
            http_status=(
                last_error.code if isinstance(last_error, HTTPError) else None
            ),
            http_attempts=attempt,
            attempts=failed_attempts,
        )

    def download(self, url: str, destination: Path) -> Dict[str, Any]:
        requested_url = normalize_source_url(url)
        last_error: Optional[BaseException] = None
        failed_attempts: List[DownloadAttempt] = []
        total_attempts = self.config.retries + 1
        for attempt in range(1, total_attempts + 1):
            destination.unlink(missing_ok=True)
            try:
                request = Request(
                    safe_url(requested_url),
                    headers={"User-Agent": USER_AGENT},
                )
                with self._opener.open(
                    request,
                    timeout=self.config.timeout_seconds,
                ) as response:
                    final_url = normalize_source_url(response.geturl())
                    content_length = response.headers.get("Content-Length")
                    expected_length = int(content_length) if content_length else None
                    if expected_length and expected_length > self.config.max_download_bytes:
                        raise DownloadError(
                            "response exceeds {} bytes".format(self.config.max_download_bytes)
                        )
                    digest = hashlib.sha256()
                    size = 0
                    with destination.open("xb") as handle:
                        while True:
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            size += len(block)
                            if size > self.config.max_download_bytes:
                                raise DownloadError(
                                    "response exceeds {} bytes".format(
                                        self.config.max_download_bytes
                                    )
                                )
                            digest.update(block)
                            handle.write(block)
                        if expected_length is not None and size != expected_length:
                            raise http.client.IncompleteRead(
                                b"",
                                max(0, expected_length - size),
                            )
                        handle.flush()
                        os.fsync(handle.fileno())
                    return {
                        "url": final_url,
                        "bytes": size,
                        "sha256": digest.hexdigest(),
                        "http_attempts": attempt,
                    }
            except HTTPError as exc:
                last_error = exc
                destination.unlink(missing_ok=True)
                failed_attempts.append(
                    DownloadAttempt(
                        phase="http_get",
                        url=requested_url,
                        attempt=attempt,
                        outcome="error",
                        http_status=exc.code,
                        error_type=type(exc).__name__,
                        reason=str(exc),
                        failure_kind="http",
                    )
                )
                if exc.code not in RETRYABLE_HTTP_STATUSES or attempt >= total_attempts:
                    break
            except (
                URLError,
                TimeoutError,
                socket.timeout,
                ConnectionError,
                http.client.HTTPException,
                ssl.SSLError,
            ) as exc:
                last_error = exc
                destination.unlink(missing_ok=True)
                failed_attempts.append(
                    DownloadAttempt(
                        phase="http_get",
                        url=requested_url,
                        attempt=attempt,
                        outcome="error",
                        error_type=type(exc).__name__,
                        reason=str(exc),
                        failure_kind="network",
                    )
                )
                if attempt >= total_attempts:
                    break
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            self._sleep_before_retry(attempt)
        destination.unlink(missing_ok=True)
        raise DownloadError(
            "GET {} failed after {} attempt(s): {}".format(url, attempt, last_error),
            url=requested_url,
            http_status=(
                last_error.code if isinstance(last_error, HTTPError) else None
            ),
            http_attempts=attempt,
            attempts=failed_attempts,
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def reject_entity_declarations(raw: bytes, label: str) -> None:
    """Validate XML bytes with an encoding-aware entity-declaration hook."""

    parser = expat.ParserCreate()

    def reject(*_args: Any) -> None:
        raise SourceError("XML entity declarations are not allowed in {}".format(label))

    parser.EntityDeclHandler = reject
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise SourceError("invalid {} XML: {}".format(label, exc)) from exc


def parse_gii_toc(raw: bytes, base_url: str = DEFAULT_TOC_URL) -> List[TocEntry]:
    reject_entity_declarations(raw, "gii-toc")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceError("invalid gii-toc XML: {}".format(exc)) from exc
    entries: List[TocEntry] = []
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        title = ""
        link = ""
        for child in list(element):
            if _local_name(child.tag) == "title":
                title = " ".join("".join(child.itertext()).split())
            elif _local_name(child.tag) == "link":
                link = "".join(child.itertext()).strip()
        if not link:
            raise SourceError("gii-toc item {} has no link".format(len(entries) + 1))
        xml_url = normalize_source_url(link, base_url=base_url)
        if not urlsplit(xml_url).path.lower().endswith(".zip"):
            raise SourceError("gii-toc link is not a ZIP URL: {}".format(xml_url))
        entries.append(
            TocEntry(
                index=len(entries) + 1,
                title=title or Path(urlsplit(xml_url).path).parent.name,
                xml_url=xml_url,
            )
        )
    if not entries:
        raise SourceError("gii-toc contains no <item> entries")
    return entries


def load_pdf_manifest(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("cannot read PDF manifest {}: {}".format(path, exc)) from exc
    if not isinstance(payload, dict):
        raise SourceError("PDF manifest {} must contain a JSON object".format(path))
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise SourceError("PDF manifest {} has no entries list".format(path))
    normalized: List[Dict[str, Any]] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        row["_manifest_position"] = position
        normalized.append(row)
    metadata = {
        "path": str(path.resolve()),
        "sha256": sha256_bytes(raw),
        "bytes": len(raw),
        "entry_count": len(normalized),
        "generated_at": payload.get("generated_at"),
    }
    return normalized, metadata


def make_item_id(law_key: str, duplicate_number: int = 1) -> str:
    path = urlsplit(law_key).path.rstrip("/")
    stem = slugify(unquote(PurePosixPath(path).name))
    identifier = "{}-{}".format(stem, sha256_bytes(law_key.encode("utf-8"))[:12])
    if duplicate_number > 1:
        identifier += "-{}".format(duplicate_number)
    return identifier


def pdf_match_summary(entry: Mapping[str, Any]) -> Dict[str, Any]:
    fields = (
        "ordinal",
        "category",
        "category_index",
        "title",
        "detail_url",
        "pdf_url",
        "relative_file_path",
        "sha256",
        "status",
    )
    return {field: entry.get(field) for field in fields if entry.get(field) is not None}


def reconcile_sources(
    toc_entries: Sequence[TocEntry],
    pdf_entries: Sequence[Mapping[str, Any]],
) -> Tuple[List[CorpusItem], Dict[str, int]]:
    """Reconcile catalog and PDF entries while retaining both unmatched sides."""

    pdf_by_key: Dict[str, Deque[Tuple[int, Mapping[str, Any]]]] = defaultdict(deque)
    for position, entry in enumerate(pdf_entries):
        detail_url = str(entry.get("detail_url") or "").strip()
        if not detail_url:
            continue
        try:
            key = law_key_from_url(detail_url)
        except SourceError:
            continue
        pdf_by_key[key].append((position, entry))

    used_pdf_positions = set()
    key_counts: Counter[str] = Counter()
    items: List[CorpusItem] = []
    matched = 0
    duplicate_pdf_aliases = 0
    for toc_entry in toc_entries:
        key = law_key_from_url(toc_entry.xml_url)
        pdf_matches: List[Mapping[str, Any]] = []
        pdf_match: Optional[Mapping[str, Any]] = None
        while pdf_by_key.get(key):
            pdf_position, match = pdf_by_key[key].popleft()
            used_pdf_positions.add(pdf_position)
            pdf_matches.append(match)
        if pdf_matches:
            pdf_match = pdf_matches[0]
            matched += 1
            duplicate_pdf_aliases += len(pdf_matches) - 1
        key_counts[key] += 1
        detail_url = (
            normalize_source_url(str(pdf_match.get("detail_url")))
            if pdf_match
            else derive_detail_url(toc_entry.xml_url)
        )
        items.append(
            CorpusItem(
                item_id=make_item_id(key, key_counts[key]),
                law_key=key,
                title=toc_entry.title,
                xml_url=toc_entry.xml_url,
                detail_url=detail_url,
                reconciliation_status=(
                    "matched_pdf_manifest" if pdf_match else "toc_only"
                ),
                toc_index=toc_entry.index,
                toc_title=toc_entry.title,
                pdf_manifest_ordinal=(
                    int(pdf_match.get("ordinal") or pdf_match.get("_manifest_position") or 0)
                    if pdf_match
                    else None
                ),
                pdf_title=str(pdf_match.get("title") or "") or None if pdf_match else None,
                category=str(pdf_match.get("category") or "") or None if pdf_match else None,
                category_index=(
                    int(pdf_match.get("category_index") or 0) or None if pdf_match else None
                ),
                pdf_manifest_matches=tuple(
                    pdf_match_summary(match) for match in pdf_matches
                ),
            )
        )

    pdf_only = 0
    invalid_pdf = 0
    for position, pdf_entry in enumerate(pdf_entries):
        if position in used_pdf_positions:
            continue
        detail_value = str(pdf_entry.get("detail_url") or "").strip()
        pdf_matches = [pdf_entry]
        if not detail_value:
            invalid_pdf += 1
            key = "invalid-pdf-entry:{}".format(
                pdf_entry.get("ordinal") or pdf_entry.get("_manifest_position") or position + 1
            )
            xml_url = None
            detail_url = None
        else:
            try:
                detail_url = normalize_source_url(detail_value)
                key = law_key_from_url(detail_url)
                xml_url = derive_xml_url(detail_url)
                while pdf_by_key.get(key):
                    match_position, match = pdf_by_key[key].popleft()
                    used_pdf_positions.add(match_position)
                    if match_position != position:
                        pdf_matches.append(match)
                pdf_only += 1
                duplicate_pdf_aliases += len(pdf_matches) - 1
            except SourceError:
                invalid_pdf += 1
                key = "invalid-pdf-entry:{}".format(
                    pdf_entry.get("ordinal")
                    or pdf_entry.get("_manifest_position")
                    or position + 1
                )
                xml_url = None
                detail_url = None
        key_counts[key] += 1
        pdf_title = str(pdf_entry.get("title") or "").strip() or "PDF manifest entry"
        items.append(
            CorpusItem(
                item_id=make_item_id(key, key_counts[key]),
                law_key=key,
                title=pdf_title,
                xml_url=xml_url,
                detail_url=detail_url,
                reconciliation_status=(
                    "pdf_manifest_only" if xml_url else "invalid_pdf_manifest_entry"
                ),
                pdf_manifest_ordinal=int(
                    pdf_entry.get("ordinal")
                    or pdf_entry.get("_manifest_position")
                    or position + 1
                ),
                pdf_title=pdf_title,
                category=str(pdf_entry.get("category") or "") or None,
                category_index=int(pdf_entry.get("category_index") or 0) or None,
                pdf_manifest_matches=tuple(
                    pdf_match_summary(match) for match in pdf_matches
                ),
            )
        )

    return items, {
        "toc_entries": len(toc_entries),
        "pdf_manifest_entries": len(pdf_entries),
        "matched": matched,
        "duplicate_pdf_aliases": duplicate_pdf_aliases,
        "toc_only": len(toc_entries) - matched,
        "pdf_manifest_only": pdf_only,
        "invalid_pdf_manifest_entries": invalid_pdf,
        "total_corpus_items": len(items),
    }


def parse_links(html: str) -> List[Tuple[str, str, Dict[str, str]]]:
    parser = LinkParser()
    parser.feed(html)
    return parser.links


def discover_xml_urls(detail_url: str, html_bytes: bytes) -> List[str]:
    """Find same-origin XML ZIP anchors, ranked ahead of generic XML ZIPs."""

    try:
        html = html_bytes.decode("utf-8")
    except UnicodeDecodeError:
        html = html_bytes.decode("iso-8859-1", errors="replace")
    candidates: List[Tuple[int, int, str]] = []
    seen = set()
    for position, (href, text, attrs) in enumerate(parse_links(html)):
        if not href:
            continue
        try:
            url = normalize_source_url(href, base_url=detail_url)
        except SourceError:
            continue
        if not same_origin(detail_url, url):
            continue
        path = urlsplit(url).path.lower()
        label = "{} {}".format(text, attrs.get("title") or "").lower()
        if not path.endswith(".zip"):
            continue
        score = 0
        if path.endswith("/xml.zip"):
            score += 10
        if "xml" in PurePosixPath(path).name.lower():
            score += 5
        if "xml" in label:
            score += 2
        if score == 0 or url in seen:
            continue
        seen.add(url)
        candidates.append((-score, position, url))
    candidates.sort()
    return [url for _score, _position, url in candidates]


def _safe_zip_member(name: str) -> PurePosixPath:
    if "\\" in name:
        raise ValidationError("ZIP member uses a backslash: {!r}".format(name))
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValidationError("unsafe ZIP member path: {!r}".format(name))
    return path


def validate_xml_file(path: Path) -> Dict[str, Any]:
    try:
        root_tag: Optional[str] = None
        root_attributes: Dict[str, str] = {}
        parser = expat.ParserCreate(namespace_separator="}")

        def capture_root(name: str, attributes: Mapping[str, str]) -> None:
            nonlocal root_tag, root_attributes
            if root_tag is None:
                root_tag = _local_name(name)
                root_attributes = {
                    _local_name(str(key)): str(value)
                    for key, value in sorted(attributes.items())
                }

        def reject_entity(*_args: Any) -> None:
            raise ValidationError(
                "XML entity declarations are not allowed: {}".format(path.name)
            )

        parser.StartElementHandler = capture_root
        parser.EntityDeclHandler = reject_entity
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        with path.open("rb") as raw_handle:
            while True:
                block = raw_handle.read(1024 * 1024)
                if not block:
                    break
                parser.Parse(block, False)
            parser.Parse(b"", True)
        if root_tag is None:
            raise ValidationError("XML file is empty: {}".format(path.name))
        return {
            "root_element": root_tag,
            "root_attributes": root_attributes,
            "builddate": root_attributes.get("builddate"),
            "document_number": (
                root_attributes.get("doknr")
                or root_attributes.get("document_number")
                or root_attributes.get("id")
            ),
        }
    except (expat.ExpatError, OSError) as exc:
        raise ValidationError("invalid XML {}: {}".format(path.name, exc)) from exc


def validate_and_extract_zip(
    archive_path: Path,
    extraction_dir: Path,
    max_uncompressed_bytes: int,
    max_zip_members: int = 10000,
) -> Dict[str, List[Dict[str, Any]]]:
    """Validate an archive, safely extract XML members, and return their hashes."""

    extraction_dir.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(str(archive_path), "r") as archive:
            infos = archive.infolist()
            if not infos:
                raise ValidationError("ZIP archive is empty")
            if len(infos) > max_zip_members:
                raise ValidationError(
                    "ZIP contains {} members, exceeding limit {}".format(
                        len(infos),
                        max_zip_members,
                    )
                )
            total_uncompressed = 0
            xml_infos: List[Tuple[zipfile.ZipInfo, PurePosixPath]] = []
            asset_infos: List[Tuple[zipfile.ZipInfo, PurePosixPath]] = []
            seen_paths = set()
            for info in infos:
                if info.flag_bits & 0x1:
                    raise ValidationError("encrypted ZIP members are not supported")
                unix_mode = info.external_attr >> 16
                if unix_mode and stat.S_ISLNK(unix_mode):
                    raise ValidationError("ZIP symlink is not allowed: {!r}".format(info.filename))
                if info.is_dir():
                    continue
                member_path = _safe_zip_member(info.filename)
                total_uncompressed += info.file_size
                if total_uncompressed > max_uncompressed_bytes:
                    raise ValidationError(
                        "ZIP exceeds {} uncompressed bytes".format(max_uncompressed_bytes)
                    )
                normalized_name = member_path.as_posix()
                if normalized_name in seen_paths:
                    raise ValidationError(
                        "duplicate ZIP member path: {!r}".format(normalized_name)
                    )
                seen_paths.add(normalized_name)
                if member_path.suffix.lower() == ".xml":
                    xml_infos.append((info, member_path))
                else:
                    asset_infos.append((info, member_path))
            if not xml_infos:
                raise ValidationError("ZIP contains no .xml files")

            metadata: List[Dict[str, Any]] = []
            for info, member_path in xml_infos:
                target = extraction_dir.joinpath(*member_path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info, "r") as source, target.open("xb") as destination:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        digest.update(block)
                        destination.write(block)
                    destination.flush()
                    os.fsync(destination.fileno())
                if size != info.file_size:
                    raise ValidationError(
                        "XML member size mismatch for {!r}".format(info.filename)
                    )
                source_metadata = validate_xml_file(target)
                metadata.append(
                    dict(
                        {
                            "member_name": member_path.as_posix(),
                            "bytes": size,
                            "sha256": digest.hexdigest(),
                        },
                        **source_metadata
                    )
                )
            assets: List[Dict[str, Any]] = []
            for info, member_path in asset_infos:
                digest = hashlib.sha256()
                size = 0
                with archive.open(info, "r") as source:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        digest.update(block)
                if size != info.file_size:
                    raise ValidationError(
                        "ZIP member size mismatch for {!r}".format(info.filename)
                    )
                assets.append(
                    {
                        "member_name": member_path.as_posix(),
                        "bytes": size,
                        "sha256": digest.hexdigest(),
                    }
                )
            return {"xml_files": metadata, "assets": assets}
    except zipfile.BadZipFile as exc:
        raise ValidationError("invalid ZIP archive: {}".format(exc)) from exc


def _safe_output_path(output_dir: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValidationError("unsafe manifest path: {!r}".format(relative_path))
    candidate = output_dir.joinpath(*pure.parts)
    output_root = output_dir.resolve()
    resolved = candidate.resolve()
    if resolved != output_root and output_root not in resolved.parents:
        raise ValidationError("manifest path escapes output directory: {!r}".format(relative_path))
    return candidate


def validate_existing_record(
    record: Mapping[str, Any],
    output_dir: Path,
    max_uncompressed_bytes: int,
    max_zip_members: int = 10000,
) -> Tuple[bool, Optional[str]]:
    try:
        if record.get("status") not in {"ok", "ok_existing"}:
            return False, "prior status is not successful"
        archive_rel = str(record.get("relative_archive_path") or "")
        archive_hash_fields = [
            field for field in ("sha256", "archive_sha256") if field in record
        ]
        if not archive_rel or not archive_hash_fields:
            return False, "prior record lacks archive path or hash"
        archive_path = _safe_output_path(output_dir, archive_rel)
        archive_parent_rel = PurePosixPath(archive_rel).parent
        if "relative_build_dir" in record:
            build_rel = str(record.get("relative_build_dir") or "")
            _safe_output_path(output_dir, build_rel)
            if PurePosixPath(build_rel) != archive_parent_rel:
                return False, "archive path differs from recorded build directory"
        if not archive_path.is_file():
            return False, "archive is missing"
        if archive_path.stat().st_size != int(record.get("archive_bytes") or -1):
            return False, "archive size changed"
        actual_archive_hash = file_sha256(archive_path)
        for field in archive_hash_fields:
            if actual_archive_hash != str(record.get(field) or ""):
                return False, "archive hash changed ({})".format(field)
        archive_xml_sizes: Dict[str, int] = {}
        try:
            with zipfile.ZipFile(str(archive_path), "r") as archive:
                infos = archive.infolist()
                if len(infos) > max_zip_members:
                    return False, "archive exceeds current member-count limit"
                seen_members = set()
                total_uncompressed = 0
                for info in infos:
                    if info.flag_bits & 0x1:
                        return False, "archive contains an encrypted member"
                    unix_mode = info.external_attr >> 16
                    if unix_mode and stat.S_ISLNK(unix_mode):
                        return False, "archive contains a symlink"
                    if info.is_dir():
                        continue
                    member = _safe_zip_member(info.filename).as_posix()
                    if member in seen_members:
                        return False, "archive contains a duplicate member path"
                    seen_members.add(member)
                    total_uncompressed += info.file_size
                    if member.lower().endswith(".xml"):
                        archive_xml_sizes[member] = info.file_size
                if not archive_xml_sizes:
                    return False, "archive no longer contains XML"
                if total_uncompressed > max_uncompressed_bytes:
                    return False, "archive exceeds current uncompressed limit"
        except zipfile.BadZipFile:
            return False, "archive is not a valid ZIP"
        xml_files = record.get("xml_files")
        if not isinstance(xml_files, list) or not xml_files:
            return False, "prior record has no XML file metadata"
        record_xml_names = [
            str(xml_record.get("member_name") or "")
            for xml_record in xml_files
            if isinstance(xml_record, dict)
        ]
        if (
            len(record_xml_names) != len(xml_files)
            or len(set(record_xml_names)) != len(record_xml_names)
            or set(record_xml_names) != set(archive_xml_sizes)
        ):
            return False, "recorded XML member set differs from archive"
        if int(record.get("xml_file_count") or -1) != len(xml_files):
            return False, "recorded XML member count is inconsistent"
        verified_xml: Dict[str, Dict[str, Any]] = {}
        for xml_record in xml_files:
            if not isinstance(xml_record, dict):
                return False, "malformed XML metadata"
            member_name = str(xml_record.get("member_name") or "")
            member_path = _safe_zip_member(member_name)
            xml_rel = str(xml_record.get("relative_file_path") or "")
            xml_path = _safe_output_path(
                output_dir,
                xml_rel,
            )
            expected_xml_rel = archive_parent_rel / "xml" / member_path
            if PurePosixPath(xml_rel) != expected_xml_rel:
                return False, "extracted XML path is inconsistent with archive member"
            if not xml_path.is_file():
                return False, "extracted XML is missing"
            recorded_xml_bytes = int(xml_record.get("bytes") or -1)
            if xml_path.stat().st_size != recorded_xml_bytes:
                return False, "extracted XML size changed"
            if archive_xml_sizes[member_name] != recorded_xml_bytes:
                return False, "recorded XML size differs from archive"
            actual_xml_hash = file_sha256(xml_path)
            if actual_xml_hash != str(xml_record.get("sha256") or ""):
                return False, "extracted XML hash changed"
            actual_xml_metadata = validate_xml_file(xml_path)
            for field in (
                "root_element",
                "root_attributes",
                "builddate",
                "document_number",
            ):
                if field in xml_record and xml_record.get(field) != actual_xml_metadata[field]:
                    return False, "extracted XML metadata changed ({})".format(field)
            verified_xml[xml_rel] = {
                "bytes": recorded_xml_bytes,
                "sha256": actual_xml_hash,
                "member_name": member_name,
                "metadata": actual_xml_metadata,
            }
        if "xml_uncompressed_bytes" in record and int(
            record.get("xml_uncompressed_bytes") or -1
        ) != sum(int(row["bytes"]) for row in verified_xml.values()):
            return False, "recorded XML byte total is inconsistent"

        primary_alias_fields = (
            "xml_bytes",
            "xml_sha256",
            "xml_document_number",
            "xml_build_date",
        )
        if "relative_file_path" in record:
            primary_rel = str(record.get("relative_file_path") or "")
            _safe_output_path(output_dir, primary_rel)
            if primary_rel not in verified_xml:
                return False, "top-level XML path is not in recorded XML files"
            expected_primary_rel = max(
                verified_xml,
                key=lambda relative: (
                    int(verified_xml[relative]["bytes"]),
                    str(verified_xml[relative]["member_name"]),
                ),
            )
            if primary_rel != expected_primary_rel:
                return False, "top-level XML path does not identify the primary XML"
            primary = verified_xml[primary_rel]
            alias_values = {
                "xml_bytes": primary["bytes"],
                "xml_sha256": primary["sha256"],
                "xml_document_number": primary["metadata"]["document_number"],
                "xml_build_date": primary["metadata"]["builddate"],
            }
            for field in primary_alias_fields:
                if field in record and record.get(field) != alias_values[field]:
                    return False, "top-level XML metadata changed ({})".format(field)
        elif any(field in record for field in primary_alias_fields):
            return False, "top-level XML metadata lacks its relative path"
        return True, None
    except (OSError, ValueError, ValidationError) as exc:
        return False, str(exc)


def cleanup_staging(output_dir: Path) -> None:
    """Remove only downloader-owned stale temporary directories."""

    staging_dir = output_dir / ".staging"
    if not staging_dir.exists():
        return
    if staging_dir.is_symlink() or not staging_dir.is_dir():
        raise ValidationError(
            "refusing to clean non-directory staging path: {}".format(staging_dir)
        )
    for child in staging_dir.iterdir():
        if child.name.startswith("download-"):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)


def _build_directories_match(left: Path, right: Path) -> bool:
    if (
        left.is_symlink()
        or right.is_symlink()
        or not left.is_dir()
        or not right.is_dir()
    ):
        return False
    if any(path.is_symlink() for path in left.rglob("*")) or any(
        path.is_symlink() for path in right.rglob("*")
    ):
        return False
    left_files = sorted(
        path.relative_to(left).as_posix()
        for path in left.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    right_files = sorted(
        path.relative_to(right).as_posix()
        for path in right.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if left_files != right_files:
        return False
    return all(
        (left / relative).stat().st_size == (right / relative).stat().st_size
        and file_sha256(left / relative) == file_sha256(right / relative)
        for relative in left_files
    )


def _remove_owned_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _recover_interrupted_target(target_dir: Path) -> None:
    """Resolve downloader-owned replacement backups for one content hash."""

    if not re.fullmatch(r"[0-9a-f]{64}", target_dir.name):
        return
    parent = target_dir.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValidationError(
            "refusing publication recovery below non-directory path: {}".format(parent)
        )
    backups = []
    for child in parent.iterdir():
        match = REPLACED_BUILD_RE.fullmatch(child.name)
        if match and match.group("target") == target_dir.name:
            if child.is_symlink() or not child.is_dir():
                raise ValidationError(
                    "refusing malformed publication backup: {}".format(child)
                )
            backups.append(child)
    if not backups:
        return
    backups.sort(key=lambda path: path.name)
    if target_dir.is_symlink() or (
        target_dir.exists() and not target_dir.is_dir()
    ):
        raise ValidationError(
            "refusing publication recovery over non-directory target: {}".format(
                target_dir
            )
        )
    if not target_dir.exists():
        restored = backups.pop(0)
        os.replace(str(restored), str(target_dir))
    for stale_backup in backups:
        _remove_owned_path(stale_backup)


def recover_interrupted_publications(output_dir: Path) -> None:
    """Recover only exact downloader replacement directories at their known depth."""

    documents_dir = output_dir / "documents"
    if not documents_dir.exists():
        return
    if documents_dir.is_symlink() or not documents_dir.is_dir():
        raise ValidationError(
            "refusing publication recovery below non-directory path: {}".format(
                documents_dir
            )
        )
    targets = set()
    for item_dir in documents_dir.iterdir():
        if item_dir.is_symlink() or not item_dir.is_dir():
            continue
        for child in item_dir.iterdir():
            match = REPLACED_BUILD_RE.fullmatch(child.name)
            if match:
                targets.add(item_dir / match.group("target"))
    for target_dir in sorted(targets, key=lambda path: path.as_posix()):
        _recover_interrupted_target(target_dir)


def _install_build(stage_dir: Path, target_dir: Path) -> None:
    """Publish one validated content-addressed build with rollback on repair."""

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    _recover_interrupted_target(target_dir)
    if not target_dir.exists():
        os.replace(str(stage_dir), str(target_dir))
        return

    if _build_directories_match(stage_dir, target_dir):
        _remove_owned_path(stage_dir)
        return

    backup = target_dir.with_name(".{}-replaced-{}".format(target_dir.name, uuid.uuid4().hex))
    os.replace(str(target_dir), str(backup))
    try:
        os.replace(str(stage_dir), str(target_dir))
    except BaseException:
        os.replace(str(backup), str(target_dir))
        raise
    else:
        _remove_owned_path(backup)


def build_from_url(
    item: CorpusItem,
    source_url: str,
    output_dir: Path,
    client: HTTPClient,
    max_uncompressed_bytes: int,
    max_zip_members: int,
) -> Dict[str, Any]:
    staging_root = output_dir / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    stage_dir: Optional[Path] = Path(
        tempfile.mkdtemp(prefix="download-", dir=str(staging_root))
    )
    try:
        assert stage_dir is not None
        partial_archive = stage_dir / "source.xml.zip.part"
        download = client.download(source_url, partial_archive)
        extraction_dir = stage_dir / "xml"
        archive_metadata = validate_and_extract_zip(
            partial_archive,
            extraction_dir,
            max_uncompressed_bytes=max_uncompressed_bytes,
            max_zip_members=max_zip_members,
        )
        xml_metadata = archive_metadata["xml_files"]
        archive_path = stage_dir / "source.xml.zip"
        os.replace(str(partial_archive), str(archive_path))
        build_hash = str(download["sha256"])
        target_dir = output_dir / "documents" / item.item_id / build_hash
        relative_build = target_dir.relative_to(output_dir).as_posix()
        _safe_output_path(output_dir, relative_build)
        _install_build(stage_dir, target_dir)
        stage_dir = None

        xml_files = []
        for metadata in xml_metadata:
            relative_path = "{}/xml/{}".format(relative_build, metadata["member_name"])
            xml_files.append(
                {
                    "member_name": metadata["member_name"],
                    "relative_file_path": relative_path,
                    "bytes": metadata["bytes"],
                    "sha256": metadata["sha256"],
                    "root_element": metadata["root_element"],
                    "root_attributes": metadata["root_attributes"],
                    "builddate": metadata["builddate"],
                    "document_number": metadata["document_number"],
                }
            )
        builddates = sorted(
            {
                str(row["builddate"])
                for row in xml_files
                if row.get("builddate")
            }
        )
        document_numbers = sorted(
            {
                str(row["document_number"])
                for row in xml_files
                if row.get("document_number")
            }
        )
        primary_xml = max(xml_files, key=lambda row: (int(row["bytes"]), row["member_name"]))
        return {
            "source_url": download["url"],
            "relative_build_dir": relative_build,
            "relative_archive_path": "{}/source.xml.zip".format(relative_build),
            "relative_file_path": primary_xml["relative_file_path"],
            "archive_bytes": download["bytes"],
            "archive_sha256": build_hash,
            "sha256": build_hash,
            "xml_bytes": primary_xml["bytes"],
            "xml_sha256": primary_xml["sha256"],
            "xml_document_number": primary_xml["document_number"],
            "xml_build_date": primary_xml["builddate"],
            "xml_file_count": len(xml_files),
            "xml_uncompressed_bytes": sum(row["bytes"] for row in xml_files),
            "xml_files": xml_files,
            "assets": archive_metadata["assets"],
            "source_build_metadata": {
                "builddates": builddates,
                "document_numbers": document_numbers,
            },
            "http_attempts": download["http_attempts"],
        }
    finally:
        if stage_dir is not None and stage_dir.exists():
            _remove_owned_path(stage_dir)


def _candidate_urls(item: CorpusItem) -> List[str]:
    candidates: List[str] = []
    if item.xml_url:
        candidates.append(normalize_source_url(item.xml_url))
    if item.detail_url:
        derived = derive_xml_url(item.detail_url)
        if derived not in candidates:
            candidates.append(derived)
    return candidates


def prior_source_matches_item(
    item: CorpusItem,
    prior_record: Mapping[str, Any],
) -> Tuple[bool, Optional[str]]:
    candidates = (
        {normalize_source_url(item.xml_url)}
        if item.xml_url
        else set(_candidate_urls(item))
    )
    prior_xml_url = str(prior_record.get("xml_url") or "").strip()
    if not candidates or not prior_xml_url:
        return False, "prior record lacks a comparable requested XML URL"
    try:
        normalized_prior = normalize_source_url(prior_xml_url)
    except SourceError as exc:
        return False, "prior XML URL is invalid: {}".format(exc)
    if normalized_prior not in candidates:
        return False, "catalog XML URL changed from {}".format(normalized_prior)
    return True, None


def _item_attempts_for_error(
    phase: str,
    url: str,
    exc: BaseException,
) -> List[DownloadAttempt]:
    if isinstance(exc, DownloadError) and exc.attempts:
        return [
            DownloadAttempt(
                phase=phase,
                url=attempt.url,
                attempt=attempt.attempt,
                outcome=attempt.outcome,
                http_status=attempt.http_status,
                error_type=attempt.error_type,
                reason=attempt.reason,
                failure_kind=attempt.failure_kind,
            )
            for attempt in exc.attempts
        ]
    return [
        DownloadAttempt(
            phase=phase,
            url=url,
            attempt=1,
            outcome="error",
            error_type=type(exc).__name__,
            reason=str(exc),
            failure_kind=(
                "validation"
                if isinstance(exc, ValidationError)
                else "source"
                if isinstance(exc, SourceError)
                else "download"
                if isinstance(exc, DownloadError)
                else "io"
                if isinstance(exc, OSError)
                else "other"
            ),
        )
    ]


def download_item(
    item: CorpusItem,
    output_dir: Path,
    client: HTTPClient,
    max_uncompressed_bytes: int,
    max_zip_members: int,
) -> Dict[str, Any]:
    attempted: List[str] = []
    failures: List[str] = []
    attempt_metadata: List[DownloadAttempt] = []
    candidates = _candidate_urls(item)
    for candidate in candidates:
        attempted.append(candidate)
        try:
            result = build_from_url(
                item,
                candidate,
                output_dir,
                client,
                max_uncompressed_bytes,
                max_zip_members,
            )
            result["attempted_urls"] = attempted
            result["discovery_used"] = False
            return result
        except (CorpusError, OSError) as exc:
            failures.append("{}: {}".format(candidate, exc))
            attempt_metadata.extend(
                _item_attempts_for_error("direct_xml", candidate, exc)
            )

    if item.detail_url:
        discovery_metadata: Optional[Dict[str, Any]] = None
        try:
            html, final_url, discovery_attempts = client.fetch_bytes(item.detail_url)
            final_detail_url = normalize_source_url(final_url)
            discovery_metadata = {
                "requested_url": item.detail_url,
                "source_url": final_detail_url,
                "bytes": len(html),
                "sha256": sha256_bytes(html),
                "http_attempts": discovery_attempts,
            }
            discovered = discover_xml_urls(final_detail_url, html)
            attempt_metadata.append(
                DownloadAttempt(
                    phase="detail_page",
                    url=final_detail_url,
                    attempt=discovery_attempts,
                    outcome="success",
                )
            )
        except (CorpusError, OSError) as exc:
            failures.append("{}: detail-page discovery failed: {}".format(item.detail_url, exc))
            attempt_metadata.extend(
                _item_attempts_for_error("detail_page", item.detail_url, exc)
            )
            discovered = []
        for candidate in discovered:
            if candidate in attempted:
                continue
            attempted.append(candidate)
            try:
                result = build_from_url(
                    item,
                    candidate,
                    output_dir,
                    client,
                    max_uncompressed_bytes,
                    max_zip_members,
                )
                result["attempted_urls"] = attempted
                result["discovery_used"] = True
                result["discovery_detail_page"] = discovery_metadata
                return result
            except (CorpusError, OSError) as exc:
                failures.append("{}: {}".format(candidate, exc))
                attempt_metadata.extend(
                    _item_attempts_for_error("discovered_xml", candidate, exc)
                )

    if not attempted:
        raise SourceError("entry has neither a valid XML URL nor a detail URL")
    raise DownloadError(
        "; ".join(failures) or "no usable XML ZIP URL",
        attempts=attempt_metadata,
    )


def base_record(item: CorpusItem) -> Dict[str, Any]:
    source_slug = slugify(unquote(PurePosixPath(urlsplit(item.law_key).path.rstrip("/")).name))
    return {
        "item_id": item.item_id,
        "law_key": item.law_key,
        "slug": source_slug,
        "title": item.title,
        "toc_title": item.toc_title,
        "pdf_title": item.pdf_title,
        "toc_index": item.toc_index,
        "pdf_manifest_ordinal": item.pdf_manifest_ordinal,
        "category": item.category,
        "category_index": item.category_index,
        "pdf_match": item.reconciliation_status == "matched_pdf_manifest",
        "pdf_manifest_matches": [
            dict(match) for match in item.pdf_manifest_matches
        ],
        "reconciliation_status": item.reconciliation_status,
        "xml_url": item.xml_url,
        "detail_url": item.detail_url,
        "status": "not_selected",
        "error": None,
    }


def _is_conclusive_pdf_only_404(
    item: CorpusItem,
    exc: DownloadError,
) -> bool:
    if item.reconciliation_status != "pdf_manifest_only":
        return False
    if any(
        attempt.outcome == "error"
        and attempt.failure_kind not in {"http", "network"}
        for attempt in exc.attempts
    ):
        return False
    for phase in ("direct_xml", "detail_page"):
        terminal = exc.terminal_attempt(phase)
        if (
            terminal is None
            or terminal.outcome != "error"
            or terminal.http_status != 404
        ):
            return False
    return True


def process_item(
    item: CorpusItem,
    prior_record: Optional[Mapping[str, Any]],
    config: RunConfig,
    client: HTTPClient,
) -> Dict[str, Any]:
    record = base_record(item)
    started = time.monotonic()
    record["started_at"] = utc_now()
    if config.resume and prior_record:
        source_matches, validation_error = prior_source_matches_item(item, prior_record)
        if source_matches:
            valid, validation_error = validate_existing_record(
                prior_record,
                config.output_dir,
                config.max_uncompressed_bytes,
                config.max_zip_members,
            )
        else:
            valid = False
        if valid:
            preserved = dict(prior_record)
            preserved.update({key: value for key, value in record.items() if key != "status"})
            preserved["status"] = "ok_existing"
            preserved["error"] = None
            preserved["resume_validation"] = "verified"
            preserved["completed_at"] = utc_now()
            preserved["duration_seconds"] = round(time.monotonic() - started, 3)
            return preserved
        record["resume_validation"] = validation_error
    try:
        result = download_item(
            item,
            config.output_dir,
            client,
            config.max_uncompressed_bytes,
            config.max_zip_members,
        )
        record.update(result)
        record["status"] = "ok"
        record["error"] = None
    except DownloadError as exc:
        record["download_attempts"] = [
            attempt.manifest_dict() for attempt in exc.attempts
        ]
        if _is_conclusive_pdf_only_404(item, exc):
            record["status"] = "xml_unavailable"
            record["error"] = None
            record["xml_unavailable_reason"] = str(exc)
        else:
            record["status"] = "error"
            record["error"] = "{}: {}".format(type(exc).__name__, exc)
    except Exception as exc:  # one bad law must not abort a full-corpus run
        record["status"] = "error"
        record["error"] = "{}: {}".format(type(exc).__name__, exc)
    record["completed_at"] = utc_now()
    record["duration_seconds"] = round(time.monotonic() - started, 3)
    return record


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, str(path))
    except BaseException:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def append_manifest_journal(path: Path, record: Mapping[str, Any]) -> None:
    """Durably append one result without rewriting the full corpus manifest."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_manifest_journal(path: Path) -> Dict[str, Dict[str, Any]]:
    """Load complete journal rows, tolerating only a torn final append."""

    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SourceError("cannot read XML recovery journal {}: {}".format(path, exc)) from exc
    records: Dict[str, Dict[str, Any]] = {}
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1:
                break
            raise SourceError(
                "malformed XML recovery journal {} at line {}: {}".format(
                    path,
                    index + 1,
                    exc,
                )
            ) from exc
        if isinstance(record, dict) and record.get("item_id"):
            records[str(record["item_id"])] = record
    return records


def load_existing_xml_manifest(
    path: Path,
    journal_path: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("cannot resume malformed XML manifest {}: {}".format(path, exc)) from exc
    if not isinstance(payload, dict):
        raise SourceError("XML manifest {} must contain a JSON object".format(path))
    records: Dict[str, Dict[str, Any]] = {}
    for entry in payload.get("entries") or []:
        if isinstance(entry, dict) and entry.get("item_id"):
            records[str(entry["item_id"])] = dict(entry)
    if journal_path is not None:
        records.update(load_manifest_journal(journal_path))
    return records


def read_toc_source(
    source: str,
    client: HTTPClient,
) -> Tuple[List[TocEntry], Dict[str, Any]]:
    source_parts = urlsplit(source)
    if source_parts.scheme.lower() in {"http", "https"}:
        raw, final_url, attempts = client.fetch_bytes(normalize_source_url(source))
        normalized_final = normalize_source_url(final_url)
        entries = parse_gii_toc(raw, base_url=normalized_final)
        metadata = {
            "source": normalized_final,
            "sha256": sha256_bytes(raw),
            "bytes": len(raw),
            "entry_count": len(entries),
            "http_attempts": attempts,
        }
    elif not source_parts.scheme:
        path = Path(source)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SourceError("cannot read gii-toc source {}: {}".format(path, exc)) from exc
        entries = parse_gii_toc(raw, base_url=DEFAULT_TOC_URL)
        metadata = {
            "source": str(path.resolve()),
            "sha256": sha256_bytes(raw),
            "bytes": len(raw),
            "entry_count": len(entries),
            "http_attempts": 0,
        }
    else:
        raise SourceError(
            "unsupported gii-toc source scheme {!r}; use HTTP(S) or a local path".format(
                source_parts.scheme
            )
        )
    return entries, metadata


def build_manifest(
    items: Sequence[CorpusItem],
    records: Mapping[str, Mapping[str, Any]],
    toc_metadata: Optional[Mapping[str, Any]],
    pdf_metadata: Optional[Mapping[str, Any]],
    reconciliation: Mapping[str, int],
    config: RunConfig,
    selected_count: int,
) -> Dict[str, Any]:
    entries = [dict(records[item.item_id]) for item in items]
    status_counts = dict(sorted(Counter(str(row.get("status") or "unknown") for row in entries).items()))
    script_path = Path(__file__).resolve()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "source": "Gesetze-im-Internet XML corpus",
        "updated_at": utc_now(),
        "tool": {
            "name": TOOL_NAME,
            "version": TOOL_VERSION,
            "script": str(script_path),
            "script_sha256": file_sha256(script_path),
            "python": platform.python_version(),
        },
        "sources": {
            "gii_toc": dict(toc_metadata) if toc_metadata else None,
            "pdf_manifest": dict(pdf_metadata) if pdf_metadata else None,
        },
        "reconciliation": dict(reconciliation),
        "run": {
            "workers": config.workers,
            "timeout_seconds": config.timeout_seconds,
            "retries": config.retries,
            "retry_backoff_seconds": config.retry_backoff_seconds,
            "max_download_bytes": config.max_download_bytes,
            "max_uncompressed_bytes": config.max_uncompressed_bytes,
            "max_zip_members": config.max_zip_members,
            "limit": config.limit,
            "resume": config.resume,
            "force": config.force,
            "selected_count": selected_count,
        },
        "entries_recorded": len(entries),
        "status_counts": status_counts,
        "entries": entries,
    }


def _run_locked(config: RunConfig) -> Dict[str, Any]:
    if config.workers < 1:
        raise ValueError("workers must be at least 1")
    if config.limit is not None and config.limit < 0:
        raise ValueError("limit cannot be negative")
    if config.resume and config.force:
        raise ValueError("resume and force are mutually exclusive")
    if config.retries < 0:
        raise ValueError("retries cannot be negative")
    if config.timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    if config.max_zip_members < 1:
        raise ValueError("max_zip_members must be at least 1")

    manifest_path = config.output_dir / "manifest.json"
    journal_path = config.output_dir / ".manifest-journal.jsonl"
    if manifest_path.exists() and not config.resume and not config.force:
        raise SourceError(
            "{} already exists; pass --resume to verify/reuse it or --force to redownload".format(
                manifest_path
            )
        )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    recover_interrupted_publications(config.output_dir)
    cleanup_staging(config.output_dir)
    client = HTTPClient(
        HTTPConfig(
            timeout_seconds=config.timeout_seconds,
            retries=config.retries,
            retry_backoff_seconds=config.retry_backoff_seconds,
            max_download_bytes=config.max_download_bytes,
        )
    )

    toc_entries: List[TocEntry] = []
    toc_metadata: Optional[Dict[str, Any]] = None
    if config.toc_source:
        toc_entries, toc_metadata = read_toc_source(config.toc_source, client)

    pdf_entries: List[Dict[str, Any]] = []
    pdf_metadata: Optional[Dict[str, Any]] = None
    if config.pdf_manifest:
        if config.pdf_manifest.exists():
            pdf_entries, pdf_metadata = load_pdf_manifest(config.pdf_manifest)
        elif not toc_entries:
            raise SourceError("PDF manifest does not exist: {}".format(config.pdf_manifest))

    if not toc_entries and not pdf_entries:
        raise SourceError("no gii-toc or PDF manifest entries are available")

    items, reconciliation = reconcile_sources(toc_entries, pdf_entries)
    selected = items if config.limit is None else items[: config.limit]
    try:
        existing_records = (
            load_existing_xml_manifest(manifest_path, journal_path)
            if manifest_path.exists()
            else load_manifest_journal(journal_path)
        )
    except SourceError:
        if not config.force:
            raise
        existing_records = {}
    prior_records = existing_records if config.resume else {}
    records: Dict[str, Dict[str, Any]] = {}
    for item in items:
        base = base_record(item)
        prior = existing_records.get(item.item_id)
        if prior:
            source_matches, source_error = prior_source_matches_item(item, prior)
            if source_matches:
                preserved = dict(prior)
                preserved.update(
                    {key: value for key, value in base.items() if key != "status"}
                )
                records[item.item_id] = preserved
            else:
                base["status"] = "stale_source"
                base["error"] = source_error
                base["previous_source_url"] = prior.get("source_url")
                base["previous_archive_sha256"] = (
                    prior.get("archive_sha256") or prior.get("sha256")
                )
                records[item.item_id] = base
        else:
            records[item.item_id] = base

    def persist(clear_journal: bool = False) -> Dict[str, Any]:
        manifest = build_manifest(
            items,
            records,
            toc_metadata,
            pdf_metadata,
            reconciliation,
            config,
            selected_count=len(selected),
        )
        atomic_write_json(manifest_path, manifest)
        if clear_journal:
            journal_path.unlink(missing_ok=True)
        return manifest

    persist(clear_journal=True)
    executor = ThreadPoolExecutor(max_workers=config.workers)
    futures: Dict[Any, CorpusItem] = {}
    completed_count = 0
    try:
        for item in selected:
            future = executor.submit(
                process_item,
                item,
                prior_records.get(item.item_id),
                config,
                client,
            )
            futures[future] = item
        for future in as_completed(futures):
            item = futures[future]
            record = future.result()
            records[item.item_id] = record
            completed_count += 1
            append_manifest_journal(journal_path, record)
            print(
                "[{}/{}] {} {}".format(
                    completed_count,
                    len(selected),
                    record["status"],
                    item.item_id,
                ),
                flush=True,
            )
            if completed_count % MANIFEST_CHECKPOINT_EVERY == 0:
                persist(clear_journal=True)
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        try:
            persist(clear_journal=True)
        except Exception:
            pass
        raise
    else:
        executor.shutdown(wait=True)
    return persist(clear_journal=True)


def run(config: RunConfig) -> Dict[str, Any]:
    """Run with an exclusive per-output lock to protect manifest and staging state."""

    config.output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = config.output_dir / ".download.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SourceError(
                "another downloader is already using {}".format(config.output_dir)
            ) from exc
        try:
            return _run_locked(config)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--toc",
        default=DEFAULT_TOC_URL,
        help="gii-toc.xml URL or local path (default: %(default)s)",
    )
    parser.add_argument(
        "--no-toc",
        action="store_true",
        help="disable the authoritative catalog and use only the PDF manifest",
    )
    parser.add_argument(
        "--pdf-manifest",
        type=Path,
        default=DEFAULT_PDF_MANIFEST,
        help="existing GII PDF manifest used for reconciliation",
    )
    parser.add_argument(
        "--no-pdf-manifest",
        action="store_true",
        help="download the catalog without PDF-manifest reconciliation",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(4, os.cpu_count() or 1)),
    )
    parser.add_argument("--limit", type=int, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=float, default=45.0, dest="timeout_seconds")
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="retries after the initial HTTP request",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=0.5,
        dest="retry_backoff_seconds",
    )
    parser.add_argument("--max-download-mib", type=int, default=100)
    parser.add_argument("--max-uncompressed-mib", type=int, default=500)
    parser.add_argument("--max-zip-members", type=int, default=10000)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit cannot be negative")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    config = RunConfig(
        output_dir=args.output_dir,
        toc_source=None if args.no_toc else args.toc,
        pdf_manifest=None if args.no_pdf_manifest else args.pdf_manifest,
        workers=args.workers,
        limit=args.limit,
        resume=args.resume,
        force=args.force,
        timeout_seconds=args.timeout_seconds,
        retries=args.retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
        max_download_bytes=args.max_download_mib * 1024 * 1024,
        max_uncompressed_bytes=args.max_uncompressed_mib * 1024 * 1024,
        max_zip_members=args.max_zip_members,
    )
    try:
        manifest = run(config)
    except (CorpusError, OSError, ValueError) as exc:
        parser.exit(2, "error: {}\n".format(exc))
    selected_ids = {
        entry["item_id"]
        for entry in manifest["entries"][: manifest["run"]["selected_count"]]
    }
    selected_errors = [
        entry
        for entry in manifest["entries"]
        if entry["item_id"] in selected_ids and entry.get("status") == "error"
    ]
    print(
        "manifest={} selected={} status_counts={}".format(
            config.output_dir / "manifest.json",
            manifest["run"]["selected_count"],
            json.dumps(manifest["status_counts"], sort_keys=True),
        )
    )
    return 1 if selected_errors else 0


if __name__ == "__main__":
    sys.exit(main())
