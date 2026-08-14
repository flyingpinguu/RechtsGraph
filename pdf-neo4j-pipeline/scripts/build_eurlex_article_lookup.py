#!/usr/bin/env python3
"""Build a compact CELEX -> article -> paragraph -> chunk lookup."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXTRACTION_DIR = PROJECT_ROOT / "output" / "eurlex_formex_rl"


def slugify(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(path.name), suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extraction-dir", type=Path, default=DEFAULT_EXTRACTION_DIR)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = args.output or args.extraction_dir / "lookup" / "article_lookup.json"
    manifest = json.loads((args.extraction_dir / "manifest.json").read_text(encoding="utf-8"))
    documents: Dict[str, Any] = {}
    aliases: Dict[str, str] = {}
    ambiguous_aliases = set()
    chunk_count = 0
    article_count = 0

    for result in manifest.get("results") or []:
        if result.get("status") not in {"ok", "skipped_existing"}:
            continue
        raw_path = args.extraction_dir / result["output_path"]
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        document = payload["documents"][0]
        metadata = document.get("metadata") or {}
        base_celex = str(metadata.get("base_celex") or document.get("document_key"))
        units = document.get("structural_units") or []
        chunks = document.get("chunks") or []
        chunks_by_unit: Dict[str, list] = {}
        for chunk in chunks:
            chunks_by_unit.setdefault(str(chunk.get("unit_id")), []).append(chunk)
        article_units = [unit for unit in units if unit.get("unit_type") == "article"]
        annex_units = [unit for unit in units if unit.get("unit_type") == "annex"]
        article_map: Dict[str, Any] = {}
        for unit in article_units:
            number = str(unit.get("number") or "")
            if not number:
                continue
            unit_chunks = sorted(
                chunks_by_unit.get(str(unit.get("unit_id")), []),
                key=lambda chunk: (int(chunk.get("sequence") or 0), str(chunk.get("chunk_id"))),
            )
            paragraphs: Dict[str, list] = {}
            unnumbered = []
            for chunk in unit_chunks:
                if chunk.get("chunk_type") == "subsection":
                    paragraphs.setdefault(str(chunk.get("number")), []).append(chunk["chunk_id"])
                else:
                    unnumbered.append(chunk["chunk_id"])
                chunk_count += 1
            article_map[number] = {
                "unit_id": unit["unit_id"],
                "global_key": unit.get("global_key"),
                "legal_citation": unit.get("legal_citation"),
                "paragraphs": paragraphs,
                "unnumbered_chunks": unnumbered,
            }
            article_count += 1
        annex_map = {
            str(unit.get("number")): {
                "unit_id": unit.get("unit_id"),
                "global_key": unit.get("global_key"),
                "legal_citation": unit.get("legal_citation"),
                "chunk_ids": [
                    chunk["chunk_id"] for chunk in chunks_by_unit.get(str(unit.get("unit_id")), [])
                ],
            }
            for unit in annex_units
        }
        documents[base_celex] = {
            "document_id": document.get("document_id"),
            "document_global_key": document.get("document_global_key"),
            "consolidated_celex": metadata.get("consolidated_celex"),
            "consolidation_date": metadata.get("consolidation_date"),
            "in_force": metadata.get("in_force"),
            "title": document.get("title"),
            "raw_path": result["output_path"],
            "articles": article_map,
            "annexes": annex_map,
        }
        for alias in [base_celex, metadata.get("consolidated_celex"), *(metadata.get("citation_aliases") or [])]:
            key = slugify(alias)
            if not key:
                continue
            previous = aliases.get(key)
            if previous and previous != base_celex:
                ambiguous_aliases.add(key)
            else:
                aliases[key] = base_celex

    for key in ambiguous_aliases:
        aliases.pop(key, None)
    lookup = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_manifest": str(args.extraction_dir / "manifest.json"),
        "counts": {
            "documents": len(documents),
            "articles": article_count,
            "article_chunks": chunk_count,
            "aliases": len(aliases),
            "ambiguous_aliases_omitted": len(ambiguous_aliases),
        },
        "aliases": dict(sorted(aliases.items())),
        "documents": documents,
    }
    atomic_write_json(output, lookup)
    print(json.dumps(lookup["counts"], ensure_ascii=False, indent=2))
    print(output)


if __name__ == "__main__":
    main()
