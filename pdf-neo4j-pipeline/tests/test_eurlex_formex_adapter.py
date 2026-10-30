import copy
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import extract_content_nodes as content_nodes
import extract_reference_relations as reference_relations
import export_eurlex_manifest_neo4j_csv as eurlex_csv
import export_neo4j_cypher as neo4j_cypher
from normtext_extractor.eurlex_formex import (
    eu_citation_aliases,
    extract_package,
    parse_formex_table,
)


FORMEX = """<?xml version="1.0" encoding="utf-8"?>
<CONS.ACT>
  <INFO.CONSLEG CONSLEG.REF="2016R0679" START.DATE="20160524" END="NONE" LEG.VAL="REG"/>
  <CONS.DOC>
    <TITLE><TI>Verordnung zum Schutz personenbezogener Daten</TI></TITLE>
    <PREAMBLE>
      <PREAMBLE.INIT>gestützt auf die Verträge,</PREAMBLE.INIT>
      <GR.CONSID><CONSID>(1) Der Schutz natürlicher Personen ist ein Grundrecht.</CONSID></GR.CONSID>
      <PREAMBLE.FINAL>haben folgende Verordnung erlassen:</PREAMBLE.FINAL>
    </PREAMBLE>
    <ENACTING.TERMS>
      <DIVISION>
        <TITLE><TI>KAPITEL I Allgemeine Bestimmungen</TI></TITLE>
        <ARTICLE IDENTIFIER="006">
          <TI.ART>Artikel 6</TI.ART><STI.ART>Rechtmäßigkeit der Verarbeitung</STI.ART>
          <PARAG IDENTIFIER="006.001"><NO.PARAG>1.</NO.PARAG><ALINEA>Die Verarbeitung ist rechtmäßig.</ALINEA></PARAG>
          <PARAG IDENTIFIER="006.002"><NO.PARAG>2.</NO.PARAG><ALINEA>Absatz 1 gilt entsprechend.</ALINEA></PARAG>
        </ARTICLE>
      </DIVISION>
    </ENACTING.TERMS>
    <CONS.ANNEX>
      <TITLE><TI>ANHANG I</TI></TITLE>
      <CONTENTS>
        <TBL COLS="3" NO.SEQ="1">
          <TITLE><TI>Tabelle 1 Kategorien</TI></TITLE>
          <CORPUS>
            <ROW TYPE="HEADER"><CELL COL="1" COLSPAN="3">Kategorien</CELL></ROW>
            <ROW><CELL COL="1">Merkmal</CELL><CELL COL="2">Wert A</CELL><CELL COL="3">Wert B</CELL></ROW>
            <ROW><CELL COL="1" ROWSPAN="2">pH</CELL><CELL COL="2">6</CELL><CELL COL="3">7</CELL></ROW>
            <ROW><CELL COL="2">8</CELL><CELL COL="3">9</CELL></ROW>
          </CORPUS>
          <GR.NOTES><NOTE NOTE.ID="n1">Gilt für beide Zeilen.</NOTE></GR.NOTES>
        </TBL>
      </CONTENTS>
    </CONS.ANNEX>
  </CONS.DOC>
</CONS.ACT>
"""

ORIGINAL_FORMEX = """<?xml version="1.0" encoding="utf-8"?>
<ACT>
  <BIB.INSTANCE><DATE ISO="20170321">20170321</DATE></BIB.INSTANCE>
  <TITLE><TI><P>Verordnung (EU) 2017/492 der Kommission</P></TI></TITLE>
  <PREAMBLE><PREAMBLE.INIT>DIE EUROPÄISCHE KOMMISSION —</PREAMBLE.INIT></PREAMBLE>
  <ENACTING.TERMS>
    <ARTICLE IDENTIFIER="001">
      <TI.ART>Artikel 1</TI.ART>
      <ALINEA><P>Die Verordnung wird geändert.</P></ALINEA>
    </ARTICLE>
  </ENACTING.TERMS>
  <FINAL><P>Diese Verordnung gilt unmittelbar.</P></FINAL>
</ACT>
"""


