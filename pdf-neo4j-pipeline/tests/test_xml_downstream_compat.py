import copy
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extract_content_nodes as content_nodes
import extract_reference_relations as reference_relations
import validate_parsed_jsons as validator


def raw_unit(
    document_id,
    unit_id,
    global_key,
    *,
    parent_unit_id=None,
    child_unit_ids=None,
    sequence=1,
    source_order=1,
    unit_type="paragraph",
):
    return {
        "unit_id": unit_id,
        "global_key": global_key,
        "document_id": document_id,
        "document_key": global_key.split("_", 1)[0],
        "document_global_key": global_key.split("_", 1)[0],
        "unit_type": unit_type,
        "legal_citation": global_key,
        "display_name": global_key,
        "parent_unit_id": parent_unit_id,
        "child_unit_ids": child_unit_ids or [],
        "sequence": sequence,
        "source_order": source_order,
        "page_range": None,
        "text": global_key,
        "text_sha256": "unit-sha",
    }


def raw_chunk(
    unit_id,
    chunk_id,
    global_key,
    text,
    *,
    sequence=1,
    source_order=None,
):
    return {
        "chunk_id": chunk_id,
        "global_key": global_key,
        "unit_id": unit_id,
        "document_global_key": global_key.split("_", 1)[0],
        "chunk_type": "provision_text",
        "legal_citation": global_key,
        "display_name": global_key,
        "parent_chunk_id": None,
        "child_chunk_ids": [],
        "sequence": sequence,
        "source_order": source_order if source_order is not None else [0, sequence],
        "page_id": None,
        "page_range": None,
        "text": text,
        "text_sha256": "chunk-sha",
    }


def raw_document(
    document_id,
    document_key,
    units,
    chunks,
    *,
    title=None,
    source_xml=None,
    source_zip=None,
):
    return {
        "document_id": document_id,
        "document_key": document_key,
        "document_global_key": document_key,
        "global_key": document_key,
        "title": title or document_key,
        "canonical_citation": title or document_key,
        "source_xml": source_xml,
        "source_zip": source_zip,
        "pages": [],
        "page_refs": [],
        "structural_units": units,
        "chunks": chunks,
        "metadata": {
            "source_format": "gii_xml",
            "extractor": "gii_xml",
            "gii_document_number": "BJNRTEST",
            "source_xml_sha256": "xml-sha",
            "source_package_kind": "zip",
            "source_package_sha256": "zip-sha",
            "pdf_alignment_status": "not_attempted",
            "pdf_pages": 0,
        },
    }


def test_xml_only_document_does_not_require_page_evidence(tmp_path):
    unit = raw_unit(
        "doc_xml",
        "unit_test_art_1",
        "test_art_1",
        unit_type="article",
    )
    chunk = raw_chunk(
        unit["unit_id"],
        "chunk_test_art_1_text",
        "test_art_1_text_1",
        "Artikelinhalt",
    )
    document = raw_document("doc_xml", "test", [unit], [chunk])
    context = validator.ValidationContext(tmp_path / "xml_raw.json")

    validator.validate_raw_document(context, document, check_page_files=True)

    codes = {finding.code for finding in context.findings}
    assert "NO_PAGE_REFS" not in codes
    assert "UNIT_MISSING_PAGE_RANGE" not in codes
    assert "CHUNK_MISSING_PAGE_RANGE" not in codes
    assert "NO_PARAGRAPH_UNITS" not in codes


def test_legacy_pdf_document_still_requires_page_evidence(tmp_path):
    unit = raw_unit("doc_pdf", "unit_test_para_1", "test_para_1")
    chunk = raw_chunk(
        unit["unit_id"],
        "chunk_test_para_1_text",
        "test_para_1_text_1",
        "Text",
    )
    document = raw_document("doc_pdf", "test", [unit], [chunk])
    document["metadata"] = {"source_format": "pdf"}
    context = validator.ValidationContext(tmp_path / "pdf_raw.json")

    validator.validate_raw_document(context, document, check_page_files=False)

    assert "NO_PAGE_REFS" in {finding.code for finding in context.findings}


