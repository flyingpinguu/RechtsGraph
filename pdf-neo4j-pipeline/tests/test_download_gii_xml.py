import importlib.util
import io
import json
import re
import sys
import threading
import zipfile
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "download_gii_xml.py"
SPEC = importlib.util.spec_from_file_location("download_gii_xml", SCRIPT_PATH)
assert SPEC and SPEC.loader
downloader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = downloader
SPEC.loader.exec_module(downloader)


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class LocalServer:
    def __init__(self, routes):
        self.routes = routes
        self.counts = Counter()
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with outer.lock:
                    outer.counts[self.path] += 1
                    request_number = outer.counts[self.path]
                route = outer.routes.get(self.path)
                if route is None:
                    response = (404, "text/plain", b"missing")
                elif callable(route):
                    response = route(request_number)
                else:
                    response = route
                status, content_type, body = response[:3]
                extra_headers = dict(response[3]) if len(response) > 3 else {}
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                if "Content-Length" not in extra_headers:
                    self.send_header("Content-Length", str(len(body)))
                for key, value in extra_headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:{}".format(self.httpd.server_port)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()


def toc_xml(items):
    body = ["<?xml version='1.0' encoding='UTF-8'?><items>"]
    for title, link in items:
        body.append("<item><title>{}</title><link>{}</link></item>".format(title, link))
    body.append("</items>")
    return "".join(body).encode("utf-8")


def write_pdf_manifest(path, entries):
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-01T00:00:00Z",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )


def test_toc_parsing_normalizes_gii_http_and_reconciles_without_dropping_entries():
    toc_entries = downloader.parse_gii_toc(
        toc_xml(
            [
                (
                    "Matched official title",
                    "http://www.gesetze-im-internet.de/matched/xml.zip",
                ),
                (
                    "Catalog-only official title",
                    "http://www.gesetze-im-internet.de/toc_only/xml.zip",
                ),
            ]
        )
    )
    assert toc_entries[0].xml_url == (
        "https://www.gesetze-im-internet.de/matched/xml.zip"
    )
    assert downloader.law_key_from_url(
        "https://gesetze-im-internet.de/matched/index.html"
    ) == downloader.law_key_from_url(toc_entries[0].xml_url)

    items, stats = downloader.reconcile_sources(
        toc_entries,
        [
            {
                "ordinal": 7,
                "category": "M",
                "category_index": 2,
                "title": "MATCH",
                "detail_url": "https://www.gesetze-im-internet.de/matched/index.html",
            },
            {
                "ordinal": 8,
                "category": "P",
                "category_index": 3,
                "title": "PDFONLY",
                "detail_url": "http://www.gesetze-im-internet.de/pdf_only/index.html",
            },
            {
                "ordinal": 9,
                "category": "Z",
                "category_index": 4,
                "title": "MATCH-ALIAS",
                "detail_url": "http://www.gesetze-im-internet.de/matched/index.html",
            },
            {
                "ordinal": 10,
                "category": "X",
                "category_index": 1,
                "title": "BROKEN",
                "detail_url": "https://www.gesetze-im-internet.de:bad/broken/index.html",
            },
        ],
    )

    assert [item.reconciliation_status for item in items] == [
        "matched_pdf_manifest",
        "toc_only",
        "pdf_manifest_only",
        "invalid_pdf_manifest_entry",
    ]
    assert items[0].pdf_title == "MATCH"
    assert [row["title"] for row in items[0].pdf_manifest_matches] == [
        "MATCH",
        "MATCH-ALIAS",
    ]
    assert items[1].toc_title == "Catalog-only official title"
    assert items[2].xml_url == (
        "https://www.gesetze-im-internet.de/pdf_only/xml.zip"
    )
    assert stats == {
        "toc_entries": 2,
        "pdf_manifest_entries": 4,
        "matched": 1,
        "duplicate_pdf_aliases": 1,
        "toc_only": 1,
        "pdf_manifest_only": 1,
        "invalid_pdf_manifest_entries": 1,
        "total_corpus_items": 4,
    }


