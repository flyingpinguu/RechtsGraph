import json

import pytest

from normtext_extractor.gii_pdf_alignment import (
    align_document_to_page_texts,
    align_document_with_pdf,
)


def sample_document():
    article_id = "unit_testg_art_232"
    para_1_id = "unit_testg_art_232_para_1"
    para_2_id = "unit_testg_para_2"
    return {
        "document_id": "doc_test",
        "document_key": "testg",
        "document_global_key": "testgesetz",
        "source_xml": "BJNRTEST.xml",
        "source_pdf": None,
        "pages": [],
        "page_refs": [],
        "metadata": {
            "source_format": "gii_xml",
            "pdf_alignment_status": "not_attempted",
        },
        "structural_units": [
            {
                "unit_id": article_id,
                "unit_type": "article",
                "label": "Art 232",
                "title": "Übergangsvorschriften",
                "text": "",
                "sequence": 1,
                "source_order": 1,
                "parent_unit_id": None,
                "child_unit_ids": [para_1_id],
                "page_range": None,
            },
            {
                "unit_id": para_1_id,
                "unit_type": "paragraph",
                "label": "§ 1",
                "title": "Erster Test",
                "text": (
                    "§ 1\nErster Test\n"
                    "(1) Der seltene Albatrosbegriff steht nur im Normtext. "
                    "Der Absatz endet mit dem unverwechselbaren Schlusswort."
                ),
                "sequence": 2,
                "source_order": 2,
                "parent_unit_id": article_id,
                "child_unit_ids": [],
                "page_range": None,
            },
            {
                "unit_id": para_2_id,
                "unit_type": "paragraph",
                "label": "§ 2",
                "title": "Zweiter Test",
                "text": (
                    "§ 2\nZweiter Test\n"
                    "Der zweite Paragraph enthält einen Kometenbegriff."
                ),
                "sequence": 3,
                "source_order": 3,
                "parent_unit_id": None,
                "child_unit_ids": [],
                "page_range": None,
            },
        ],
        "chunks": [
            {
                "chunk_id": "chunk_para_1_abs_1",
                "unit_id": para_1_id,
                "chunk_type": "subsection",
                "label": "Abs. 1",
                "text": (
                    "(1) Der seltene Albatrosbegriff steht nur im Normtext. "
                    "Der Absatz endet mit dem unverwechselbaren Schlusswort."
                ),
                "sequence": 1,
                "source_order": [2, 1],
                "page_id": None,
                "page_range": None,
            },
            {
                "chunk_id": "chunk_para_1_asset",
                "unit_id": para_1_id,
                "chunk_type": "source_asset",
                "label": "img",
                "text": "[Bild: formel.png]",
                "sequence": 2,
                "source_order": [2, 2],
                "page_id": None,
                "page_range": None,
            },
            {
                "chunk_id": "chunk_para_2_text",
                "unit_id": para_2_id,
                "chunk_type": "provision_text",
                "label": "§ 2",
                "text": "Der zweite Paragraph enthält einen Kometenbegriff.",
                "sequence": 1,
                "source_order": [3, 1],
                "page_id": None,
                "page_range": None,
            },
        ],
    }


def test_alignment_uses_body_evidence_instead_of_toc_and_inherits_ranges(tmp_path):
    pages = [
        "Inhaltsübersicht\n§ 1 Erster Test\n§ 2 Zweiter Test",
        (
            "§ 1 Erster Test\n(1) Der seltene Albatrosbegriff steht nur im "
            "Normtext. Der Absatz endet mit dem unverwechselbaren Schlusswort."
        ),
        "§ 2 Zweiter Test\nDer zweite Paragraph enthält einen Kometenbegriff.",
    ]
    aligned, issues = align_document_to_page_texts(
        sample_document(),
        pages,
        source_pdf="/corpus/testg.pdf",
        pdf_sha256="a" * 64,
        backends=["test"] * 3,
        output_base_dir=tmp_path,
    )
    units = {unit["unit_id"]: unit for unit in aligned["structural_units"]}
    assert units["unit_testg_art_232_para_1"]["page_range"] == {
        "start": 2,
        "end": 2,
    }
    assert units["unit_testg_para_2"]["page_range"] == {"start": 3, "end": 3}
    assert units["unit_testg_art_232"]["page_range"] == {"start": 2, "end": 2}
    assert units["unit_testg_art_232"]["pdf_alignment"]["method"] == "child_inherited"

    chunks = {chunk["chunk_id"]: chunk for chunk in aligned["chunks"]}
    assert chunks["chunk_para_1_abs_1"]["page_range"]["start"] == 2
    assert chunks["chunk_para_1_asset"]["page_range"] == {"start": 2, "end": 2}
    assert (
        chunks["chunk_para_1_asset"]["pdf_alignment"]["method"]
        == "parent_inherited"
    )
    assert chunks["chunk_para_1_abs_1"]["page_id"] == aligned["page_refs"][1]["page_id"]
    assert aligned["source_pdf"] == "testg.pdf"
    assert aligned["metadata"]["pdf_alignment_status"] == "aligned"
    assert aligned["metadata"]["pdf_alignment"]["unit_coverage"] == 1.0

    for page_ref in aligned["page_refs"]:
        page_file = tmp_path / page_ref["path"]
        assert page_file.is_file()
        assert json.loads(page_file.read_text(encoding="utf-8"))["page_id"] == page_ref["page_id"]
    assert not [issue for issue in issues if issue["severity"] == "error"]


def test_no_pdf_pages_is_nonfatal_and_does_not_fabricate_page_numbers():
    aligned, issues = align_document_to_page_texts(sample_document(), [])
    assert aligned["page_refs"] == []
    assert aligned["metadata"]["pdf_alignment_status"] == "unavailable"
    assert issues[0]["issue_type"] == "pdf_alignment_no_pages"
    assert all(
        unit["page_range"] is None for unit in aligned["structural_units"]
    )


def test_pdf_extraction_alignment_is_stable_across_pdf_paths(tmp_path):
    fitz = pytest.importorskip("fitz")
    first_path = tmp_path / "first.pdf"
    second_path = tmp_path / "renamed.pdf"
    pdf = fitz.open()
    for text in (
        "Inhaltsübersicht\n§ 1 Erster Test\n§ 2 Zweiter Test",
        (
            "§ 1 Erster Test\n(1) Der seltene Albatrosbegriff steht nur im "
            "Normtext. Der Absatz endet mit dem unverwechselbaren Schlusswort."
        ),
        "§ 2 Zweiter Test\nDer zweite Paragraph enthält einen Kometenbegriff.",
    ):
        page = pdf.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 550, 800), text, fontsize=11)
    pdf.save(first_path)
    pdf.close()
    second_path.write_bytes(first_path.read_bytes())

    first, _ = align_document_with_pdf(sample_document(), first_path)
    second, _ = align_document_with_pdf(sample_document(), second_path)
    assert [page["page_id"] for page in first["page_refs"]] == [
        page["page_id"] for page in second["page_refs"]
    ]
    assert first["structural_units"][1]["page_range"]["start"] == 2
    assert second["structural_units"][1]["page_range"]["start"] == 2