def record():
    return {
        "base_celex": "32016R0679",
        "consolidated_celex": "02016R0679-20160524",
        "consolidation_date": "2016-05-24",
        "descriptor": "R",
        "descriptor_label": "regulation",
        "legal_value": "REG",
        "local_path": "packages/R/02016R0679-20160524.fmx4.zip",
        "download_url": "https://publications.europa.eu/resource/celex/02016R0679-20160524",
        "title": "Datenschutz-Grundverordnung",
        "info_consleg": {"END": "NONE", "START.DATE": "20160524"},
    }


def package(tmp_path):
    path = tmp_path / "act.fmx4.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("CL2016R0679DE0000010.xml", FORMEX)
        archive.writestr("CL2016R0679DE0000010.doc.xml", "<DOC/>")
    return path


def original_package(tmp_path):
    path = tmp_path / "original.fmx4.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("L_2017.xml", ORIGINAL_FORMEX)
        archive.writestr("L_2017.doc.xml", "<DOC/>")
    return path


def test_formex_table_expands_spans_and_promotes_real_column_header():
    table_element = ET.fromstring(FORMEX).find("./CONS.DOC/CONS.ANNEX/CONTENTS/TBL")
    table = parse_formex_table(table_element)

    assert table["columns"] == [
        "Kategorien / Merkmal",
        "Kategorien / Wert A",
        "Kategorien / Wert B",
    ]
    assert table["body_matrix"] == [["pH", "6", "7"], ["pH", "8", "9"]]
    p_h = next(cell for cell in table["cells"] if cell["text"] == "pH")
    assert p_h["row_span"] == 2
    assert table["notes"][0]["text"] == "Gilt für beide Zeilen."


def test_adapter_emits_shared_contract_and_addressable_articles(tmp_path):
    payload = extract_package(package(tmp_path), record())
    document = payload["documents"][0]
    units = document["structural_units"]
    chunks = document["chunks"]

    assert payload["schema_version"] == "1.0.0-draft"
    assert document["document_global_key"] == "celex_32016r0679"
    assert document["metadata"]["in_force"] is True
    assert "Verordnung (EU) 2016/679" in document["metadata"]["citation_aliases"]
    article = next(unit for unit in units if unit["unit_type"] == "article")
    assert article["global_key"] == "celex_32016r0679_art_6"
    assert article["parent_unit_id"] is not None
    paragraph = next(chunk for chunk in chunks if chunk["global_key"].endswith("_art_6_abs_1"))
    assert paragraph["legal_citation"] == "32016R0679 Art. 6 Abs. 1"
    table = next(unit for unit in units if unit["unit_type"] == "table")
    table_chunk = next(chunk for chunk in chunks if chunk["unit_id"] == table["unit_id"])
    assert table_chunk["rows"][1]["Kategorien / Merkmal"] == "pH"
    assert table["table_data"]["row_count"] == 2
    assert any(chunk["chunk_type"] == "footnote" for chunk in chunks)


def test_adapter_accepts_original_formex_acts(tmp_path):
    original_record = {
        "base_celex": "32017R0492",
        "consolidated_celex": "32017R0492",
        "descriptor": "R",
        "local_path": "documents/eu_referenced_originals/32017R0492.fmx4.zip",
        "download_url": "https://publications.europa.eu/resource/celex/32017R0492",
    }
    payload = extract_package(original_package(tmp_path), original_record)
    document = payload["documents"][0]

    assert document["metadata"]["source_format"] == "eurlex_formex_original"
    assert document["metadata"]["source_package_kind"] == "fmx4_original_zip"
    assert document["date_enacted"] == "20170321"
    assert document["title"] == "Verordnung (EU) 2017/492 der Kommission"
    article = next(
        unit for unit in document["structural_units"] if unit["unit_type"] == "article"
    )
    assert article["global_key"] == "celex_32017r0492_art_1"


