"""Deterministic, header-preserving chunking for structured table rows."""

from __future__ import annotations

import copy
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Sequence

import tiktoken


DEFAULT_ENCODING = "cl100k_base"
DEFAULT_MAX_TOKENS = 3500
DEFAULT_TARGET_TOKENS = 3200


@lru_cache(maxsize=None)
def _encoding(name: str):
    return tiktoken.get_encoding(name)


def token_count(text: str, encoding_name: str = DEFAULT_ENCODING) -> int:
    """Return the exact token count for ``text`` under ``encoding_name``."""

    return len(_encoding(encoding_name).encode(text or ""))


def table_header_text(columns: Sequence[str]) -> str:
    return " | ".join(str(column or "") for column in columns)


def table_rows_text(
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
) -> str:
    """Render rows in the stable pipe-delimited representation used by the corpus."""

    lines = [table_header_text(columns)]
    lines.extend(
        " | ".join(str(row.get(column) or "") for column in columns)
        for row in rows
    )
    return "\n".join(lines).strip()


def compact_row_text(columns: Sequence[str], row: Mapping[str, Any]) -> str:
    """Render an expanded CALS row without repeating horizontally spanned text.

    CALS colspan expansion intentionally copies a cell value into every covered
    logical column.  Repeating a long value many times wastes LLM context.  The
    arrow explicitly means "same value as the cell to the left" while the exact
    expanded mapping remains available in the structured ``rows`` metadata.
    """

    rendered: List[str] = []
    previous: Any = object()
    for column in columns:
        value = str(row.get(column) or "")
        rendered.append("↳" if value and value == previous else value)
        previous = value
    return " | ".join(rendered)


def table_part_text(
    table_citation: str,
    table_title: str,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    row_start: int,
    row_end: int,
) -> str:
    """Render one retrievable table part with self-contained table context."""

    heading = "Tabelle: {}".format(table_citation.strip())
    if table_title.strip():
        heading += " — {}".format(table_title.strip())
    return "\n".join(
        [
            heading,
            "Spaltenüberschriften:",
            table_header_text(columns),
            "Zeilen {}–{}:".format(row_start, row_end),
            *(
                compact_row_text(columns, row)
                for row in rows
            ),
        ]
    ).strip()


def split_table_rows(
    *,
    table_citation: str,
    table_title: str,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    header_matrix: Sequence[Sequence[str]] | None = None,
    encoding_name: str = DEFAULT_ENCODING,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
) -> List[Dict[str, Any]]:
    """Return one unchanged part or row-aligned, header-preserving table parts.

    Only tables whose existing corpus representation exceeds ``max_tokens`` are
    split.  This deliberately keeps all existing small-table text and IDs stable.
    Every split part repeats the table citation/title and normalized column path.
    A single body row is atomic; if that row alone exceeds the hard limit, it is
    retained and explicitly marked for review rather than losing cell structure.
    """

    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if target_tokens <= 0 or target_tokens > max_tokens:
        raise ValueError("target_tokens must be positive and at most max_tokens")

    copied_rows = [dict(row) for row in rows]
    existing_text = table_rows_text(columns, copied_rows)
    existing_tokens = token_count(existing_text, encoding_name)
    common = {
        "columns": list(columns),
        "column_header_text": table_header_text(columns),
        "header_matrix": copy.deepcopy(list(header_matrix or [])),
        "table_title": table_title or None,
        "token_encoding": encoding_name,
        "max_tokens": max_tokens,
    }
    if existing_tokens <= max_tokens or not copied_rows:
        return [
            {
                **common,
                "rows": copied_rows,
                "text": existing_text,
                "row_start": 1 if copied_rows else None,
                "row_end": len(copied_rows) if copied_rows else None,
                "token_count": existing_tokens,
                "was_split": False,
                "oversized_atomic_row": False,
            }
        ]

    parts: List[Dict[str, Any]] = []
    cursor = 0
    while cursor < len(copied_rows):
        row_start = cursor + 1
        part_rows: List[Dict[str, Any]] = []
        chosen_text = ""
        chosen_tokens = 0
        while cursor < len(copied_rows):
            candidate_rows = part_rows + [copied_rows[cursor]]
            candidate_text = table_part_text(
                table_citation,
                table_title,
                columns,
                candidate_rows,
                row_start,
                cursor + 1,
            )
            candidate_tokens = token_count(candidate_text, encoding_name)
            if part_rows and candidate_tokens > target_tokens:
                break
            part_rows = candidate_rows
            chosen_text = candidate_text
            chosen_tokens = candidate_tokens
            cursor += 1
            if chosen_tokens >= target_tokens:
                break

        # A target below the hard limit normally supplies ample headroom.  Keep
        # this guard so different encodings/configurations cannot violate it.
        while len(part_rows) > 1 and chosen_tokens > max_tokens:
            cursor -= 1
            part_rows.pop()
            chosen_text = table_part_text(
                table_citation,
                table_title,
                columns,
                part_rows,
                row_start,
                cursor,
            )
            chosen_tokens = token_count(chosen_text, encoding_name)

        parts.append(
            {
                **common,
                "rows": part_rows,
                "text": chosen_text,
                "row_start": row_start,
                "row_end": cursor,
                "token_count": chosen_tokens,
                "was_split": True,
                "oversized_atomic_row": chosen_tokens > max_tokens,
            }
        )

    part_count = len(parts)
    for part_index, part in enumerate(parts, 1):
        part["part_index"] = part_index
        part["part_count"] = part_count
    return parts
