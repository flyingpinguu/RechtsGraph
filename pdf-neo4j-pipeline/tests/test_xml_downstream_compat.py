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


def test_generic_external_law_title_resolves_without_pseudo_document_key():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        (
            "Nach § 2 Absatz 1 der Verordnung über Testanlagen "
            "vom 1. Januar 2020 gilt die Pflicht."
        ),
    )
    target_unit = raw_unit(
        "doc_target",
        "unit_target_para_2",
        "verordnung_ueber_testanlagen_para_2",
    )
    target_chunk = raw_chunk(
        target_unit["unit_id"],
        "chunk_target_para_2",
        "verordnung_ueber_testanlagen_para_2_text",
        "Zieltext",
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                ),
                raw_document(
                    "doc_target",
                    "verordnung_ueber_testanlagen",
                    [target_unit],
                    [target_chunk],
                    title="Verordnung über Testanlagen",
                ),
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert matches
    assert matches[0]["end_node_id"] == "chunk_target_para_2__doc_target"
    assert matches[0]["properties"]["reference_kind"] == "external_long_name"
    assert matches[0]["properties"]["target_document_key"] == (
        "verordnung_ueber_testanlagen"
    )
    assert matches[0]["properties"]["resolution_status"] == "resolved"
    assert matches[0]["properties"]["resolution_method"] == "nearest_ancestor"
    assert matches[0]["properties"]["requested_target_global_key"] == (
        "verordnung_ueber_testanlagen_para_2_abs_1"
    )
    assert matches[0]["properties"]["resolved_target_global_key"] == (
        "verordnung_ueber_testanlagen_para_2"
    )


def test_generic_law_title_stops_before_following_clause():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        "Es gilt § 1 des Gesetzes über Ordnungswidrigkeiten; sie ist anzuwenden.",
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["target_document_key"] == (
        "gesetz_ueber_ordnungswidrigkeiten"
    )


def test_external_law_title_uses_complete_suffix_and_stops_before_predicate():
    cases = (
        (
            "Dabei gilt § 7 Absatz 4 des Raumordnungsgesetzes unverändert.",
            "raumordnungsgesetz",
        ),
        (
            "Für die Frist gilt eine § 83c des Gesetzes über die internationale "
            "Rechtshilfe in Strafsachen vergleichbare Fristenregelung.",
            "gesetz_ueber_die_internationale_rechtshilfe_in_strafsachen",
        ),
        (
            "Die Leistung gemäß § 4 der Verordnung über mittelgroße Feuerungs-, "
            "Gasturbinen- und Verbrennungsmotoranlagen ist maßgeblich.",
            "verordnung_ueber_mittelgrosse_feuerungs_gasturbinen_und_"
            "verbrennungsmotoranlagen",
        ),
    )

    for index, (text, expected_key) in enumerate(cases, start=1):
        unit = raw_unit(
            "doc_source_{}".format(index),
            "unit_source_para_{}".format(index),
            "quellgesetz_para_{}".format(index),
        )
        chunk = raw_chunk(
            unit["unit_id"],
            "chunk_source_para_{}".format(index),
            "quellgesetz_para_{}_text".format(index),
            text,
        )
        graph = content_nodes.build_graph(
            {
                "documents": [
                    raw_document(
                        "doc_source_{}".format(index),
                        "quellgesetz",
                        [unit],
                        [chunk],
                    )
                ]
            },
            "memory",
        )

        updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
        matches = [
            rel
            for rel in updated["relationships"]
            if rel["type"] == "REFERS_TO"
            and rel["start_node_id"] == "chunk_source_para_{}__doc_source_{}".format(
                index,
                index,
            )
        ]

        assert len(matches) == 1
        assert matches[0]["properties"]["target_document_key"] == expected_key