def test_aligned_xml_page_numbers_do_not_require_duplicate_page_text_files(
    tmp_path,
):
    unit = raw_unit("doc_xml", "unit_test_para_1", "test_para_1")
    unit["page_range"] = {"start": 1, "end": 1}
    chunk = raw_chunk(
        unit["unit_id"],
        "chunk_test_para_1_text",
        "test_para_1_text_1",
        "Text",
    )
    chunk["page_range"] = {"start": 1, "end": 1}
    chunk["page_id"] = "pg_test_000"
    document = raw_document("doc_xml", "test", [unit], [chunk])
    document["page_refs"] = [
        {
            "page_id": "pg_test_000",
            "page_number": 1,
            "pdf_page_index": 0,
            "text_sha256": "page-sha",
            "text_extraction_backend": "pypdf",
        }
    ]
    document["pages"] = list(document["page_refs"])
    document["metadata"]["pdf_pages"] = 1
    document["metadata"]["pdf_alignment_status"] = "aligned"
    context = validator.ValidationContext(tmp_path / "xml_aligned_raw.json")

    validator.validate_raw_document(context, document, check_page_files=True)

    codes = {finding.code for finding in context.findings}
    assert "MISSING_PAGE_FILE" not in codes
    assert "NO_PAGE_REFS" not in codes


def test_validator_allows_verified_asset_without_text_only(tmp_path):
    available_asset = raw_chunk(
        "unit_asset",
        "chunk_asset",
        "test_available_asset",
        " ",
    )
    available_asset.update(
        {
            "chunk_type": "source_asset",
            "source_asset": "diagram.jpg",
            "source_asset_status": "available",
            "source_asset_sha256": "a" * 64,
        }
    )
    missing_asset = copy.deepcopy(available_asset)
    missing_asset.update(
        {
            "chunk_id": "chunk_missing_asset",
            "global_key": "test_missing_asset",
            "legal_citation": "test_missing_asset",
            "source_asset_status": "missing",
            "source_asset_sha256": None,
        }
    )
    semantic_chunk = raw_chunk(
        "unit_text",
        "chunk_text",
        "test_empty_semantic_text",
        " ",
    )
    context = validator.ValidationContext(tmp_path / "asset_validation.json")

    for chunk in (available_asset, missing_asset, semantic_chunk):
        validator.validate_text_chunk(context, chunk, "doc_xml")

    empty_findings = [
        finding
        for finding in context.findings
        if finding.code == "EMPTY_TEXT_CHUNK"
    ]
    assert {finding.citation for finding in empty_findings} == {
        "test_missing_asset",
        "test_empty_semantic_text",
    }


def test_content_graph_preserves_xml_provenance_and_source_order():
    early = raw_unit(
        "doc_xml",
        "unit_z",
        "test_z",
        sequence=1,
        source_order=1,
        unit_type="article",
    )
    early["source_xml_norm_index"] = 1
    early["source_xml_division_key"] = "010"
    late = raw_unit(
        "doc_xml",
        "unit_a",
        "test_a",
        sequence=2,
        source_order=2,
        unit_type="article",
    )
    first_chunk = raw_chunk(
        early["unit_id"],
        "chunk_z",
        "test_z_text",
        "zuerst",
        sequence=1,
        source_order=[1, 1],
    )
    first_chunk["structured_lists"] = [{"kind": "DL", "items": ["eins"]}]
    first_chunk["source_xml_norm_index"] = 1
    second_chunk = raw_chunk(
        early["unit_id"],
        "chunk_a",
        "test_a_text",
        "danach",
        sequence=2,
        source_order=[1, 2],
    )
    second_chunk["source_asset"] = "bild.gif"
    second_chunk["source_asset_attributes"] = {"src": "bild.gif"}
    late_chunk = raw_chunk(
        late["unit_id"],
        "chunk_late",
        "test_late_text",
        "zuletzt",
        source_order=[2, 1],
    )
    document = raw_document(
        "doc_xml",
        "test",
        [late, early],
        [second_chunk, late_chunk, first_chunk],
        source_xml="BJNRTEST.xml",
        source_zip="/corpus/testg/xml.zip",
    )

    graph = content_nodes.build_graph({"documents": [document]}, "memory")
    document_props = next(
        node["properties"] for node in graph["nodes"] if "Document" in node["labels"]
    )
    assert document_props["source_xml"] == "BJNRTEST.xml"
    assert document_props["source_zip"] == "/corpus/testg/xml.zip"
    assert document_props["source_format"] == "gii_xml"
    assert document_props["source_xml_sha256"] == "xml-sha"
    assert document_props["pdf_alignment_status"] == "not_attempted"

    early_props = next(
        node["properties"]
        for node in graph["nodes"]
        if node["properties"].get("global_key") == "test_z"
    )
    assert early_props["source_order"] == 1
    assert early_props["source_xml_norm_index"] == 1
    assert early_props["source_xml_division_key"] == "010"

    first_props = next(
        node["properties"]
        for node in graph["nodes"]
        if node["properties"].get("global_key") == "test_z_text"
    )
    assert first_props["source_order"] == [1, 1]
    assert json.loads(first_props["structured_lists"])[0]["kind"] == "DL"

    next_units = [
        rel for rel in graph["relationships"] if rel["type"] == "NEXT_UNIT"
    ]
    assert [(rel["start_node_id"], rel["end_node_id"]) for rel in next_units] == [
        ("unit_z__doc_xml", "unit_a__doc_xml")
    ]
    next_chunks = [
        rel for rel in graph["relationships"] if rel["type"] == "NEXT_CHUNK"
    ]
    assert ("chunk_z__doc_xml", "chunk_a__doc_xml") in {
        (rel["start_node_id"], rel["end_node_id"]) for rel in next_chunks
    }


