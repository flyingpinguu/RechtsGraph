import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "batch_extract_gii_pdf_fallbacks.py"
SPEC = importlib.util.spec_from_file_location(
    "batch_extract_gii_pdf_fallbacks",
    SCRIPT_PATH,
)
assert SPEC and SPEC.loader
batch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = batch
SPEC.loader.exec_module(batch)


def write_manifest(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
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


def write_test_pdf(path, text="§ 1 Zweck\n(1) Ein Testsatz."):
    fitz = pytest.importorskip("fitz")
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 550, 800), text, fontsize=11)
    pdf.save(path)
    pdf.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pdf_only_entry(relative_path, sha256, **updates):
    entry = {
        "item_id": "fallbackg-123456789abc",
        "law_key": "https://www.gesetze-im-internet.de/fallbackg/",
        "slug": "fallbackg",
        "title": "FallbackG",
        "pdf_manifest_ordinal": 17,
        "category": "F",
        "category_index": 3,
        "reconciliation_status": "pdf_manifest_only",
        "status": "error",
        "error": "official XML returned 404",
        "pdf_manifest_matches": [
            {
                "ordinal": 17,
                "category": "F",
                "category_index": 3,
                "title": "FallbackG",
                "relative_file_path": relative_path,
                "sha256": sha256,
                "status": "ok",
            }
        ],
    }
    entry.update(updates)
    return entry


def test_selection_accepts_only_pdf_only_records_without_xml():
    manifest = {
        "entries": [
            pdf_only_entry("F/0003_fallbackg.pdf", "a" * 64),
            pdf_only_entry(
                "F/0004_late.pdf",
                "b" * 64,
                item_id="late",
                pdf_manifest_ordinal=40,
            ),
            {
                **pdf_only_entry("F/0001_hasxml.pdf", "c" * 64, item_id="hasxml"),
                "relative_archive_path": "documents/hasxml/source.xml.zip",
            },
            {
                **pdf_only_entry("F/0002_matched.pdf", "d" * 64, item_id="matched"),
                "reconciliation_status": "matched_pdf_manifest",
            },
            {"status": "error", "error": "unrelated download failure"},
        ]
    }

    selected = batch.select_entries(manifest, limit=1)

    assert [(index, row["item_id"]) for index, row in selected] == [
        (0, "fallbackg-123456789abc")
    ]


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.pdf",
        "/absolute/outside.pdf",
        "F\\..\\outside.pdf",
    ],
)
def test_unsafe_pdf_paths_are_isolated_without_extraction(tmp_path, relative_path):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    manifest_path = tmp_path / "xml-manifest.json"
    write_manifest(
        manifest_path,
        [pdf_only_entry(relative_path, "a" * 64)],
    )
    output_dir = tmp_path / "output"

    aggregate = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
        )
    )

    assert aggregate["status_counts"] == {"error": 1}
    assert aggregate["entries"][0]["error_type"] == "SourcePathError"
    assert "unsafe PDF manifest path" in aggregate["entries"][0]["error"]
    assert not list((output_dir / "raw").rglob("*.json"))


def test_hash_mismatch_is_rejected_before_output_is_published(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    relative_path = "F/0003_fallbackg.pdf"
    write_test_pdf(pdf_dir / relative_path)
    manifest_path = tmp_path / "xml-manifest.json"
    write_manifest(
        manifest_path,
        [pdf_only_entry(relative_path, "0" * 64)],
    )
    output_dir = tmp_path / "output"

    aggregate = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
        )
    )

    assert aggregate["status_counts"] == {"error": 1}
    assert "hash differs" in aggregate["entries"][0]["error"]
    assert not list((output_dir / "raw").rglob("*.json"))


