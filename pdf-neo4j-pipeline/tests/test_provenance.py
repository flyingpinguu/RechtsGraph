import pytest

from rechtsgraph_mcp.provenance import (
    compact_evidence,
    normalize_legal_status,
    parse_metadata_json,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("repealed", "not_in_force"),
        ("REPEALED", "not_in_force"),
        ("none", "unknown"),
        ("unknown", "unknown"),
        ("given", "unknown"),
        ("new.cor", "unknown"),
        ("new.mod", "unknown"),
        (None, "unknown"),
    ],
)
def test_legal_status_normalization_never_promotes_unknown_source_values(raw, expected):
    assert normalize_legal_status(raw) == expected


def test_compact_evidence_keeps_dates_descriptive_and_surfaces_pdf_cautions():
    result = compact_evidence(
        {
            "source_url": "https://eur-lex.europa.eu/example.pdf",
            "source_format": "eurlex_pdf_original",
            "consolidation_date": "2025-01-15",
            "legal_status": "none",
            "in_force": True,
            "pdf_alignment_status": "source_pdf",
            "source_file_sha256": "a" * 64,
        },
        {"passage_kind": "structured_chunk"},
        {"text_extraction_backend": "pymupdf"},
    )

    assert result["legal_status"] == {"raw": "none", "normalized": "unknown"}
    assert result["consolidation_date"] == "2025-01-15"
    assert result["date_coverage"] == "unknown"
    assert result["temporal_applicability"] == "undetermined"
    assert result["text_extraction_backend"] == "pymupdf"
    assert result["cautions"] == [
        "pdf_text_extraction",
        "structure_and_tables_best_effort",
    ]
    assert "source_file_sha256" not in result


def test_non_direct_page_alignment_is_explicit_on_passage_evidence():
    result = compact_evidence(
        {"source_format": "gii_xml", "pdf_alignment_status": "partial"},
        {"passage_kind": "structured_chunk"},
        {"pdf_alignment": {"evidence": "inherited"}},
    )

    assert result["pdf_alignment_status"] == "partial"
    assert result["pdf_alignment_evidence"] == "inherited"
    assert result["cautions"] == [
        "pdf_page_alignment_uncertain",
        "pdf_page_range_not_directly_aligned",
    ]


def test_legacy_or_malformed_metadata_is_safe_and_unknown():
    assert parse_metadata_json("{not-json") == {}
    assert compact_evidence(parse_metadata_json("{}"))["legal_status"] == {
        "raw": None,
        "normalized": "unknown",
    }
