import inspect

from normtext_extractor.pipeline import (
    column_bounds_from_centers,
    collect_multiline_table_header,
    annex_looks_like_form,
    detect_waste_codes,
    grid_key_column_count,
    detect_row_header_label,
    infer_table_columns,
    is_material_class_header_row,
    is_material_header_continuation,
    is_table_note_start,
    is_implicit_annex_table_header,
    is_table_section_header,
    looks_like_table_header_line,
    material_table_stop_row,
    match_table_heading,
    parse_table_block,
    parse_geometric_material_table,
    parse_material_panel_rows,
    match_annex_heading,
    match_para_heading,
    slugify,
    split_annex_into_chunks,
    split_into_subsections,
    sorted_unique_positions,
)
from normtext_extractor.table_parsers import (
    TableParseContext,
    TextFallbackTableParser,
    default_table_parser_registry,
)


def test_heading_detection_rejects_reference_fragments():
    assert match_para_heading("§ 13 Satzung oder sonstige Regelung")
    assert not match_para_heading("§ 17 der Ersatzbaustoffverordnung nachzuweisen.")
    assert not match_para_heading("§ 12 Satz 1")
    assert not match_annex_heading("Anlage 3 zu entsprechen.")
    assert match_annex_heading("Anhang 1 Anforderungen an den Standort")
    assert match_annex_heading("Anlage (zu § 4 Satz 1)")
    assert not match_annex_heading("Anhang 1")
    assert not match_annex_heading("Anhang 4 Nr. 3.2.20 Kursivdruck")
    assert not match_annex_heading("Anhang 5 Nummer 4 Ziffer 2 Abfälle nicht besprengt")
    assert not match_annex_heading("Anlage 2 Abschnitt 3 festgelegten Untersuchungsverfahren enthalten.")
    assert not match_annex_heading("Anhang III der Richtlinie 2008/98/EG.")
    assert not match_annex_heading("Anhang 36 Teil C Abs. 4;")
    assert not match_annex_heading("Anlage 1 AuslZuschlV)")
    assert not match_annex_heading("Anhang 53 (Fotografische Prozesse) ohne Ansäuern und ohne")
    assert match_annex_heading("Anhang 53 Fotografische Prozesse (Silberhalogenid-Fotografie)")
    assert match_table_heading("Tabelle 1")
    assert match_table_heading("Tabelle 1: Aufbau des Oberflächenabdichtungssystems")
    assert not match_table_heading("Tabelle 1 Nummer 2 bis 4 zu erreichen.")
    assert not match_table_heading("Tabelle 2 zu errichten.")


def test_subsection_split_rejects_inline_numbered_values():
    text = "\n".join(
        [
            "§ 9 Bewertung",
            "(1) Für die Bewertung sind folgende Noten zu verwenden:",
            '"sehr gut" (1) = eine hervorragende Leistung,',
            '"gut" (2) = eine Leistung über Durchschnitt,',
            "(2) Die Note errechnet sich aus dem Durchschnitt.",
        ]
    )
    subsections = split_into_subsections(text)
    assert [num for num, _text in subsections] == [1, 2]
    assert '"sehr gut" (1)' in subsections[0][1]


def test_table_heading_lists_stay_annex_text():
    chunks = split_annex_into_chunks(
        {
            "lines": [
                "Anlage (zu § 2 Absatz 1)",
                "Kapitel 4 Tabellen",
                "Tabelle 1 Brücken",
                "Tabelle 2 Tunnel",
                "Tabelle 3 Trogbauwerke",
                "Tabelle 4 Stützbauwerke",
                "1.1 Bauliche Anlagen",
            ],
            "line_pages": [0] * 7,
        }
    )
    assert [chunk["chunk_type"] for chunk in chunks] == ["annex_text"]


def test_implicit_table_detection_rejects_annex_section_and_intro_lines():
    assert not is_implicit_annex_table_header(
        [
            "Teil A - Zeugnis ohne Prüfungsergebnisse:",
            "1. Bezeichnung der ausstellenden Behörde,",
            "2. Name und Geburtsdatum der zu prüfenden Person,",
            "3. Datum des Bestehens der Prüfung,",
        ],
        0,
    )
    assert not is_implicit_annex_table_header(
        [
            "B Allgemeine Anforderungen",
            "Es werden keine über § 3 hinausgehenden Anforderungen gestellt.",
            "C Anforderungen an das Abwasser für die Einleitungsstelle",
            "Bereich 1 Bereich 2",
            "Abfiltrierbare Stoffe 100 100",
            "Chemischer Sauerstoffbedarf (CSB) - 150",
        ],
        0,
    )
    assert not is_implicit_annex_table_header(
        [
            "Gewässer die folgenden Anforderungen:",
            "Bereiche 1 2 3 4",
            "TOC kg/t 7,0 7,0 17 0,70",
            "CSB kg/t 20 20 50 2,0",
        ],
        0,
    )
    assert is_implicit_annex_table_header(
        [
            "Bereiche 1 2 3 4",
            "TOC kg/t 7,0 7,0 17 0,70",
            "CSB kg/t 20 20 50 2,0",
        ],
        0,
    )
    assert not is_implicit_annex_table_header(
        [
            "Das Abwasser darf nur eingeleitet werden, soweit Prozesswasser aus der Prozess- und",
            "Abluftbehandlung mechanisch-aerobbiologischer Behandlungsanlagen nicht prozessintern vollständig genutzt",
            "werden kann. Für diesen Fall gelten die Anforderungen nach Teil C und D.",
        ],
        1,
    )
    assert not is_implicit_annex_table_header(
        [
            "8. Vermeidung von hochmolekularen, wasserlöslichen Trennmitteln, die einen DOC-",
            "Dioxine und Furane als Summe der einzelnen, nach Anhang",
            "VI Teil 2 der Richtlinie 2010/75/EU berechneten Dioxine und",
            "Furane",
            "ng/l 0,3",
        ],
        1,
    )
    assert is_implicit_annex_table_header(
        [
            "In diesem Fall gelten Teil C und D und zusätzlich folgende Anforderungen:",
            "Fracht in Milligramm je Tonne Abfall",
            "Cadmium 15",
            "Quecksilber 9",
            "Chrom, gesamt 150",
        ],
        1,
    )