def test_toc_and_resume_source_identity_reject_entity_or_changed_url():
    utf16_toc = (
        "<?xml version='1.0' encoding='UTF-16'?>"
        "<!DOCTYPE items [<!ENTITY unsafe 'expanded'>]>"
        "<items><item><title>&unsafe;</title>"
        "<link>https://www.gesetze-im-internet.de/x/xml.zip</link>"
        "</item></items>"
    ).encode("utf-16")
    with pytest.raises(downloader.SourceError, match="entity declarations"):
        downloader.parse_gii_toc(utf16_toc)

    item = downloader.CorpusItem(
        item_id="law-id",
        law_key="https://www.gesetze-im-internet.de/law/",
        title="Law",
        xml_url="https://www.gesetze-im-internet.de/law/xml.zip?build=2",
        detail_url="https://www.gesetze-im-internet.de/law/index.html",
        reconciliation_status="toc_only",
    )
    matches, reason = downloader.prior_source_matches_item(
        item,
        {
            "xml_url": (
                "https://www.gesetze-im-internet.de/law/xml.zip?build=1"
            )
        },
    )
    assert matches is False
    assert "changed" in reason


@pytest.mark.parametrize(
    "url, expected_error",
    [
        ("https://example.test/law/xml.zip", "not allowlisted"),
        (
            "https://www.gesetze-im-internet.de.example.test/law/xml.zip",
            "not allowlisted",
        ),
        (
            "https://user@www.gesetze-im-internet.de/law/xml.zip",
            "credentials",
        ),
        (
            "https://www.gesetze-im-internet.de:443/law/xml.zip",
            "not allowlisted",
        ),
        (
            "ftp://www.gesetze-im-internet.de/law/xml.zip",
            "absolute HTTP\\(S\\)",
        ),
        ("file:///tmp/gii-toc.xml", "absolute HTTP\\(S\\)"),
    ],
)
def test_source_url_allowlist_rejects_unsafe_origins(url, expected_error):
    with pytest.raises(downloader.SourceError, match=expected_error):
        downloader.normalize_source_url(url)


@pytest.mark.parametrize("operation", ["fetch", "download"])
def test_http_client_rejects_disallowed_final_redirect_before_read(
    tmp_path,
    monkeypatch,
    operation,
):
    class RedirectedResponse:
        headers = {}

        def __init__(self):
            self.read_called = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return "https://attacker.example/redirected"

        def read(self, _size):
            self.read_called = True
            return b"must not be read"

    response = RedirectedResponse()
    client = downloader.HTTPClient(downloader.HTTPConfig(retries=0))
    monkeypatch.setattr(
        client._opener,
        "open",
        lambda *_args, **_kwargs: response,
    )
    with pytest.raises(downloader.SourceError, match="not allowlisted"):
        if operation == "fetch":
            client.fetch_bytes(downloader.DEFAULT_TOC_URL)
        else:
            client.download(
                "https://www.gesetze-im-internet.de/law/xml.zip",
                tmp_path / "source.zip.part",
            )
    assert response.read_called is False
    assert not (tmp_path / "source.zip.part").exists()


def test_redirect_handler_blocks_disallowed_location_before_outbound_request(
    monkeypatch,
):
    blocked_routes = {
        "/internal": (200, "text/plain", b"must never be requested"),
    }
    allowed_routes = {}
    with LocalServer(blocked_routes) as blocked_server, LocalServer(
        allowed_routes
    ) as allowed_server:
        allowed_routes["/start"] = (
            302,
            "text/plain",
            b"",
            {"Location": blocked_server.base_url + "/internal"},
        )
        monkeypatch.setattr(
            downloader,
            "GII_ALLOWED_ORIGINS",
            downloader.GII_ALLOWED_ORIGINS | {allowed_server.base_url},
        )
        client = downloader.HTTPClient(downloader.HTTPConfig(retries=0))
        with pytest.raises(downloader.SourceError, match="refusing redirect"):
            client.fetch_bytes(allowed_server.base_url + "/start")

        assert allowed_server.counts["/start"] == 1
        assert blocked_server.counts["/internal"] == 0


def test_redirect_handler_resolves_relative_redirect_chains(monkeypatch):
    routes = {
        "/start": (302, "text/plain", b"", {"Location": "middle"}),
        "/middle": (307, "text/plain", b"", {"Location": "/final"}),
        "/final": (200, "text/plain", b"allowed"),
    }
    with LocalServer(routes) as server:
        monkeypatch.setattr(
            downloader,
            "GII_ALLOWED_ORIGINS",
            downloader.GII_ALLOWED_ORIGINS | {server.base_url},
        )
        client = downloader.HTTPClient(downloader.HTTPConfig(retries=0))
        body, final_url, attempts = client.fetch_bytes(server.base_url + "/start")

    assert body == b"allowed"
    assert final_url == server.base_url + "/final"
    assert attempts == 1
    assert {path: server.counts[path] for path in routes} == {
        "/start": 1,
        "/middle": 1,
        "/final": 1,
    }


