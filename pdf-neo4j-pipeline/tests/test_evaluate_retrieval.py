import importlib.util
import json
from pathlib import Path

import pytest

from rechtsgraph_mcp.index import RetrievalIndex, build_index


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_retrieval.py"
SPEC = importlib.util.spec_from_file_location("evaluate_retrieval", SCRIPT)
evaluator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(evaluator)


def _build_fixture_index(tmp_path: Path) -> Path:
    raw = {
        "documents": [{
            "document_id": "fixture_doc", "document_key": "fixture", "document_global_key": "fixture",
            "title": "Fixture Act", "canonical_citation": "FixtureG", "abbreviation": "FixtureG",
            "metadata": {},
            "structural_units": [{
                "unit_id": "fixture_unit", "document_id": "fixture_doc", "unit_type": "paragraph",
                "global_key": "fixture_paragraph_1", "label": "§ 1", "number": "1", "title": "Purpose",
                "legal_citation": "FixtureG § 1", "display_name": "FixtureG § 1", "breadcrumbs": [],
                "parent_unit_id": None, "sequence": 1, "source_order": 1, "page_range": {},
            }],
            "chunks": [
                {"chunk_id": "water", "unit_id": "fixture_unit", "label": "text", "legal_citation": "FixtureG § 1",
                 "text": "Water protection is mandatory.", "source_order": 1, "page_range": {}},
                {"chunk_id": "noise", "unit_id": "fixture_unit", "label": "text", "legal_citation": "FixtureG § 2",
                 "text": "Noise monitoring is mandatory.", "source_order": 2, "page_range": {}},
            ],
        }]
    }
    raw_path = tmp_path / "raw.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"entries": [{"output_path": "raw.json"}]}), encoding="utf-8")
    index_path = tmp_path / "fixture.sqlite"
    build_index(index_path, [manifest])
    return index_path


def test_evaluates_actual_built_index_and_excludes_unreviewed_cases(tmp_path):
    index_path = _build_fixture_index(tmp_path)
    payload = {"cases": [
        {"id": "water", "query": "water protection", "reference_status": "reviewed",
         "relevant_passage_ids": ["structured:water"]},
        {"id": "missing", "query": "unfindable mineral", "reference_status": "reviewed",
         "relevant_passage_ids": ["structured:noise"]},
        {"id": "holdout", "query": "noise monitoring", "reference_status": "unreviewed",
         "relevant_passage_ids": ["structured:noise"]},
    ]}
    with RetrievalIndex(index_path) as index:
        cases = evaluator.validate_cases(payload, index)
        result = evaluator.evaluate(index, cases, k=3)

    assert result["counts"] == {
        "cases_loaded": 3, "reviewed_cases_searched": 2, "unreviewed_cases_skipped": 1,
        "reviewed_cases_with_gold": 2, "reviewed_cases_without_gold": 0, "reference_units": 2,
    }
    assert result["metrics"]["recall_at_k"] == 0.5
    assert result["metrics"]["mean_reciprocal_rank"] == 0.5
    assert result["metrics"]["all_required_evidence_found"] == 0.5
    assert "holdout" not in evaluator.render_report(result, index_path).split("## Per reviewed case", 1)[1]


def test_no_gold_cases_have_no_evidence_metric_denominator(tmp_path):
    index_path = _build_fixture_index(tmp_path)
    payload = [{"id": "no-gold", "query": "water", "reference_status": "reviewed"}]
    with RetrievalIndex(index_path) as index:
        result = evaluator.evaluate(index, evaluator.validate_cases(payload, index), k=1)

    assert result["counts"]["reviewed_cases_without_gold"] == 1
    assert result["metrics"]["recall_at_k"] is None
    assert result["metrics"]["mean_reciprocal_rank"] is None
    assert result["metrics"]["all_required_evidence_found"] is None


def test_citation_gold_is_resolved_and_scored_on_the_built_index(tmp_path):
    index_path = _build_fixture_index(tmp_path)
    payload = [{"id": "citation", "query": "water protection", "reference_status": "reviewed",
                "relevant_citations": ["FixtureG § 1"]}]
    with RetrievalIndex(index_path) as index:
        result = evaluator.evaluate(index, evaluator.validate_cases(payload, index), k=1)

    assert result["metrics"]["recall_at_k"] == 1.0
    assert result["records"][0]["matched"] == ["FixtureG § 1"]


def test_citation_validation_accepts_valid_reference_when_fts_has_no_match(tmp_path, monkeypatch):
    index_path = _build_fixture_index(tmp_path)
    payload = [{"id": "citation", "query": "water protection", "reference_status": "reviewed",
                "relevant_citations": ["FixtureG § 1"]}]
    with RetrievalIndex(index_path) as index:
        monkeypatch.setattr(index, "search_corpus", lambda *args, **kwargs: [])
        assert evaluator.validate_cases(payload, index)[0]["gold"] == ["FixtureG § 1"]


def test_rejects_normalized_duplicate_legal_citations(tmp_path):
    index_path = _build_fixture_index(tmp_path)
    with RetrievalIndex(index_path) as index:
        with pytest.raises(evaluator.CaseValidationError, match="normalized duplicates"):
            evaluator.validate_cases([{"id": "duplicate", "query": "water", "reference_status": "reviewed",
                                       "relevant_citations": ["FixtureG § 1", "fixtureg  § 1"]}], index)


def test_rejects_unknown_passage_identifier_and_mixed_reference_modes(tmp_path):
    index_path = _build_fixture_index(tmp_path)
    with RetrievalIndex(index_path) as index:
        with pytest.raises(evaluator.CaseValidationError, match="unknown relevant passage ID"):
            evaluator.validate_cases([{"id": "bad-id", "query": "water", "reference_status": "reviewed",
                                       "relevant_passage_ids": ["structured:absent"]}], index)
        with pytest.raises(evaluator.CaseValidationError, match="passage IDs or citations"):
            evaluator.validate_cases([{"id": "mixed", "query": "water", "reference_status": "reviewed",
                                       "relevant_passage_ids": ["structured:water"],
                                       "relevant_citations": ["FixtureG § 1"]}], index)