def test_implausible_clause_is_not_used_as_external_law_name():
    target_unit = raw_unit(
        "doc_source",
        "unit_source_para_8",
        "quellgesetz_para_8",
    )
    target_chunk = raw_chunk(
        target_unit["unit_id"],
        "chunk_source_para_8",
        "quellgesetz_para_8_text",
        "Zieltext",
    )
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_9",
        "quellgesetz_para_9",
        sequence=2,
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_9",
        "quellgesetz_para_9_text",
        (
            "Nach § 8 der Nachweis innerhalb eines Monats nach dem "
            "Inkrafttreten der Verordnung vorzulegen."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [target_unit, source_unit],
                    [target_chunk, source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_9__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["target_document_key"] == "quellgesetz"
    assert matches[0]["properties"]["reference_kind"] == "internal"


def test_historical_law_modifier_is_part_of_external_citation_not_title():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        (
            "Es gelten die §§ 23 und 24 des bis zum 1. Juni 2012 "
            "geltenden Kreislaufwirtschafts- und Abfallgesetzes."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = {
        rel["properties"]["target_document_key"]
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    }

    assert matches == {"kreislaufwirtschafts_und_abfallgesetz"}


def test_external_law_is_inherited_by_von_denen_boilerplate():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellverordnung_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellverordnung_para_1_text",
        (
            "Auf Grund des § 24 des Kreislaufwirtschaftsgesetzes vom "
            "24. Februar 2012 (BGBl. I S. 212), von denen § 24 durch "
            "Artikel 1 geändert worden ist."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellverordnung",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 2
    assert {
        rel["properties"]["target_document_key"]
        for rel in matches
    } == {"kreislaufwirtschaftsgesetz"}
    assert len({
        rel["properties"]["char_start"]
        for rel in matches
    }) == 2


def test_internal_reference_is_not_misread_as_long_law_name():
    target_unit = raw_unit(
        "doc_source",
        "unit_source_para_8",
        "quellgesetz_para_8",
    )
    target_chunk = raw_chunk(
        target_unit["unit_id"],
        "chunk_source_para_8",
        "quellgesetz_para_8_text",
        "Zieltext",
    )
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_9",
        "quellgesetz_para_9",
        sequence=2,
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_9",
        "quellgesetz_para_9_text",
        (
            "Bei Prüfungen nach § 8 Absatz 3 und 5 gelten die "
            "Zulässigkeits- und Zuordnungskriterien."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [target_unit, source_unit],
                    [target_chunk, source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_9__doc_source"
    ]

    assert matches
    assert all(
        rel["properties"]["reference_kind"] == "internal"
        for rel in matches
    )
    assert {
        rel["properties"]["target_document_key"]
        for rel in matches
    } == {"quellgesetz"}
    assert len(matches) == 2
    assert {
        rel["properties"]["requested_target_global_key"]
        for rel in matches
    } == {
        "quellgesetz_para_8_abs_3",
        "quellgesetz_para_8_abs_5",
    }
    assert {
        rel["properties"]["resolution_method"]
        for rel in matches
    } == {"nearest_ancestor"}


def test_unresolved_reference_distinguishes_missing_unit_from_missing_document():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        "Es gilt § 99.",
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["resolution_status"] == "unresolved"
    assert matches[0]["properties"]["resolution_method"] == "target_unit_missing"
    assert matches[0]["properties"]["requested_target_global_key"] == (
        "quellgesetz_para_99"
    )
    assert "resolved_target_global_key" not in matches[0]["properties"]


def test_trailing_external_law_name_scopes_over_preceding_paragraph_chain():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        (
            "Die §§ 6 oder 7, auch in Verbindung mit § 15 "
            "des Infektionsschutzgesetzes gelten entsprechend."
        ),
    )
    target_units = [
        raw_unit(
            "doc_target",
            "unit_target_para_{}".format(number),
            "infektionsschutzgesetz_para_{}".format(number),
            sequence=index,
        )
        for index, number in enumerate(("6", "7", "15"), 1)
    ]
    target_chunks = [
        raw_chunk(
            unit["unit_id"],
            "chunk_target_para_{}".format(number),
            "infektionsschutzgesetz_para_{}_text".format(number),
            "Zieltext {}".format(number),
        )
        for unit, number in zip(target_units, ("6", "7", "15"))
    ]
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                ),
                raw_document(
                    "doc_target",
                    "infektionsschutzgesetz",
                    target_units,
                    target_chunks,
                    title="Infektionsschutzgesetz",
                ),
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert {
        rel["properties"]["target_global_key"]
        for rel in matches
    } == {
        "infektionsschutzgesetz_para_6",
        "infektionsschutzgesetz_para_7",
        "infektionsschutzgesetz_para_15",
    }
    assert {
        rel["properties"]["reference_kind"]
        for rel in matches
    } == {"external_long_name"}


def test_trailing_external_law_name_does_not_cross_repeated_genitive_article():
    source_units = [
        raw_unit(
            "doc_source",
            "unit_source_para_{}".format(number),
            "strafgesetzbuch_para_{}".format(number),
            sequence=index,
        )
        for index, number in enumerate(("76b", "78"), 1)
    ]
    source_chunks = [
        raw_chunk(
            source_units[0]["unit_id"],
            "chunk_source_para_76b",
            "strafgesetzbuch_para_76b_text",
            (
                "In den Fällen des § 78 Absatz 2 und des § 5 des "
                "Völkerstrafgesetzbuches gelten besondere Regeln."
            ),
        ),
        raw_chunk(
            source_units[1]["unit_id"],
            "chunk_source_para_78",
            "strafgesetzbuch_para_78_text",
            "Zieltext",
        ),
    ]
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "strafgesetzbuch",
                    source_units,
                    source_chunks,
                    title="Strafgesetzbuch",
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_76b__doc_source"
    ]

    assert {
        (
            rel["properties"]["target_document_key"],
            rel["properties"]["requested_target_global_key"],
        )
        for rel in matches
    } == {
        ("strafgesetzbuch", "strafgesetzbuch_para_78_abs_2"),
        ("voelkerstrafgesetzbuch", "voelkerstrafgesetzbuch_para_5"),
    }


def test_trailing_external_abbreviation_scopes_over_preceding_paragraph_chain():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "gebuehrenverordnung_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "gebuehrenverordnung_para_1_text",
        "Anordnungen nach § 15 Absatz 4 Satz 1 und § 38 Absatz 3 ElektroG.",
    )
    target_units = [
        raw_unit(
            "doc_target",
            "unit_target_para_{}".format(number),
            "elektrogesetz_para_{}".format(number),
            sequence=index,
        )
        for index, number in enumerate(("15", "38"), 1)
    ]
    target_chunks = [
        raw_chunk(
            unit["unit_id"],
            "chunk_target_para_{}".format(number),
            "elektrogesetz_para_{}_text".format(number),
            "Zieltext {}".format(number),
        )
        for unit, number in zip(target_units, ("15", "38"))
    ]
    target_document = raw_document(
        "doc_target",
        "elektrogesetz",
        target_units,
        target_chunks,
        title="Elektrogesetz",
    )
    target_document["abbreviation"] = "ElektroG"
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "gebuehrenverordnung",
                    [source_unit],
                    [source_chunk],
                ),
                target_document,
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert {
        rel["properties"]["target_global_key"]
        for rel in matches
    } == {"elektrogesetz_para_15", "elektrogesetz_para_38"}
    assert {
        rel["properties"]["reference_kind"]
        for rel in matches
    } == {"external_abbreviation"}


