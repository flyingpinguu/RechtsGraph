"""Lossless CALS-table parsing and logical continuation merging for GII XML."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET


WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")


def normalize_inline_text(value: str) -> str:
    """Normalize inline whitespace without removing intentional line breaks."""
    lines = []
    for line in (value or "").replace("\u00a0", " ").splitlines():
        lines.append(WHITESPACE_RE.sub(" ", line).strip())
    return "\n".join(line for line in lines if line).strip()


def element_text(element: ET.Element) -> str:
    """Render mixed GII XML inline content to deterministic plain text."""
    parts: List[str] = []

    def visit(node: ET.Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            tag = child.tag
            if tag == "BR":
                parts.append("\n")
            elif tag == "DT":
                visit(child)
                parts.append(" ")
            elif tag == "DD":
                visit(child)
                parts.append("\n")
            elif tag == "P":
                visit(child)
                parts.append("\n")
            elif tag == "QuoteL":
                parts.append("\u201e")
            elif tag == "QuoteR":
                parts.append("\u201c")
            elif tag == "IMG":
                description = child.get("alt") or child.get("title") or ""
                source = child.get("SRC") or ""
                parts.append(description or "[Bild: {}]".format(source or "ohne Quelle"))
            elif tag == "FILE":
                description = child.get("title") or child.get("SRC") or ""
                parts.append(description or "[Datei]")
            elif tag == "FnArea":
                # FnArea repeats one or more references next to the visual
                # footnote separator.  The meaningful reference already
                # appears at its source position and must not be duplicated.
                pass
            elif tag == "FnR":
                reference = child.get("ID") or ""
                if reference:
                    parts.append("[{}]".format(reference))
            else:
                visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(element)
    return normalize_inline_text("".join(parts))


def _positive_int(value: Optional[str], default: int) -> int:
    try:
        parsed = int(value or "")
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _nonnegative_int(value: Optional[str], default: int = 0) -> int:
    try:
        parsed = int(value or "")
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _colspecs(tgroup: ET.Element, num_columns: int) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    specs: List[Dict[str, Any]] = []
    name_to_index: Dict[str, int] = {}
    next_index = 0
    for colspec in tgroup.findall("./colspec"):
        declared = _positive_int(colspec.get("colnum"), next_index + 1) - 1
        index = max(next_index, declared)
        name = colspec.get("colname") or "col{}".format(index + 1)
        spec = {
            "index": index,
            "name": name,
            "width": colspec.get("colwidth"),
            "align": colspec.get("align"),
            "attributes": dict(colspec.attrib),
        }
        specs.append(spec)
        name_to_index[name] = index
        next_index = index + 1
    for index in range(num_columns):
        default_name = "col{}".format(index + 1)
        name_to_index.setdefault(default_name, index)
        if not any(spec["index"] == index for spec in specs):
            specs.append(
                {
                    "index": index,
                    "name": default_name,
                    "width": None,
                    "align": None,
                    "attributes": {},
                }
            )
    specs.sort(key=lambda item: item["index"])
    return specs, name_to_index


def _spanspecs(tgroup: ET.Element) -> Dict[str, Dict[str, str]]:
    spans: Dict[str, Dict[str, str]] = {}
    for spanspec in tgroup.findall("./spanspec"):
        name = spanspec.get("spanname")
        if not name:
            continue
        spans[name] = {
            "namest": spanspec.get("namest") or "",
            "nameend": spanspec.get("nameend") or "",
        }
    return spans


def _entry_span(
    entry: ET.Element,
    name_to_index: Dict[str, int],
    spanspecs: Dict[str, Dict[str, str]],
    cursor: int,
    occupied: Iterable[int],
    num_columns: int,
) -> Tuple[int, int]:
    occupied_set = set(occupied)
    span = spanspecs.get(entry.get("spanname") or "", {})
    start_name = entry.get("namest") or span.get("namest") or entry.get("colname")
    end_name = entry.get("nameend") or span.get("nameend")
    if start_name and start_name in name_to_index:
        start = name_to_index[start_name]
    else:
        start = max(cursor, 0)
        while start in occupied_set and start < num_columns:
            start += 1
    if end_name and end_name in name_to_index:
        end = name_to_index[end_name]
    else:
        end = start
    start = min(max(start, 0), max(num_columns - 1, 0))
    end = min(max(end, start), max(num_columns - 1, 0))
    return start, end


def _parse_rows(
    rows: List[ET.Element],
    section: str,
    num_columns: int,
    name_to_index: Dict[str, int],
    spanspecs: Dict[str, Dict[str, str]],
    row_offset: int,
) -> Dict[str, Any]:
    expanded_matrix: List[List[str]] = []
    origin_matrix: List[List[str]] = []
    cells: List[Dict[str, Any]] = []
    active_spans: Dict[int, Dict[str, Any]] = {}

    for local_row, row_element in enumerate(rows):
        row_index = row_offset + local_row
        expanded = [""] * num_columns
        origins = [""] * num_columns
        occupied = set()
        expired = []
        for column, span in active_spans.items():
            if span["row_end"] < row_index:
                expired.append(column)
                continue
            expanded[column] = span["text"]
            occupied.add(column)
        for column in expired:
            active_spans.pop(column, None)

        cursor = 0
        for entry in row_element.findall("./entry"):
            start, end = _entry_span(
                entry,
                name_to_index,
                spanspecs,
                cursor,
                occupied,
                num_columns,
            )
            text = element_text(entry)
            row_span = _nonnegative_int(entry.get("morerows")) + 1
            row_end = row_index + row_span - 1
            cell_index = len(cells)
            cell = {
                "cell_index": cell_index,
                "text": text,
                "section": section,
                "row_start": row_index,
                "row_end": row_end,
                "row_span": row_span,
                "col_start": start,
                "col_end": end,
                "col_span": end - start + 1,
                "is_header": section == "thead",
                "attributes": dict(entry.attrib),
                "asset_refs": [
                    {
                        "kind": asset.tag.lower(),
                        "source": asset.get("SRC"),
                        "preview": asset.get("PREVIEW"),
                        "title": asset.get("title") or asset.get("alt"),
                        "attributes": dict(asset.attrib),
                    }
                    for asset in entry.iter()
                    if asset is not entry and asset.tag in {"IMG", "FILE"}
                ],
                "nested_tables": _direct_nested_tables(entry),
            }
            cells.append(cell)
            if start < num_columns:
                origins[start] = text
            for column in range(start, min(end + 1, num_columns)):
                expanded[column] = text
                occupied.add(column)
                if row_span > 1:
                    active_spans[column] = {
                        "row_end": row_end,
                        "text": text,
                        "cell_index": cell_index,
                    }
            cursor = end + 1
        expanded_matrix.append(expanded)
        origin_matrix.append(origins)

    return {
        "expanded_matrix": expanded_matrix,
        "origin_matrix": origin_matrix,
        "cells": cells,
    }


def _direct_nested_tables(node: ET.Element) -> List[Dict[str, Any]]:
    """Return nested CALS tables once, without duplicating deeper descendants."""
    nested: List[Dict[str, Any]] = []

    def visit(parent: ET.Element) -> None:
        for child in parent:
            if child.tag == "table":
                nested.append(parse_cals_table(child))
            else:
                visit(child)

    visit(node)
    return nested


def _unique_columns(columns: List[str]) -> List[str]:
    result: List[str] = []
    counts: Dict[str, int] = {}
    for index, raw_name in enumerate(columns):
        base = normalize_inline_text(raw_name) or "column_{}".format(index + 1)
        counts[base] = counts.get(base, 0) + 1
        name = base if counts[base] == 1 else "{}__{}".format(base, counts[base])
        result.append(name)
    return result


def columns_from_header_matrix(header_matrix: List[List[str]], num_columns: int) -> List[str]:
    columns: List[str] = []
    for column in range(num_columns):
        path: List[str] = []
        for row in header_matrix:
            text = normalize_inline_text(row[column] if column < len(row) else "")
            if text and (not path or path[-1] != text):
                path.append(text)
        columns.append(" / ".join(path))
    return _unique_columns(columns)


def _rows_as_mappings(columns: List[str], matrix: List[List[str]]) -> List[Dict[str, str]]:
    return [
        {
            column: normalize_inline_text(row[index] if index < len(row) else "")
            for index, column in enumerate(columns)
        }
        for row in matrix
    ]


def parse_cals_tgroup(tgroup: ET.Element) -> Dict[str, Any]:
    """Parse one CALS ``tgroup`` into a lossless and row-oriented form."""
    num_columns = _positive_int(tgroup.get("cols"), 1)
    colspecs, name_to_index = _colspecs(tgroup, num_columns)
    spanspecs = _spanspecs(tgroup)
    section_rows = {
        "thead": tgroup.findall("./thead/row"),
        "tbody": tgroup.findall("./tbody/row"),
        "tfoot": tgroup.findall("./tfoot/row"),
    }
    sections: Dict[str, Dict[str, Any]] = {}
    offset = 0
    for section_name in ("thead", "tbody", "tfoot"):
        parsed = _parse_rows(
            section_rows[section_name],
            section_name,
            num_columns,
            name_to_index,
            spanspecs,
            offset,
        )
        sections[section_name] = parsed
        offset += len(section_rows[section_name])

    header_matrix = sections["thead"]["expanded_matrix"]
    body_matrix = sections["tbody"]["expanded_matrix"]
    footer_matrix = sections["tfoot"]["expanded_matrix"]
    columns = columns_from_header_matrix(header_matrix, num_columns)
    cells = []
    for section_name in ("thead", "tbody", "tfoot"):
        cells.extend(sections[section_name]["cells"])
    for cell_index, cell in enumerate(cells):
        cell["cell_index"] = cell_index
    return {
        "num_columns": num_columns,
        "columns": columns,
        "rows": _rows_as_mappings(columns, body_matrix),
        "header_matrix": header_matrix,
        "body_matrix": body_matrix,
        "footer_matrix": footer_matrix,
        "origin_header_matrix": sections["thead"]["origin_matrix"],
        "origin_body_matrix": sections["tbody"]["origin_matrix"],
        "origin_footer_matrix": sections["tfoot"]["origin_matrix"],
        "cells": cells,
        "colspecs": colspecs,
        "spanspecs": spanspecs,
        "attributes": dict(tgroup.attrib),
    }


def parse_cals_table(table: ET.Element) -> Dict[str, Any]:
    """Parse a GII ``table`` element, retaining every physical tgroup."""
    tgroups = [parse_cals_tgroup(tgroup) for tgroup in table.findall("./tgroup")]
    if not tgroups:
        return {
            "num_columns": 0,
            "columns": [],
            "rows": [],
            "header_matrix": [],
            "body_matrix": [],
            "footer_matrix": [],
            "cells": [],
            "tgroups": [],
            "title": "",
            "attributes": dict(table.attrib),
        }

    primary = copy.deepcopy(tgroups[0])
    primary["cells"] = []
    for tgroup_index, tgroup in enumerate(tgroups):
        for source_cell in tgroup.get("cells") or []:
            cell = copy.deepcopy(source_cell)
            cell["tgroup_index"] = tgroup_index
            cell["cell_index"] = len(primary["cells"])
            primary["cells"].append(cell)
    if len(tgroups) > 1:
        unmerged_tgroups = []
        for tgroup_index, continuation in enumerate(tgroups[1:], 1):
            if continuation["columns"] == primary["columns"]:
                primary["body_matrix"].extend(continuation["body_matrix"])
                primary["rows"].extend(continuation["rows"])
                primary["footer_matrix"].extend(continuation["footer_matrix"])
            else:
                unmerged_tgroups.append(tgroup_index)
        if unmerged_tgroups:
            primary["multi_tgroup_layout"] = True
            primary["unmerged_tgroup_indexes"] = unmerged_tgroups
    primary["tgroups"] = tgroups
    title_element = table.find("./Title")
    primary["title"] = element_text(title_element) if title_element is not None else ""
    primary["attributes"] = dict(table.attrib)
    return primary


def _row_prefix_match_ratio(
    left_matrix: List[List[str]],
    right_matrix: List[List[str]],
    prefix_columns: int,
) -> float:
    if not left_matrix or len(left_matrix) != len(right_matrix):
        return 0.0
    matches = 0
    comparable = 0
    for left_row, right_row in zip(left_matrix, right_matrix):
        left_key = tuple(
            normalize_inline_text(left_row[index] if index < len(left_row) else "")
            for index in range(prefix_columns)
        )
        right_key = tuple(
            normalize_inline_text(right_row[index] if index < len(right_row) else "")
            for index in range(prefix_columns)
        )
        if not any(left_key) and not any(right_key):
            continue
        comparable += 1
        if left_key == right_key:
            matches += 1
    return matches / comparable if comparable else 0.0


def continuation_key_columns(left: Dict[str, Any], right: Dict[str, Any]) -> int:
    """Infer the repeated leading row-key area of horizontal table panels."""
    max_prefix = min(
        4,
        max(len(left.get("columns") or []) - 1, 0),
        max(len(right.get("columns") or []) - 1, 0),
    )
    best = 0
    for prefix in range(1, max_prefix + 1):
        if _row_prefix_match_ratio(
            left.get("body_matrix") or [],
            right.get("body_matrix") or [],
            prefix,
        ) >= 0.8:
            best = prefix
        else:
            break
    return best


def merge_continuation_tables(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a labeled continuation vertically or as a horizontal panel."""
    merged = copy.deepcopy(left)
    merged.setdefault("physical_tables", [copy.deepcopy(left)])
    merged["physical_tables"].append(copy.deepcopy(right))

    if left.get("columns") == right.get("columns"):
        merged["merge_mode"] = "vertical"
        merged["body_matrix"] = list(left.get("body_matrix") or []) + list(
            right.get("body_matrix") or []
        )
        merged["rows"] = _rows_as_mappings(merged["columns"], merged["body_matrix"])
        return merged

    key_columns = continuation_key_columns(left, right)
    if key_columns:
        merged["merge_mode"] = "horizontal"
        merged["continuation_key_columns"] = key_columns
        columns = list(left.get("columns") or []) + list(right.get("columns") or [])[key_columns:]
        columns = _unique_columns(columns)
        matrix = []
        for left_row, right_row in zip(
            left.get("body_matrix") or [],
            right.get("body_matrix") or [],
        ):
            matrix.append(list(left_row) + list(right_row[key_columns:]))
        merged["columns"] = columns
        merged["num_columns"] = len(columns)
        merged["body_matrix"] = matrix
        merged["rows"] = _rows_as_mappings(columns, matrix)
        return merged

    merged["merge_mode"] = "unmerged_panels"
    merged["continuation_unmerged"] = True
    return merged