def test_source_xml_alias_resolves_external_abbreviation():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        "Nach § 21 KrWG gilt die Pflicht.",
    )
    source_doc = raw_document(
        "doc_source",
        "quellgesetz",
        [source_unit],
        [source_chunk],
    )

    target_unit = raw_unit(
        "doc_target",
        "unit_target_para_21",
        "kreislaufwirtschaftsgesetz_para_21",
    )
    target_unit["document_key"] = "kreislaufwirtschaftsgesetz"
    target_unit["document_global_key"] = "kreislaufwirtschaftsgesetz"
    target_chunk = raw_chunk(
        target_unit["unit_id"],
        "chunk_target_para_21",
        "kreislaufwirtschaftsgesetz_para_21_text",
        "Zieltext",
    )
    target_chunk["document_global_key"] = "kreislaufwirtschaftsgesetz"
    target_doc = raw_document(
        "doc_target",
        "kreislaufwirtschaftsgesetz",
        [target_unit],
        [target_chunk],
        title="Kreislaufwirtschaftsgesetz",
        source_xml="KrWG.xml",
        source_zip="/corpus/krwg/xml.zip",
    )

    graph = content_nodes.build_graph(
        {"documents": [source_doc, target_doc]},
        "memory",
    )
    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    resolved = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["properties"].get("mention_text") == "§ 21 KrWG"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]
    assert resolved
    assert resolved[0]["end_node_id"] == "chunk_target_para_21__doc_target"
    assert resolved[0]["properties"]["resolution_status"] == "resolved"
    assert "krwg" in {
        reference_relations.slugify(alias)
        for alias in reference_relations.source_xml_aliases(
            "BJNR000.xml",
            "/corpus/krwg/xml.zip",
        )
    }


def test_internal_paragraph_reference_prefers_article_context():
    article = raw_unit(
        "doc_egbgb",
        "unit_egbgb_art_232",
        "egbgb_art_232",
        child_unit_ids=[
            "unit_egbgb_art_232_para_1",
            "unit_egbgb_art_232_para_2",
        ],
        unit_type="article",
    )
    para_1 = raw_unit(
        "doc_egbgb",
        "unit_egbgb_art_232_para_1",
        "egbgb_art_232_para_1",
        parent_unit_id=article["unit_id"],
        source_order=2,
    )
    para_2 = raw_unit(
        "doc_egbgb",
        "unit_egbgb_art_232_para_2",
        "egbgb_art_232_para_2",
        parent_unit_id=article["unit_id"],
        sequence=2,
        source_order=3,
    )
    source_chunk = raw_chunk(
        para_1["unit_id"],
        "chunk_egbgb_art_232_para_1",
        "egbgb_art_232_para_1_text",
        "Nach § 2 gilt die Übergangsregel.",
    )
    target_chunk = raw_chunk(
        para_2["unit_id"],
        "chunk_egbgb_art_232_para_2",
        "egbgb_art_232_para_2_text",
        "Zieltext",
    )
    document = raw_document(
        "doc_egbgb",
        "egbgb",
        [article, para_2, para_1],
        [target_chunk, source_chunk],
        source_xml="EGBGB.xml",
    )

    graph = content_nodes.build_graph({"documents": [document]}, "memory")
    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_egbgb_art_232_para_1__doc_egbgb"
        and rel["properties"].get("mention_text") == "§ 2"
    ]
    assert matches
    assert matches[0]["end_node_id"] == "chunk_egbgb_art_232_para_2__doc_egbgb"
    assert matches[0]["properties"]["target_global_key"] == "egbgb_art_232_para_2"
