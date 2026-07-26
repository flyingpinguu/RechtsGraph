import hashlib
import importlib.util
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "audit_gii_xml_corpus.py"
SPEC = importlib.util.spec_from_file_location("audit_gii_xml_corpus", SCRIPT_PATH)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


def unit(unit_id, unit_type, label, **updates):
    value = {
        "unit_id": unit_id,
        "global_key": unit_id.removeprefix("unit_"),
        "unit_type": unit_type,
        "label": label,
        "title": None,
        "text": label,
        "parent_unit_id": None,
        "child_unit_ids": [],
        "page_range": None,
    }
    value.update(updates)
    return value


def document(document_id, key, abbreviation, units, chunks=None, **metadata):
    return {
        "document_id": document_id,
        "document_key": key,
        "document_global_key": key,
        "abbreviation": abbreviation,
        "canonical_citation": abbreviation,
        "structural_units": units,
        "chunks": chunks or [],
        "page_refs": [],
        "metadata": {
            "source_format": "gii_xml",
            "pdf_alignment_status": "unavailable",
            "pdf_pages": 0,
            "source_asset_count": 0,
            "missing_source_asset_count": 0,
            **metadata,
        },
    }


def payload(doc, slug, issues=None):
    return {
        "schema_version": "1.0.0-draft",
        "source_manifest_entry": {"slug": slug},
        "documents": [doc],
        "extraction_issues": issues or [],
    }