def test_read_toc_source_supports_plain_local_path_and_rejects_other_schemes(
    tmp_path,
):
    toc_path = tmp_path / "gii-toc.xml"
    toc_path.write_bytes(
        toc_xml(
            [
                (
                    "Local catalog",
                    "https://www.gesetze-im-internet.de/local/xml.zip",
                )
            ]
        )
    )

    class NoNetworkClient:
        def fetch_bytes(self, _url):
            raise AssertionError("local catalog must not use HTTP")

    entries, metadata = downloader.read_toc_source(toc_path.as_posix(), NoNetworkClient())
    assert [entry.title for entry in entries] == ["Local catalog"]
    assert metadata["source"] == str(toc_path.resolve())
    assert metadata["http_attempts"] == 0
    with pytest.raises(downloader.SourceError, match="unsupported"):
        downloader.read_toc_source("ftp://example.test/gii-toc.xml", NoNetworkClient())


def test_pdf_only_all_404_is_non_error_and_resume_retries(tmp_path, monkeypatch):
    valid_zip = make_zip({"later.xml": b"<dokumente doknr='LATER'/>"})
    routes = {
        "/pdf_only/xml.zip": (404, "text/plain", b"missing"),
        "/pdf_only/index.html": (404, "text/plain", b"missing"),
    }
    with LocalServer(routes) as server:
        monkeypatch.setattr(
            downloader,
            "GII_ALLOWED_ORIGINS",
            downloader.GII_ALLOWED_ORIGINS | {server.base_url},
        )
        pdf_manifest = tmp_path / "pdf-manifest.json"
        write_pdf_manifest(
            pdf_manifest,
            [
                {
                    "ordinal": 1,
                    "title": "PDFONLY",
                    "detail_url": server.base_url + "/pdf_only/index.html",
                }
            ],
        )
        output_dir = tmp_path / "xml-corpus"
        exit_code = downloader.main(
            [
                "--no-toc",
                "--pdf-manifest",
                str(pdf_manifest),
                "--output-dir",
                str(output_dir),
                "--workers",
                "1",
                "--retries",
                "0",
                "--timeout",
                "2",
            ]
        )
        manifest = json.loads(
            (output_dir / "manifest.json").read_text(encoding="utf-8")
        )
        record = manifest["entries"][0]
        assert exit_code == 0
        assert record["status"] == "xml_unavailable"
        assert record["error"] is None
        assert "404" in record["xml_unavailable_reason"]
        assert {
            (attempt["phase"], attempt["http_status"])
            for attempt in record["download_attempts"]
        } == {("direct_xml", 404), ("detail_page", 404)}

        routes["/pdf_only/xml.zip"] = (200, "application/zip", valid_zip)
        resumed = downloader.run(
            downloader.RunConfig(
                output_dir=output_dir,
                toc_source=None,
                pdf_manifest=pdf_manifest,
                workers=1,
                retries=0,
                timeout_seconds=2,
                resume=True,
            )
        )

    assert resumed["entries"][0]["status"] == "ok"
    assert routes
    assert server.counts["/pdf_only/xml.zip"] == 2
    assert server.counts["/pdf_only/index.html"] == 1


@pytest.mark.parametrize("failure_kind", ["http_500", "timeout"])
def test_pdf_only_non_404_download_failures_remain_errors(
    tmp_path,
    monkeypatch,
    failure_kind,
):
    item = downloader.CorpusItem(
        item_id="pdf-only",
        law_key="https://www.gesetze-im-internet.de/pdf_only/",
        title="PDF only",
        xml_url="https://www.gesetze-im-internet.de/pdf_only/xml.zip",
        detail_url="https://www.gesetze-im-internet.de/pdf_only/index.html",
        reconciliation_status="pdf_manifest_only",
    )
    client = downloader.HTTPClient(downloader.HTTPConfig(retries=0))

    def fail_open(request, **_kwargs):
        if failure_kind == "http_500":
            raise downloader.HTTPError(
                request.full_url,
                500,
                "server error",
                None,
                None,
            )
        raise downloader.socket.timeout("timed out")

    monkeypatch.setattr(client._opener, "open", fail_open)
    record = downloader.process_item(
        item,
        None,
        downloader.RunConfig(
            output_dir=tmp_path,
            toc_source=None,
            pdf_manifest=None,
            retries=0,
        ),
        client,
    )
    assert record["status"] == "error"
    assert record["error"].startswith("DownloadError:")
    assert "xml_unavailable_reason" not in record
    if failure_kind == "http_500":
        assert {row["http_status"] for row in record["download_attempts"]} == {
            500
        }
    else:
        assert {row["error_type"] for row in record["download_attempts"]} == {
            "timeout"
        }


