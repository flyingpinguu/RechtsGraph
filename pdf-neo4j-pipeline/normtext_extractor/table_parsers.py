"""Structural table parser registry.

The current implementation keeps the proven legacy parsing functions as parser
backends, but exposes them through structural parser classes. This gives future
edge cases a stable place to live without adding another ad-hoc branch to the
main extraction pipeline.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class TableParseContext:
    table: Dict[str, Any]
    block: Dict[str, Any]
    fitz_word_pages: Optional[List[Any]] = None
    fitz_page_lines: Optional[List[Any]] = None


class TableParser(Protocol):
    name: str

    def can_parse(self, context: TableParseContext) -> bool:
        ...

    def parse(self, context: TableParseContext) -> Optional[Dict[str, Any]]:
        ...


def _pipeline_module():
    from normtext_extractor import pipeline

    return pipeline


def _has_page_geometry(context: TableParseContext) -> bool:
    return bool(context.fitz_word_pages and context.block.get("line_pages"))


class HierarchicalHeaderTableParser:
    """Parse tables whose value columns are defined by nested header rows."""

    name = "hierarchical_header"

    def can_parse(self, context: TableParseContext) -> bool:
        return _has_page_geometry(context)

    def parse(self, context: TableParseContext) -> Optional[Dict[str, Any]]:
        pipeline = _pipeline_module()
        return pipeline.parse_geometric_symbol_table(
            context.table,
            context.block,
            context.fitz_word_pages,
            context.fitz_page_lines,
        )


class MatrixSymbolTableParser(HierarchicalHeaderTableParser):
    """Alias parser type for matrix-style symbol tables.

    The shared hierarchical parser already handles the observed matrix layout:
    a left row-key area plus narrow symbol/value columns. Keeping this class
    named separately documents the intended parser taxonomy for future splits.
    """

    name = "matrix_symbol"


class MultiPanelContinuationTableParser:
    """Parse wide tables whose columns continue across pages or panels."""

    name = "multi_panel_continuation"

    def can_parse(self, context: TableParseContext) -> bool:
        return _has_page_geometry(context)

    def parse(self, context: TableParseContext) -> Optional[Dict[str, Any]]:
        pipeline = _pipeline_module()
        return pipeline.parse_geometric_material_table(
            context.table,
            context.block,
            context.fitz_word_pages,
            context.fitz_page_lines,
        )


class GridTableParser:
    """Parse tables with explicit horizontal and vertical grid lines."""

    name = "grid"

    def can_parse(self, context: TableParseContext) -> bool:
        return _has_page_geometry(context)

    def parse(self, context: TableParseContext) -> Optional[Dict[str, Any]]:
        pipeline = _pipeline_module()
        return pipeline.parse_geometric_grid_table(
            context.table,
            context.block,
            context.fitz_word_pages,
            context.fitz_page_lines,
        )


class KeyValueTableParser(GridTableParser):
    """Placeholder for two-column definition and parameter tables."""

    name = "key_value"


class TextFallbackTableParser:
    """Return the already parsed conservative text table."""

    name = "text_fallback"

    def can_parse(self, context: TableParseContext) -> bool:
        return True

    def parse(self, context: TableParseContext) -> Optional[Dict[str, Any]]:
        return context.table


class TableParserRegistry:
    def __init__(self, parsers: List[TableParser]):
        self.parsers = parsers

    def parse_first(self, context: TableParseContext) -> Dict[str, Any]:
        for parser in self.parsers:
            if not parser.can_parse(context):
                continue
            parsed = parser.parse(context)
            if parsed is not None:
                parsed.setdefault("parser_name", parser.name)
                return parsed
        return context.table


def default_table_parser_registry() -> TableParserRegistry:
    return TableParserRegistry(
        [
            HierarchicalHeaderTableParser(),
            GridTableParser(),
            KeyValueTableParser(),
            MultiPanelContinuationTableParser(),
            TextFallbackTableParser(),
        ]
    )