def test_external_law_heading_scopes_paragraphs_until_next_numbered_item():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "strafgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "strafgesetz_para_1_text",
        (
            "2. aus der Abgabenordnung:\n"
            "a) Steuerhinterziehung nach § 370 Absatz 3,\n"
            "b) Schmuggel nach § 373,\n"
            "3. sonstige Straftaten nach § 10."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "strafgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = {
        (
            rel["properties"]["target_document_key"],
            rel["properties"]["target_global_key"],
            rel["properties"]["reference_kind"],
        )
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    }

    assert matches == {
        ("abgabenordnung", "abgabenordnung_para_370_abs_3", "external_long_name"),
        ("abgabenordnung", "abgabenordnung_para_373", "external_long_name"),
        ("strafgesetz", "strafgesetz_para_10", "internal"),
    }


def test_eu_act_article_chain_resolves_to_one_canonical_external_document():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "abfallgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "abfallgesetz_para_1_text",
        (
            "Es gelten Artikel 22 oder 24, jeweils auch in Verbindung mit "
            "Artikel 35 Absatz 1 der Verordnung (EG) Nr. 1013/2006."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "abfallgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = {
        (
            rel["properties"]["target_document_key"],
            rel["properties"]["target_global_key"],
        )
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    }

    assert matches == {
        ("verordnung_eg_nr_1013_2006", "verordnung_eg_nr_1013_2006_art_22"),
        ("verordnung_eg_nr_1013_2006", "verordnung_eg_nr_1013_2006_art_24"),
        ("verordnung_eg_nr_1013_2006", "verordnung_eg_nr_1013_2006_art_35"),
    }


