import json
import zipfile
from pathlib import Path

import pytest

from normtext_extractor.gii_xml import (
    extract_document_from_xml,
    extract_package,
    read_gii_xml_package,
)


XML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<dokumente builddate="20260726010101" doknr="BJNRTEST">
  <norm builddate="20260726010101" doknr="BJNRTEST">
    <metadaten>
      <jurabk>TestG</jurabk>
      <amtabk>TestG</amtabk>
      <ausfertigung-datum manuell="ja">2020-01-02</ausfertigung-datum>
      <fundstelle typ="amtlich"><periodikum>BGBl I</periodikum><zitstelle>2020, 1</zitstelle></fundstelle>
      <kurzue>Testgesetz</kurzue>
      <langue>Gesetz zum Testen des XML-Adapters</langue>
    </metadaten>
    <textdaten><fussnoten><Content><P>Allgemeiner Hinweis.</P></Content></fussnoten></textdaten>
  </norm>
  <norm doknr="BJNRTESTTOC">
    <metadaten><jurabk>TestG</jurabk><enbez>Inhaltsübersicht</enbez></metadaten>
    <textdaten><text><TOC><P>§ 1 Zweck</P></TOC></text></textdaten>
  </norm>
  <norm doknr="BJNRTESTART">
    <metadaten>
      <jurabk>TestG</jurabk>
      <gliederungseinheit>
        <gliederungskennzahl>010020010000232</gliederungskennzahl>
        <gliederungsbez>Art 232</gliederungsbez>
        <gliederungstitel>Übergangsvorschriften</gliederungstitel>
      </gliederungseinheit>
    </metadaten>
    <textdaten/>
  </norm>
  <norm doknr="BJNRTESTP1">
    <metadaten>
      <jurabk>TestG</jurabk>
      <gliederungseinheit>
        <gliederungskennzahl>010020010000232</gliederungskennzahl>
        <gliederungsbez>Art 232</gliederungsbez>
        <gliederungstitel>Übergangsvorschriften</gliederungstitel>
      </gliederungseinheit>
      <enbez>§ 1</enbez><titel>Erster Test</titel>
    </metadaten>
    <textdaten><text><Content>
      <P>(1) Der erste Absatz.</P>
      <P>(2) Der zweite Absatz mit <B>Hervorhebung</B>.</P>
    </Content></text></textdaten>
  </norm>
  <norm doknr="BJNRTESTANNEX">
    <metadaten>
      <jurabk>TestG</jurabk>
      <gliederungseinheit>
        <gliederungskennzahl>700190</gliederungskennzahl>
        <gliederungsbez>-</gliederungsbez>
        <gliederungstitel>Platzhalter</gliederungstitel>
      </gliederungseinheit>
      <enbez>Anlage 19</enbez><titel>Messwerte</titel>
    </metadaten>
    <textdaten><text><Content>
      <P>Tabelle 1:</P>
      <P>Grenzwerte</P>
      <table frame="all"><tgroup cols="3">
        <thead><row><entry>Parameter</entry><entry>Dim.</entry><entry>A</entry></row></thead>
        <tbody>
          <row><entry>pH</entry><entry>-</entry><entry>6</entry></row>
          <row><entry>Blei</entry><entry>mg/l</entry><entry>1</entry></row>
        </tbody>
      </tgroup></table>
      <P>Fortsetzung Tabelle 1:
        <table frame="all"><tgroup cols="3">
          <thead><row><entry>Parameter</entry><entry>Dim.</entry><entry>B</entry></row></thead>
          <tbody>
            <row><entry>pH</entry><entry>-</entry><entry>7</entry></row>
            <row><entry>Blei</entry><entry>mg/l</entry><entry>2</entry></row>
          </tbody>
        </tgroup></table>
      </P>
    </Content></text></textdaten>
  </norm>
