import json

import normtext_extractor.gii_xml as gii_xml
from normtext_extractor.table_chunking import (
    compact_row_text,
    split_table_rows,
    table_rows_text,
)
from scripts.rechunk_gii_xml_tables import rechunk_document


def test_small_table_keeps_legacy_text_and_single_part():
    columns = ["Stoff", "Wert"]
    rows = [{"Stoff": "Arsen", "Wert": "10"}]

    parts = split_table_rows(
        table_citation="TestV Anlage 1 Tabelle 1",
        table_title="Messwerte",
        columns=columns,
        rows=rows,
        max_tokens=3500,
        target_tokens=3200,
    )

    assert len(parts) == 1
    assert parts[0]["was_split"] is False
    assert parts[0]["text"] == table_rows_text(columns, rows)
    assert parts[0]["row_start"] == 1
    assert parts[0]["row_end"] == 1


def test_large_table_parts_repeat_title_and_columns_without_losing_rows():
    columns = ["Kennung", "Beschreibung"]
    rows = [
        {
            "Kennung": str(index),
            "Beschreibung": "Beschreibung {} ".format(index) + "lang " * 25,
        }
        for index in range(1, 16)
    ]

    parts = split_table_rows(
        table_citation="TestV Anlage 1 Tabelle 1",
        table_title="Messwerte",
        columns=columns,
        rows=rows,
        header_matrix=[["Kennung", "Beschreibung"]],
        max_tokens=110,
        target_tokens=90,
    )

    assert len(parts) > 1
    assert [row for part in parts for row in part["rows"]] == rows
    assert [part["part_index"] for part in parts] == list(range(1, len(parts) + 1))
    assert all(part["part_count"] == len(parts) for part in parts)
    assert all(part["token_count"] <= 110 for part in parts)
    assert all(
        part["text"].startswith(
            "Tabelle: TestV Anlage 1 Tabelle 1 — Messwerte\n"
            "Spaltenüberschriften:\nKennung | Beschreibung\n"
        )
        for part in parts
    )
    assert [(part["row_start"], part["row_end"]) for part in parts] == [
        (part["row_start"], part["row_start"] + len(part["rows"]) - 1)
        for part in parts
    ]


def test_single_oversized_row_is_kept_and_marked():
    parts = split_table_rows(
        table_citation="TestV Tabelle 1",
        table_title="",
        columns=["Inhalt"],
        rows=[{"Inhalt": "sehrlang " * 200}],
        max_tokens=40,
        target_tokens=30,
    )

    assert len(parts) == 1
    assert parts[0]["was_split"] is True
    assert parts[0]["oversized_atomic_row"] is True
    assert parts[0]["rows"] == [{"Inhalt": "sehrlang " * 200}]


def test_split_text_compacts_expanded_colspans_but_keeps_structured_values():
    repeated = "gemeinsamer Inhalt " * 40
    rows = [{"A": repeated, "B": repeated, "C": "anderer Inhalt"}]

    assert compact_row_text(["A", "B", "C"], rows[0]) == (
        repeated + " | ↳ | anderer Inhalt"
    )
    parts = split_table_rows(
        table_citation="TestV Tabelle 1",
        table_title="",
        columns=["A", "B", "C"],
        rows=rows * 10,
        max_tokens=200,
        target_tokens=160,
    )

    assert len(parts) > 1
    assert [row for part in parts for row in part["rows"]] == rows * 10
    assert all(" | ↳ | " in part["text"] for part in parts)


def test_xml_parser_uses_same_chunking_for_future_full_parses(tmp_path, monkeypatch):
    body_rows = "".join(
        "<row><entry>{}</entry><entry>{}</entry></row>".format(
            index,
            "langer Tabellenwert " * 6,
        )
        for index in range(1, 11)
    )
    source = """<dokumente doknr="BJNRCHUNKTEST">
      <norm doknr="BJNRCHUNKTEST"><metadaten>
        <jurabk>ChunkTestV</jurabk><amtabk>ChunkTestV</amtabk>
        <langue>Tabellenchunk-Testverordnung</langue>
      </metadaten></norm>
      <norm doknr="BJNRCHUNKTESTA1"><metadaten>
        <jurabk>ChunkTestV</jurabk><enbez>Anlage 1</enbez>
      </metadaten><textdaten><text><Content>
        <P>Tabelle 1: Messwerte</P>
        <table><tgroup cols="2">
          <thead><row><entry>Kennung</entry><entry>Beschreibung</entry></row></thead>
          <tbody>{}</tbody>
        </tgroup></table>
      </Content></text></textdaten></norm>
    </dokumente>""".format(body_rows)
    source_path = tmp_path / "source.xml"
    source_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(gii_xml, "TABLE_CHUNK_MAX_TOKENS", 100)
    monkeypatch.setattr(gii_xml, "TABLE_CHUNK_TARGET_TOKENS", 80)

    document, issues = gii_xml.extract_document_from_xml(str(source_path))
    table = next(
        unit for unit in document["structural_units"] if unit["unit_type"] == "table"
    )
    chunks = [
        chunk
        for chunk in document["chunks"]
        if chunk["unit_id"] == table["unit_id"]
        and chunk["chunk_type"] == "table_rows"
    ]

    assert len(chunks) > 1
    assert [row for chunk in chunks for row in chunk["rows"]] == table["table_data"]["rows"]
    assert all("_rows_part_" in chunk["global_key"] for chunk in chunks)
    assert [chunk["sequence"] for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert all(chunk["table_chunk_token_count"] <= 100 for chunk in chunks)
    assert all("Tabelle:" in chunk["text"] for chunk in chunks)
    assert all("Spaltenüberschriften:" in chunk["text"] for chunk in chunks)
    assert not any(issue["severity"] == "error" for issue in issues)

    # The result must remain JSON-serializable for the batch extraction contract.
    json.dumps(document, ensure_ascii=False)


def test_existing_corpus_rechunking_is_idempotent(tmp_path, monkeypatch):
    # Reuse the parser fixture above through a compact in-memory raw document.
    rows = [
        {"A": str(index), "B": "langer Wert " * 30}
        for index in range(1, 8)
    ]
    table = {
        "unit_id": "unit_table",
        "global_key": "testv_anlage_1_tabelle_1",
        "legal_citation": "TestV Anlage 1 Tabelle 1",
        "unit_type": "table",
        "title": "Messwerte",
        "source_xml_norm_index": 1,
        "source_xml_table_index": 1,
        "columns": ["A", "B"],
        "table_data": {
            "columns": ["A", "B"],
            "header_matrix": [["A", "B"]],
            "rows": rows,
        },
    }
    document = {
        "document_id": "doc_test",
        "structural_units": [table],
        "chunks": [
            {
                "chunk_id": "chunk_testv_anlage_1_tabelle_1_rows__doc_test",
                "global_key": "testv_anlage_1_tabelle_1_rows",
                "unit_id": "unit_table",
                "chunk_type": "table_rows",
                "sequence": 1,
                "source_order": [1, 1, 0],
                "columns": ["A", "B"],
                "rows": rows,
                "text": table_rows_text(["A", "B"], rows),
                "text_sha256": "legacy",
                "table_data": table["table_data"],
            }
        ],
    }

    first = rechunk_document(
        document,
        encoding_name="cl100k_base",
        max_tokens=120,
        target_tokens=100,
    )
    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True)
    second = rechunk_document(
        document,
        encoding_name="cl100k_base",
        max_tokens=120,
        target_tokens=100,
    )

    assert first["changed_tables"] == 1
    assert second["changed_tables"] == 0
    assert json.dumps(document, ensure_ascii=False, sort_keys=True) == serialized