def _german_raw():
    return {
        "schema_version": "1.0.0-draft",
        "phase": "normtext",
        "documents": [
            {
                "document_id": "doc_de_test",
                "document_key": "testg",
                "document_global_key": "testgesetz",
                "global_key": "testgesetz",
                "title": "Testgesetz",
                "canonical_citation": "TestG",
                "abbreviation": "TestG",
                "citation_prefix": "TestG",
                "pages": [],
                "page_refs": [],
                "structural_units": [
                    {
                        "unit_id": "unit_testg_para_1",
                        "global_key": "testgesetz_para_1",
                        "document_id": "doc_de_test",
                        "document_key": "testg",
                        "document_global_key": "testgesetz",
                        "unit_type": "paragraph",
                        "legal_citation": "TestG § 1",
                        "display_name": "TestG § 1",
                        "label": "§ 1",
                        "number": "1",
                        "title": "Verweis",
                        "parent_unit_id": None,
                        "child_unit_ids": [],
                        "sequence": 1,
                        "source_order": 1,
                        "page_range": None,
                        "text": "Verweis",
                        "text_sha256": "u",
                    }
                ],
                "chunks": [
                    {
                        "chunk_id": "chunk_testg_para_1",
                        "global_key": "testgesetz_para_1_text",
                        "unit_id": "unit_testg_para_1",
                        "document_global_key": "testgesetz",
                        "chunk_type": "paragraph_text",
                        "legal_citation": "TestG § 1",
                        "display_name": "TestG § 1",
                        "parent_chunk_id": None,
                        "child_chunk_ids": [],
                        "sequence": 1,
                        "source_order": 1,
                        "page_range": None,
                        "text": "Nach Artikel 6 Absatz 1 der Verordnung (EU) 2016/679 ist dies zulässig.",
                        "text_sha256": "c",
                    }
                ],
                "metadata": {"source_format": "gii_xml", "extractor": "gii_xml"},
            }
        ],
    }


def test_german_reference_resolves_to_eu_article(tmp_path):
    eu_raw = extract_package(package(tmp_path), record())
    eu_graph = content_nodes.build_graph(eu_raw, "eu.json")
    de_graph = content_nodes.build_graph(_german_raw(), "de.json")
    merged = {
        "nodes": de_graph["nodes"] + eu_graph["nodes"],
        "relationships": de_graph["relationships"] + eu_graph["relationships"],
    }
    result = reference_relations.ReferenceExtractor(copy.deepcopy(merged)).extract()
    references = [rel for rel in result["relationships"] if rel["type"] == "REFERS_TO"]

    exact = next(
        rel
        for rel in references
        if rel["properties"].get("mention_text", "").startswith("Artikel 6")
    )
    assert exact["properties"]["resolution_status"] == "resolved"
    assert exact["properties"]["resolved_target_global_key"] == "celex_32016r0679_art_6"
    assert "ReferenceTarget" not in next(
        node["labels"] for node in result["nodes"] if node["id"] == exact["end_node_id"]
    )


def test_citation_aliases_cover_old_and_new_number_order():
    aliases = eu_citation_aliases("32008R0440", "R")
    assert "Verordnung (EG) Nr. 440/2008" in aliases
    assert "Verordnung (EU) 2008/440" in aliases


def test_citation_aliases_cover_legacy_two_digit_years():
    aliases = eu_citation_aliases("31983R0918", "R")

    assert "Verordnung (EWG) Nr. 918/83" in aliases
    assert "Verordnung 83/918/EWG" in aliases


def test_eu_identifier_normalization_does_not_expand_two_digit_act_number():
    assert reference_relations.eu_act_key("Richtlinie", "2003/87/EG") == (
        "richtlinie_2003_87_eg"
    )
    assert reference_relations.eu_act_key("Verordnung", "(EWG) Nr. 918/83") == (
        "verordnung_ewg_nr_918_1983"
    )
    assert reference_relations.eu_act_key("Verordnung", "(EU) Nr. 10/2011") == (
        "verordnung_eu_nr_10_2011"
    )


