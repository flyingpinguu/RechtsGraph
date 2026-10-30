from scripts.build_external_document_inventory import (
    celex_from_target_key,
    load_gii_documents,
    target_category,
)


def test_celex_from_year_number_style_key() -> None:
    assert celex_from_target_key("verordnung_eu_2016_679") == "32016R0679"


def test_celex_from_number_year_style_key_with_nr() -> None:
    assert celex_from_target_key("verordnung_eg_nr_1013_2006") == "32006R1013"
    assert celex_from_target_key("verordnung_eu_nr_1308_2013") == "32013R1308"


def test_celex_from_uniform_numbering_style_even_when_nr_is_present() -> None:
    assert celex_from_target_key("verordnung_eu_nr_2015_1963") == "32015R1963"


def test_celex_from_suffix_region_style_key() -> None:
    assert celex_from_target_key("richtlinie_2005_36_eg") == "32005L0036"
    assert celex_from_target_key("entscheidung_2006_860_eg") == "32006D0860"
    assert celex_from_target_key("richtlinie_1929_2013_eu") == "32013L0029"


def test_celex_from_short_year_expanded_by_reference_parser() -> None:
    assert celex_from_target_key("verordnung_eg_nr_1008_1908") == "32008R1008"
    assert celex_from_target_key("verordnung_eg_nr_2027_1997") == "31997R2027"
    assert celex_from_target_key("verordnung_eu_nr_1024_1913") == "32013R1024"


def test_cellar_inventory_disambiguates_short_numbers_and_wrong_act_type() -> None:
    cellar = {"32015R0035", "32021L1206"}
    assert target_category("verordnung_eu_nr_2015_1935", cellar) == (
        "eu_already_in_cellar",
        "32015R0035",
    )
    assert target_category("verordnung_eu_2021_1206", cellar) == (
        "eu_already_in_cellar",
        "32021L1206",
    )


def test_uniform_number_above_1900_is_not_treated_as_a_short_year() -> None:
    assert celex_from_target_key("verordnung_eu_nr_2022_1925") == "32022R1925"


def test_exact_unique_german_document_is_classified_as_core_internal() -> None:
    documents = {"einigungsvertrag": [{"graph_id": "doc_example"}]}
    assert target_category("einigungsvertrag", set(), documents) == (
        "de_already_in_gii_corpus",
        None,
    )


def test_unique_normalized_german_title_alias_is_classified_as_core_internal(tmp_path) -> None:
    path = tmp_path / "documents.csv"
    path.write_text(
        "graph_id,document_global_key,document_key,global_key,title,short_title\n"
        "doc_example,testg,testg,testg,Gesetz über die Finanzverwaltung,FVG\n",
        encoding="utf-8",
    )
    documents = load_gii_documents(path)
    assert target_category("gesetz_ueber_die_finanzverwaltung", set(), documents) == (
        "de_already_in_gii_corpus",
        None,
    )


def test_genitive_german_title_alias_is_classified_as_core_internal(tmp_path) -> None:
    path = tmp_path / "documents.csv"
    path.write_text(
        "graph_id,document_global_key,document_key,global_key,title,short_title\n"
        "doc_example,drig,drig,drig,Deutsches Richtergesetz,DRiG\n",
        encoding="utf-8",
    )
    documents = load_gii_documents(path)

    assert target_category("deutschen_richtergesetz", set(), documents) == (
        "de_already_in_gii_corpus",
        None,
    )
