import json
import sqlite3
from pathlib import Path

import fitz

from rechtsgraph_mcp.index import RetrievalIndex, _unit_part, build_index
from rechtsgraph_mcp.presentation import make_snippet


def _raw_document(document_id="doc_demo"):
    return {
        "documents": [
            {
                "document_id": document_id,
                "document_key": "demo_g",
                "document_global_key": "demogesetz",
                "title": "Demonstrationsgesetz",
                "canonical_citation": "DemoG",
                "abbreviation": "DemoG",
                "metadata": {"citation_aliases": ["Demo Gesetz"]},
                "structural_units": [
                    {
                        "unit_id": "unit_1", "document_id": document_id, "unit_type": "paragraph",
                        "global_key": "demogesetz_paragraph_1",
                        "label": "§ 1", "number": "1", "title": "Zweck", "legal_citation": "DemoG § 1",
                        "display_name": "DemoG § 1 Zweck", "breadcrumbs": ["DemoG", "§ 1"],
                        "parent_unit_id": None, "sequence": 1, "source_order": 1,
                        "page_range": {"start": 1, "end": 1},
                    },
                    {
                        "unit_id": "unit_1_1", "document_id": document_id, "unit_type": "section",
                        "global_key": "demogesetz_paragraph_1_absatz_1",
                        "label": "Absatz 1", "number": "1", "title": "Details", "legal_citation": "DemoG § 1 Abs. 1",
                        "display_name": "DemoG § 1 Absatz 1", "breadcrumbs": ["DemoG", "§ 1", "Absatz 1"],
                        "parent_unit_id": "unit_1", "sequence": 2, "source_order": 2,
                        "page_range": {"start": 1, "end": 1},
                    },
                ],
                "chunks": [
                    {"chunk_id": "chunk_one", "unit_id": "unit_1", "label": "text", "legal_citation": "DemoG § 1",
                     "text": "Der Zweck dieses Gesetzes ist der Schutz von Wasser.", "source_order": [1, 1],
                     "page_range": {"start": 1, "end": 1}},
                    {"chunk_id": "chunk_two", "unit_id": "unit_1_1", "label": "text", "legal_citation": "DemoG § 1 Abs. 1",
                     "text": "Die Behörde prüft die Einhaltung sorgfältig.", "source_order": 2,
                     "page_range": {"start": 1, "end": 1}},
                ],
            }
        ]
    }


def _write_manifest(tmp_path: Path, raw_payload: dict) -> Path:
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(raw_payload), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"entries": [{"output_path": "raw.json"}]}), encoding="utf-8")
    return manifest


def test_unit_part_recognizes_common_de_and_eu_subsection_spellings():
    assert _unit_part("§ 6 Satz 2") == "§ 6"
    assert _unit_part("§ 6 Nummer 3") == "§ 6"
    assert _unit_part("§ 6 Buchstabe a") == "§ 6"
    assert _unit_part("Art. 4 Unterabsatz 2") == "Art. 4"
    assert _unit_part("Art. 4 lit. b") == "Art. 4"


def test_builds_atomic_structured_index_and_exposes_agent_api(tmp_path):
    database = tmp_path / "index.sqlite"
    stats = build_index(database, [_write_manifest(tmp_path, _raw_document())])
    assert stats.documents == 1
    assert stats.structural_units == 2
    assert stats.passages == 2

    with RetrievalIndex(database) as index:
        assert index.status()["passages"] == 2
        assert index.find_documents("DemoG")[0]["document_id"] == "doc_demo"
        title_matches = index.find_documents("demonstrationsgesetz", corpus="de")
        assert [item["document_id"] for item in title_matches] == ["doc_demo"]
        outline = index.get_document_outline("DemoG", max_depth=0)
        assert [unit["unit_id"] for unit in outline["units"]] == ["unit_1"]
        unit = index.get_structural_unit("DemoG", "§ 1")
        assert unit["unit"]["unit_id"] == "unit_1"
        assert [passage["passage_id"] for passage in unit["passages"]] == [
            "structured:chunk_one", "structured:chunk_two"
        ]
        subsection = index.get_structural_unit("DemoG", "§ 1 Abs. 1")
        assert subsection["unit"]["unit_id"] == "unit_1_1"
        assert subsection["requested_subsection"] == "Abs. 1"
        assert [item["passage_id"] for item in subsection["matching_passages"]] == [
            "structured:chunk_two"
        ]
        by_global_key = index.get_structural_unit(
            "DemoG", "demogesetz_paragraph_1_absatz_1"
        )
        assert by_global_key["unit"]["unit_id"] == "unit_1_1"
        found = index.search_corpus("Schutz Wasser")
        assert found[0]["passage_id"] == "structured:chunk_one"
        relaxed = index.search_corpus("Schutz nichtvorhanden")
        assert relaxed[0]["passage_id"] == "structured:chunk_one"
        context = index.get_context([found[0]["passage_id"]], before=0, after=1)
        assert len(context["passages"]) == 2

    # A full rebuild replaces the prior file rather than appending duplicate rows.
    repeat = build_index(database, [_write_manifest(tmp_path, _raw_document())])
    assert repeat.passages == 2
    with RetrievalIndex(database) as index:
        assert index.status()["documents"] == 1


