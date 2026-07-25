from xml.etree import ElementTree as ET

from normtext_extractor.gii_xml_tables import (
    element_text,
    merge_continuation_tables,
    parse_cals_table,
)


def parse_table(source):
    return parse_cals_table(ET.fromstring(source))


def test_mixed_inline_text_preserves_breaks_and_footnote_references():
    element = ET.fromstring(
        '<P>Vor <B>fett</B><BR/>nach<FnR ID="fn-1"/>.</P>'
    )
    assert element_text(element) == "Vor fett\nnach[fn-1]."


def test_visual_footnote_area_does_not_duplicate_reference():
    element = ET.fromstring(
        '<P>Begriff<FnR ID="fn-1"/>.'
        '<FnArea><FnR ID="fn-1"/></FnArea></P>'
    )
    assert element_text(element) == "Begriff[fn-1]."


def test_cals_parser_expands_column_and_row_spans_losslessly():
    table = parse_table(
        """
        <table frame="all">
          <tgroup cols="4">
            <colspec colname="c1"/>
            <colspec colname="c2"/>
            <colspec colname="c3"/>
            <colspec colname="c4"/>
            <thead>
              <row>
                <entry namest="c1" nameend="c2">Merkmal</entry>
                <entry namest="c3" nameend="c4">Klassen</entry>
              </row>
              <row>
                <entry>Parameter</entry>
                <entry>Einheit</entry>
                <entry>A</entry>
                <entry>B</entry>
              </row>
            </thead>
            <tbody>
              <row>
                <entry morerows="1">pH</entry>
                <entry>–</entry>
                <entry>6</entry>
                <entry>7</entry>
              </row>
              <row>
                <entry>mg/l</entry>
                <entry>8</entry>
                <entry>9</entry>
              </row>
            </tbody>
          </tgroup>
        </table>
        """
    )

    assert table["columns"] == [
        "Merkmal / Parameter",
        "Merkmal / Einheit",
        "Klassen / A",
        "Klassen / B",
    ]
    assert table["body_matrix"] == [
        ["pH", "–", "6", "7"],
        ["pH", "mg/l", "8", "9"],
    ]
    spanning = next(cell for cell in table["cells"] if cell["text"] == "pH")
    assert spanning["row_span"] == 2
    assert spanning["row_start"] == 2
    assert spanning["row_end"] == 3
    assert next(cell for cell in table["cells"] if cell["text"] == "Klassen")[
        "col_span"
    ] == 2


def test_spanspec_and_explicit_column_placement_are_respected():
    table = parse_table(
        """
        <table>
          <tgroup cols="3">
            <colspec colname="left" colnum="1"/>
            <colspec colname="middle" colnum="2"/>
            <colspec colname="right" colnum="3"/>
            <spanspec spanname="tail" namest="middle" nameend="right"/>
            <tbody>
              <row>
                <entry colname="left">A</entry>
                <entry spanname="tail">B</entry>
              </row>
            </tbody>
          </tgroup>
        </table>
        """
    )
    assert table["body_matrix"] == [["A", "B", "B"]]
    assert table["cells"][-1]["col_start"] == 1
    assert table["cells"][-1]["col_end"] == 2


def test_horizontal_continuation_merges_repeated_row_keys():
    left = parse_table(
        """
        <table><tgroup cols="3">
          <thead><row><entry>Parameter</entry><entry>Dim.</entry><entry>A</entry></row></thead>
          <tbody>
            <row><entry>pH</entry><entry>-</entry><entry>6</entry></row>
            <row><entry>Blei</entry><entry>mg/l</entry><entry>1</entry></row>
          </tbody>
        </tgroup></table>
        """
    )
    right = parse_table(
        """
        <table><tgroup cols="3">
          <thead><row><entry>Parameter</entry><entry>Dim.</entry><entry>B</entry></row></thead>
          <tbody>
            <row><entry>pH</entry><entry>-</entry><entry>7</entry></row>
            <row><entry>Blei</entry><entry>mg/l</entry><entry>2</entry></row>
          </tbody>
        </tgroup></table>
        """
    )
    merged = merge_continuation_tables(left, right)

    assert merged["merge_mode"] == "horizontal"
    assert merged["continuation_key_columns"] == 2
    assert merged["columns"] == ["Parameter", "Dim.", "A", "B"]
    assert merged["rows"][1] == {
        "Parameter": "Blei",
        "Dim.": "mg/l",
        "A": "1",
        "B": "2",
    }
    assert len(merged["physical_tables"]) == 2


def test_vertical_continuation_appends_rows_for_identical_columns():
    first = parse_table(
        """
        <table><tgroup cols="2">
          <thead><row><entry>Nr.</entry><entry>Wert</entry></row></thead>
          <tbody><row><entry>1</entry><entry>A</entry></row></tbody>
        </tgroup></table>
        """
    )
    second = parse_table(
        """
        <table><tgroup cols="2">
          <thead><row><entry>Nr.</entry><entry>Wert</entry></row></thead>
          <tbody><row><entry>2</entry><entry>B</entry></row></tbody>
        </tgroup></table>
        """
    )
    merged = merge_continuation_tables(first, second)
    assert merged["merge_mode"] == "vertical"
    assert [row["Nr."] for row in merged["rows"]] == ["1", "2"]


def test_table_without_thead_keeps_first_body_row_as_data():
    table = parse_table(
        """
        <table><tgroup cols="2">
          <tbody>
            <row><entry>erste Datenzelle</entry><entry>1</entry></row>
            <row><entry>zweite Datenzelle</entry><entry>2</entry></row>
          </tbody>
        </tgroup></table>
        """
    )
    assert table["columns"] == ["column_1", "column_2"]
    assert table["rows"] == [
        {"column_1": "erste Datenzelle", "column_2": "1"},
        {"column_1": "zweite Datenzelle", "column_2": "2"},
    ]


def test_incompatible_multiple_tgroups_are_retained_losslessly():
    table = parse_table(
        """
        <table>
          <tgroup cols="2">
            <thead><row><entry>A</entry><entry>B</entry></row></thead>
            <tbody><row><entry>1</entry><entry>2</entry></row></tbody>
          </tgroup>
          <tgroup cols="3">
            <thead><row><entry>C</entry><entry>D</entry><entry>E</entry></row></thead>
            <tbody><row><entry>3</entry><entry>4</entry><entry>5</entry></row></tbody>
          </tgroup>
        </table>
        """
    )
    assert table["multi_tgroup_layout"] is True
    assert table["unmerged_tgroup_indexes"] == [1]
    assert len(table["tgroups"]) == 2
    assert {cell["tgroup_index"] for cell in table["cells"]} == {0, 1}
    assert table["tgroups"][1]["rows"][0]["E"] == "5"


def test_nested_tables_and_cell_assets_remain_structured():
    table = parse_table(
        """
        <table><tgroup cols="1"><tbody><row><entry>
          Formel <IMG SRC="formula.png" alt=""/>
          <table><tgroup cols="1"><tbody>
            <row><entry>innerer Wert</entry></row>
          </tbody></tgroup></table>
        </entry></row></tbody></tgroup></table>
        """
    )
    cell = table["cells"][0]
    assert cell["asset_refs"][0]["source"] == "formula.png"
    assert cell["nested_tables"][0]["rows"] == [{"column_1": "innerer Wert"}]
