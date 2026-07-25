import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "batch_extract_gii_xml.py"
SPEC = importlib.util.spec_from_file_location("batch_extract_gii_xml", SCRIPT_PATH)
assert SPEC and SPEC.loader
batch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = batch
SPEC.loader.exec_module(batch)


XML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<dokumente builddate="20260726010101" doknr="BJNRBATCH">
  <norm builddate="20260726010101" doknr="BJNRBATCH">
    <metadaten>
      <jurabk>BatchG</jurabk>
      <amtabk>BatchG</amtabk>
      <kurzue>Batchgesetz</kurzue>
      <langue>Gesetz für den XML-Batchtest</langue>
    </metadaten>
    <textdaten/>
  </norm>
  <norm doknr="BJNRBATCH001">
    <metadaten>
      <jurabk>BatchG</jurabk>
      <enbez>§ 1</enbez>
      <titel>Zweck</titel>
    </metadaten>
    <textdaten><text><Content><P>(1) Ein Testsatz.</P></Content></text></textdaten>
  </norm>
</dokumente>
"""


def write_package(source_dir, relative_path):
    package = source_dir / relative_path
    package.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BJNRBATCH.xml", XML_SAMPLE)
        archive.writestr("formula.png", b"image-placeholder")
    return package


def write_manifest(path, entries):
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "updated_at": "2026-07-26T00:00:00Z",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )


def good_entry(package, relative_package, **updates):
    entry = {
        "item_id": "batchg-123456789abc",
        "slug": "batchg",
        "title": "Batchgesetz",
        "toc_index": 2,
        "status": "ok",
        "relative_archive_path": relative_package,
        "relative_file_path": "documents/batch/xml/BJNRBATCH.xml",
        "archive_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "xml_sha256": hashlib.sha256(XML_SAMPLE.encode("utf-8")).hexdigest(),
        "pdf_manifest_matches": [
            {
                "ordinal": 7,
                "category": "B",
                "title": "BatchG",
                "relative_file_path": "B/0007_batchg.pdf",
                "sha256": "pdf-sha256",
                "status": "ok_existing",
            }
        ],
    }
    entry.update(updates)
    return entry


def test_selection_is_success_only_catalog_ordered_and_limited():
    manifest = {
        "entries": [
            {"item_id": "late", "toc_index": 4, "status": "ok"},
            {"item_id": "ignored", "toc_index": 1, "status": "not_selected"},
            {"item_id": "first", "toc_index": 2, "status": "ok_existing"},
            {"item_id": "middle", "toc_index": 3, "status": "ok"},
        ]
    }
    selected = batch.select_entries(manifest, limit=2)
    assert [(index, row["item_id"]) for index, row in selected] == [
        (2, "first"),
        (3, "middle"),
    ]


def test_source_manifest_reconciliation_separates_xml_and_pdf_paths():
    entry = {
        "item_id": "law-id",
        "relative_file_path": "documents/law/xml/BJNR.xml",
        "relative_archive_path": "documents/law/source.xml.zip",
        "pdf_manifest_matches": [
            {
                "ordinal": 9,
                "category": "Z",
                "relative_file_path": "Z/0009_law.pdf",
                "status": "ok",
            },
            {
                "ordinal": 2,
                "category": "A",
                "relative_file_path": "A/0002_law.pdf",
                "status": "ok_existing",
            },
        ],
    }
    adapted = batch.reconciled_source_manifest_entry(entry)
    assert adapted["xml_relative_file_path"] == "documents/law/xml/BJNR.xml"
    assert adapted["xml_package_relative_path"] == "documents/law/source.xml.zip"
    assert adapted["relative_file_path"] == "A/0002_law.pdf"
    assert adapted["pdf_file"] == "A/0002_law.pdf"
    assert adapted["source_pdf"] == "A/0002_law.pdf"

    unmatched = batch.reconciled_source_manifest_entry(
        {
            "item_id": "xml-only",
            "relative_file_path": "documents/xml-only/BJNR.xml",
            "pdf_manifest_matches": [],
        }
    )
    assert unmatched["source_xml"] == "documents/xml-only/BJNR.xml"
    assert "relative_file_path" not in unmatched
    assert "source_pdf" not in unmatched


def test_batch_isolates_errors_and_writes_atomic_raw_and_aggregate_manifests(tmp_path):
    source_dir = tmp_path / "xml-corpus"
    source_dir.mkdir()
    relative_package = "documents/batch/content/source.xml.zip"
    package = write_package(source_dir, relative_package)
    manifest_path = source_dir / "manifest.json"
    write_manifest(
        manifest_path,
        [
            good_entry(package, relative_package),
            {
                "item_id": "missing-123456789abc",
                "slug": "missing",
                "title": "Missing source",
                "toc_index": 1,
                "status": "ok_existing",
                "relative_archive_path": "documents/missing/source.xml.zip",
                "archive_sha256": "missing-sha256",
                "relative_file_path": "documents/missing/BJNRMISSING.xml",
                "pdf_manifest_matches": [],
            },
            {
                "item_id": "not-selected",
                "toc_index": 0,
                "status": "not_selected",
            },
        ],
    )
    output_dir = tmp_path / "output"
    config = batch.BatchConfig(
        manifest_path=manifest_path,
        source_dir=source_dir,
        output_dir=output_dir,
        workers=1,
    )

    aggregate = batch.run(config)

    assert aggregate["source_manifest"]["eligible_entries"] == 2
    assert aggregate["run"]["selected_entries"] == 2
    assert aggregate["status_counts"] == {"error": 1, "ok": 1}
    assert [row["item_id"] for row in aggregate["entries"]] == [
        "missing-123456789abc",
        "batchg-123456789abc",
    ]
    assert aggregate["entries"][0]["error_type"] == "FileNotFoundError"
    assert aggregate["errors"][0]["item_id"] == "missing-123456789abc"

    raw_path = output_dir / "raw" / "B" / "batchg-123456789abc_raw.json"
    assert raw_path.is_file()
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    assert payload["source_manifest_entry"]["source_xml"].endswith("BJNRBATCH.xml")
    assert payload["source_manifest_entry"]["relative_file_path"] == "B/0007_batchg.pdf"
    assert payload["documents"][0]["source_pdf"] == "B/0007_batchg.pdf"
    assert payload["documents"][0]["metadata"]["source_package_kind"] == "zip"
    assert payload["documents"][0]["metadata"]["source_asset_count"] == 0
    assert (output_dir / "manifest.json").is_file()
    assert not list(output_dir.rglob("*.tmp"))


def test_resume_verifies_existing_source_and_force_regenerates(tmp_path):
    source_dir = tmp_path / "xml-corpus"
    source_dir.mkdir()
    relative_package = "documents/batch/source.xml.zip"
    package = write_package(source_dir, relative_package)
    manifest_path = source_dir / "manifest.json"
    write_manifest(manifest_path, [good_entry(package, relative_package)])
    output_dir = tmp_path / "output"

    initial = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            source_dir=source_dir,
            output_dir=output_dir,
            workers=1,
        )
    )
    assert initial["status_counts"] == {"ok": 1}
    raw_path = output_dir / initial["entries"][0]["output_path"]
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    payload["test_sentinel"] = True
    batch.atomic_write_json(raw_path, payload)

    resumed = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            source_dir=source_dir,
            output_dir=output_dir,
            workers=1,
            resume=True,
        )
    )
    assert resumed["status_counts"] == {"skipped_existing": 1}
    assert resumed["entries"][0]["resume_validation"] == "verified"
    assert json.loads(raw_path.read_text(encoding="utf-8"))["test_sentinel"] is True

    forced = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            source_dir=source_dir,
            output_dir=output_dir,
            workers=1,
            force=True,
        )
    )
    assert forced["status_counts"] == {"ok": 1}
    assert "test_sentinel" not in json.loads(raw_path.read_text(encoding="utf-8"))


def test_changed_source_package_is_rejected_before_raw_output_is_published(tmp_path):
    source_dir = tmp_path / "xml-corpus"
    source_dir.mkdir()
    relative_package = "documents/batch/source.xml.zip"
    package = write_package(source_dir, relative_package)
    manifest_path = source_dir / "manifest.json"
    entry = good_entry(package, relative_package, archive_sha256="stale-hash")
    write_manifest(manifest_path, [entry])
    output_dir = tmp_path / "output"

    aggregate = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            source_dir=source_dir,
            output_dir=output_dir,
            workers=1,
        )
    )

    assert aggregate["status_counts"] == {"error": 1}
    assert "hash differs" in aggregate["entries"][0]["error"]
    assert not (output_dir / "raw" / "B" / "batchg-123456789abc_raw.json").exists()


def test_existing_batch_requires_explicit_resume_or_force(tmp_path):
    source_dir = tmp_path / "xml-corpus"
    source_dir.mkdir()
    relative_package = "documents/batch/source.xml.zip"
    package = write_package(source_dir, relative_package)
    manifest_path = source_dir / "manifest.json"
    write_manifest(manifest_path, [good_entry(package, relative_package)])
    output_dir = tmp_path / "output"
    config = batch.BatchConfig(
        manifest_path=manifest_path,
        source_dir=source_dir,
        output_dir=output_dir,
        workers=1,
    )
    batch.run(config)
    with pytest.raises(batch.BatchSourceError, match="--resume"):
        batch.run(config)


def test_batch_adds_secondary_pdf_page_numbers_when_matched(tmp_path):
    fitz = pytest.importorskip("fitz")
    source_dir = tmp_path / "xml-corpus"
    source_dir.mkdir()
    relative_package = "documents/batch/source.xml.zip"
    package = write_package(source_dir, relative_package)
    manifest_path = source_dir / "manifest.json"
    write_manifest(manifest_path, [good_entry(package, relative_package)])

    pdf_dir = tmp_path / "pdf-corpus"
    pdf_path = pdf_dir / "B" / "0007_batchg.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_textbox(
        fitz.Rect(40, 40, 550, 800),
        "§ 1 Zweck\n(1) Ein Testsatz.",
        fontsize=11,
    )
    pdf.save(pdf_path)
    pdf.close()

    output_dir = tmp_path / "output"
    aggregate = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            source_dir=source_dir,
            output_dir=output_dir,
            pdf_dir=pdf_dir,
            align_pdf=True,
            workers=1,
        )
    )

    assert aggregate["status_counts"] == {"ok": 1}
    assert aggregate["entries"][0]["pdf_alignment_status"] == "aligned"
    raw_path = output_dir / aggregate["entries"][0]["output_path"]
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    document = payload["documents"][0]
    assert document["metadata"]["pdf_pages"] == 1
    assert document["page_refs"][0]["page_number"] == 1
    assert "path" not in document["page_refs"][0]
    paragraph = next(
        unit
        for unit in document["structural_units"]
        if unit["unit_type"] == "paragraph"
    )
    assert paragraph["page_range"] == {"start": 1, "end": 1}