def test_pdf_only_transient_retry_then_terminal_404_is_unavailable(
    tmp_path,
    monkeypatch,
):
    item = downloader.CorpusItem(
        item_id="pdf-only",
        law_key="https://www.gesetze-im-internet.de/pdf_only/",
        title="PDF only",
        xml_url="https://www.gesetze-im-internet.de/pdf_only/xml.zip",
        detail_url="https://www.gesetze-im-internet.de/pdf_only/index.html",
        reconciliation_status="pdf_manifest_only",
    )
    client = downloader.HTTPClient(
        downloader.HTTPConfig(retries=1, retry_backoff_seconds=0)
    )
    outcomes = iter(
        [
            ConnectionResetError("reset"),
            downloader.HTTPError(
                item.xml_url,
                404,
                "not found",
                None,
                None,
            ),
            ConnectionResetError("reset"),
            downloader.HTTPError(
                item.detail_url,
                404,
                "not found",
                None,
                None,
            ),
        ]
    )

    def fail_open(_request, **_kwargs):
        raise next(outcomes)

    monkeypatch.setattr(client._opener, "open", fail_open)
    record = downloader.process_item(
        item,
        None,
        downloader.RunConfig(
            output_dir=tmp_path,
            toc_source=None,
            pdf_manifest=None,
            retries=1,
            retry_backoff_seconds=0,
        ),
        client,
    )
    assert record["status"] == "xml_unavailable"
    assert [
        (row["phase"], row["http_status"], row["failure_kind"])
        for row in record["download_attempts"]
    ] == [
        ("direct_xml", None, "network"),
        ("direct_xml", 404, "http"),
        ("detail_page", None, "network"),
        ("detail_page", 404, "http"),
    ]


@pytest.mark.parametrize(
    "earlier_attempt, expected",
    [
        (
            downloader.DownloadAttempt(
                phase="direct_xml",
                url="https://www.gesetze-im-internet.de/law/xml.zip",
                attempt=1,
                outcome="error",
                http_status=500,
                failure_kind="http",
            ),
            True,
        ),
        (
            downloader.DownloadAttempt(
                phase="direct_xml",
                url="https://www.gesetze-im-internet.de/law/xml.zip",
                attempt=1,
                outcome="error",
                http_status=404,
                failure_kind="http",
            ),
            False,
        ),
        (
            downloader.DownloadAttempt(
                phase="direct_xml",
                url="https://www.gesetze-im-internet.de/law/xml.zip",
                attempt=1,
                outcome="error",
                error_type="ValidationError",
                failure_kind="validation",
            ),
            False,
        ),
    ],
)
def test_pdf_only_terminal_or_validation_failure_remains_error(
    earlier_attempt,
    expected,
):
    item = downloader.CorpusItem(
        item_id="pdf-only",
        law_key="https://www.gesetze-im-internet.de/law/",
        title="PDF only",
        xml_url="https://www.gesetze-im-internet.de/law/xml.zip",
        detail_url="https://www.gesetze-im-internet.de/law/index.html",
        reconciliation_status="pdf_manifest_only",
    )
    attempts = [
        earlier_attempt,
        downloader.DownloadAttempt(
            phase="direct_xml",
            url=item.xml_url,
            attempt=2,
            outcome="error",
            http_status=(500 if earlier_attempt.http_status == 404 else 404),
            failure_kind="http",
        ),
        downloader.DownloadAttempt(
            phase="detail_page",
            url=item.detail_url,
            attempt=1,
            outcome="error",
            http_status=404,
            failure_kind="http",
        ),
    ]
    error = downloader.DownloadError("failed", attempts=attempts)
    assert downloader._is_conclusive_pdf_only_404(item, error) is expected


def test_http_download_retries_a_truncated_body(tmp_path, monkeypatch):
    valid_zip = make_zip({"law.xml": b"<dokumente/>"})

    def truncated_once(request_number):
        if request_number == 1:
            return (
                200,
                "application/zip",
                valid_zip[:8],
                {"Content-Length": str(len(valid_zip))},
            )
        return 200, "application/zip", valid_zip

    with LocalServer({"/law/xml.zip": truncated_once}) as server:
        monkeypatch.setattr(
            downloader,
            "GII_ALLOWED_ORIGINS",
            downloader.GII_ALLOWED_ORIGINS | {server.base_url},
        )
        client = downloader.HTTPClient(
            downloader.HTTPConfig(
                timeout_seconds=2,
                retries=1,
                retry_backoff_seconds=0,
            )
        )
        result = client.download(
            server.base_url + "/law/xml.zip",
            tmp_path / "source.zip.part",
        )
    assert result["http_attempts"] == 2
    assert result["sha256"] == downloader.sha256_bytes(valid_zip)