def test_curated_ai_act_alias_resolves_without_rebuilding_existing_indexes(tmp_path):
    raw = _raw_document("doc_ai_act")
    document = raw["documents"][0]
    document.update(
        title="Verordnung über künstliche Intelligenz",
        canonical_citation="32024R1689",
        abbreviation="AI Act",
    )
    document["metadata"] = {"base_celex": "32024R1689"}
    database = tmp_path / "ai.sqlite"
    build_index(database, [_write_manifest(tmp_path, raw)])

    with RetrievalIndex(database) as index:
        result = index.find_documents("KI-Verordnung")

    assert result[0]["document_id"] == "doc_ai_act"


def test_query_centered_snippet_preserves_relevant_late_text():
    text = "Einleitung " * 100 + "Güteüberwachung ist hier geregelt. " + "Nachsatz " * 100

    snippet, truncated = make_snippet(text, "Güteüberwachung", 300)

    assert truncated is True
    assert "Güteüberwachung" in snippet
    assert len(snippet) <= 302


def test_subsection_lookup_falls_back_to_exact_parent_without_fuzzy_table_match(tmp_path):
    raw = _raw_document("doc_flat")
    document = raw["documents"][0]
    document["structural_units"] = [document["structural_units"][0]]
    document["chunks"][1]["unit_id"] = "unit_1"
    document["structural_units"].append(
        {
            "unit_id": "unit_table", "document_id": "doc_flat", "unit_type": "table",
            "global_key": "demogesetz_anlage_3_tabelle_6", "label": "Tabelle 6",
            "number": "6", "title": "Absatz 1 Grenzwerte",
            "legal_citation": "DemoG Anlage 3 Tabelle 6 Absatz 1",
            "display_name": "DemoG Anlage 3 Tabelle 6 Absatz 1 Grenzwerte",
            "breadcrumbs": ["DemoG", "Anlage 3", "Tabelle 6"],
            "parent_unit_id": None, "sequence": 3, "source_order": 3,
            "page_range": {"start": 2, "end": 2},
        }
    )
    database = tmp_path / "flat.sqlite"
    build_index(database, [_write_manifest(tmp_path, raw)])

    with RetrievalIndex(database) as index:
        result = index.get_structural_unit("DemoG", "§ 1 Abs. 1")
        assert result["unit"]["unit_id"] == "unit_1"
        assert result["requested_subsection"] == "Abs. 1"
        assert [item["passage_id"] for item in result["matching_passages"]] == [
            "structured:chunk_two"
        ]
        missing = index.get_structural_unit("DemoG", "§ 999 Abs. 1")
        assert missing["unit"] is None
        assert missing["passages"] == []


def test_context_qualified_nested_article_resolves_without_document_prefix(tmp_path):
    raw = _raw_document("doc_nested")
    document = raw["documents"][0]
    article = document["structural_units"][0]
    article.update(
        unit_type="article", label="Art. 1", number="1",
        global_key="demogesetz_anhang_1_anhang_iii_artikel_1",
        legal_citation="DemoG Anhang 1 Anhang III Art. 1",
        display_name="DemoG Anhang 1 Anhang III Art. 1",
    )
    document["structural_units"] = [article]
    document["chunks"][0].update(
        unit_id="unit_1", legal_citation="DemoG Anhang 1 Anhang III Art. 1 Abs. 1"
    )
    document["chunks"][1].update(
        unit_id="unit_1", legal_citation="DemoG Anhang 1 Anhang III Art. 1 Abs. 2"
    )
    document["structural_units"].append(
        {
            **article,
            "unit_id": "unit_other_article",
            "global_key": "demogesetz_anhang_1_anhang_iv_artikel_1",
            "legal_citation": "DemoG Anhang 1 Anhang IV Art. 1",
            "display_name": "DemoG Anhang 1 Anhang IV Art. 1",
            "sequence": 3,
        }
    )
    database = tmp_path / "nested.sqlite"
    build_index(database, [_write_manifest(tmp_path, raw)])

    with RetrievalIndex(database) as index:
        result = index.get_structural_unit(
            "DemoG", "Anhang 1 Anhang III Art. 1 Abs. 2"
        )
        assert result["unit"]["unit_id"] == "unit_1"
        assert [item["passage_id"] for item in result["matching_passages"]] == [
            "structured:chunk_two"
        ]