def write_batch(tmp_path, payloads, extra_entries=None):
    batch_dir = tmp_path / "batch"
    entries = []
    for index, (slug, value) in enumerate(payloads):
        output_path = batch_dir / "raw" / slug[:1].upper() / (
            "{}_raw.json".format(slug)
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        output_path.write_bytes(raw)
        entries.append(
            {
                "item_id": "{}-id".format(slug),
                "slug": slug,
                "status": "ok",
                "output_path": output_path.relative_to(batch_dir).as_posix(),
                "output_bytes": len(raw),
                "output_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    entries.extend(extra_entries or [])
    status_counts = {}
    for entry in entries:
        status = entry["status"]
        status_counts[status] = status_counts.get(status, 0) + 1
    manifest = {
        "schema_version": "1.0",
        "run": {"selected_entries": len(entries)},
        "status_counts": status_counts,
        "errors": [
            {
                "item_id": entry.get("item_id"),
                "error_type": entry.get("error_type"),
                "error": entry.get("error"),
            }
            for entry in entries
            if entry["status"] == "error"
        ],
        "entries": entries,
    }
    (batch_dir / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return batch_dir


def clean_hard_case_payloads():
    ersatz_columns = ["C{}".format(index) for index in range(20)]
    ersatz_matrix = [
        ["" for _column in ersatz_columns]
        for _index in range(18)
    ]
    ersatz_annex = unit(
        "unit_ersatz_anlage_1",
        "annex",
        "Anlage 1",
        child_unit_ids=["unit_ersatz_anlage_1_tabelle_1"],
    )
    ersatz_table = unit(
        "unit_ersatz_anlage_1_tabelle_1",
        "table",
        "Tabelle 1",
        parent_unit_id=ersatz_annex["unit_id"],
        columns=ersatz_columns,
        row_count=18,
        parser_name="gii_xml_cals",
        table_data={
            "num_columns": len(ersatz_columns),
            "body_matrix": ersatz_matrix,
            "rows": [
                {
                    column: row[index]
                    for index, column in enumerate(ersatz_columns)
                }
                for row in ersatz_matrix
            ]
        },
    )
    ersatz = payload(
        document(
            "doc_ersatz",
            "ersatzbaustoffv",
            "ErsatzbaustoffV",
            [ersatz_annex, ersatz_table],
        ),
        "ersatzbaustoffv",
    )

    article = unit(
        "unit_egbgb_art_232",
        "article",
        "Art 232",
        child_unit_ids=["unit_egbgb_art_232_para_1"],
    )
    paragraph = unit(
        "unit_egbgb_art_232_para_1",
        "paragraph",
        "§ 1",
        parent_unit_id=article["unit_id"],
    )
    egbgb = payload(
        document("doc_egbgb", "bgbeg", "BGBEG", [article, paragraph]),
        "bgbeg",
    )

    part_four = unit(
        "unit_abf_part_4",
        "division",
        "Teil 4",
        is_uncertain=True,
    )
    part_five = unit(
        "unit_abf_part_5",
        "division",
        "Teil 5",
        is_uncertain=True,
    )
    abf_issues = [
        {
            "issue_id": "issue_part_4",
            "unit_id": part_four["unit_id"],
            "issue_type": "xml_hierarchy_rank_conflict",
            "severity": "warning",
            "evidence": {"label": "Teil 4"},
        },
        {
            "issue_id": "issue_part_5",
            "unit_id": part_five["unit_id"],
            "issue_type": "xml_hierarchy_rank_conflict",
            "severity": "warning",
            "evidence": {"label": "Teil 5"},
        },
    ]
    abf = payload(
        document(
            "doc_abf",
            "abfklaerv",
            "AbfKlärV",
            [part_four, part_five],
        ),
        "abfkl_rv_2017",
        abf_issues,
    )
    return [
        ("ersatzbaustoffv", ersatz),
        ("bgbeg", egbgb),
        ("abfkl_rv_2017", abf),
    ]


def test_clean_audit_reports_counts_hard_cases_and_nonfatal_alignment_absence(
    tmp_path,
):
    batch_dir = write_batch(tmp_path, clean_hard_case_payloads())

    report = audit.audit_corpus(batch_dir, require_hard_cases=True)

    assert report["audit_status"] == "passed"
    assert report["failures"] == []
    assert report["totals"]["documents"] == 3
    assert report["totals"]["structural_units"] == 6
    assert report["tables"]["by_shape"] == {"20x18": 1}
    assert report["alignment"]["documents_by_status"] == {"unavailable": 3}
    assert report["alignment"]["units"]["coverage"] == 0.0
    assert report["alignment"]["absence_is_failure"] is False
    assert {
        value["status"] for value in report["hard_cases"].values()
    } == {"passed"}

    markdown = audit.render_markdown(report)
    assert "# GII XML corpus audit" in markdown
    assert "Alignment absence" in markdown
    assert "EGBGB Art 232 hierarchy" in markdown
    assert "None." in markdown


def test_integrity_failures_cover_duplicates_footnote_leaks_assets_and_zero_units(
    tmp_path,
):
    visible_chunk = {
        "chunk_id": "chunk_visible",
        "unit_id": "unit_one",
        "chunk_type": "provision_text",
        "text": "Visible text leaked fn-technical into the result.",
        "page_range": None,
    }
    definition_chunk = {
        "chunk_id": "chunk_footnote",
        "unit_id": "unit_one",
        "chunk_type": "footnote",
        "text": "Footnote definition.",
        "source_xml_footnote_id": "fn-technical",
        "page_range": None,
    }
    missing_asset_chunk = {
        "chunk_id": "chunk_asset",
        "unit_id": "unit_one",
        "chunk_type": "source_asset",
        "text": "[Bild: absent.png]",
        "source_asset": "absent.png",
        "source_asset_status": "missing",
        "page_range": None,
    }
    first = payload(
        document(
            "doc_duplicate",
            "one",
            "OneG",
            [unit("unit_one", "paragraph", "§ 1")],
            [visible_chunk, definition_chunk, missing_asset_chunk],
            source_asset_count=1,
            missing_source_asset_count=1,
        ),
        "one",
    )
    second = payload(
        document("doc_duplicate", "two", "TwoG", [], []),
        "two",
    )
    batch_dir = write_batch(tmp_path, [("one", first), ("two", second)])

    report = audit.audit_corpus(batch_dir)

    assert report["audit_status"] == "failed"
    assert report["integrity"]["duplicate_document_ids"]["count"] == 1
    assert report["integrity"]["internal_footnote_leaks"]["count"] == 1
    assert report["integrity"]["missing_assets"]["count"] == 1
    assert report["integrity"]["zero_unit_documents"]["count"] == 1
    assert all(
        case["status"] == "not_present"
        for case in report["hard_cases"].values()
    )
    assert not any(
        finding["code"] == "hard_case_missing"
        for finding in report["failures"]
    )


def test_audit_treats_table_row_keys_as_visible_and_checks_chunk_sequences(
    tmp_path,
):
    internal_id = "F817098_02_01_01_02_BJNR271600021BJNE003000000"
    visible_column = "Vorsorgewert[2]"
    leaked_column = "Vorsorgewert[{}]".format(internal_id)
    table = unit(
        "unit_table",
        "table",
        "Tabelle 1",
        columns=[visible_column],
        row_count=1,
        parser_name="gii_xml_cals",
        table_data={"body_matrix": [["10"]], "rows": [{leaked_column: "10"}]},
    )
    rows_chunk = {
        "chunk_id": "chunk_rows",
        "unit_id": table["unit_id"],
        "chunk_type": "table_rows",
        "sequence": 1,
        "columns": [visible_column],
        "rows": [{leaked_column: "10"}],
        "text": "10",
        "source_xml_footnote_ids": [internal_id],
        "page_range": None,
    }
    note_chunk = {
        "chunk_id": "chunk_note",
        "unit_id": table["unit_id"],
        "chunk_type": "table_note",
        "sequence": 1,
        "text": "Tabellenhinweis.",
        "source_xml_footnote_id": internal_id,
        "page_range": None,
    }
    batch_dir = write_batch(
        tmp_path,
        [
            (
                "tableg",
                payload(
                    document(
                        "doc_table",
                        "tableg",
                        "TableG",
                        [table],
                        [rows_chunk, note_chunk],
                    ),
                    "tableg",
                ),
            )
        ],
    )

    report = audit.audit_corpus(batch_dir)

    assert report["integrity"]["internal_footnote_leaks"]["count"] == 1
    leak = report["integrity"]["internal_footnote_leaks"]["examples"][0]
    assert leak["field"] == "rows"
    assert leak["text_preview"] == leaked_column
    assert report["integrity"]["invalid_table_shapes"]["count"] == 1
    assert report["integrity"]["duplicate_chunk_sequences"]["count"] == 1


def test_ersatzbaustoffv_hard_case_rejects_malformed_lossless_matrix(
    tmp_path,
):
    rows = clean_hard_case_payloads()
    table = next(
        item
        for item in rows[0][1]["documents"][0]["structural_units"]
        if item["unit_type"] == "table"
    )
    table["table_data"]["body_matrix"][3] = table["table_data"][
        "body_matrix"
    ][3][:-1]

    batch_dir = write_batch(tmp_path, rows)
    report = audit.audit_corpus(batch_dir, require_hard_cases=True)

    hard_case = report["hard_cases"]["ersatzbaustoffv_table_1"]
    assert hard_case["status"] == "failed"
    assert hard_case["observed"]["columns"] == 20
    assert hard_case["observed"]["rows"] == 18
    assert hard_case["observed"]["body_widths"] == [19, 20]
    assert "1 body_matrix rows differ from column width" in (
        hard_case["observed"]["shape_errors"]
    )
    assert report["integrity"]["invalid_table_shapes"]["count"] == 1
    assert "hard_case_ersatzbaustoffv" in {
        finding["code"] for finding in report["failures"]
    }


def test_bad_hard_case_contracts_and_batch_error_fail_cli(tmp_path):
    rows = clean_hard_case_payloads()
    ersatz_doc = rows[0][1]["documents"][0]
    table = next(
        item
        for item in ersatz_doc["structural_units"]
        if item["unit_type"] == "table"
    )
    table["columns"] = table["columns"][:-1]
    table["table_data"]["rows"] = table["table_data"]["rows"][:-1]
    table["row_count"] = 17

    egbgb_doc = rows[1][1]["documents"][0]
    paragraph = next(
        item
        for item in egbgb_doc["structural_units"]
        if item["unit_type"] == "paragraph"
    )
    paragraph["parent_unit_id"] = None

    rows[2][1]["extraction_issues"] = rows[2][1]["extraction_issues"][:1]
    batch_dir = write_batch(
        tmp_path,
        rows,
        extra_entries=[
            {
                "item_id": "broken-source",
                "status": "error",
                "error_type": "ValueError",
                "error": "broken XML",
            }
        ],
    )
    json_out = tmp_path / "audit.json"
    markdown_out = tmp_path / "audit.md"

    return_code = audit.main(
        [
            "--batch-dir",
            str(batch_dir),
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
            "--require-hard-cases",
        ]
    )

    assert return_code == 1
    report = json.loads(json_out.read_text(encoding="utf-8"))
    failure_codes = {finding["code"] for finding in report["failures"]}
    assert {
        "batch_errors",
        "hard_case_ersatzbaustoffv",
        "hard_case_egbgb",
        "hard_case_abfklaerv",
    }.issubset(failure_codes)
    assert markdown_out.read_text(encoding="utf-8").startswith(
        "# GII XML corpus audit"
    )