def test_run_retries_discovers_extracts_atomically_and_resumes(tmp_path, monkeypatch):
    direct_zip = make_zip(
        {
            "direct.xml": (
                b"<dokumente builddate='20260725010101' doknr='TEST0001'>"
                b"<law id='direct'/></dokumente>"
            )
        }
    )
    fallback_zip = make_zip(
        {
            "nested/fallback.xml": (
                b"<?xml version='1.0'?><documents><law id='fallback'/></documents>"
            ),
            "assets/notice.txt": b"source package asset",
        }
    )
    pdf_only_zip = make_zip({"pdf-only.xml": b"<documents><law/></documents>"})

    routes = {}
    with LocalServer(routes) as server:
        monkeypatch.setattr(
            downloader,
            "GII_ALLOWED_ORIGINS",
            downloader.GII_ALLOWED_ORIGINS | {server.base_url},
        )
        catalog = toc_xml(
            [
                ("Direct", server.base_url + "/direct/xml.zip"),
                ("Fallback", server.base_url + "/fallback/xml.zip"),
            ]
        )

        def direct_route(request_number):
            if request_number == 1:
                return 503, "text/plain", b"retry"
            return 200, "application/zip", direct_zip

        routes.update(
            {
                "/gii-toc.xml": (200, "application/xml", catalog),
                "/direct/xml.zip": direct_route,
                "/fallback/xml.zip": (200, "text/html", b"<html>not a zip</html>"),
                "/fallback/index.html": (
                    302,
                    "text/plain",
                    b"",
                    {"Location": "/landing/detail.html"},
                ),
                "/landing/detail.html": (
                    200,
                    "text/html",
                    (
                        b"<a title='XML download' href='export-data.zip'>"
                        b"XML export</a>"
                    ),
                ),
                "/landing/export-data.zip": (
                    200,
                    "application/zip",
                    fallback_zip,
                ),
                "/pdf_only/xml.zip": (
                    200,
                    "application/zip",
                    pdf_only_zip,
                ),
            }
        )
        pdf_manifest = tmp_path / "pdf-manifest.json"
        write_pdf_manifest(
            pdf_manifest,
            [
                {
                    "ordinal": 1,
                    "category": "D",
                    "category_index": 1,
                    "title": "DIRECT",
                    "detail_url": server.base_url + "/direct/index.html",
                },
                {
                    "ordinal": 2,
                    "category": "P",
                    "category_index": 1,
                    "title": "PDFONLY",
                    "detail_url": server.base_url + "/pdf_only/index.html",
                },
            ],
        )
        output_dir = tmp_path / "xml-corpus"
        stale = output_dir / ".staging" / "download-stale"
        stale.mkdir(parents=True)
        (stale / "orphan.part").write_bytes(b"partial")

        config = downloader.RunConfig(
            output_dir=output_dir,
            toc_source=server.base_url + "/gii-toc.xml",
            pdf_manifest=pdf_manifest,
            workers=3,
            retries=1,
            retry_backoff_seconds=0,
            timeout_seconds=2,
        )
        manifest = downloader.run(config)

        assert manifest["reconciliation"] == {
            "toc_entries": 2,
            "pdf_manifest_entries": 2,
            "matched": 1,
            "duplicate_pdf_aliases": 0,
            "toc_only": 1,
            "pdf_manifest_only": 1,
            "invalid_pdf_manifest_entries": 0,
            "total_corpus_items": 3,
        }
        assert manifest["status_counts"] == {"ok": 3}
        assert manifest["sources"]["gii_toc"]["sha256"]
        assert manifest["sources"]["pdf_manifest"]["sha256"]
        assert manifest["tool"]["script_sha256"]
        assert routes
        records = {row["title"]: row for row in manifest["entries"]}
        assert records["Fallback"]["discovery_used"] is True
        assert records["Fallback"]["source_url"].endswith("/landing/export-data.zip")
        assert records["Fallback"]["discovery_detail_page"]["source_url"].endswith(
            "/landing/detail.html"
        )
        assert records["Fallback"]["discovery_detail_page"]["sha256"]
        assert records["Fallback"]["assets"][0]["member_name"] == "assets/notice.txt"
        assert records["Fallback"]["assets"][0]["sha256"]
        assert records["PDFONLY"]["reconciliation_status"] == "pdf_manifest_only"
        assert records["Direct"]["http_attempts"] == 2
        assert records["Direct"]["source_build_metadata"] == {
            "builddates": ["20260725010101"],
            "document_numbers": ["TEST0001"],
        }
        assert records["Direct"]["xml_document_number"] == "TEST0001"
        assert records["Direct"]["xml_build_date"] == "20260725010101"
        assert records["Direct"]["relative_file_path"].endswith("/xml/direct.xml")
        assert records["Direct"]["xml_files"][0]["root_element"] == "dokumente"
        for record in manifest["entries"]:
            archive = output_dir / record["relative_archive_path"]
            assert archive.is_file()
            assert downloader.file_sha256(archive) == record["sha256"]
            assert record["xml_file_count"] >= 1
            for xml_record in record["xml_files"]:
                xml_path = output_dir / xml_record["relative_file_path"]
                assert xml_path.is_file()
                downloader.validate_xml_file(xml_path)
        assert not any((output_dir / ".staging").iterdir())
        assert not list(output_dir.rglob("*.part"))
        assert not (output_dir / ".manifest-journal.jsonl").exists()

        zip_counts_before_resume = {
            path: server.counts[path]
            for path in (
                "/direct/xml.zip",
                "/fallback/xml.zip",
                "/landing/export-data.zip",
                "/pdf_only/xml.zip",
            )
        }
        resumed = downloader.run(
            downloader.RunConfig(
                output_dir=output_dir,
                toc_source=server.base_url + "/gii-toc.xml",
                pdf_manifest=pdf_manifest,
                workers=3,
                retries=1,
                retry_backoff_seconds=0,
                timeout_seconds=2,
                resume=True,
            )
        )
        assert resumed["status_counts"] == {"ok_existing": 3}
        assert not (output_dir / ".manifest-journal.jsonl").exists()
        assert {
            path: server.counts[path] for path in zip_counts_before_resume
        } == zip_counts_before_resume

        fallback_record = next(
            row for row in resumed["entries"] if row["title"] == "Fallback"
        )
        corrupted_xml = output_dir / fallback_record["xml_files"][0]["relative_file_path"]
        corrupted_xml.write_text("<broken>", encoding="utf-8")
        repaired = downloader.run(
            downloader.RunConfig(
                output_dir=output_dir,
                toc_source=server.base_url + "/gii-toc.xml",
                pdf_manifest=pdf_manifest,
                workers=2,
                retries=1,
                retry_backoff_seconds=0,
                timeout_seconds=2,
                resume=True,
            )
        )
        repaired_record = next(
            row for row in repaired["entries"] if row["title"] == "Fallback"
        )
        assert repaired_record["status"] == "ok"
        assert repaired_record["resume_validation"] == "extracted XML size changed"
        downloader.validate_xml_file(
            output_dir / repaired_record["xml_files"][0]["relative_file_path"]
        )
        assert not any((output_dir / ".staging").iterdir())

        direct_count_before_force = server.counts["/direct/xml.zip"]
        forced = downloader.run(
            downloader.RunConfig(
                output_dir=output_dir,
                toc_source=server.base_url + "/gii-toc.xml",
                pdf_manifest=pdf_manifest,
                workers=2,
                limit=1,
                retries=1,
                retry_backoff_seconds=0,
                timeout_seconds=2,
                force=True,
            )
        )
        assert forced["run"]["selected_count"] == 1
        assert server.counts["/direct/xml.zip"] == direct_count_before_force + 1
        assert next(row for row in forced["entries"] if row["title"] == "Direct")[
            "status"
        ] == "ok"
        assert next(row for row in forced["entries"] if row["title"] == "PDFONLY")[
            "relative_archive_path"
        ]

        (output_dir / "manifest.json").write_text("[]", encoding="utf-8")
        direct_count_before_recovery = server.counts["/direct/xml.zip"]
        recovered = downloader.run(
            downloader.RunConfig(
                output_dir=output_dir,
                toc_source=server.base_url + "/gii-toc.xml",
                pdf_manifest=pdf_manifest,
                workers=1,
                limit=1,
                retries=1,
                retry_backoff_seconds=0,
                timeout_seconds=2,
                force=True,
            )
        )
        assert recovered["entries"][0]["status"] == "ok"
        assert server.counts["/direct/xml.zip"] == direct_count_before_recovery + 1
        json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        routes["/gii-toc.xml"] = (
            200,
            "application/xml",
            toc_xml(
                [
                    ("Direct", server.base_url + "/direct/xml.zip?revision=2"),
                    ("Fallback", server.base_url + "/fallback/xml.zip"),
                ]
            ),
        )
        limited_resume = downloader.run(
            downloader.RunConfig(
                output_dir=output_dir,
                toc_source=server.base_url + "/gii-toc.xml",
                pdf_manifest=pdf_manifest,
                workers=1,
                limit=0,
                resume=True,
                timeout_seconds=2,
            )
        )
        stale_direct = next(
            row for row in limited_resume["entries"] if row["title"] == "Direct"
        )
        assert stale_direct["status"] == "stale_source"
        assert stale_direct["xml_url"].endswith("/direct/xml.zip?revision=2")
        assert "relative_archive_path" not in stale_direct
        assert stale_direct["previous_archive_sha256"]


