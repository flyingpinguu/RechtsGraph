import json

import pytest

import normtext_extractor.gii_pdf_alignment as alignment_module
from normtext_extractor.gii_pdf_alignment import (
    ALIGNMENT_VERSION,
    align_document_to_page_texts,
    align_document_with_pdf,
    extract_pdf_page_texts,
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


def chunk_document(
    chunk_texts,
    *,
    document_id="doc_chunks",
    document_key="chunks",
):
    units = []
    chunks = []
    for index, text in enumerate(chunk_texts, start=1):
        unit_id = "unit_{}_{}".format(document_key, index)
        units.append(
            {
                "unit_id": unit_id,
                "unit_type": "paragraph",
                "label": "§ {}".format(index),
                "title": "",
                "text": "",
                "sequence": index,
                "source_order": index,
                "parent_unit_id": None,
                "child_unit_ids": [],
                "page_range": None,
            }
        )
        chunks.append(
            {
                "chunk_id": "chunk_{}_{}".format(document_key, index),
                "unit_id": unit_id,
                "chunk_type": "provision_text",
                "label": "§ {}".format(index),
                "text": text,
                "sequence": index,
                "source_order": [index, 1],
                "page_id": None,
                "page_range": None,
            }
        )
    return {
        "document_id": document_id,
        "document_key": document_key,
        "document_global_key": document_key,
        "source_xml": "{}.xml".format(document_key),
        "source_pdf": None,
        "pages": [],
        "page_refs": [],
        "metadata": {
            "source_format": "gii_xml",
            "pdf_alignment_status": "not_attempted",
        },
        "structural_units": units,
        "chunks": chunks,
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
    assert aligned["metadata"]["pdf_alignment_version"] == ALIGNMENT_VERSION
    assert (
        aligned["metadata"]["pdf_alignment"]["version"]
        == ALIGNMENT_VERSION
    )
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
    assert aligned["metadata"]["pdf_alignment_version"] == ALIGNMENT_VERSION


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


def test_monotone_dp_avoids_later_best_match_cascade():
    first_text = (
        "Albatros Bernstein Chrysantheme Drossel Eukalyptus "
        "Falkenauge Granit Horizont Ibis Komet"
    )
    second_text = (
        "Laterne Mosaik Nordwind Opal Prisma Quarzit "
        "Rosenholz Silberfaden Topas Ulme"
    )
    document = chunk_document([first_text, second_text])
    pages = [
        (
            "Albatros Bernstein Chrysantheme Drossel Eukalyptus "
            "Falkenauge Granit"
        ),
        second_text,
        first_text,
    ]

    aligned, _issues = align_document_to_page_texts(document, pages)
    chunks = aligned["chunks"]
    assert chunks[0]["page_range"]["start"] == 1
    assert chunks[1]["page_range"]["start"] == 2
    assert all(
        chunk["pdf_alignment"]["evidence"] == "direct"
        for chunk in chunks
    )


def test_direct_chunk_matches_never_run_backwards():
    document = chunk_document(
        [
            "Erster Ankertext mit Albatros Bernstein Chrysantheme Drossel.",
            "Zweiter Ankertext mit Eukalyptus Falkenauge Granit Horizont.",
        ]
    )
    pages = [
        "Zweiter Ankertext mit Eukalyptus Falkenauge Granit Horizont.",
        "Erster Ankertext mit Albatros Bernstein Chrysantheme Drossel.",
    ]

    aligned, _issues = align_document_to_page_texts(document, pages)
    direct_starts = [
        chunk["page_range"]["start"]
        for chunk in aligned["chunks"]
        if chunk.get("pdf_alignment", {}).get("evidence") == "direct"
    ]
    assert direct_starts == sorted(direct_starts)
    assert len(direct_starts) == 1


def test_orphan_chunk_is_not_globally_matched():
    document = chunk_document(
        ["Regulärer Albatros Bernstein Chrysantheme Drossel Text."]
    )
    document["chunks"].append(
        {
            "chunk_id": "chunk_orphan",
            "unit_id": "unit_missing",
            "chunk_type": "provision_text",
            "label": "§ 99",
            "text": "Orphan Eukalyptus Falkenauge Granit Horizont Text.",
            "sequence": 2,
            "source_order": [2, 1],
            "page_id": None,
            "page_range": None,
        }
    )

    aligned, _issues = align_document_to_page_texts(
        document,
        [
            "Regulärer Albatros Bernstein Chrysantheme Drossel Text.",
            "Orphan Eukalyptus Falkenauge Granit Horizont Text.",
        ],
    )
    orphan = aligned["chunks"][-1]
    assert orphan["page_range"] is None
    assert orphan["page_id"] is None
    assert "pdf_alignment" not in orphan


def test_multi_page_chunk_tail_expands_parent_range():
    long_text = (
        "Albatros Bernstein Chrysantheme Drossel Eukalyptus Falkenauge "
        "Granit Horizont Ibis Komet Laterne Mosaik Nordwind Opal Prisma "
        "Quarzit Rosenholz Silberfaden Topas Ulme Veilchen Wacholder "
        "Xylophon Ypsilon Zirkon Abschlussanker Einzigartig"
    )
    next_text = (
        "Folgenorm Adler Biber Condor Distel Eiche Fichte Ginkgo Hasel."
    )
    document = chunk_document([long_text, next_text])
    pages = [
        (
            "Albatros Bernstein Chrysantheme Drossel Eukalyptus Falkenauge "
            "Granit Horizont Ibis Komet Laterne Mosaik"
        ),
        (
            "Nordwind Opal Prisma Quarzit Rosenholz Silberfaden Topas Ulme "
            "Veilchen Wacholder Xylophon Ypsilon Zirkon Abschlussanker "
            "Einzigartig"
        ),
        next_text,
    ]

    aligned, _issues = align_document_to_page_texts(document, pages)
    assert aligned["chunks"][0]["page_range"] == {"start": 1, "end": 2}
    assert aligned["structural_units"][0]["page_range"] == {
        "start": 1,
        "end": 2,
    }
    assert (
        aligned["structural_units"][0]["pdf_alignment"]["method"]
        == "direct_chunks_derived"
    )


def test_inherited_ranges_do_not_inflate_direct_status():
    direct_text = (
        "Albatros Bernstein Chrysantheme Drossel Eukalyptus Falkenauge."
    )
    document = chunk_document([direct_text])
    parent_id = document["structural_units"][0]["unit_id"]
    document["chunks"].append(
        {
            "chunk_id": "chunk_unmatched",
            "unit_id": parent_id,
            "chunk_type": "provision_text",
            "label": "Abs. 2",
            "text": "Völlig anderer Marmor Nessel Orchidee Pappel Quendel Text.",
            "sequence": 2,
            "source_order": [1, 2],
            "page_id": None,
            "page_range": None,
        }
    )

    aligned, issues = align_document_to_page_texts(document, [direct_text])
    metadata = aligned["metadata"]["pdf_alignment"]
    assert metadata["direct_chunk_matches"] == 1
    assert metadata["inherited_chunk_matches"] == 1
    assert metadata["direct_evidence_coverage"] == 0.5
    assert aligned["metadata"]["pdf_alignment_status"] == "partial"
    assert (
        aligned["chunks"][1]["pdf_alignment"]["evidence"]
        == "inherited"
    )
    unmatched_issue = next(
        issue
        for issue in issues
        if issue["issue_type"] == "pdf_alignment_unmatched_items"
    )
    assert unmatched_issue["evidence"]["unmatched_chunk_count"] == 1


def test_realign_clears_stale_ranges_refs_and_ids():
    stale = sample_document()
    stale["pages"] = [{"page_id": "stale"}]
    stale["page_refs"] = [{"page_id": "stale"}]
    for unit in stale["structural_units"]:
        unit["page_range"] = {"start": 9, "end": 9}
        unit["pdf_alignment"] = {"method": "old"}
    for chunk in stale["chunks"]:
        chunk["page_range"] = {"start": 9, "end": 9}
        chunk["page_id"] = "stale"
        chunk["pdf_alignment"] = {"method": "old"}

    aligned, _issues = align_document_to_page_texts(stale, [])
    assert aligned["pages"] == []
    assert aligned["page_refs"] == []
    assert all(unit["page_range"] is None for unit in aligned["structural_units"])
    assert all(
        "pdf_alignment" not in unit for unit in aligned["structural_units"]
    )
    assert all(chunk["page_id"] is None for chunk in aligned["chunks"])
    assert all(chunk["page_range"] is None for chunk in aligned["chunks"])


def test_page_paths_are_collision_safe_for_shared_document_key(tmp_path):
    text = "Albatros Bernstein Chrysantheme Drossel Eukalyptus Falkenauge."
    first, _ = align_document_to_page_texts(
        chunk_document(
            [text],
            document_id="doc_first",
            document_key="shared",
        ),
        [text],
        pdf_sha256="a" * 64,
        output_base_dir=tmp_path,
    )
    second, _ = align_document_to_page_texts(
        chunk_document(
            [text],
            document_id="doc_second",
            document_key="shared",
        ),
        [text],
        pdf_sha256="a" * 64,
        output_base_dir=tmp_path,
    )

    first_path = tmp_path / first["page_refs"][0]["path"]
    second_path = tmp_path / second["page_refs"][0]["path"]
    assert first_path != second_path
    assert first_path.is_file()
    assert second_path.is_file()
    assert json.loads(first_path.read_text(encoding="utf-8"))["page_id"] != (
        json.loads(second_path.read_text(encoding="utf-8"))["page_id"]
    )


def test_no_sha_identity_depends_on_content_not_source_path():
    text = "Albatros Bernstein Chrysantheme Drossel Eukalyptus Falkenauge."
    document = chunk_document([text])
    first, _ = align_document_to_page_texts(
        document,
        [text],
        source_pdf="/one/name.pdf",
    )
    renamed, _ = align_document_to_page_texts(
        document,
        [text],
        source_pdf="/another/renamed.pdf",
    )
    changed, _ = align_document_to_page_texts(
        document,
        [text + " Zusatz."],
        source_pdf="/one/name.pdf",
    )

    assert first["page_refs"][0]["page_id"] == renamed["page_refs"][0]["page_id"]
    assert first["page_refs"][0]["page_id"] != changed["page_refs"][0]["page_id"]


def test_existing_source_pdf_is_refreshed_to_current_alignment_source():
    document = sample_document()
    document["source_pdf"] = "B/0345_bgb.pdf"
    aligned, _ = align_document_to_page_texts(
        document,
        ["Albatros Bernstein Chrysantheme Drossel Eukalyptus Falkenauge."],
        source_pdf="/tmp/downloads/0345_bgb.pdf",
    )
    assert aligned["source_pdf"] == "0345_bgb.pdf"
    assert (
        aligned["metadata"]["source_pdf_relative_path"]
        == "/tmp/downloads/0345_bgb.pdf"
    )


def test_successful_realign_clears_stale_reason_and_secondary_provenance():
    text = "Albatros Bernstein Chrysantheme Drossel Eukalyptus Falkenauge."
    document = chunk_document([text])
    document["source_pdf"] = "old.pdf"
    document["metadata"].update(
        {
            "pdf_alignment_status": "error",
            "pdf_alignment_reason": "alignment_failed",
            "source_pdf_relative_path": "old/path.pdf",
            "source_pdf_manifest_sha256": "old-manifest-hash",
            "source_pdf_sha256": "old-content-hash",
            "source_pdf_bytes": 12,
            "secondary_pdf_backend": "old-backend",
        }
    )

    aligned, _issues = align_document_to_page_texts(
        document,
        [text],
        source_pdf="/new/corpus/current.pdf",
        pdf_sha256="b" * 64,
        backends=["test"],
    )

    assert aligned["source_pdf"] == "current.pdf"
    assert (
        aligned["metadata"]["source_pdf_relative_path"]
        == "/new/corpus/current.pdf"
    )
    assert aligned["metadata"]["source_pdf_sha256"] == "b" * 64
    assert aligned["metadata"]["pdf_alignment_status"] == "aligned"
    assert "pdf_alignment_reason" not in aligned["metadata"]
    assert "source_pdf_manifest_sha256" not in aligned["metadata"]
    assert "source_pdf_bytes" not in aligned["metadata"]
    assert "secondary_pdf_backend" not in aligned["metadata"]


def test_candidate_scoring_is_subcartesian(monkeypatch):
    chunk_texts = [
        (
            "Anker{0} Albatros{0} Bernstein{0} Chrysantheme{0} "
            "Drossel{0} Eukalyptus{0}"
        ).format(index)
        for index in range(20)
    ]
    pages = [
        (
            "Seite{0} Anker{0} Albatros{0} Bernstein{0} "
            "Chrysantheme{0} Drossel{0} Eukalyptus{0}"
        ).format(index)
        for index in range(200)
    ]
    document = chunk_document(chunk_texts)
    calls = 0
    original = alignment_module._score_profile

    def counted(profile, page):
        nonlocal calls
        calls += 1
        return original(profile, page)

    monkeypatch.setattr(alignment_module, "_score_profile", counted)
    aligned, _issues = align_document_to_page_texts(document, pages)
    assert aligned["metadata"]["pdf_alignment"]["direct_chunk_matches"] == 20
    assert calls < 400


def test_pypdf_fallback_survives_pymupdf_failure(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "fallback.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((40, 60), "Albatros Bernstein Chrysantheme Drossel")
    pdf.save(pdf_path)
    pdf.close()

    class BrokenFitz:
        @staticmethod
        def open(*_args, **_kwargs):
            raise RuntimeError("fitz failed")

    monkeypatch.setattr(alignment_module, "fitz", BrokenFitz())
    texts, backends, metadata = extract_pdf_page_texts(pdf_path)
    assert "Albatros" in texts[0]
    assert backends == ["pypdf"]
    assert "pymupdf" in metadata["backend_errors"]


def test_pymupdf_result_survives_pypdf_failure(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "blank.pdf"
    pdf = fitz.open()
    pdf.new_page()
    pdf.save(pdf_path)
    pdf.close()

    def broken_reader(*_args, **_kwargs):
        raise RuntimeError("pypdf failed")

    monkeypatch.setattr(alignment_module, "PdfReader", broken_reader)
    texts, backends, metadata = extract_pdf_page_texts(pdf_path)
    assert texts == [""]
    assert backends == ["pymupdf"]
    assert "pypdf" in metadata["backend_errors"]