def test_pdf_symlink_cannot_escape_the_configured_source_root(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    outside_pdf = tmp_path / "outside.pdf"
    pdf_sha256 = write_test_pdf(outside_pdf)
    linked_pdf = pdf_dir / "F" / "0003_fallbackg.pdf"
    linked_pdf.parent.mkdir()
    linked_pdf.symlink_to(outside_pdf)
    manifest_path = tmp_path / "xml-manifest.json"
    write_manifest(
        manifest_path,
        [pdf_only_entry("F/0003_fallbackg.pdf", pdf_sha256)],
    )
    output_dir = tmp_path / "output"

    aggregate = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
        )
    )

    assert aggregate["status_counts"] == {"error": 1}
    assert aggregate["entries"][0]["error_type"] == "SourcePathError"
    assert "escapes PDF directory" in aggregate["entries"][0]["error"]
    assert not list((output_dir / "raw").rglob("*.json"))


def test_successful_fallback_has_explicit_provenance_and_atomic_manifest(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    relative_path = "F/0003_fallbackg.pdf"
    pdf_sha256 = write_test_pdf(pdf_dir / relative_path)
    manifest_path = tmp_path / "xml-manifest.json"
    write_manifest(
        manifest_path,
        [pdf_only_entry(relative_path, pdf_sha256)],
    )
    output_dir = tmp_path / "output"

    aggregate = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
        )
    )

    assert aggregate["source_kind"] == "gii_pdf_fallback"
    assert aggregate["status_counts"] == {"ok": 1}
    assert aggregate["source_manifest"]["eligible_pdf_only_entries"] == 1
    raw_path = output_dir / aggregate["entries"][0]["output_path"]
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    source_entry = payload["source_manifest_entry"]
    document = payload["documents"][0]
    assert payload["source_kind"] == "gii_pdf_fallback"
    assert source_entry["source_kind"] == "gii_pdf_fallback"
    assert source_entry["source_role"] == "authoritative_fallback"
    assert source_entry["relative_file_path"] == relative_path
    assert document["source_kind"] == "gii_pdf_fallback"
    assert document["source_pdf"] == relative_path
    assert document["sha256"] == pdf_sha256
    assert document["metadata"]["source_pdf_sha256"] == pdf_sha256
    assert (
        document["metadata"]["gii_xml_reconciliation_status"]
        == "pdf_manifest_only"
    )
    assert document["page_refs"]
    first_page_ref = document["page_refs"][0]
    first_page_path = (raw_path.parent / first_page_ref["path"]).resolve()
    assert first_page_path.is_file()
    first_page = json.loads(first_page_path.read_text(encoding="utf-8"))
    assert first_page_ref["text_sha256"] == first_page["text_sha256"]
    assert first_page_ref["content_sha256"] == first_page["content_sha256"]
    assert (
        first_page["content_sha256"]
        == batch._page_content_sha256(first_page)
    )
    assert (output_dir / "manifest.json").is_file()
    assert not list(output_dir.rglob("*.tmp"))


def test_unsectioned_pdf_text_becomes_an_explicit_document_body(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    relative_path = "F/0003_fallbackg.pdf"
    pdf_sha256 = write_test_pdf(
        pdf_dir / relative_path,
        "Amtliche Bekanntmachung ohne Paragraphengliederung.",
    )
    manifest_path = tmp_path / "xml-manifest.json"
    write_manifest(
        manifest_path,
        [pdf_only_entry(relative_path, pdf_sha256)],
    )
    output_dir = tmp_path / "output"

    aggregate = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
        )
    )

    assert aggregate["status_counts"] == {"ok": 1}
    assert aggregate["entries"][0]["fallback_content_mode"] == "whole_document_text"
    raw_path = output_dir / aggregate["entries"][0]["output_path"]
    document = json.loads(raw_path.read_text(encoding="utf-8"))["documents"][0]
    assert document["metadata"]["fallback_content_mode"] == "whole_document_text"
    assert document["structural_units"][0]["unit_type"] == "document_body"
    assert document["structural_units"][0]["is_uncertain"] is True
    assert document["chunks"][0]["chunk_type"] == "document_text"
    assert "Amtliche Bekanntmachung" in document["chunks"][0]["text"]