def test_resume_validation_requires_complete_archive_member_inventory(tmp_path):
    output_dir = tmp_path / "corpus"
    build_dir = output_dir / "documents" / "law" / "build"
    build_dir.mkdir(parents=True)
    archive_path = build_dir / "source.xml.zip"
    archive_path.write_bytes(
        make_zip(
            {
                "one.xml": b"<dokumente doknr='ONE'/>",
                "two.xml": b"<dokumente doknr='TWO'/>",
            }
        )
    )
    extracted = downloader.validate_and_extract_zip(
        archive_path,
        build_dir / "xml",
        max_uncompressed_bytes=1024 * 1024,
    )
    first = extracted["xml_files"][0]
    record = {
        "status": "ok",
        "relative_archive_path": archive_path.relative_to(output_dir).as_posix(),
        "archive_bytes": archive_path.stat().st_size,
        "sha256": downloader.file_sha256(archive_path),
        "xml_file_count": 1,
        "xml_files": [
            {
                **first,
                "relative_file_path": (
                    build_dir / "xml" / first["member_name"]
                ).relative_to(output_dir).as_posix(),
            }
        ],
    }
    valid, reason = downloader.validate_existing_record(
        record,
        output_dir,
        max_uncompressed_bytes=1024 * 1024,
    )
    assert valid is False
    assert reason == "recorded XML member set differs from archive"