def test_document_provenance_is_joined_at_read_time_across_retrieval_paths(tmp_path):
    raw = _raw_document("doc_provenance")
    raw["documents"][0]["metadata"].update(
        {
            "base_celex": "32024R9999",
            "source_format": "eurlex_formex_original",
            "source_url": "https://eur-lex.europa.eu/example",
            "consolidation_date": "2024-06-01",
            "legal_status": "repealed",
            "in_force": False,
        }
    )
    database = tmp_path / "provenance.sqlite"
    build_index(database, [_write_manifest(tmp_path, raw)])

    with RetrievalIndex(database) as index:
        document = index.find_documents("DemoG")[0]
        outline = index.get_document_outline("DemoG")
        structural = index.get_structural_unit("DemoG", "§ 1")
        hit = index.search_corpus("Schutz Wasser")[0]
        context = index.get_context([hit["passage_id"]], before=0, after=0)

    expected = {
        "source_url": "https://eur-lex.europa.eu/example",
        "source_format": "eurlex_formex_original",
        "consolidation_date": "2024-06-01",
        "legal_status": {"raw": "repealed", "normalized": "not_in_force"},
        "date_coverage": "unknown",
        "temporal_applicability": "undetermined",
    }
    assert document["evidence"] == expected
    assert outline["document"]["evidence"] == expected
    assert structural["document"]["evidence"] == expected
    assert structural["passages"][0]["evidence"] == expected
    assert hit["evidence"] == expected
    assert context["passages"][0]["evidence"] == expected

    # The compact evidence is derived without changing or replacing the stored
    # audit metadata, so an already-built index remains usable as-is.
    with sqlite3.connect(database) as connection:
        stored = json.loads(connection.execute(
            "SELECT metadata_json FROM documents WHERE document_id = ?",
            ("doc_provenance",),
        ).fetchone()[0])
    assert stored["in_force"] is False
    assert stored["source_url"] == expected["source_url"]


def _write_external_register(path: Path, pdf_path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, short_title TEXT, document_type TEXT,
              category TEXT, jurisdiction TEXT, authority TEXT, edition TEXT, language TEXT, publication_date TEXT,
              valid_from TEXT, valid_to TEXT, priority TEXT, access_status TEXT, corpus_status TEXT, source_kind TEXT,
              landing_url TEXT, license_note TEXT, notes TEXT, parent_document_id TEXT, core_location TEXT,
              graph_mention_count INTEGER, graph_source_document_count INTEGER);
              CREATE TABLE aliases (document_id TEXT, alias TEXT, normalized_alias TEXT);
              CREATE TABLE files (document_id TEXT, role TEXT, local_path TEXT, storage_root TEXT, download_url TEXT,
              final_url TEXT, media_type TEXT, size_bytes INTEGER, sha256 TEXT, page_count INTEGER,
              download_status TEXT, validation_status TEXT);"""
    )
    connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       ("adr_demo", "ADR Demonstration", "ADR", "dangerous_goods", "dangerous_goods", "international",
                        "UNECE", "2025", "de", None, None, None, "A", "public", "downloaded", "curated", None,
                        None, None, None, None, 0, 0))
    connection.execute("INSERT INTO aliases VALUES (?, ?, ?)", ("adr_demo", "ADR", "adr"))
    connection.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       ("adr_demo", "full_text", pdf_path.name, "external_documents", None, None, "application/pdf",
                        pdf_path.stat().st_size, "fixturehash", 1, "ok", "valid"))
    connection.commit()
    connection.close()


def test_selected_external_pdf_is_token_and_page_aware_without_fake_units(tmp_path):
    pdf = tmp_path / "adr.pdf"
    document = fitz.open()
    for page_number in range(2):
        page = document.new_page()
        page.insert_text((72, 72), "ADR Regelung Gefahrgut auf Seite {}. ".format(page_number + 1) * 10)
    document.save(pdf)
    document.close()
    register = tmp_path / "register.sqlite"
    _write_external_register(register, pdf)
    output = tmp_path / "index.sqlite"
    stats = build_index(output, [], external_register_path=register, external_document_ids=["adr_demo"], external_root=tmp_path,
                        pdf_chunk_tokens=60, pdf_chunk_overlap=10)
    assert stats.pdf_fallback_documents == 1
    assert stats.pdf_fallback_passages == 2
    with RetrievalIndex(output, external_register_path=register) as index:
        document_rows = index.find_documents("ADR", corpus="external")
        assert document_rows[0]["document_id"] == "external:adr_demo"
        assert document_rows[0]["document_type"] == "dangerous_goods"
        assert document_rows[0]["edition"] == "2025"
        assert document_rows[0]["language"] == "de"
        assert index.outline("external:adr_demo") == []
        passages = index.search_corpus("Gefahrgut", corpus="external")
        assert passages and passages[0]["passage_kind"] == "pdf_fallback"
        assert passages[0]["page_start"] == 1
        assert passages[0]["evidence"]["source_format"] == "pdf_fallback"
        assert passages[0]["evidence"]["cautions"] == [
            "pdf_text_extraction",
            "structure_and_tables_best_effort",
        ]
        assert index.search_external_register("Demonstration", category="dangerous_goods")[0]["id"] == "adr_demo"