def test_governing_eu_act_context_survives_nested_reference_to_second_act():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "batteriegesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "batteriegesetz_para_1_text",
        (
            "Ordnungswidrig handelt, wer gegen die Verordnung (EU) 2023/1542 "
            "in der Fassung vom 13. Juni 2024 verstößt, indem er entgegen "
            "Artikel 11 Absatz 1 Unterabsatz 1 Satz 1 handelt oder entgegen "
            "Artikel 19 in Verbindung mit Artikel 30 Absatz 5 Satz 1 "
            "der Verordnung (EG) Nr. 765/2008 eine Kennzeichnung anbringt."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "batteriegesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = {
        (
            rel["properties"]["target_document_key"],
            rel["properties"]["target_global_key"],
        )
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    }

    assert matches == {
        ("verordnung_eu_2023_1542", "verordnung_eu_2023_1542_art_11"),
        ("verordnung_eg_nr_765_2008", "verordnung_eg_nr_765_2008_art_19"),
        ("verordnung_eg_nr_765_2008", "verordnung_eg_nr_765_2008_art_30"),
    }


def test_dated_amending_act_article_gets_stable_external_target():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        (
            "Die Vorschrift wurde durch Artikel 2 des Gesetzes vom "
            "19. Mai 2020 geändert."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["target_document_key"] == (
        "gesetz_vom_19_mai_2020"
    )
    assert matches[0]["properties"]["target_global_key"] == (
        "gesetz_vom_19_mai_2020_art_2"
    )
    assert matches[0]["properties"]["resolution_method"] == (
        "target_document_missing"
    )


def test_framework_decision_article_chain_gets_canonical_target():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "strafgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "strafgesetz_para_1_text",
        (
            "Es gelten Artikel 2 oder Artikel 3 des Rahmenbeschlusses "
            "2003/568/JI des Rates."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "strafgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = {
        rel["properties"]["target_global_key"]
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    }

    assert matches == {
        "rahmenbeschluss_2003_568_ji_art_2",
        "rahmenbeschluss_2003_568_ji_art_3",
    }


def test_nested_article_paragraph_reference_preserves_both_levels():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        (
            "Die Freistellung richtet sich nach Artikel 4 § 3 "
            "des Umweltrahmengesetzes."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["target_global_key"] == (
        "umweltrahmengesetz_art_4_para_3"
    )


def test_abbreviated_article_reference_to_external_act_is_detected():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "strafgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "strafgesetz_para_1_text",
        "Zur Anwendung vgl. Art. 316j StGBEG.",
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "strafgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["target_document_key"] == "stgbeg"
    assert matches[0]["properties"]["target_global_key"] == "stgbeg_art_316j"
    assert matches[0]["properties"]["reference_kind"] == "external_abbreviation"


def test_xml_jurabk_alias_resolves_alternative_official_abbreviation():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "strafgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "strafgesetz_para_1_text",
        "Zur Anwendung vgl. Art. 316j StGBEG.",
    )
    target_key = "einfuehrungsgesetz_zum_strafgesetzbuch"
    target_unit = raw_unit(
        "doc_target",
        "unit_target_art_316j",
        "{}_art_316j".format(target_key),
        unit_type="article",
    )
    target_unit["document_key"] = "egstgb"
    target_unit["document_global_key"] = target_key
    target_chunk = raw_chunk(
        target_unit["unit_id"],
        "chunk_target_art_316j",
        "{}_art_316j_text".format(target_key),
        "Zieltext",
    )
    target_chunk["document_global_key"] = target_key
    target_document = raw_document(
        "doc_target",
        "egstgb",
        [target_unit],
        [target_chunk],
        title="Einführungsgesetz zum Strafgesetzbuch",
    )
    target_document["document_global_key"] = target_key
    target_document["global_key"] = target_key
    target_document["metadata"]["jurabk"] = ["StGBEG"]
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "strafgesetz",
                    [source_unit],
                    [source_chunk],
                ),
                target_document,
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["resolution_status"] == "resolved"
    assert matches[0]["properties"]["resolved_target_global_key"] == (
        "{}_art_316j".format(target_key)
    )