def test_resume_verifies_pdf_and_provenance_then_reuses_output(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    relative_path = "F/0003_fallbackg.pdf"
    pdf_path = pdf_dir / relative_path
    pdf_sha256 = write_test_pdf(pdf_path)
    manifest_path = tmp_path / "xml-manifest.json"
    write_manifest(
        manifest_path,
        [pdf_only_entry(relative_path, pdf_sha256)],
    )
    output_dir = tmp_path / "output"
    initial = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
        )
    )
    raw_path = output_dir / initial["entries"][0]["output_path"]
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    payload["test_sentinel"] = True
    batch.atomic_write_json(raw_path, payload)

    resumed = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
            resume=True,
        )
    )

    assert resumed["status_counts"] == {"skipped_existing": 1}
    assert resumed["entries"][0]["resume_validation"] == "verified"
    assert json.loads(raw_path.read_text(encoding="utf-8"))["test_sentinel"] is True

    changed_pdf = tmp_path / "changed.pdf"
    write_test_pdf(changed_pdf, "§ 2 Geändert\nEin anderer Inhalt.")
    shutil.copyfile(changed_pdf, pdf_path)
    changed = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
            resume=True,
        )
    )
    assert changed["status_counts"] == {"error": 1}
    assert "hash differs" in changed["entries"][0]["error"]
    assert json.loads(raw_path.read_text(encoding="utf-8"))["test_sentinel"] is True


@pytest.mark.parametrize(
    "tamper",
    ["deletion", "corruption", "content_mutation", "traversal"],
)
def test_resume_regenerates_invalid_external_page_artifacts(tmp_path, tamper):
    pdf_dir = tmp_path / "pdfs"
    relative_path = "F/0003_fallbackg.pdf"
    pdf_sha256 = write_test_pdf(pdf_dir / relative_path)
    manifest_path = tmp_path / "xml-manifest.json"
    write_manifest(
        manifest_path,
        [pdf_only_entry(relative_path, pdf_sha256)],
    )
    output_dir = tmp_path / "output"
    initial = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
        )
    )
    raw_path = output_dir / initial["entries"][0]["output_path"]
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    payload["test_sentinel"] = True
    page_ref = payload["documents"][0]["page_refs"][0]
    page_path = (raw_path.parent / page_ref["path"]).resolve()

    if tamper == "deletion":
        page_path.unlink()
    elif tamper == "corruption":
        page_path.write_text('{"truncated":', encoding="utf-8")
    elif tamper == "content_mutation":
        page = json.loads(page_path.read_text(encoding="utf-8"))
        page["text"] += "\nManipulierter Inhalt."
        batch.atomic_write_json(page_path, page)
    else:
        outside_page = tmp_path / "outside-page.json"
        shutil.copyfile(page_path, outside_page)
        page_ref["path"] = os.path.relpath(outside_page, raw_path.parent)
    batch.atomic_write_json(raw_path, payload)

    resumed = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
            resume=True,
        )
    )

    assert resumed["status_counts"] == {"ok": 1}
    assert "page artifacts failed validation" in (
        resumed["entries"][0]["resume_validation"]
    )
    regenerated = json.loads(raw_path.read_text(encoding="utf-8"))
    assert "test_sentinel" not in regenerated
    regenerated_ref = regenerated["documents"][0]["page_refs"][0]
    regenerated_page_path = (
        raw_path.parent / regenerated_ref["path"]
    ).resolve()
    assert regenerated_page_path.is_file()
    assert regenerated_page_path.is_relative_to(output_dir.resolve())