def test_implicit_table_blocks_end_at_norm_text_boundaries():
    chunks = split_annex_into_chunks(
        {
            "lines": [
                "Anhang 43 Beispiel",
                "Bereiche 1 2 3 4",
                "TOC kg/t 7,0 7,0 17 0,70",
                "CSB kg/t 20 20 50 2,0",
                "(2) Die produktionsspezifischen Frachtwerte beziehen sich auf die Kapazität.",
            ],
            "line_pages": [0] * 5,
        }
    )
    assert [chunk["chunk_type"] for chunk in chunks] == ["annex_text", "table_block", "annex_text"]
    assert chunks[1]["text"].startswith("Bereiche 1 2 3 4")
    assert chunks[2]["text"].startswith("(2)")


def test_waste_code_detection_rejects_range_references():
    assert detect_waste_codes("16 02 09* Transformatoren und Kondensatoren, die PCB enthalten")
    assert not detect_waste_codes("16 02 09 bis 16 02 12 fallen")


def test_geometry_helpers_are_stable():
    assert slugify("Bundes-Bodenschutz- und Altlastenverordnung") == (
        "bundes_bodenschutz_und_altlastenverordnung"
    )
    assert sorted_unique_positions([10.0, 10.4, 20.0, 31.0], tolerance=1.0) == [10.2, 20.0, 31.0]
    assert column_bounds_from_centers([20.0, 40.0, 80.0]) == [10.0, 30.0, 60.0, 100.0]


def test_table_parser_registry_has_text_fallback():
    table = {"label": "Tabelle 1", "columns": ["A"], "rows": [["x"]]}
    context = TableParseContext(table=table, block={})
    assert TextFallbackTableParser().parse(context) == table
    parsed = default_table_parser_registry().parse_first(context)
    assert parsed["label"] == "Tabelle 1"
    assert parsed["parser_name"] == "text_fallback"


def test_grid_tables_precede_multi_panel_fallback():
    parser_names = [parser.name for parser in default_table_parser_registry().parsers]
    assert parser_names.index("grid") < parser_names.index("multi_panel_continuation")
    assert grid_key_column_count(["Parameter", "Analysemethode(n)"]) == 1
    assert grid_key_column_count(["Nr.", "Stoff", "Grenzwert"]) == 2


def test_form_annexes_do_not_start_implicit_tables():
    form_lines = [
        "1.1 Klärschlammerzeuger (Name, Anschrift): . . . . .",
        "1.2 Angaben zur vorgesehenen Klärschlammverwertung",
        "Am . . . . . werde ich aus meiner Anlage",
        "# abgeben. # aufbringen/einbringen,",
        "1.3 Name, Anschrift: . . . . .",
        "1.4 Bodenbezogene Angaben",
        "1.4.1 Aufbringung erfolgt zu folgender Kultur: . . . . .",
        "1.4.2 Bodenart: . . . . .",
        "1.4.3 Untersuchungsstelle: . . . . .",
        "1.4.4 Datum der Probennahme: . . . . .",
        "1.4.5 Ergebnisse der Bodenuntersuchung",
        "# nicht ergeben.",
        "# ergeben.",
    ] * 2
    assert annex_looks_like_form(form_lines)
    assert not annex_looks_like_form(["Parameter Analysemethode(n)", "pH-Wert DIN EN 15933"] * 3)


def test_multi_panel_parser_core_avoids_document_specific_keywords():
    source = "\n".join(
        inspect.getsource(obj)
        for obj in (
            is_material_class_header_row,
            is_material_header_continuation,
            material_table_stop_row,
            parse_material_panel_rows,
            parse_geometric_material_table,
        )
    )
    forbidden = (
        "Materialwerte",
        "RC-",
        "HOS-",
        "SWS-",
        "HMVA-",
        "GKOS",
        "Anorganische Stoffe",
        "Organische Stoffe",
        "Stoffspezifischer",
        "In Gebieten",
    )
    assert not any(keyword in source for keyword in forbidden)


def test_table_parsing_core_has_no_known_content_keywords():
    source = "\n".join(
        inspect.getsource(obj)
        for obj in (
            looks_like_table_header_line,
            collect_multiline_table_header,
            infer_table_columns,
            is_table_section_header,
            is_table_note_start,
            detect_row_header_label,
            parse_table_block,
        )
    )
    forbidden = (
        "pH-Wert",
        "Cyanid",
        "Blei",
        "PAK",
        "DIN",
        "ISO",
        "HPLC",
        "AAS",
        "Untersuchungsparameter",
        "Parameter Dimension",
        "Überschreitung",
        "Anorganische Stoffe",
        "Organische Stoffe",
        "Einbauweise",
    )
    assert not any(keyword in source for keyword in forbidden)
