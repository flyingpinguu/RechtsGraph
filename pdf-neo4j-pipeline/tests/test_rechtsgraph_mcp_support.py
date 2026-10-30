import json
import sqlite3
from pathlib import Path

import pytest

from rechtsgraph_mcp.external_registry import ExternalRegistry
from rechtsgraph_mcp.neo4j_store import Neo4jSettings, Neo4jStore
from rechtsgraph_mcp.presentation import (
    compact_context,
    compact_document,
    compact_external_search,
    compact_outline,
    compact_search_result,
    compact_structural_unit,
)


def _external_register(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE documents (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, short_title TEXT,
              document_type TEXT, category TEXT, jurisdiction TEXT,
              authority TEXT, edition TEXT, language TEXT, publication_date TEXT,
              valid_from TEXT, valid_to TEXT, priority TEXT, access_status TEXT,
              corpus_status TEXT, source_kind TEXT, landing_url TEXT,
              license_note TEXT, notes TEXT, parent_document_id TEXT,
              core_location TEXT, graph_mention_count INTEGER NOT NULL DEFAULT 0,
              graph_source_document_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE aliases (
              document_id TEXT NOT NULL, alias TEXT NOT NULL,
              normalized_alias TEXT NOT NULL,
              PRIMARY KEY (document_id, normalized_alias)
            );
            CREATE TABLE files (
              id INTEGER PRIMARY KEY, document_id TEXT NOT NULL, role TEXT NOT NULL,
              local_path TEXT, storage_root TEXT, download_url TEXT, final_url TEXT,
              media_type TEXT, size_bytes INTEGER, sha256 TEXT, page_count INTEGER,
              download_status TEXT, validation_status TEXT, retrieved_at TEXT,
              error TEXT
            );
            """
        )
        connection.execute(
            """INSERT INTO documents
               (id, title, short_title, document_type, category, corpus_status,
                priority, graph_mention_count, graph_source_document_count)
               VALUES ('adr_2025', 'Agreement concerning dangerous goods', 'ADR 2025',
                       'international_regulation', 'dangerous_goods', 'downloaded',
                       'A', 40, 5)"""
        )
        connection.execute(
            "INSERT INTO aliases VALUES ('adr_2025', 'ADR', 'adr')"
        )
        connection.execute(
            """INSERT INTO files
               (document_id, role, local_path, media_type, validation_status)
               VALUES ('adr_2025', 'volume', 'documents/adr.pdf',
                       'application/pdf', 'valid')"""
        )


def test_external_registry_distinguishes_registered_full_text(tmp_path: Path) -> None:
    path = tmp_path / "register.sqlite"
    _external_register(path)

    result = ExternalRegistry(path).search("ADR", category="dangerous_goods")

    assert result["count"] == 1
    assert result["results"][0]["id"] == "adr_2025"
    assert result["results"][0]["has_valid_local_text"] is True
    assert result["results"][0]["files"][0]["local_path"] == "documents/adr.pdf"

    compact = compact_external_search(result)
    full = compact_external_search(result, detail="full")
    assert compact["results"][0]["document_ref"] == "adr_2025"
    assert compact["results"][0]["has_valid_local_text"] is True
    assert "files" not in compact["results"][0]
    assert full["results"][0]["files"][0]["local_path"] == "documents/adr.pdf"


class RecordingNeo4jStore(Neo4jStore):
    def __init__(self) -> None:
        super().__init__(Neo4jSettings())
        self.cypher = ""
        self.parameters = {}

    def _query(self, cypher, parameters=None):
        self.cypher = cypher
        self.parameters = parameters or {}
        return [{"neighbor_node_id": "neighbor", "resolution_status": "resolved"}]


@pytest.mark.parametrize(
    ("direction", "fragment"),
    [("outgoing", "-[r:REFERS_TO]->"), ("incoming", "<-[r:REFERS_TO]-")],
)
def test_reference_exploration_is_fixed_and_parameterized(direction: str, fragment: str) -> None:
    store = RecordingNeo4jStore()

    result = store.explore_references("chunk_123", direction=direction, limit=500)

    assert fragment in store.cypher
    assert "$node_id" in store.cypher
    assert "chunk_123" not in store.cypher
    assert store.parameters == {"node_id": "chunk_123", "limit": 100}
    assert result["count"] == 1
    assert result["continuation_node_id_field"] == "neighbor_node_id"
    assert "other.graph_id AS neighbor_node_id" in store.cypher
    expected_source = "origin.graph_id AS edge_source_id" if direction == "outgoing" else "other.graph_id AS edge_source_id"
    expected_target = "other.graph_id AS edge_target_id" if direction == "outgoing" else "origin.graph_id AS edge_target_id"
    assert expected_source in store.cypher
    assert expected_target in store.cypher


def test_reference_exploration_rejects_arbitrary_direction() -> None:
    with pytest.raises(ValueError):
        RecordingNeo4jStore().explore_references("chunk_123", direction="both")


def test_compact_projections_retain_nested_evidence() -> None:
    evidence = {
        "source_url": "https://example.invalid/source",
        "legal_status": {"raw": "repealed", "normalized": "not_in_force"},
        "date_coverage": "unknown",
        "temporal_applicability": "undetermined",
    }

    document = compact_document({"document_id": "doc", "evidence": evidence})
    context = compact_context(
        {"passages": [{"passage_id": "passage", "text": "Text", "evidence": evidence}]},
        detail="full",
    )

    assert document["evidence"] == evidence
    assert context["passages"][0]["evidence"] == evidence


def test_compact_context_keeps_operational_fields_without_repeated_metadata() -> None:
    compact = compact_context({
        "passages": [{
            "passage_id": "passage",
            "document_id": "internal_doc",
            "canonical_citation": "DemoG",
            "document_title": "Demonstrationsgesetz",
            "legal_citation": "DemoG § 1",
            "text": "Text",
            "evidence": {
                "source_url": "https://example.invalid/source",
                "cautions": ["verify_current_version"],
            },
        }]
    })

    assert compact["passages"] == [{
        "passage_id": "passage",
        "document_ref": "DemoG",
        "title": "Demonstrationsgesetz",
        "citation": "DemoG § 1",
        "text": "Text",
        "cautions": ["verify_current_version"],
    }]


def test_structural_projection_omits_metadata_but_keeps_actionable_cautions() -> None:
    evidence = {
        "source_url": "https://example.invalid/act",
        "source_format": "gii_xml",
        "pdf_alignment_status": "aligned",
        "legal_status": {"raw": None, "normalized": "unknown"},
        "temporal_applicability": "undetermined",
    }
    payload = {
        "document": {
            "document_id": "doc_1", "document_global_key": "demo_act",
            "canonical_citation": "DemoG", "evidence": evidence,
        },
        "unit": {
            "unit_id": "unit_1", "global_key": "demo_act_para_1",
            "legal_citation": "DemoG § 1",
        },
        "passages": [
            {
                "passage_id": "passage_1", "unit_id": "unit_1",
                "legal_citation": "DemoG § 1 Abs. 1", "text": "First paragraph.",
                "evidence": {**evidence, "pdf_alignment_evidence": "direct"},
            },
            {
                "passage_id": "passage_2", "unit_id": "child_unit",
                "legal_citation": "DemoG § 1 Abs. 2", "text": "Second paragraph.",
                "evidence": {
                    **evidence, "pdf_alignment_evidence": "inherited",
                    "cautions": ["pdf_page_range_not_directly_aligned"],
                },
            },
        ],
        "truncated": False,
    }

    compact = compact_structural_unit(payload)
    full = compact_structural_unit(payload, detail="full")

    assert compact["document"] == {"document_ref": "DemoG"}
    assert compact["unit"] == {"unit_ref": "DemoG § 1"}
    assert compact["passages"][0]["text"] == "First paragraph."
    assert "evidence" not in compact["passages"][0]
    assert compact["passages"][1]["cautions"] == [
        "pdf_page_range_not_directly_aligned"
    ]
    assert "unit_id" not in compact["passages"][1]
    assert full["document"]["evidence"] == evidence
    assert full["unit"]["global_key"] == "demo_act_para_1"
    assert full["passages"][1]["evidence"]["source_url"] == evidence["source_url"]
    assert len(json.dumps(compact)) < len(json.dumps(full))


def test_large_default_projections_are_materially_smaller_than_full_detail() -> None:
    evidence = {
        "source_url": "https://example.invalid/official/very/long/source/url",
        "source_format": "gii_xml",
        "pdf_alignment_status": "aligned",
        "legal_status": {"raw": None, "normalized": "unknown"},
        "date_coverage": "unknown",
        "temporal_applicability": "undetermined",
    }
    document = {
        "document_id": "internal_document_pk",
        "document_key": "demo_g",
        "document_global_key": "demonstrationsgesetz",
        "title": "Demonstrationsgesetz",
        "canonical_citation": "DemoG",
        "abbreviation": "DemoG",
        "source_kind": "gii_xml",
        "source_format": "xml",
        "landing_url": evidence["source_url"],
        "evidence": evidence,
    }
    unit = {
        "unit_id": "internal_unit_pk",
        "global_key": "demonstrationsgesetz_paragraph_1",
        "document_id": document["document_id"],
        "unit_type": "paragraph",
        "label": "§ 1",
        "title": "Zweck",
        "legal_citation": "DemoG § 1",
        "display_name": "DemoG § 1 Zweck",
        "page_start": 1,
        "page_end": 2,
        "depth": 0,
    }
    passage = {
        "passage_id": "internal_passage_pk",
        "document_id": document["document_id"],
        "unit_id": unit["unit_id"],
        "passage_kind": "text",
        "legal_citation": "DemoG § 1 Abs. 1",
        "document_title": document["title"],
        "canonical_citation": document["canonical_citation"],
        "source_kind": document["source_kind"],
        "page_start": 1,
        "page_end": 1,
        "evidence": evidence,
        "text": "Die Behörde schützt das Wasser.",
    }

    outline_payload = {"document": document, "count": 1, "units": [unit]}
    structural_payload = {
        "document": document, "unit": unit, "passages": [passage]
    }
    compact_values = [
        compact_outline(outline_payload),
        compact_search_result(passage, "Wasser", 500),
        compact_structural_unit(structural_payload),
    ]
    full_values = [
        compact_outline(outline_payload, detail="full"),
        compact_search_result(passage, "Wasser", 500, detail="full"),
        compact_structural_unit(structural_payload, detail="full"),
    ]

    compact_chars = sum(len(json.dumps(value)) for value in compact_values)
    full_chars = sum(len(json.dumps(value)) for value in full_values)
    assert compact_chars < full_chars * 0.55
    assert all("internal_document_pk" not in json.dumps(value) for value in compact_values)
    assert all("source_url" not in json.dumps(value) for value in compact_values)