def test_leading_compound_title_alias_resolves_short_genitive_title():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        "Die Regelung folgt aus § 1 des Grundgesetzes.",
    )
    target_key = "grundgesetz_fuer_die_bundesrepublik_deutschland"
    target_unit = raw_unit(
        "doc_target",
        "unit_target_para_1",
        "{}_para_1".format(target_key),
    )
    target_unit["document_key"] = "gg"
    target_unit["document_global_key"] = target_key
    target_chunk = raw_chunk(
        target_unit["unit_id"],
        "chunk_target_para_1",
        "{}_para_1_text".format(target_key),
        "Zieltext",
    )
    target_chunk["document_global_key"] = target_key
    target_document = raw_document(
        "doc_target",
        "gg",
        [target_unit],
        [target_chunk],
        title="Grundgesetz für die Bundesrepublik Deutschland",
    )
    target_document["document_global_key"] = target_key
    target_document["global_key"] = target_key
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                ),
                target_document,
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["resolution_status"] == "resolved"
    assert matches[0]["properties"]["resolved_target_global_key"] == (
        "{}_para_1".format(target_key)
    )


def test_document_aliases_handle_hyphens_articles_and_footnote_markers():
    cases = (
        (
            "Die Pflicht folgt aus § 1 des Bundesimmissionsschutzgesetzes.",
            "bundes_immissionsschutzgesetz",
            "Bundes-Immissionsschutzgesetz",
        ),
        (
            "Es gilt § 1 der Verordnung über Verbrennung und die "
            "Mitverbrennung von Abfällen.",
            "verordnung_ueber_die_verbrennung_und_die_mitverbrennung_von_abfaellen",
            "Verordnung über die Verbrennung und die Mitverbrennung von Abfällen",
        ),
        (
            "Maßgeblich ist § 1 des Alkoholsteuergesetzes.",
            "alkoholsteuergesetz_2",
            "Alkoholsteuergesetz[2]",
        ),
    )

    for index, (text, target_key, title) in enumerate(cases, start=1):
        source_unit = raw_unit(
            "doc_source_{}".format(index),
            "unit_source_para_{}".format(index),
            "quellgesetz_para_{}".format(index),
        )
        source_chunk = raw_chunk(
            source_unit["unit_id"],
            "chunk_source_para_{}".format(index),
            "quellgesetz_para_{}_text".format(index),
            text,
        )
        target_unit = raw_unit(
            "doc_target_{}".format(index),
            "unit_target_para_1",
            "{}_para_1".format(target_key),
        )
        target_unit["document_key"] = "target_{}".format(index)
        target_unit["document_global_key"] = target_key
        target_chunk = raw_chunk(
            target_unit["unit_id"],
            "chunk_target_para_1",
            "{}_para_1_text".format(target_key),
            "Zieltext",
        )
        target_chunk["document_global_key"] = target_key
        target_document = raw_document(
            "doc_target_{}".format(index),
            "target_{}".format(index),
            [target_unit],
            [target_chunk],
            title=title,
        )
        target_document["document_global_key"] = target_key
        target_document["global_key"] = target_key
        graph = content_nodes.build_graph(
            {
                "documents": [
                    raw_document(
                        "doc_source_{}".format(index),
                        "quellgesetz",
                        [source_unit],
                        [source_chunk],
                    ),
                    target_document,
                ]
            },
            "memory",
        )

        updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
        matches = [
            rel
            for rel in updated["relationships"]
            if rel["type"] == "REFERS_TO"
            and rel["start_node_id"]
            == "chunk_source_para_{}__doc_source_{}".format(index, index)
        ]

        assert len(matches) == 1
        assert matches[0]["properties"]["resolution_status"] == "resolved"
        assert matches[0]["properties"]["resolved_target_global_key"] == (
            "{}_para_1".format(target_key)
        )


