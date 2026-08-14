#!/usr/bin/env python3
"""Rechunk only oversized structured tables in an existing GII XML corpus.

The XML adapter already stores lossless row dictionaries and header matrices in
each table StructuralUnit.  This migration therefore does not parse XML or PDFs
again.  It rewrites only raw JSON documents containing a table above the token
limit and updates their batch-manifest checksums atomically.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Sequence, Tuple

from normtext_extractor.table_chunking import (
    DEFAULT_ENCODING,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TARGET_TOKENS,
    split_table_rows,
)


TOOL_NAME = "rechunk_gii_xml_tables"
TOOL_VERSION = "1.0.0"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name), suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _chunk_id(global_key: str, document_id: str) -> str:
    return "chunk_{}__{}".format(global_key, document_id)


def _part_chunk(
    template: Mapping[str, Any],
    table: Mapping[str, Any],
    part: Mapping[str, Any],
    part_index: int,
    document_id: str,
) -> Dict[str, Any]:
    table_global_key = str(table["global_key"])
    global_key = "{}_rows_part_{:04d}".format(table_global_key, part_index)
    row_start = int(part["row_start"])
    row_end = int(part["row_end"])
    chunk = dict(template)
    chunk.update(
        {
            "chunk_id": _chunk_id(global_key, document_id),
            "global_key": global_key,
            "legal_citation": table.get("legal_citation"),
            "display_name": "{} Zeilen {}–{}".format(
                table.get("legal_citation") or "Tabelle", row_start, row_end
            ),
            "chunk_type": "table_rows",
            "unit_id": table["unit_id"],
            "parent_chunk_id": None,
            "child_chunk_ids": [],
            "label": "rows {}-{}".format(row_start, row_end),
            "number": None,
            "sequence": part_index,
            "source_order": [
                int(table.get("source_xml_norm_index") or 0),
                int(table.get("source_xml_table_index") or 0),
                part_index - 1,
            ],
            "columns": part["columns"],
            "column_header_text": part["column_header_text"],
            "header_matrix": part["header_matrix"],
            "table_title": part["table_title"],
            "table_part_index": part_index,
            "table_part_count": part["part_count"],
            "table_chunk_token_count": part["token_count"],
            "table_chunk_token_encoding": part["token_encoding"],
            "table_chunk_max_tokens": part["max_tokens"],
            "oversized_atomic_row": part["oversized_atomic_row"],
            "rows": part["rows"],
            "text": part["text"],
            "text_sha256": sha256_text(str(part["text"])),
            "row_range": {"start": row_start, "end": row_end},
        }
    )
    chunk.pop("table_data", None)
    return chunk


def rechunk_document(
    document: MutableMapping[str, Any],
    *,
    encoding_name: str,
    max_tokens: int,
    target_tokens: int,
) -> Dict[str, int]:
    units = document.get("structural_units") or []
    chunks = document.get("chunks") or []
    document_id = str(document.get("document_id") or "")
    table_units = {
        str(unit.get("unit_id")): unit
        for unit in units
        if isinstance(unit, dict) and unit.get("unit_type") == "table"
    }
    changed_tables = 0
    old_row_chunks = 0
    new_row_chunks = 0
    oversized_atomic_rows = 0

    for unit_id, table in table_units.items():
        table_data = table.get("table_data") or {}
        rows = table_data.get("rows") or []
        columns = table.get("columns") or table_data.get("columns") or []
        existing_indexes = [
            index
            for index, chunk in enumerate(chunks)
            if isinstance(chunk, dict)
            and chunk.get("unit_id") == unit_id
            and chunk.get("chunk_type") == "table_rows"
        ]
        if not existing_indexes:
            continue
        parts = split_table_rows(
            table_citation=str(table.get("legal_citation") or ""),
            table_title=str(table.get("title") or ""),
            columns=columns,
            rows=rows,
            header_matrix=table_data.get("header_matrix") or [],
            encoding_name=encoding_name,
            max_tokens=max_tokens,
            target_tokens=target_tokens,
        )
        if len(parts) == 1 and not parts[0]["was_split"]:
            continue

        template = chunks[existing_indexes[0]]
        replacements = [
            _part_chunk(template, table, part, part_index, document_id)
            for part_index, part in enumerate(parts, 1)
        ]
        existing_row_chunks = [chunks[index] for index in existing_indexes]
        if existing_row_chunks == replacements:
            continue
        first_index = existing_indexes[0]
        existing_index_set = set(existing_indexes)
        chunks = [
            chunk
            for index, chunk in enumerate(chunks)
            if index not in existing_index_set
        ]
        chunks[first_index:first_index] = replacements

        # Table-only notes follow all row parts in the same StructuralUnit.
        table_notes = sorted(
            (
                chunk
                for chunk in chunks
                if isinstance(chunk, dict)
                and chunk.get("unit_id") == unit_id
                and chunk.get("chunk_type") == "table_note"
            ),
            key=lambda chunk: (chunk.get("sequence") or 0, chunk.get("chunk_id") or ""),
        )
        for note_offset, note in enumerate(table_notes, len(replacements) + 1):
            note["sequence"] = note_offset
            note["source_order"] = [
                int(table.get("source_xml_norm_index") or 0),
                int(table.get("source_xml_table_index") or 0),
                note_offset - 1,
            ]

        changed_tables += 1
        old_row_chunks += len(existing_indexes)
        new_row_chunks += len(replacements)
        oversized_atomic_rows += sum(
            1 for part in parts if part.get("oversized_atomic_row")
        )

    if changed_tables:
        document["chunks"] = chunks
    return {
        "changed_tables": changed_tables,
        "old_row_chunks": old_row_chunks,
        "new_row_chunks": new_row_chunks,
        "oversized_atomic_rows": oversized_atomic_rows,
    }


def migrate_batch(
    batch_dir: Path,
    *,
    encoding_name: str,
    max_tokens: int,
    target_tokens: int,
    dry_run: bool,
) -> Dict[str, Any]:
    manifest_path = batch_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("entries") or []
    totals = {
        "documents_scanned": 0,
        "documents_changed": 0,
        "tables_changed": 0,
        "old_row_chunks": 0,
        "new_row_chunks": 0,
        "oversized_atomic_rows": 0,
    }
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            continue
        output_path = batch_dir / str(entry["output_path"])
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        documents = payload.get("documents") or []
        totals["documents_scanned"] += len(documents)
        document_changed = False
        for document in documents:
            result = rechunk_document(
                document,
                encoding_name=encoding_name,
                max_tokens=max_tokens,
                target_tokens=target_tokens,
            )
            if result["changed_tables"]:
                document_changed = True
            totals["tables_changed"] += result["changed_tables"]
            totals["old_row_chunks"] += result["old_row_chunks"]
            totals["new_row_chunks"] += result["new_row_chunks"]
            totals["oversized_atomic_rows"] += result["oversized_atomic_rows"]
        if not document_changed:
            continue
        totals["documents_changed"] += 1
        if dry_run:
            continue
        payload["table_chunking"] = {
            "tool": TOOL_NAME,
            "version": TOOL_VERSION,
            "encoding": encoding_name,
            "max_tokens": max_tokens,
            "target_tokens": target_tokens,
        }
        atomic_write_json(output_path, payload)
        entry["chunks"] = sum(
            len(document.get("chunks") or []) for document in documents
        )
        entry["output_bytes"] = output_path.stat().st_size
        entry["output_sha256"] = sha256_file(output_path)
        entry["table_chunking"] = payload["table_chunking"]

    report = {
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "updated_at": utc_now(),
        "batch_dir": str(batch_dir.resolve()),
        "encoding": encoding_name,
        "max_tokens": max_tokens,
        "target_tokens": target_tokens,
        "dry_run": dry_run,
        **totals,
    }
    if not dry_run:
        manifest["updated_at"] = report["updated_at"]
        manifest["table_chunking"] = report
        atomic_write_json(manifest_path, manifest)
        atomic_write_json(batch_dir / "table_chunking_report.json", report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", required=True, type=Path)
    parser.add_argument("--encoding", default=DEFAULT_ENCODING)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--target-tokens", type=int, default=DEFAULT_TARGET_TOKENS)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock_path = args.batch_dir / ".rechunk_gii_xml_tables.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = migrate_batch(
            args.batch_dir,
            encoding_name=args.encoding,
            max_tokens=args.max_tokens,
            target_tokens=args.target_tokens,
            dry_run=args.dry_run,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