def test_resume_validation_checks_every_present_archive_and_xml_alias(tmp_path):
    output_dir = tmp_path / "corpus"
    build_dir = output_dir / "documents" / "law" / "build"
    build_dir.mkdir(parents=True)
    archive_path = build_dir / "source.xml.zip"
    archive_path.write_bytes(
        make_zip(
            {
                "one.xml": b"<dokumente doknr='ONE'/>",
                "nested/two.xml": (
                    b"<dokumente builddate='20260726010101' doknr='TWO'>"
                    b"<text>larger primary document</text></dokumente>"
                ),
            }
        )
    )
    extracted = downloader.validate_and_extract_zip(
        archive_path,
        build_dir / "xml",
        max_uncompressed_bytes=1024 * 1024,
    )
    xml_files = []
    for metadata in extracted["xml_files"]:
        xml_files.append(
            {
                **metadata,
                "relative_file_path": (
                    build_dir / "xml" / metadata["member_name"]
                ).relative_to(output_dir).as_posix(),
            }
        )
    primary = max(
        xml_files,
        key=lambda row: (int(row["bytes"]), str(row["member_name"])),
    )
    archive_hash = downloader.file_sha256(archive_path)
    record = {
        "status": "ok",
        "relative_build_dir": build_dir.relative_to(output_dir).as_posix(),
        "relative_archive_path": archive_path.relative_to(output_dir).as_posix(),
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": archive_hash,
        "sha256": archive_hash,
        "relative_file_path": primary["relative_file_path"],
        "xml_bytes": primary["bytes"],
        "xml_sha256": primary["sha256"],
        "xml_document_number": primary["document_number"],
        "xml_build_date": primary["builddate"],
        "xml_file_count": len(xml_files),
        "xml_uncompressed_bytes": sum(row["bytes"] for row in xml_files),
        "xml_files": xml_files,
    }
    assert downloader.validate_existing_record(
        record,
        output_dir,
        max_uncompressed_bytes=1024 * 1024,
    ) == (True, None)

    copied_xml = output_dir / "documents" / "law" / "copied" / "one.xml"
    copied_xml.parent.mkdir(parents=True)
    copied_xml.write_bytes(
        output_dir.joinpath(*Path(xml_files[0]["relative_file_path"]).parts).read_bytes()
    )
    mutations = [
        (
            lambda row: row.update({"archive_sha256": "0" * 64}),
            "archive hash changed \\(archive_sha256\\)",
        ),
        (
            lambda row: row.update({"xml_sha256": "0" * 64}),
            "top-level XML metadata changed \\(xml_sha256\\)",
        ),
        (
            lambda row: row.update(
                {
                    "relative_build_dir": (
                        build_dir.parent / "other"
                    ).relative_to(output_dir).as_posix()
                }
            ),
            "archive path differs",
        ),
        (
            lambda row: row.update(
                {
                    "relative_file_path": copied_xml.relative_to(
                        output_dir
                    ).as_posix()
                }
            ),
            "top-level XML path",
        ),
        (
            lambda row: row["xml_files"][0].update(
                {
                    "relative_file_path": copied_xml.relative_to(
                        output_dir
                    ).as_posix()
                }
            ),
            "extracted XML path is inconsistent",
        ),
    ]
    for mutate, expected_error in mutations:
        mutated = json.loads(json.dumps(record))
        mutate(mutated)
        valid, reason = downloader.validate_existing_record(
            mutated,
            output_dir,
            max_uncompressed_bytes=1024 * 1024,
        )
        assert valid is False
        assert reason is not None
        assert re.search(expected_error, reason)