</dokumente>
"""


def write_xml(tmp_path, text=XML_SAMPLE):
    path = tmp_path / "sample.xml"
    path.write_text(text, encoding="utf-8")
    return path


def test_adapter_preserves_article_context_and_prefers_annex_enbez(tmp_path):
    document, issues = extract_document_from_xml(write_xml(tmp_path))
    assert not issues
    assert document["document_key"] == "testg"
    assert document["metadata"]["source_format"] == "gii_xml"

    units = document["structural_units"]
    article = next(unit for unit in units if unit["label"] == "Art 232")
    paragraph = next(unit for unit in units if unit["label"] == "§ 1")
    annex = next(unit for unit in units if unit["label"] == "Anlage 19")
    assert article["unit_type"] == "article"
    assert paragraph["parent_unit_id"] == article["unit_id"]
    assert paragraph["global_key"].endswith("_art_232_para_1")
    assert paragraph["unit_id"] in article["child_unit_ids"]
    assert annex["unit_type"] == "annex"
    assert not any(unit["label"] == "-" for unit in units)
    assert not any(unit["label"] == "Inhaltsübersicht" for unit in units)

    subsection_chunks = [
        chunk
        for chunk in document["chunks"]
        if chunk["unit_id"] == paragraph["unit_id"] and chunk["chunk_type"] == "subsection"
    ]
    assert [chunk["number"] for chunk in subsection_chunks] == ["1", "2"]
    assert "Hervorhebung" in subsection_chunks[1]["text"]


def test_adapter_merges_horizontal_xml_table_continuations(tmp_path):
    document, _issues = extract_document_from_xml(write_xml(tmp_path))
    tables = [unit for unit in document["structural_units"] if unit["unit_type"] == "table"]
    assert len(tables) == 1
    table = tables[0]
    assert table["label"] == "Tabelle 1"
    assert table["columns"] == ["Parameter", "Dim.", "A", "B"]
    assert table["row_count"] == 2
    assert table["table_data"]["merge_mode"] == "horizontal"
    assert table["table_data"]["physical_tables"][1]["rows"][1]["B"] == "2"
    assert table["parent_unit_id"] == next(
        unit["unit_id"]
        for unit in document["structural_units"]
        if unit["label"] == "Anlage 19"
    )


def test_top_level_payload_is_deterministic(tmp_path):
    source = write_xml(tmp_path)
    first = extract_package(source, {"title": "Test source"})
    second = extract_package(source, {"title": "Test source"})
    assert first == second
    assert first["schema_version"] == "1.0.0-draft"
    assert first["documents"][0]["page_refs"] == []
    json.dumps(first, ensure_ascii=False)


def test_zip_reader_records_assets_without_extracting(tmp_path):
    package = tmp_path / "sample.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("BJNRTEST.xml", XML_SAMPLE)
        archive.writestr("formula.png", b"not-a-real-png")
    loaded = read_gii_xml_package(package)
    assert loaded["xml_name"] == "BJNRTEST.xml"
    assert {item["archive_member"] for item in loaded["assets"]} == {
        "BJNRTEST.xml",
        "formula.png",
    }


def test_zip_reader_rejects_unsafe_members(tmp_path):
    package = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../BJNRTEST.xml", XML_SAMPLE)
    with pytest.raises(ValueError, match="unsafe ZIP member"):
        read_gii_xml_package(package)


def test_adapter_resolves_footnotes_and_keeps_list_and_preformatted_structure(
    tmp_path,
):
    source = """<?xml version="1.0" encoding="UTF-8"?>
    <dokumente doknr="BJNRFOOT">
      <norm doknr="BJNRFOOT">
        <metadaten>
          <jurabk>FootG</jurabk><amtabk>FootG</amtabk>
          <langue>Fußnotentestgesetz</langue>
        </metadaten>
        <textdaten><fussnoten><Content><P><pre xml:space="preserve">A
      eingerückt</pre></P></Content></fussnoten></textdaten>
      </norm>
      <norm doknr="BJNRFOOTP1">
        <metadaten><jurabk>FootG</jurabk><enbez>§ 1</enbez></metadaten>
        <textdaten><text><Content>
          <P>(1) Haupttext<FnR ID="fn-1"/>.<FnArea><FnR ID="fn-1"/></FnArea></P>
          <P>Fortsetzung mit Liste:
            <DL Type="alpha"><DT>a)</DT><DD><LA>erster Punkt</LA></DD></DL>
          </P>
        </Content><Footnotes>
          <Footnote ID="fn-1" FnZ="1">Erläuterung.</Footnote>
          <Footnote ID="fn-orphan" FnZ="*">Unreferenziert, aber erhalten.</Footnote>
        </Footnotes></text></textdaten>
      </norm>
    </dokumente>
    """
    document, issues = extract_document_from_xml(write_xml(tmp_path, source))
    paragraph = next(
        unit for unit in document["structural_units"] if unit["label"] == "§ 1"
    )
    subsection = next(
        chunk
        for chunk in document["chunks"]
        if chunk["unit_id"] == paragraph["unit_id"]
        and chunk["chunk_type"] == "subsection"
    )
    assert subsection["text"].count("[1]") == 1
    assert "Fortsetzung mit Liste" in subsection["text"]
    assert subsection["structured_lists"][0]["items"][0]["marker"] == "a)"
    assert subsection["structured_lists"][0]["items"][0]["text"] == "erster Punkt"

    source_note = next(
        chunk for chunk in document["chunks"] if chunk["chunk_type"] == "source_note"
    )
    assert "      eingerückt" in source_note["preformatted_blocks"][0]
    assert any(
        issue["issue_type"] == "xml_orphan_footnote_definition"
        and issue["evidence"] == ["fn-orphan"]
        for issue in issues
    )
    assert any(
        chunk["chunk_type"] == "footnote"
        and chunk["source_xml_footnote_id"] == "fn-orphan"
        for chunk in document["chunks"]
    )


def test_adapter_reports_missing_assets_and_uses_retrieval_placeholder(tmp_path):
    source = """<dokumente doknr="BJNRIMG">
      <norm doknr="BJNRIMG"><metadaten>
        <jurabk>ImgG</jurabk><amtabk>ImgG</amtabk><langue>Bildgesetz</langue>
      </metadaten></norm>
      <norm doknr="BJNRIMGP1"><metadaten>
        <jurabk>ImgG</jurabk><enbez>§ 1</enbez>
      </metadaten><textdaten><text><Content>
        <P><IMG SRC="formula.png" alt=""/></P>
      </Content></text></textdaten></norm>
    </dokumente>"""
    document, issues = extract_document_from_xml(write_xml(tmp_path, source))
    asset = next(
        chunk for chunk in document["chunks"] if chunk["chunk_type"] == "source_asset"
    )
    assert asset["text"] == "[Bild: formula.png]"
    assert asset["source_asset_status"] == "missing"
    assert document["metadata"]["missing_source_asset_count"] == 1
    assert any(issue["issue_type"] == "xml_missing_asset" for issue in issues)


def test_adapter_rejects_inline_entity_declarations(tmp_path):
    source = """<!DOCTYPE dokumente [
      <!ENTITY x "expanded">
    ]>
    <dokumente doknr="BJNRXXE"><norm><metadaten>
      <jurabk>&x;</jurabk>
    </metadaten></norm></dokumente>"""
    with pytest.raises(ValueError, match="entity declarations"):
        extract_document_from_xml(write_xml(tmp_path, source))