def write_rule_file(path, description):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source_text_anomaly_patterns": [
                    {
                        "pattern": "Testsatz",
                        "issue_type": "test_rule",
                        "description": description,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("rules_change", ["added", "removed", "changed", "mode"])
def test_resume_regenerates_after_effective_rules_change(tmp_path, rules_change):
    pdf_dir = tmp_path / "pdfs"
    relative_path = "F/0003_fallbackg.pdf"
    pdf_sha256 = write_test_pdf(pdf_dir / relative_path)
    manifest_path = tmp_path / "xml-manifest.json"
    write_manifest(
        manifest_path,
        [pdf_only_entry(relative_path, pdf_sha256)],
    )
    output_dir = tmp_path / "output"
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rule_path = rules_dir / "rules.json"
    if rules_change in {"removed", "changed", "mode"}:
        write_rule_file(rule_path, "original")
    initial = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            rules_dir=rules_dir,
            workers=1,
        )
    )
    raw_path = output_dir / initial["entries"][0]["output_path"]
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    original_rules_sha256 = payload["documents"][0]["metadata"][
        "rules_configuration"
    ]["sha256"]
    payload["test_sentinel"] = True
    batch.atomic_write_json(raw_path, payload)

    resumed_rules_dir = rules_dir
    if rules_change == "added":
        write_rule_file(rule_path, "added")
    elif rules_change == "removed":
        rule_path.unlink()
    elif rules_change == "changed":
        write_rule_file(rule_path, "changed")
    else:
        resumed_rules_dir = None

    resumed = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            rules_dir=resumed_rules_dir,
            workers=1,
            resume=True,
        )
    )

    assert resumed["status_counts"] == {"ok": 1}
    assert resumed["entries"][0]["resume_validation"] == (
        "existing document used a different rules configuration"
    )
    regenerated = json.loads(raw_path.read_text(encoding="utf-8"))
    assert "test_sentinel" not in regenerated
    assert regenerated["documents"][0]["metadata"]["rules_configuration"][
        "sha256"
    ] != original_rules_sha256


def test_force_rerun_preserves_then_cleans_exact_scope_page_generations(
    tmp_path,
    monkeypatch,
):
    pdf_dir = tmp_path / "pdfs"
    relative_path = "F/0003_fallbackg.pdf"
    pdf_sha256 = write_test_pdf(pdf_dir / relative_path)
    manifest_path = tmp_path / "xml-manifest.json"
    write_manifest(
        manifest_path,
        [pdf_only_entry(relative_path, pdf_sha256)],
    )
    output_dir = tmp_path / "output"
    initial = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
        )
    )
    raw_path = output_dir / initial["entries"][0]["output_path"]
    initial_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    initial_page_path = (
        raw_path.parent
        / initial_payload["documents"][0]["page_refs"][0]["path"]
    ).resolve()
    old_generation = initial_page_path.parents[1]
    item_pages_root = old_generation.parent
    published_orphan = item_pages_root / "generation-deadbeef"
    staging_orphan = item_pages_root / ".generation-cafebabe"
    published_orphan.mkdir()
    staging_orphan.mkdir()

    raw_publication_observations = []
    original_atomic_write_json = batch.atomic_write_json

    def observing_atomic_write_json(path, payload):
        if Path(path) == raw_path:
            raw_publication_observations.append(old_generation.is_dir())
        return original_atomic_write_json(path, payload)

    monkeypatch.setattr(batch, "atomic_write_json", observing_atomic_write_json)
    forced = batch.run(
        batch.BatchConfig(
            manifest_path=manifest_path,
            pdf_dir=pdf_dir,
            output_dir=output_dir,
            workers=1,
            force=True,
        )
    )

    assert forced["status_counts"] == {"ok": 1}
    assert raw_publication_observations == [True]
    assert not old_generation.exists()
    assert not published_orphan.exists()
    assert not staging_orphan.exists()
    forced_payload = json.loads(raw_path.read_text(encoding="utf-8"))
    current_page_path = (
        raw_path.parent
        / forced_payload["documents"][0]["page_refs"][0]["path"]
    ).resolve()
    assert current_page_path.is_file()
    assert current_page_path.parents[1] != old_generation
    remaining_generations = sorted(
        child.name
        for child in item_pages_root.iterdir()
        if batch._is_page_generation_name(child.name)
    )
    assert remaining_generations == [current_page_path.parents[1].name]