def test_interrupted_publication_recovery_is_deterministic_and_narrow(tmp_path):
    output_dir = tmp_path / "corpus"
    item_dir = output_dir / "documents" / "law"
    item_dir.mkdir(parents=True)

    missing_hash = "a" * 64
    first_backup = item_dir / ".{}-replaced-{}".format(missing_hash, "0" * 32)
    second_backup = item_dir / ".{}-replaced-{}".format(missing_hash, "1" * 32)
    first_backup.mkdir()
    second_backup.mkdir()
    (first_backup / "marker").write_text("first", encoding="utf-8")
    (second_backup / "marker").write_text("second", encoding="utf-8")

    published_hash = "b" * 64
    published = item_dir / published_hash
    stale_backup = item_dir / ".{}-replaced-{}".format(
        published_hash,
        "2" * 32,
    )
    published.mkdir()
    stale_backup.mkdir()
    (published / "marker").write_text("published", encoding="utf-8")
    (stale_backup / "marker").write_text("old", encoding="utf-8")

    unrelated = item_dir / ".notes-replaced-{}".format("3" * 32)
    unrelated.mkdir()
    downloader.recover_interrupted_publications(output_dir)

    restored = item_dir / missing_hash
    assert (restored / "marker").read_text(encoding="utf-8") == "first"
    assert not first_backup.exists()
    assert not second_backup.exists()
    assert (published / "marker").read_text(encoding="utf-8") == "published"
    assert not stale_backup.exists()
    assert unrelated.is_dir()


def test_zip_member_count_is_bounded(tmp_path):
    archive_path = tmp_path / "many.zip"
    archive_path.write_bytes(
        make_zip(
            {
                "law.xml": b"<dokumente/>",
                "asset-1.txt": b"",
                "asset-2.txt": b"",
            }
        )
    )
    with pytest.raises(downloader.ValidationError, match="exceeding limit 2"):
        downloader.validate_and_extract_zip(
            archive_path,
            tmp_path / "many-extracted",
            max_uncompressed_bytes=1024,
            max_zip_members=2,
        )


@pytest.mark.parametrize(
    "archive_bytes, expected_error",
    [
        (b"this is not a zip", "invalid ZIP archive"),
        (make_zip({"readme.txt": b"not XML"}), "contains no .xml files"),
        (make_zip({"payload.xml": b"<unclosed>"}), "invalid XML"),
        (
            make_zip(
                {
                    "payload.xml": (
                        b"<!DOCTYPE root [<!ENTITY unsafe 'expanded'>]>"
                        b"<root>&unsafe;</root>"
                    )
                }
            ),
            "entity declarations are not allowed",
        ),
        (
            make_zip(
                {
                    "payload.xml": (
                        "<?xml version='1.0' encoding='UTF-16'?>"
                        "<!DOCTYPE root [<!ENTITY unsafe 'expanded'>]>"
                        "<root>&unsafe;</root>"
                    ).encode("utf-16")
                }
            ),
            "entity declarations are not allowed",
        ),
    ],
)
def test_validation_rejects_bad_zip_and_non_xml_payloads(
    tmp_path,
    archive_bytes,
    expected_error,
):
    archive_path = tmp_path / "source.zip"
    archive_path.write_bytes(archive_bytes)
    with pytest.raises(downloader.ValidationError, match=expected_error):
        downloader.validate_and_extract_zip(
            archive_path,
            tmp_path / "extracted",
            max_uncompressed_bytes=1024 * 1024,
        )
