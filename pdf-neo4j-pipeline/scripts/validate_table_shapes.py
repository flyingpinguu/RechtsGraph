#!/usr/bin/env python3
"""Validate extracted table shapes in a raw normtext JSON file."""

import argparse
import json
import re
import sys
from typing import Any, Dict, Iterable, List


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def iter_table_chunks(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for document in payload.get("documents", []):
        for chunk in document.get("chunks", []):
            if chunk.get("chunk_type") == "table_rows":
                yield chunk


def table_rows(chunk: Dict[str, Any]) -> List[Any]:
    rows = chunk.get("structured_rows")
    if rows is None:
        rows = chunk.get("rows")
    return rows or []


def row_marker(row: Any) -> str:
    if isinstance(row, dict):
        for key in ("Einbauweise Nummer", "Nummer", "Parameter"):
            if key in row:
                return str(row.get(key) or "")
        if row:
            first_key = next(iter(row))
            return str(row.get(first_key) or "")
    if isinstance(row, list) and row:
        return str(row[0] or "")
    if isinstance(row, str):
        return row.split(maxsplit=1)[0] if row.strip() else ""
    return ""


def citation(chunk: Dict[str, Any]) -> str:
    return chunk.get("legal_citation") or chunk.get("global_key") or "<unknown>"


def validate(args: argparse.Namespace) -> int:
    payload = load_json(args.input)
    chunks = [
        chunk for chunk in iter_table_chunks(payload)
        if args.contains is None or args.contains in citation(chunk)
    ]
    failures = []

    print("tables\t{}".format(len(chunks)))
    for chunk in chunks:
        rows = table_rows(chunk)
        columns = chunk.get("columns") or []
        markers = [row_marker(row) for row in rows]
        summary = "{}\tcolumns={}\trows={}".format(citation(chunk), len(columns), len(rows))
        if markers:
            summary += "\tfirst={}\tlast={}".format(markers[0], markers[-1])
        print(summary)

        if args.expect_columns is not None and len(columns) != args.expect_columns:
            failures.append(
                "{}: expected {} columns, got {}".format(
                    citation(chunk), args.expect_columns, len(columns)
                )
            )
        if args.expect_rows is not None and len(rows) != args.expect_rows:
            failures.append(
                "{}: expected {} rows, got {}".format(
                    citation(chunk), args.expect_rows, len(rows)
                )
            )
        if args.row_marker_regex:
            marker_re = re.compile(args.row_marker_regex)
            bad_markers = [marker for marker in markers if not marker_re.match(marker)]
            if bad_markers:
                failures.append(
                    "{}: row markers failed regex {}: {}".format(
                        citation(chunk), args.row_marker_regex, ", ".join(bad_markers[:5])
                    )
                )

    if failures:
        print("\nFAIL", file=sys.stderr)
        for failure in failures:
            print("- {}".format(failure), file=sys.stderr)
        return 1

    print("OK")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Raw normtext JSON file")
    parser.add_argument("--contains", help="Only validate tables whose citation contains this text")
    parser.add_argument("--expect-columns", type=int)
    parser.add_argument("--expect-rows", type=int)
    parser.add_argument("--row-marker-regex")
    return parser.parse_args()


def main() -> None:
    raise SystemExit(validate(parse_args()))


if __name__ == "__main__":
    main()
