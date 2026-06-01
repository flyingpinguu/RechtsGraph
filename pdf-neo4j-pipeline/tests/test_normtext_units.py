import inspect

from normtext_extractor.pipeline import (
    column_bounds_from_centers,
    is_material_class_header_row,
    is_material_header_continuation,
    material_table_stop_row,
    parse_geometric_material_table,
    parse_material_panel_rows,
    match_annex_heading,
    match_para_heading,
    slugify,
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
