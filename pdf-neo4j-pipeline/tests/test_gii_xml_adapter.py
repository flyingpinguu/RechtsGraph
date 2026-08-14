import json
import zipfile
from pathlib import Path

import pytest

from normtext_extractor.gii_xml import (
    GII_XML_EXTRACTOR_VERSION,
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


def test_title_footnote_marker_is_not_part_of_document_global_key(tmp_path):
    source = """<?xml version="1.0" encoding="UTF-8"?>
    <dokumente doknr="BJNRFOOTTITLE">
      <norm doknr="BJNRFOOTTITLE">
        <metadaten>
          <jurabk>FootTitleG</jurabk><amtabk>FootTitleG</amtabk>
          <kurzue>Alkoholsteuergesetz<FnR ID="fn-title"/></kurzue>
          <langue>Alkoholsteuergesetz<FnR ID="fn-title"/></langue>
        </metadaten>
        <textdaten><fussnoten><Footnotes>
          <Footnote ID="fn-title" FnZ="2">Titelhinweis.</Footnote>
        </Footnotes></fussnoten></textdaten>
      </norm>
      <norm doknr="BJNRFOOTTITLEP1">
        <metadaten><jurabk>FootTitleG</jurabk><enbez>§ 1</enbez></metadaten>
        <textdaten><text><Content><P>Regelung.</P></Content></text></textdaten>
      </norm>
    </dokumente>
    """

    document, _issues = extract_document_from_xml(write_xml(tmp_path, source))

    assert document["metadata"]["short_title"] == "Alkoholsteuergesetz[2]"
    assert document["document_global_key"] == "alkoholsteuergesetz"
    assert document["structural_units"][0]["global_key"].startswith(
        "alkoholsteuergesetz_"
    )


def test_adapter_merges_horizontal_xml_table_continuations(tmp_path):
    document, _issues = extract_document_from_xml(write_xml(tmp_path))
    tables = [unit for unit in document["structural_units"] if unit["unit_type"] == "table"]
    assert len(tables) == 1
    table = tables[0]
    assert table["label"] == "Tabelle 1"
    assert table["global_key"].endswith("_anlage_19_tabelle_1")
    assert table["columns"] == ["Parameter", "Dim.", "A", "B"]
    assert table["row_count"] == 2
    assert table["table_data"]["merge_mode"] == "horizontal"
    assert table["table_data"]["physical_tables"][1]["rows"][1]["B"] == "2"
    assert table["parent_unit_id"] == next(
        unit["unit_id"]
        for unit in document["structural_units"]
        if unit["label"] == "Anlage 19"
    )


def test_explicit_cals_title_wins_over_unavailable_table_placeholder(tmp_path):
    source = """<dokumente doknr="BJNRCONFLICT">
      <norm doknr="BJNRCONFLICT"><metadaten>
        <jurabk>ConflictV</jurabk><amtabk>ConflictV</amtabk>
        <langue>Tabellenkonfliktverordnung</langue>
      </metadaten></norm>
      <norm doknr="BJNRCONFLICTANNEX"><metadaten>
        <jurabk>ConflictV</jurabk><enbez>Anlage 1</enbez>
      </metadaten><textdaten><text><Content>
        <P>Tabelle 3: Nicht verfügbare Messwerte<BR/>
          ... (Tabelle nicht darstellbar, Fundstelle: BGBl. I 2020, 3)<BR/>
          <table frame="all">
            <Title>Tabelle 4: Faktoren</Title>
            <tgroup cols="2">
              <thead><row><entry>n</entry><entry>Faktor</entry></row></thead>
              <tbody><row><entry>5</entry><entry>1,5</entry></row></tbody>
            </tgroup>
          </table>
        </P>
      </Content></text></textdaten></norm>
    </dokumente>"""

    document, issues = extract_document_from_xml(write_xml(tmp_path, source))

    tables = [
        unit
        for unit in document["structural_units"]
        if unit["unit_type"] == "table"
    ]
    assert [(table["label"], table["title"]) for table in tables] == [
        ("Tabelle 4", "Faktoren")
    ]
    assert tables[0]["columns"] == ["n", "Faktor"]
    assert tables[0]["table_data"]["body_matrix"] == [["5", "1,5"]]
    placeholder = next(
        chunk
        for chunk in document["chunks"]
        if chunk.get("source_xml_unavailable_table")
    )
    assert placeholder["source_xml_table_label"] == "Tabelle 3"
    assert "Tabelle 3: Nicht verfügbare Messwerte" in placeholder["text"]
    assert "Tabelle nicht darstellbar" in placeholder["text"]
    assert not any(issue["severity"] == "error" for issue in issues)


def test_numbered_cals_title_labels_table_without_preceding_cue(tmp_path):
    source = """<dokumente doknr="BJNRTITLE">
      <norm doknr="BJNRTITLE"><metadaten>
        <jurabk>TitleV</jurabk><amtabk>TitleV</amtabk>
        <langue>Tabellentitelverordnung</langue>
      </metadaten></norm>
      <norm doknr="BJNRTITLEANNEX"><metadaten>
        <jurabk>TitleV</jurabk><enbez>Anlage 1</enbez>
      </metadaten><textdaten><text><Content>
        <P>Dieser Text bleibt als eigener Block erhalten.</P>
        <table frame="all">
          <Title>Tabelle 7: Messwerte</Title>
          <tgroup cols="2">
            <thead><row><entry>Parameter</entry><entry>Wert</entry></row></thead>
            <tbody><row><entry>pH</entry><entry>7</entry></row></tbody>
          </tgroup>
        </table>
      </Content></text></textdaten></norm>
    </dokumente>"""

    document, issues = extract_document_from_xml(write_xml(tmp_path, source))

    table = next(
        unit
        for unit in document["structural_units"]
        if unit["unit_type"] == "table"
    )
    assert table["label"] == "Tabelle 7"
    assert table["number"] == "7"
    assert table["title"] == "Messwerte"
    assert any(
        chunk["text"] == "Dieser Text bleibt als eigener Block erhalten."
        for chunk in document["chunks"]
    )
    assert not any(issue["severity"] == "error" for issue in issues)


def test_real_beschussv_keeps_table_3_placeholder_and_labels_table_4():
    corpus_document = (
        Path(__file__).resolve().parents[2]
        / "gesetze_im_internet_xml"
        / "documents"
        / "beschussv-6097b266e41e"
    )
    packages = sorted(corpus_document.glob("*/source.xml.zip"))
    if not packages:
        pytest.skip("downloaded BeschussV XML package is not available")

    document, issues = extract_document_from_xml(packages[-1])

    annex = next(
        unit
        for unit in document["structural_units"]
        if unit["legal_citation"] == "BeschussV Anlage III"
    )
    table_4 = next(
        unit
        for unit in document["structural_units"]
        if unit.get("parent_unit_id") == annex["unit_id"]
        and unit.get("unit_type") == "table"
        and unit.get("label") == "Tabelle 4"
    )
    assert table_4["title"] == "Faktoren zur Berechnung der Anteilsgrenzen"
    assert table_4["row_count"] == 28
    placeholder = next(
        chunk
        for chunk in document["chunks"]
        if chunk.get("unit_id") == annex["unit_id"]
        and chunk.get("source_xml_table_label") == "Tabelle 3"
    )
    assert placeholder["source_xml_unavailable_table"] is True
    assert "Kombination von Druckübertragungsstempeln" in placeholder["text"]
    assert "Tabelle nicht darstellbar" in placeholder["text"]
    assert not any(issue["severity"] == "error" for issue in issues)


def test_real_ogewv_uses_numbered_cals_titles_without_text_cues():
    corpus_document = (
        Path(__file__).resolve().parents[2]
        / "gesetze_im_internet_xml"
        / "documents"
        / "ogewv_2016-6962b7a776ab"
    )
    packages = sorted(corpus_document.glob("*/source.xml.zip"))
    if not packages:
        pytest.skip("downloaded OGewV XML package is not available")

    document, issues = extract_document_from_xml(packages[-1])

    annex_8 = next(
        unit
        for unit in document["structural_units"]
        if unit["legal_citation"] == "OGewV Anlage 8"
    )
    annex_8_tables = [
        unit
        for unit in document["structural_units"]
        if unit.get("parent_unit_id") == annex_8["unit_id"]
        and unit.get("unit_type") == "table"
    ]
    assert [
        (table["label"], table["title"])
        for table in annex_8_tables
    ] == [
        ("Tabelle 1", "Stoffe des chemischen Zustands"),
        ("Tabelle 2", "Umweltqualitätsnormen"),
    ]

    annex_12 = next(
        unit
        for unit in document["structural_units"]
        if unit["legal_citation"] == "OGewV Anlage 12"
    )
    annex_12_tables = [
        unit
        for unit in document["structural_units"]
        if unit.get("parent_unit_id") == annex_12["unit_id"]
        and unit.get("unit_type") == "table"
    ]
    assert [table["label"] for table in annex_12_tables] == [
        "Tabelle 1",
        "Tabelle 2",
        "Tabelle 3",
    ]
    assert not any(issue["severity"] == "error" for issue in issues)


def test_top_level_payload_is_deterministic(tmp_path):
    source = write_xml(tmp_path)
    first = extract_package(source, {"title": "Test source"})
    second = extract_package(source, {"title": "Test source"})
    assert first == second
    assert first["schema_version"] == "1.0.0-draft"
    assert first["extractor"] == {
        "name": "gii_xml",
        "version": GII_XML_EXTRACTOR_VERSION,
    }
    assert (
        first["documents"][0]["metadata"]["gii_xml_extractor_version"]
        == GII_XML_EXTRACTOR_VERSION
    )
    assert first["documents"][0]["page_refs"] == []
    json.dumps(first, ensure_ascii=False)


def test_technical_node_ids_are_document_scoped_without_changing_semantic_keys(
    tmp_path,
):
    first_path = tmp_path / "first.xml"
    second_path = tmp_path / "second.xml"
    first_path.write_text(XML_SAMPLE, encoding="utf-8")
    second_path.write_text(
        XML_SAMPLE.replace("BJNRTEST", "BJNRTESTTWO"),
        encoding="utf-8",
    )

    first, _first_issues = extract_document_from_xml(first_path)
    second, _second_issues = extract_document_from_xml(second_path)
    first_paragraph = next(
        unit for unit in first["structural_units"] if unit["label"] == "§ 1"
    )
    second_paragraph = next(
        unit for unit in second["structural_units"] if unit["label"] == "§ 1"
    )
    first_chunk = next(
        chunk
        for chunk in first["chunks"]
        if chunk["unit_id"] == first_paragraph["unit_id"]
    )
    second_chunk = next(
        chunk
        for chunk in second["chunks"]
        if chunk["unit_id"] == second_paragraph["unit_id"]
    )

    assert first["document_global_key"] == second["document_global_key"]
    assert first_paragraph["global_key"] == second_paragraph["global_key"]
    assert first_chunk["global_key"] == second_chunk["global_key"]
    assert first_paragraph["unit_id"] != second_paragraph["unit_id"]
    assert first_chunk["chunk_id"] != second_chunk["chunk_id"]
    assert first_paragraph["unit_id"] == "unit_{}__{}".format(
        first_paragraph["global_key"],
        first["document_id"],
    )
    assert first_chunk["chunk_id"] == "chunk_{}__{}".format(
        first_chunk["global_key"],
        first["document_id"],
    )
    article = next(
        unit for unit in first["structural_units"] if unit["label"] == "Art 232"
    )
    assert first_paragraph["parent_unit_id"] == article["unit_id"]
    assert first_paragraph["unit_id"] in article["child_unit_ids"]


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


def test_downloader_manifest_provenance_does_not_confuse_xml_with_pdf(tmp_path):
    package = tmp_path / "sample.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("BJNRTEST.xml", XML_SAMPLE)
    document, _issues = extract_document_from_xml(
        package,
        {
            "relative_archive_path": "packages/test/source.zip",
            "relative_file_path": "packages/test/xml/BJNRTEST.xml",
            "xml_files": [
                {"relative_file_path": "packages/test/xml/BJNRTEST.xml"}
            ],
            "pdf_manifest_matches": [
                {"relative_file_path": "T/0001_test.pdf"}
            ],
        },
    )
    assert document["source_pdf"] == "T/0001_test.pdf"
    assert document["source_xml"] == "packages/test/xml/BJNRTEST.xml"
    assert document["source_zip"] == "packages/test/source.zip"


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
          <Footnote ID="fn-1" FnZ="2" Postfix="2)">Erläuterung.</Footnote>
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
    assert subsection["text"].count("[2)]") == 1
    assert "22)" not in subsection["text"]
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


def test_markerless_footnote_ids_are_visible_only_as_explicit_provenance(tmp_path):
    internal_id = "F789197_02_BJNR148310010BJNE000202128"
    source = """<dokumente doknr="BJNRMARKERLESS">
      <norm doknr="BJNRMARKERLESS"><metadaten>
        <jurabk>MarkerG</jurabk><amtabk>MarkerG</amtabk>
        <langue>Markerloses Fußnotengesetz</langue>
      </metadaten></norm>
      <norm doknr="BJNRMARKERLESSP1"><metadaten>
        <jurabk>MarkerG</jurabk><enbez>§ 1</enbez>
      </metadaten><textdaten><text><Content>
        <P>Eine Norm<FnR ID="{internal_id}"/> mit Hinweis.</P>
      </Content><Footnotes>
        <Footnote Group="column" ID="{internal_id}" Pos="exp">
          Amtlicher Hinweis ohne sichtbare Nummer.
        </Footnote>
      </Footnotes></text></textdaten></norm>
    </dokumente>""".format(internal_id=internal_id)

    document, issues = extract_document_from_xml(write_xml(tmp_path, source))
    main_chunk = next(
        chunk
        for chunk in document["chunks"]
        if chunk["chunk_type"] == "provision_text"
    )
    footnote = next(
        chunk
        for chunk in document["chunks"]
        if chunk["chunk_type"] == "footnote"
    )

    assert main_chunk["text"] == "Eine Norm[Fußnote 1] mit Hinweis."
    assert internal_id not in main_chunk["text"]
    assert footnote["label"] == "Fußnote 1"
    assert internal_id not in footnote["global_key"]
    assert internal_id not in footnote["chunk_id"]
    assert footnote["source_xml_footnote_id"] == internal_id
    assert not any(
        issue["issue_type"] in {
            "xml_missing_footnote_definition",
            "xml_orphan_footnote_definition",
        }
        for issue in issues
    )


def test_table_footnote_markers_rekey_rows_and_table_note_sequences(tmp_path):
    internal_id = "F817098_02_01_01_02_BJNR271600021BJNE003000000"
    source = """<dokumente doknr="BJNRTABLEFOOT">
      <norm doknr="BJNRTABLEFOOT"><metadaten>
        <jurabk>TableFootV</jurabk><amtabk>TableFootV</amtabk>
        <langue>Tabellenfußnotenverordnung</langue>
      </metadaten></norm>
      <norm doknr="BJNRTABLEFOOTA1"><metadaten>
        <jurabk>TableFootV</jurabk><enbez>Anlage 1</enbez>
      </metadaten><textdaten><text><Content>
        <P>Tabelle 1:</P>
        <table><tgroup cols="2">
          <thead><row>
            <entry>Stoff</entry>
            <entry>Vorsorgewert<FnR ID="{internal_id}"/></entry>
          </row></thead>
          <tbody><row><entry>Arsen</entry><entry>10</entry></row></tbody>
        </tgroup></table>
      </Content><Footnotes>
        <Footnote ID="{internal_id}" FnZ="2">Tabellenhinweis.</Footnote>
      </Footnotes></text></textdaten></norm>
    </dokumente>""".format(internal_id=internal_id)

    document, issues = extract_document_from_xml(write_xml(tmp_path, source))
    table = next(
        unit
        for unit in document["structural_units"]
        if unit["unit_type"] == "table"
    )
    table_chunks = [
        chunk for chunk in document["chunks"] if chunk["unit_id"] == table["unit_id"]
    ]
    rows_chunk = next(
        chunk for chunk in table_chunks if chunk["chunk_type"] == "table_rows"
    )
    note_chunk = next(
        chunk for chunk in table_chunks if chunk["chunk_type"] == "table_note"
    )

    assert table["columns"] == ["Stoff", "Vorsorgewert[2]"]
    assert table["table_data"]["rows"] == [
        {"Stoff": "Arsen", "Vorsorgewert[2]": "10"}
    ]
    assert rows_chunk["rows"] == table["table_data"]["rows"]
    assert set(rows_chunk["rows"][0]) == set(rows_chunk["columns"])
    assert internal_id not in json.dumps(
        table["table_data"],
        ensure_ascii=False,
        default=str,
    )
    assert table["source_xml_footnote_ids"] == [internal_id]
    assert [chunk["sequence"] for chunk in table_chunks] == [1, 2]
    assert rows_chunk["source_order"] == [1, 1, 0]
    assert note_chunk["source_order"] == [1, 1, 1]
    assert not any(issue["severity"] == "error" for issue in issues)


def test_shared_prose_and_table_footnote_keeps_provision_context(tmp_path):
    shared_id = "fn-shared"
    table_only_id = "fn-table-only"
    source = """<dokumente doknr="BJNRSHARED">
      <norm doknr="BJNRSHARED"><metadaten>
        <jurabk>SharedV</jurabk><amtabk>SharedV</amtabk>
        <langue>Geteilte Fußnotenverordnung</langue>
      </metadaten></norm>
      <norm doknr="BJNRSHAREDA1"><metadaten>
        <jurabk>SharedV</jurabk><enbez>Anlage 1</enbez>
      </metadaten><textdaten><text><Content>
        <P>Der Hinweis gilt im Fließtext<FnR ID="{shared_id}"/>.</P>
        <table><Title>Tabelle 1: Messwerte</Title><tgroup cols="2">
          <thead><row>
            <entry>Wert<FnR ID="{shared_id}"/></entry>
            <entry>Grenze<FnR ID="{table_only_id}"/></entry>
          </row></thead>
          <tbody><row><entry>1</entry><entry>2</entry></row></tbody>
        </tgroup></table>
      </Content><Footnotes>
        <Footnote ID="{shared_id}" FnZ="1">Gemeinsamer Hinweis.</Footnote>
        <Footnote ID="{table_only_id}" FnZ="2">Nur Tabellenhinweis.</Footnote>
      </Footnotes></text></textdaten></norm>
    </dokumente>""".format(
        shared_id=shared_id,
        table_only_id=table_only_id,
    )

    document, issues = extract_document_from_xml(write_xml(tmp_path, source))

    annex = next(
        unit
        for unit in document["structural_units"]
        if unit["legal_citation"] == "SharedV Anlage 1"
    )
    table = next(
        unit
        for unit in document["structural_units"]
        if unit["unit_type"] == "table"
    )
    shared_note = next(
        chunk
        for chunk in document["chunks"]
        if chunk.get("source_xml_footnote_id") == shared_id
    )
    table_note = next(
        chunk
        for chunk in document["chunks"]
        if chunk.get("source_xml_footnote_id") == table_only_id
    )

    assert shared_note["chunk_type"] == "footnote"
    assert shared_note["unit_id"] == annex["unit_id"]
    assert shared_note["legal_citation"] == "SharedV Anlage 1"
    assert shared_note["source_xml_referenced_outside_tables"] is True
    assert shared_note["source_xml_table_contexts"] == [
        {
            "unit_id": table["unit_id"],
            "legal_citation": "SharedV Anlage 1 Tabelle 1",
            "table_index": 1,
        }
    ]
    assert table_note["chunk_type"] == "table_note"
    assert table_note["unit_id"] == table["unit_id"]
    assert table_note["source_xml_referenced_outside_tables"] is False
    assert not any(issue["severity"] == "error" for issue in issues)


def test_real_ogewv_anlage_5_keeps_shared_footnote_under_annex():
    corpus_document = (
        Path(__file__).resolve().parents[2]
        / "gesetze_im_internet_xml"
        / "documents"
        / "ogewv_2016-6962b7a776ab"
    )
    packages = sorted(corpus_document.glob("*/source.xml.zip"))
    if not packages:
        pytest.skip("downloaded OGewV XML package is not available")

    document, issues = extract_document_from_xml(packages[-1])

    annex = next(
        unit
        for unit in document["structural_units"]
        if unit["legal_citation"] == "OGewV Anlage 5"
    )
    shared_note = next(
        chunk
        for chunk in document["chunks"]
        if chunk.get("source_xml_footnote_id")
        == "f793919_08_BJNR137310016BJNE002200000"
    )
    table_only_note = next(
        chunk
        for chunk in document["chunks"]
        if chunk.get("source_xml_footnote_id")
        == "f793919_11_BJNR137310016BJNE002200000"
    )

    assert shared_note["chunk_type"] == "footnote"
    assert shared_note["unit_id"] == annex["unit_id"]
    assert shared_note["legal_citation"] == "OGewV Anlage 5"
    assert shared_note["source_xml_referenced_outside_tables"] is True
    assert len(shared_note["source_xml_table_contexts"]) == 1
    assert shared_note["source_xml_table_contexts"][0]["legal_citation"].startswith(
        "OGewV Anlage 5 "
    )
    assert table_only_note["chunk_type"] == "table_note"
    assert table_only_note["source_xml_referenced_outside_tables"] is False
    assert table_only_note["unit_id"] != annex["unit_id"]
    assert not any(issue["severity"] == "error" for issue in issues)


def test_metadata_only_official_xml_emits_explicit_fallback_unit(tmp_path):
    source = """<dokumente builddate="20260506175142" doknr="BJNRMETA">
      <norm builddate="20260506175142" doknr="BJNRMETA">
        <metadaten>
          <jurabk>MetaBek</jurabk><amtabk>MetaBek</amtabk>
          <ausfertigung-datum manuell="ja">2010-04-29</ausfertigung-datum>
          <fundstelle typ="amtlich">
            <periodikum>BGBl I</periodikum><zitstelle>2010, 534</zitstelle>
          </fundstelle>
          <langue>Bekanntmachung ohne veröffentlichten Normtext</langue>
        </metadaten>
        <textdaten><text format="decorated"/></textdaten>
      </norm>
    </dokumente>"""

    document, issues = extract_document_from_xml(write_xml(tmp_path, source))

    assert document["metadata"]["xml_metadata_only"] is True
    assert document["metadata"]["xml_unit_count"] == 1
    assert document["metadata"]["xml_chunk_count"] == 1
    unit = document["structural_units"][0]
    chunk = document["chunks"][0]
    assert unit["unit_type"] == "document_note"
    assert unit["label"] == "Dokumentmetadaten"
    assert unit["source_xml_fallback_reason"] == "metadata_only_document"
    assert chunk["chunk_type"] == "document_note"
    assert chunk["unit_id"] == unit["unit_id"]
    assert "Bekanntmachung ohne veröffentlichten Normtext" in chunk["text"]
    assert "Ausfertigungsdatum: 2010-04-29" in chunk["text"]
    assert "Amtliche Fundstelle: BGBl I 2010, 534" in chunk["text"]
    assert any(
        issue["issue_type"] == "xml_metadata_only_document"
        and issue["severity"] == "warning"
        for issue in issues
    )
    assert not any(issue["severity"] == "error" for issue in issues)


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


def test_adapter_uses_filename_for_available_asset_with_whitespace_alt(tmp_path):
    source = """<dokumente doknr="BJNRIMG">
      <norm doknr="BJNRIMG"><metadaten>
        <jurabk>ImgG</jurabk><amtabk>ImgG</amtabk><langue>Bildgesetz</langue>
      </metadaten></norm>
      <norm doknr="BJNRIMGP1"><metadaten>
        <jurabk>ImgG</jurabk><enbez>§ 1</enbez>
      </metadaten><textdaten><text><Content>
        <P><IMG SRC="diagram.jpg" alt="  "/></P>
      </Content></text></textdaten></norm>
    </dokumente>"""
    package = tmp_path / "sample.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("sample.xml", source)
        archive.writestr("diagram.jpg", b"binary-image")

    document, issues = extract_document_from_xml(package)

    asset = next(
        chunk for chunk in document["chunks"] if chunk["chunk_type"] == "source_asset"
    )
    assert asset["text"] == "[Bild: diagram.jpg]"
    assert asset["source_asset_status"] == "available"
    assert asset["source_asset_sha256"]
    assert document["metadata"]["source_assets"][0]["title"] == ""
    assert not issues


def test_adapter_rejects_inline_entity_declarations(tmp_path):
    source = """<!DOCTYPE dokumente [
      <!ENTITY x "expanded">
    ]>
    <dokumente doknr="BJNRXXE"><norm><metadaten>
      <jurabk>&x;</jurabk>
    </metadaten></norm></dokumente>"""
    with pytest.raises(ValueError, match="entity declarations"):
        extract_document_from_xml(write_xml(tmp_path, source))


def test_adapter_rejects_utf16_entity_declaration_before_expansion(tmp_path):
    source = """<?xml version="1.0" encoding="UTF-16"?>
    <!DOCTYPE dokumente [
      <!ENTITY secret SYSTEM "file:///definitely-not-readable">
    ]>
    <dokumente doknr="BJNRXXE"><norm><metadaten>
      <jurabk>&secret;</jurabk>
    </metadaten></norm></dokumente>"""
    path = tmp_path / "utf16.xml"
    path.write_bytes(source.encode("utf-16"))

    with pytest.raises(ValueError, match="DTD/entity declarations"):
        extract_document_from_xml(path)


def test_adapter_allows_inert_official_external_doctype_without_loading_it(
    tmp_path,
):
    source = """<?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE dokumente SYSTEM "gii-norm.dtd">
    <dokumente doknr="BJNRDTD">
      <norm doknr="BJNRDTD"><metadaten>
        <jurabk>DTDTestG</jurabk>
        <langue>Gesetz mit offizieller DTD-Referenz</langue>
      </metadaten></norm>
    </dokumente>"""

    document, issues = extract_document_from_xml(write_xml(tmp_path, source))

    assert document["canonical_citation"] == "DTDTestG"
    assert not any(issue["severity"] == "error" for issue in issues)


def test_adapter_rejects_doctype_without_entity_declarations(tmp_path):
    source = """<!DOCTYPE dokumente>
    <dokumente doknr="BJNRDTD"><norm><metadaten>
      <jurabk>DTD</jurabk>
    </metadaten></norm></dokumente>"""

    with pytest.raises(ValueError, match="DTD/entity declarations"):
        extract_document_from_xml(write_xml(tmp_path, source))
