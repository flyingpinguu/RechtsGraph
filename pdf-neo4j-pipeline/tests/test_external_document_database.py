import sqlite3
import zipfile

import pytest

from scripts.build_external_document_database import (
    build_register,
    safe_local_path,
    validate_catalog,
    validate_formex_file,
    write_sqlite,
)


def test_catalog_rejects_unsafe_paths(tmp_path) -> None:
    catalog = {
        "documents": [
            {
                "id": "example",
                "files": [
                    {
                        "local_path": "../outside.pdf",
                        "media_type": "application/pdf",
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="unsafe catalog local_path"):
        validate_catalog(catalog, tmp_path)
    with pytest.raises(ValueError):
        safe_local_path(tmp_path, "/tmp/outside.pdf")


def test_original_formex_download_validation(tmp_path) -> None:
    path = tmp_path / "32017R0492.fmx4.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "L_2017.xml",
            "<ACT><TITLE><TI><P>Test</P></TI></TITLE><ENACTING.TERMS/></ACT>",
        )
        archive.writestr("L_2017.doc.xml", "<DOC/>")

    result = validate_formex_file(path)

    assert result["validation_status"] == "valid"
    assert result["formex_root"] == "ACT"
    assert result["content_member"] == "L_2017.xml"


def test_graph_and_technical_inventory_are_registered(tmp_path) -> None:
    catalog = {
        "documents": [
            {
                "id": "adr_2025",
                "title": "ADR 2025",
                "short_title": "ADR 2025",
                "priority": "A",
                "access_status": "public_official_document",
                "aliases": ["ADR"],
            }
        ]
    }
    inventory = {
        "relationship_targets": [
            {
                "target_document_key": "richtlinie_eu_2019_1",
                "category": "eu_already_in_cellar",
                "base_celex": "32019L0001",
                "mention_count": 5,
                "source_document_count": 2,
                "requested_target_count": 1,
            },
            {
                "target_document_key": "testgesetz",
                "category": "de_already_in_gii_corpus",
                "mention_count": 13,
                "source_document_count": 4,
                "requested_target_count": 3,
                "core_document": {
                    "graph_id": "doc_testgesetz",
                    "document_global_key": "testgesetz",
                    "document_key": "testg",
                    "global_key": "testgesetz",
                    "title": "Testgesetz",
                    "short_title": "TestG",
                    "source_xml": "documents/testg/xml/BJNR000000000.xml",
                },
            },
            {
                "target_document_key": "richtlinie_eu_2024_9999",
                "category": "eu_legal_act_missing_from_cellar_register",
                "base_celex": "32024L9999",
                "mention_count": 11,
                "source_document_count": 3,
                "requested_target_count": 1,
            },
            {
                "target_document_key": "urteil_des_testgerichts",
                "category": "case_law_excluded",
                "mention_count": 1,
                "source_document_count": 1,
                "requested_target_count": 1,
            },
            {
                "target_document_key": "verordnung_eu_2021_1206",
                "category": "eu_legal_act_missing_from_cellar_register",
                "base_celex": "32021R1206",
                "mention_count": 1,
                "source_document_count": 1,
                "requested_target_count": 1,
            },
        ],
        "technical_citations": [
            {
                "family": "adr",
                "normalized_citation": "adr",
                "mention_count": 7,
                "source_document_count": 2,
                "samples": [{"citation": "ADR"}],
            },
            {
                "family": "din",
                "normalized_citation": "din 1234",
                "mention_count": 2,
                "source_document_count": 1,
                "samples": [{"citation": "DIN 1234"}],
            },
        ],
    }
    cellar = {
        "32019L0001": {
            "title": "Consolidated test directive",
            "local_path": "packages/L/example.zip",
        }
    }
    documents, files, graph_references, technical_citations = build_register(
        catalog,
        inventory,
        cellar,
        [],
        {},
        [],
        [],
        {
            "32021R1206": {
                "document_id": "eu_32021l1206",
                "resolved_celex": "32021L1206",
                "role": "original_legal_text",
                "local_path": "documents/eu_referenced_originals/32021L1206.xhtml",
                "storage_root": "external_documents",
                "download_url": "https://publications.europa.eu/resource/celex/32021L1206",
                "media_type": "application/xhtml+xml",
                "download_status": "downloaded",
                "validation_status": "valid",
            }
        },
    )
    by_id = {row["id"]: row for row in documents}
    assert by_id["eu_32019l0001"]["corpus_status"] == "already_in_core_eu_corpus"
    assert by_id["de_doc_testgesetz"]["corpus_status"] == "already_in_core_de_corpus"
    assert by_id["de_doc_testgesetz"]["core_location"].endswith(
        "documents/testg/xml/BJNR000000000.xml"
    )
    assert by_id["eu_32024l9999"]["priority"] == "B"
    assert by_id["adr_2025"]["graph_mention_count"] == 7
    din = next(row for row in documents if row["category"] == "din")
    assert din["access_status"] == "licensed_full_text_metadata_only"
    assert next(row for row in graph_references if row["category"] == "case_law_excluded")[
        "document_id"
    ] is None
    candidate_reference = next(
        row for row in graph_references if row["target_document_key"] == "verordnung_eu_2021_1206"
    )
    assert candidate_reference["document_id"] == "eu_32021r1206"
    assert candidate_reference["candidate_document_id"] == "eu_32021l1206"
    assert candidate_reference["resolution_status"] == "candidate_act_type_or_sector_match"
    assert by_id["eu_32021l1206"]["corpus_status"] == "downloaded_eu_candidate"
    german_reference = next(
        row for row in graph_references if row["target_document_key"] == "testgesetz"
    )
    assert german_reference["document_id"] == "de_doc_testgesetz"
    assert german_reference["resolution_status"] == "exact_german_core_document"

    database = tmp_path / "register.sqlite"
    write_sqlite(
        database,
        documents,
        files,
        graph_references,
        technical_citations,
        "2026-01-01T00:00:00Z",
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == len(documents)
        assert connection.execute(
            "SELECT count(*) FROM graph_references WHERE exclusion_reason IS NOT NULL"
        ).fetchone()[0] == 1