def test_internal_article_is_only_emitted_when_target_article_exists():
    article_1 = raw_unit(
        "doc_source",
        "unit_source_art_1",
        "artikelgesetz_art_1",
        unit_type="article",
    )
    article_1_chunk = raw_chunk(
        article_1["unit_id"],
        "chunk_source_art_1",
        "artikelgesetz_art_1_text",
        "Zieltext",
    )
    article_2 = raw_unit(
        "doc_source",
        "unit_source_art_2",
        "artikelgesetz_art_2",
        sequence=2,
        unit_type="article",
    )
    article_2_chunk = raw_chunk(
        article_2["unit_id"],
        "chunk_source_art_2",
        "artikelgesetz_art_2_text",
        "Nach Artikel 1 gilt diese Vorschrift; Artikel 99 bleibt unerfasst.",
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "artikelgesetz",
                    [article_1, article_2],
                    [article_1_chunk, article_2_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_art_2__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["target_global_key"] == "artikelgesetz_art_1"
    assert matches[0]["properties"]["reference_kind"] == "internal"


def test_preceding_law_name_scopes_exception_list_forward():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "elektrogesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "elektrogesetz_para_1_text",
        (
            "Es gilt das Kreislaufwirtschaftsgesetz, mit Ausnahme von "
            "§ 17 Absatz 4 und § 54, und anschließend diese Vorschrift."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "elektrogesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = {
        (
            rel["properties"]["target_document_key"],
            rel["properties"]["target_global_key"],
        )
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    }

    assert matches == {
        ("kreislaufwirtschaftsgesetz", "kreislaufwirtschaftsgesetz_para_17_abs_4"),
        ("kreislaufwirtschaftsgesetz", "kreislaufwirtschaftsgesetz_para_54"),
    }


def test_external_scope_does_not_cross_a_sentence_boundary():
    local_target_unit = raw_unit(
        "doc_source",
        "unit_source_para_8",
        "quellgesetz_para_8",
    )
    local_target_chunk = raw_chunk(
        local_target_unit["unit_id"],
        "chunk_source_para_8",
        "quellgesetz_para_8_text",
        "Lokales Ziel",
    )
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_9",
        "quellgesetz_para_9",
        sequence=2,
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_9",
        "quellgesetz_para_9_text",
        (
            "Die Pflicht nach § 8 bleibt unberührt. "
            "Nach § 2 des Testgesetzes gilt eine weitere Pflicht."
        ),
    )
    external_target_unit = raw_unit(
        "doc_target",
        "unit_target_para_2",
        "testgesetz_para_2",
    )
    external_target_chunk = raw_chunk(
        external_target_unit["unit_id"],
        "chunk_target_para_2",
        "testgesetz_para_2_text",
        "Externes Ziel",
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [local_target_unit, source_unit],
                    [local_target_chunk, source_chunk],
                ),
                raw_document(
                    "doc_target",
                    "testgesetz",
                    [external_target_unit],
                    [external_target_chunk],
                    title="Testgesetz",
                ),
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_9__doc_source"
    ]

    assert {
        (
            rel["properties"]["target_global_key"],
            rel["properties"]["reference_kind"],
        )
        for rel in matches
    } == {
        ("quellgesetz_para_8", "internal"),
        ("testgesetz_para_2", "external_long_name"),
    }


def test_paragraph_ranges_expand_all_intermediate_targets():
    assert reference_relations.split_para_body(
        "121 bis 126 und 128"
    ) == ["121", "122", "123", "124", "125", "126", "128"]


