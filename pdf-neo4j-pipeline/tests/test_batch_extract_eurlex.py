import sqlite3
from pathlib import Path

from scripts.batch_extract_eurlex_formex import select_external_legal_act_records


def test_external_selection_includes_exact_valid_regulations_and_directives(tmp_path: Path) -> None:
    database = tmp_path / "register.sqlite"
    corpus = tmp_path / "external_documents"
    source_dir = corpus / "documents" / "eu_referenced_originals"
    source_dir.mkdir(parents=True)
    for name in ("32017R0492.pdf", "32019L2115.pdf", "32021R1206.pdf", "32020D0262.pdf"):
        (source_dir / name).write_bytes(b"%PDF-1.4\nfixture")

    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE documents (
              id TEXT PRIMARY KEY, title TEXT, short_title TEXT, corpus_status TEXT
            );
            CREATE TABLE files (
              document_id TEXT, local_path TEXT, media_type TEXT, sha256 TEXT,
              download_url TEXT, final_url TEXT, retrieved_at TEXT,
              validation_status TEXT
            );
            CREATE TABLE graph_references (
              document_id TEXT, resolution_status TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO documents VALUES (?, ?, ?, ?)",
            [
                ("eu_32017r0492", "Test regulation", "32017R0492", "downloaded_original_eu_text"),
                ("eu_32019l2115", "Test directive", "32019L2115", "downloaded_original_eu_text"),
                ("eu_32021r1206", "Candidate regulation", "32021R1206", "metadata_only_eu_text"),
                ("eu_32020d0262", "Test decision", "32020D0262", "downloaded_original_eu_text"),
            ],
        )
        connection.executemany(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "eu_32017r0492",
                    "documents/eu_referenced_originals/32017R0492.pdf",
                    "application/pdf",
                    "a",
                    "https://example.test/32017R0492",
                    None,
                    "2026-01-01T00:00:00Z",
                    "valid",
                ),
                (
                    "eu_32019l2115",
                    "documents/eu_referenced_originals/32019L2115.pdf",
                    "application/pdf",
                    "b",
                    "https://example.test/32019L2115",
                    None,
                    "2026-01-01T00:00:00Z",
                    "valid",
                ),
                (
                    "eu_32021r1206",
                    "documents/eu_referenced_originals/32021R1206.pdf",
                    "application/pdf",
                    "c",
                    "https://example.test/32021R1206",
                    None,
                    "2026-01-01T00:00:00Z",
                    "valid",
                ),
                (
                    "eu_32020d0262",
                    "documents/eu_referenced_originals/32020D0262.pdf",
                    "application/pdf",
                    "d",
                    "https://example.test/32020D0262",
                    None,
                    "2026-01-01T00:00:00Z",
                    "valid",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO graph_references VALUES (?, ?)",
            [
                ("eu_32017r0492", "exact_celex_downloaded"),
                ("eu_32019l2115", "exact_celex_downloaded"),
                ("eu_32021r1206", "candidate_act_type_or_sector_match"),
                ("eu_32020d0262", "exact_celex_downloaded"),
            ],
        )

    records = select_external_legal_act_records(database, corpus, ["R", "L", "D"])

    assert [record["base_celex"] for record in records] == ["32017R0492", "32019L2115"]
    by_celex = {record["base_celex"]: record for record in records}
    assert by_celex["32017R0492"]["legal_value"] == "REG"
    assert by_celex["32019L2115"]["descriptor_label"] == "directive"
    assert by_celex["32019L2115"]["legal_value"] == "DIR"
    assert all(record["manifestation_format"] == "pdf" for record in records)
    assert all(
        record["source_collection"] == "external_referenced_original"
        for record in records
    )