def test_legacy_two_digit_eu_year_resolves_to_consolidated_document(tmp_path):
    eu_raw = extract_package(package(tmp_path), record())
    eu_document = eu_raw["documents"][0]
    eu_document["document_global_key"] = "celex_31991l0676"
    eu_document["global_key"] = "celex_31991l0676"
    eu_document["metadata"]["citation_aliases"] = eu_citation_aliases(
        "31991L0676", "L"
    )
    article = next(
        unit for unit in eu_document["structural_units"] if unit["unit_type"] == "article"
    )
    article["global_key"] = "celex_31991l0676_art_6"
    for chunk in eu_document["chunks"]:
        if chunk["unit_id"] == article["unit_id"]:
            chunk["global_key"] = "celex_31991l0676_art_6_text"

    de_raw = _german_raw()
    de_raw["documents"][0]["chunks"][0]["text"] = (
        "Nach Artikel 6 der Richtlinie 91/676/EWG ist dies zulässig."
    )
    merged = {
        "nodes": content_nodes.build_graph(de_raw, "de.json")["nodes"]
        + content_nodes.build_graph(eu_raw, "eu.json")["nodes"],
        "relationships": content_nodes.build_graph(de_raw, "de.json")["relationships"]
        + content_nodes.build_graph(eu_raw, "eu.json")["relationships"],
    }

    result = reference_relations.ReferenceExtractor(copy.deepcopy(merged)).extract()
    exact = next(
        rel
        for rel in result["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["properties"].get("mention_text", "").startswith("Artikel 6")
    )

    assert exact["properties"]["resolution_status"] == "resolved"
    assert exact["properties"]["resolved_target_global_key"] == "celex_31991l0676_art_6"


def test_explicit_delegated_regulation_wins_over_generic_law_name(tmp_path):
    raw = _german_raw()
    raw["documents"][0]["chunks"][0]["text"] = (
        "Artikel 2 der Delegierten Verordnung (EU) 2020/1818 ist anzuwenden."
    )
    graph = content_nodes.build_graph(raw, "de.json")

    result = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    chunk_references = [
        rel
        for rel in result["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_testg_para_1__doc_de_test"
    ]

    assert len(chunk_references) == 1
    assert chunk_references[0]["properties"]["requested_target_global_key"] == (
        "verordnung_eu_2020_1818_art_2"
    )


def test_generic_delegated_regulation_remains_unresolved_without_identifier():
    raw = _german_raw()
    raw["documents"][0]["chunks"][0]["text"] = (
        "Artikel 2 der Delegierten Verordnung ist anzuwenden."
    )
    graph = content_nodes.build_graph(raw, "de.json")

    result = reference_relations.ReferenceExtractor(copy.deepcopy(graph)).extract()
    reference = next(
        rel
        for rel in result["relationships"]
        if rel["type"] == "REFERS_TO"
        and rel["start_node_id"] == "chunk_testg_para_1__doc_de_test"
    )

    assert reference["properties"]["resolution_status"] == "unresolved"
    assert reference["properties"]["requested_target_global_key"] == (
        "delegierten_verordnung_art_2"
    )


def test_neo4j_export_keeps_exact_reference_target_key_and_repairs_stale_edges(tmp_path):
    assert "requested_target_global_key" in neo4j_cypher.NEO4J_NODE_PROPERTY_ALLOWLIST["ReferenceTarget"]

    output = tmp_path / "load.cypher"
    eurlex_csv._write_import_cypher(output, "file:///eu/", [], [], 1000)
    cypher = output.read_text(encoding="utf-8")

    assert "coalesce(rt.requested_target_global_key, rt.global_key) AS requested_key" in cypher
    assert "size(documents) = 1" in cypher
    assert "all(candidate IN documents WHERE candidate.base_celex IS NOT NULL)" in cypher
    assert "WHEN d.consolidated_celex STARTS WITH '0' THEN 0 ELSE 1" in cypher
    assert "OPTIONAL MATCH (rt)-[obsolete:`RESOLVES_TO`]" in cypher
    assert "WHERE NOT exact:`ReferenceTarget`" in cypher
    assert "Repair stale document-level edges" in cypher
    assert "WHERE document.base_celex IS NOT NULL" in cypher
    assert "Repair any stale resolved edge to such a node" in cypher
    assert "resolved.resolution_method = 'resolved_eu_alias_exact'" in cypher