def test_external_annex_with_number_qualifier_resolves_to_external_annex():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        (
            "Der Wert wird nach Anhang 4 Nummer 3.3.1 "
            "der Deponieverordnung bestimmt."
        ),
    )
    target_unit = raw_unit(
        "doc_target",
        "unit_target_anhang_4",
        "deponieverordnung_anhang_4",
        unit_type="annex",
    )
    target_chunk = raw_chunk(
        target_unit["unit_id"],
        "chunk_target_anhang_4",
        "deponieverordnung_anhang_4_text",
        "Zieltext",
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                ),
                raw_document(
                    "doc_target",
                    "deponieverordnung",
                    [target_unit],
                    [target_chunk],
                    title="Deponieverordnung",
                ),
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["target_global_key"] == (
        "deponieverordnung_anhang_4"
    )
    assert matches[0]["properties"]["resolution_status"] == "resolved"
    assert matches[0]["properties"]["reference_kind"] == "external_long_name"


def test_genitive_gesetzbuchs_and_uppercase_compound_law_suffix_are_external():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        (
            "Es gelten § 312c des Bürgerlichen Gesetzbuchs und "
            "§ 4a des Anti-Doping-Gesetzes."
        ),
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = {
        (
            rel["properties"]["target_document_key"],
            rel["properties"]["target_global_key"],
        )
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    }

    assert matches == {
        ("buergerliches_gesetzbuch", "buergerliches_gesetzbuch_para_312c"),
        ("anti_doping_gesetz", "anti_doping_gesetz_para_4a"),
    }


def test_external_annex_abbreviation_is_not_treated_as_internal():
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        "Verwendet werden Materialien nach Anlage 2 DüMV.",
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [source_unit],
                    [source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert len(matches) == 1
    assert matches[0]["properties"]["target_document_key"] == "duemv"
    assert matches[0]["properties"]["target_global_key"] == "duemv_anlage_2"
    assert matches[0]["properties"]["reference_kind"] == "external_abbreviation"


def test_legacy_xml_table_ordinal_resolves_to_exact_table():
    annex = raw_unit(
        "doc_source",
        "unit_source_anlage_1",
        "quellgesetz_anlage_1",
        child_unit_ids=["unit_source_anlage_1_tabelle_3_4"],
        unit_type="annex",
    )
    table = raw_unit(
        "doc_source",
        "unit_source_anlage_1_tabelle_3_4",
        "quellgesetz_anlage_1_tabelle_3_4",
        parent_unit_id=annex["unit_id"],
        unit_type="table",
    )
    table["number"] = "3"
    table_chunk = raw_chunk(
        table["unit_id"],
        "chunk_source_anlage_1_tabelle_3_4_rows",
        "quellgesetz_anlage_1_tabelle_3_4_rows",
        "Spalte | Wert",
    )
    table_chunk["chunk_type"] = "table_rows"
    source_unit = raw_unit(
        "doc_source",
        "unit_source_para_1",
        "quellgesetz_para_1",
        sequence=3,
    )
    source_chunk = raw_chunk(
        source_unit["unit_id"],
        "chunk_source_para_1",
        "quellgesetz_para_1_text",
        "Es gelten die Werte aus Anlage 1 Tabelle 3.",
    )
    graph = content_nodes.build_graph(
        {
            "documents": [
                raw_document(
                    "doc_source",
                    "quellgesetz",
                    [annex, table, source_unit],
                    [table_chunk, source_chunk],
                )
            ]
        },
        "memory",
    )

    updated = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    matches = [
        rel
        for rel in updated["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_source_para_1__doc_source"
    ]

    assert matches
    assert matches[0]["end_node_id"] == (
        "chunk_source_anlage_1_tabelle_3_4_rows__doc_source"
    )
    assert matches[0]["properties"]["target_global_key"] == (
        "quellgesetz_anlage_1_tabelle_3"
    )
    assert matches[0]["properties"]["target_level"] == "table"
    assert matches[0]["properties"]["resolution_status"] == "resolved"


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
