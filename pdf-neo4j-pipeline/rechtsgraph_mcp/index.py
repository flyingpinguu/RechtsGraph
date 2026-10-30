"""Derived, local SQLite retrieval index for the RechtsGraph corpus.

The index is deliberately *not* another legal parser.  Canonical JSON stays
the source of truth for GII and EUR-Lex documents; this module only makes its
documents, structural units and chunks cheaply retrievable.  A small opt-in
PDF fallback is available for supplementary documents (for example ADR) that
do not yet have a structural parser.  Fallback passages never pretend to be
``StructuralUnit`` records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from .provenance import compact_evidence, parse_metadata_json


INDEX_SCHEMA_VERSION = "1.0"
DEFAULT_PDF_CHUNK_TOKENS = 800
DEFAULT_PDF_CHUNK_OVERLAP = 120


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _norm(value: Any) -> str:
    """Normalize a legal identifier without making it a legal interpretation."""

    return re.sub(r"[^0-9a-z]+", "", _text(value).casefold())


CURATED_DOCUMENT_QUERY_ALIASES = {
    "kiverordnung": "32024R1689",
    "eukiverordnung": "32024R1689",
    "aiact": "32024R1689",
    "euaiact": "32024R1689",
}


def _legal_ref_norm(value: Any) -> str:
    """Normalize equivalent long and abbreviated structural-reference forms."""

    normalized = _norm(value)
    for long_form, short_form in (
        ("paragraph", ""),
        ("artikel", "art"),
        ("absatz", "abs"),
        ("nummer", "nr"),
        ("buchstabe", "buchst"),
        ("litera", "lit"),
    ):
        normalized = normalized.replace(long_form, short_form)
    return normalized


def _fts_query(value: str, operator: str = "AND") -> str:
    """Turn free text into a quoted FTS5 query."""

    if operator not in {"AND", "OR"}:
        raise ValueError("FTS operator must be AND or OR")
    terms = list(dict.fromkeys(re.findall(r"[0-9A-Za-zÄÖÜäöüß]+", value)))
    return (" {} ".format(operator)).join(
        '"{}"'.format(term.replace('"', "")) for term in terms
    )


def _hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _page_range(value: Any) -> tuple[Optional[int], Optional[int]]:
    if not isinstance(value, Mapping):
        return None, None
    start, end = value.get("start"), value.get("end")
    return (int(start) if isinstance(start, int) else None, int(end) if isinstance(end, int) else None)


def _integer_order(value: Any, fallback: int = 0) -> int:
    """Return a scalar order without assuming XML source_order is scalar."""

    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return fallback


def _unit_part(reference: str) -> str:
    """Strip subsection suffixes while retaining the addressable parent unit."""

    match = re.search(
        r"\s+(?:"
        r"(?:Abs\.?|Absatz|Unterabs\.?|Unterabsatz|Satz|Nr\.?|Nummer)\s*\d"
        r"|(?:Buchst\.?|Buchstabe)\s*[0-9a-z]"
        r"|(?:lit\.?|litera)\s*[a-z]"
        r")",
        reference,
        flags=re.IGNORECASE,
    )
    return reference[: match.start()].strip() if match else reference.strip()


def _generated_unit_aliases(unit: Mapping[str, Any]) -> list[str]:
    """Add common short legal spellings without changing parser structure."""

    unit_type = _text(unit.get("unit_type")).casefold()
    number = _text(unit.get("number"))
    if not number:
        return []
    if unit_type == "article":
        return ["Art. {}".format(number), "Artikel {}".format(number)]
    if unit_type == "paragraph":
        return ["§ {}".format(number), "Paragraph {}".format(number)]
    if unit_type in {"annex", "appendix"}:
        return ["Anhang {}".format(number), "Anlage {}".format(number)]
    return []


@dataclass
class BuildStats:
    documents: int = 0
    structural_units: int = 0
    passages: int = 0
    pdf_fallback_documents: int = 0
    pdf_fallback_passages: int = 0
    skipped_raw_files: int = 0
    manifests: int = 0
    warnings: list[str] = field(default_factory=list)


class RetrievalIndex:
    """Read-only convenience API over a finished retrieval index."""

    def __init__(self, path: Path | str, external_register_path: Optional[Path | str] = None):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError("retrieval index is missing: {}".format(self.path))
        self.connection = sqlite3.connect(
            "file:{}?mode=ro".format(self.path.resolve()), uri=True
        )
        self.connection.row_factory = sqlite3.Row
        configured = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'external_register_path'"
        ).fetchone()
        self.external_register_path = Path(external_register_path) if external_register_path else (
            Path(configured[0]) if configured and configured[0] else None
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "RetrievalIndex":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            # Internal JSON is retained for rebuild/audit, but returning it from
            # every MCP hit would duplicate text and waste the agent context.
            metadata_json = item.pop("metadata_json", None)
            document_metadata_json = item.pop("document_metadata_json", None)
            passage_metadata = parse_metadata_json(metadata_json) if "passage_id" in item else {}
            document_metadata = parse_metadata_json(
                document_metadata_json if document_metadata_json is not None else metadata_json
            )
            if item.get("source_kind") == "external_pdf" and document_metadata:
                for field_name in (
                    "document_type",
                    "edition",
                    "language",
                    "landing_url",
                ):
                    if document_metadata.get(field_name) not in (None, ""):
                        item[field_name] = document_metadata[field_name]
            if "source_kind" in item or "passage_id" in item:
                item["evidence"] = compact_evidence(
                    document_metadata, item, passage_metadata
                )
            values.append(item)
        return values

    @staticmethod
    def _corpus_clause(corpus: Optional[str], column: str = "d.source_kind") -> tuple[str, list[str]]:
        if not corpus:
            return "", []
        aliases = {"de": "gii", "german": "gii", "eu": "eurlex", "external": "external_pdf"}
        return " AND {} = ?".format(column), [aliases.get(corpus.casefold(), corpus)]

    def status(self) -> dict[str, Any]:
        """Small, cheap index-health summary for a wrapper/tool host."""

        counts = dict(self.connection.execute(
            "SELECT 'documents' AS name, count(*) AS total FROM documents UNION ALL "
            "SELECT 'structural_units', count(*) FROM structural_units UNION ALL "
            "SELECT 'passages', count(*) FROM passages UNION ALL "
            "SELECT 'pdf_fallback_passages', count(*) FROM passages WHERE passage_kind = 'pdf_fallback'"
        ).fetchall())
        metadata = dict(self.connection.execute("SELECT key, value FROM metadata").fetchall())
        return {"path": str(self.path), "schema_version": metadata.get("schema_version"), **counts}

    def find_documents(self, query: str, corpus: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        """Find documents exact-first through aliases, then FTS5 titles/aliases."""

        limit = max(1, min(int(limit), 50))
        normalized = _norm(query)
        normalized = _norm(CURATED_DOCUMENT_QUERY_ALIASES.get(normalized, normalized))
        corpus_sql, corpus_params = self._corpus_clause(corpus)
        exact = self._rows(
            self.connection.execute(
                """SELECT d.*, 1.0 AS score, a.alias AS matched_alias
                   FROM document_aliases AS a JOIN documents AS d USING (document_id)
                   WHERE a.normalized_alias = ? {}
                   ORDER BY d.title COLLATE NOCASE LIMIT ?""".format(corpus_sql),
                [normalized, *corpus_params, limit],
            )
        ) if normalized else []
        seen = {row["document_id"] for row in exact}
        # An exact identifier or curated alias is stronger evidence than FTS.
        # Do not pad an exact hit set with unrelated lexical matches merely to
        # fill the requested limit.
        if exact:
            return exact[:limit]
        match = _fts_query(query)
        if not match:
            return exact

        def ranked(match_value: str) -> list[dict[str, Any]]:
            return self._rows(
                self.connection.execute(
                    """SELECT d.*, bm25(documents_fts) AS score, documents_fts.alias AS matched_alias
                       FROM documents_fts JOIN documents AS d
                         ON d.document_id = documents_fts.document_id
                       WHERE documents_fts MATCH ? {} ORDER BY score LIMIT ?""".format(corpus_sql),
                    [match_value, *corpus_params, max(limit * 3, limit)],
                )
            )

        rows = ranked(match)
        fallback = _fts_query(query, "OR")
        if not rows and fallback != match:
            rows = ranked(fallback)
        for row in rows:
            if row["document_id"] in seen:
                continue
            exact.append(row)
            seen.add(row["document_id"])
        return exact[:limit]

    def find_units(self, document_id: str, reference: str, limit: int = 10) -> list[dict[str, Any]]:
        """Resolve ``§ 6``, ``Artikel 4`` or a unit title within one document."""

        limit = max(1, min(int(limit), 50))
        exact = self._find_units_exact(document_id, reference, limit=limit)
        if exact or not reference.strip():
            return exact
        match = _fts_query(reference)
        if not match:
            return []

        def ranked(match_value: str) -> list[dict[str, Any]]:
            return self._rows(self.connection.execute(
                """SELECT u.*, bm25(units_fts) AS score
                   FROM units_fts JOIN structural_units AS u ON u.unit_id = units_fts.unit_id
                   WHERE units_fts MATCH ? AND u.document_id = ?
                   ORDER BY score, u.sequence LIMIT ?""",
                (match_value, document_id, limit),
            ))
        rows = ranked(match)
        fallback = _fts_query(reference, "OR")
        return ranked(fallback) if not rows and fallback != match else rows

    def _find_units_exact(
        self, document_id: str, reference: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Resolve stable identifiers and aliases without an FTS fallback."""

        limit = max(1, min(int(limit), 50))
        direct = self.connection.execute(
            """SELECT *, 1.0 AS score, global_key AS matched_alias
               FROM structural_units
               WHERE document_id = ? AND (unit_id = ? OR global_key = ?)
               LIMIT ?""",
            (document_id, reference, reference, limit),
        ).fetchall()
        if direct:
            return self._rows(direct)
        normalized = _norm(reference)
        exact = self._rows(
            self.connection.execute(
                """SELECT u.*, 1.0 AS score, a.alias AS matched_alias
                   FROM unit_aliases AS a JOIN structural_units AS u USING (unit_id)
                   WHERE u.document_id = ? AND a.normalized_alias = ?
                   ORDER BY u.sequence LIMIT ?""",
                (document_id, normalized, limit),
            )
        ) if normalized else []
        return exact

    def _find_exact_descendant_reference(
        self, document_id: str, parent_unit_id: str, reference: str
    ) -> list[dict[str, Any]]:
        """Find one explicit descendant whose citation ends in ``reference``."""

        requested = _legal_ref_norm(reference)
        if not requested:
            return []
        units = self.outline(document_id)
        by_id = {unit["unit_id"]: unit for unit in units}
        matches: list[dict[str, Any]] = []
        for unit in units:
            cursor = unit.get("parent_unit_id")
            is_descendant = False
            while cursor:
                if cursor == parent_unit_id:
                    is_descendant = True
                    break
                parent = by_id.get(cursor)
                cursor = parent.get("parent_unit_id") if parent else None
            citation = _legal_ref_norm(unit.get("legal_citation"))
            if is_descendant and citation and citation.endswith(requested):
                matches.append(unit)
        return matches if len(matches) == 1 else []

    def _find_unique_citation_suffix(
        self, document_id: str, reference: str
    ) -> list[dict[str, Any]]:
        """Resolve a context-qualified citation after omitting the document prefix."""

        requested = _legal_ref_norm(reference)
        if not requested:
            return []
        matches = [
            unit for unit in self.outline(document_id)
            if _legal_ref_norm(unit.get("legal_citation")).endswith(requested)
        ]
        return matches if len(matches) == 1 else []

    def outline(self, document_id: str) -> list[dict[str, Any]]:
        return self._rows(
            self.connection.execute(
                """SELECT unit_id, global_key, document_id, parent_unit_id, unit_type, label, number,
                          title, legal_citation, display_name, breadcrumbs, sequence,
                          page_start, page_end
                   FROM structural_units WHERE document_id = ? ORDER BY sequence, unit_id""",
                (document_id,),
            )
        )

    def _resolve_document(self, document_ref: str, corpus: Optional[str] = None) -> Optional[dict[str, Any]]:
        direct = self.connection.execute("SELECT * FROM documents WHERE document_id = ?", (document_ref,)).fetchone()
        if direct is not None:
            return self._rows([direct])[0]
        found = self.find_documents(document_ref, corpus=corpus, limit=1)
        return found[0] if found else None

    def get_document_outline(self, document_ref: str, max_depth: int = 2, limit: int = 300) -> dict[str, Any]:
        max_depth = max(0, min(int(max_depth), 20))
        limit = max(1, min(int(limit), 2000))
        document = self._resolve_document(document_ref)
        if document is None:
            return {"document": None, "units": [], "truncated": False}
        units = self.outline(document["document_id"])
        by_id = {unit["unit_id"]: unit for unit in units}
        depths: dict[str, int] = {}
        def depth(unit: Mapping[str, Any]) -> int:
            identifier = unit["unit_id"]
            if identifier in depths:
                return depths[identifier]
            parent = by_id.get(unit.get("parent_unit_id"))
            depths[identifier] = 0 if parent is None else depth(parent) + 1
            return depths[identifier]
        selected = []
        for unit in units:
            unit["depth"] = depth(unit)
            if unit["depth"] <= max(0, max_depth):
                selected.append(unit)
        returned = selected[:limit]
        return {
            "document": document,
            "count": len(returned),
            "units": returned,
            "truncated": len(selected) > limit,
        }

    def unit_content(self, unit_id: str) -> list[dict[str, Any]]:
        return self._rows(
            self.connection.execute(
                """SELECT p.*, d.title AS document_title, d.canonical_citation,
                          d.source_kind, d.source_format,
                          d.metadata_json AS document_metadata_json
                   FROM passages AS p JOIN documents AS d USING (document_id)
                   WHERE p.unit_id = ? ORDER BY p.sort_order, p.passage_id""",
                (unit_id,),
            )
        )

    def get_structural_unit(
        self, document_ref: str, unit_ref: str, include_descendants: bool = True,
        max_passages: int = 30, max_chars: int = 40000,
    ) -> dict[str, Any]:
        document = self._resolve_document(document_ref)
        if document is None:
            return {"document": None, "unit": None, "passages": [], "truncated": False}
        parent_ref = _unit_part(unit_ref)
        if parent_ref != unit_ref.strip():
            # Subsections may be explicit StructuralUnits, or only identifiable
            # through passage citations below their addressable parent.  Never
            # let fuzzy FTS ranking turn a missing subsection into an unrelated
            # numbered table or annex.
            units = self._find_units_exact(document["document_id"], unit_ref, limit=1)
            if not units:
                units = self._find_unique_citation_suffix(document["document_id"], unit_ref)
            parents = self._find_units_exact(document["document_id"], parent_ref, limit=1)
            if not parents:
                parents = self._find_unique_citation_suffix(
                    document["document_id"], parent_ref
                )
            if not units and parents:
                units = self._find_exact_descendant_reference(
                    document["document_id"], parents[0]["unit_id"], unit_ref
                )
            if not units:
                units = parents
        else:
            units = self.find_units(document["document_id"], unit_ref, limit=1)
        if not units:
            return {"document": document, "unit": None, "passages": [], "truncated": False}
        unit = units[0]
        selected_ids = {unit["unit_id"]}
        if include_descendants:
            all_units = self.outline(document["document_id"])
            parents = {candidate["unit_id"]: candidate.get("parent_unit_id") for candidate in all_units}
            for candidate_id in parents:
                cursor = parents[candidate_id]
                while cursor:
                    if cursor == unit["unit_id"]:
                        selected_ids.add(candidate_id)
                        break
                    cursor = parents.get(cursor)
        placeholders = ",".join("?" for _ in selected_ids)
        rows = self._rows(self.connection.execute(
            """SELECT p.*, d.title AS document_title, d.canonical_citation,
                      d.source_kind, d.source_format,
                      d.metadata_json AS document_metadata_json,
                      u.global_key AS unit_global_key, u.breadcrumbs
               FROM passages AS p JOIN documents AS d USING (document_id)
               LEFT JOIN structural_units AS u USING (unit_id)
               WHERE p.unit_id IN ({}) ORDER BY p.sort_order, p.passage_id""".format(placeholders),
            list(selected_ids),
        ))
        kept, characters = [], 0
        for row in rows:
            if len(kept) >= max(1, max_passages) or characters + len(row["text"]) > max(1, max_chars):
                break
            kept.append(row)
            characters += len(row["text"])
        subsection = unit_ref[len(parent_ref):].strip() if parent_ref != unit_ref.strip() else ""
        matching_passages = [
            row for row in kept
            if subsection and _norm(subsection) in _norm(row.get("legal_citation"))
        ]
        return {
            "document": document,
            "unit": unit,
            "requested_subsection": subsection or None,
            "passage_count": len(kept),
            "matching_passage_count": len(matching_passages),
            "matching_passages": matching_passages,
            "passages": kept,
            "truncated": len(kept) < len(rows),
        }

    def search_passages(
        self, query: str, limit: int = 10, document_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """BM25 full-text search of canonical chunks and opt-in fallback passages."""

        limit = max(1, min(int(limit), 50))
        match = _fts_query(query)
        if not match:
            return []
        sql = """SELECT p.*, d.title AS document_title, d.canonical_citation,
                         d.source_kind, d.source_format,
                         d.metadata_json AS document_metadata_json,
                         bm25(passages_fts) AS score
                  FROM passages_fts JOIN passages AS p ON p.passage_pk = passages_fts.passage_pk
                  JOIN documents AS d ON d.document_id = p.document_id
                  WHERE passages_fts MATCH ?"""
        def ranked(match_value: str) -> list[dict[str, Any]]:
            params: list[Any] = [match_value]
            scoped_sql = sql
            if document_id:
                scoped_sql += " AND p.document_id = ?"
                params.append(document_id)
            scoped_sql += " ORDER BY score, p.sort_order LIMIT ?"
            params.append(limit)
            return self._rows(self.connection.execute(scoped_sql, params))
        rows = ranked(match)
        fallback = _fts_query(query, "OR")
        return ranked(fallback) if not rows and fallback != match else rows

    def search_corpus(
        self, query: str, corpus: Optional[str] = None, document_ref: Optional[str] = None, limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        document_id = None
        if document_ref:
            document = self._resolve_document(document_ref, corpus=corpus)
            if document is None:
                return []
            document_id = document["document_id"]
        match = _fts_query(query)
        if not match:
            return []
        sql = """SELECT p.*, d.title AS document_title, d.canonical_citation,
                         d.source_kind, d.source_format,
                         d.metadata_json AS document_metadata_json,
                         bm25(passages_fts) AS score
                  FROM passages_fts JOIN passages AS p ON p.passage_pk = passages_fts.passage_pk
                  JOIN documents AS d ON d.document_id = p.document_id
                  WHERE passages_fts MATCH ?"""
        corpus_sql, corpus_params = self._corpus_clause(corpus)
        sql += corpus_sql
        def ranked(match_value: str) -> list[dict[str, Any]]:
            params: list[Any] = [match_value, *corpus_params]
            scoped_sql = sql
            if document_id:
                scoped_sql += " AND p.document_id = ?"
                params.append(document_id)
            scoped_sql += " ORDER BY score, p.sort_order LIMIT ?"
            params.append(limit)
            return self._rows(self.connection.execute(scoped_sql, params))
        rows = ranked(match)
        fallback = _fts_query(query, "OR")
        return ranked(fallback) if not rows and fallback != match else rows

    def context_neighbors(self, passage_id: str, before: int = 1, after: int = 1) -> list[dict[str, Any]]:
        """Return nearby passages from the same source document in reading order."""

        target = self.connection.execute(
            "SELECT document_id, sort_order FROM passages WHERE passage_id = ?", (passage_id,)
        ).fetchone()
        if target is None:
            return []
        return self._rows(
            self.connection.execute(
                """SELECT p.*, d.title AS document_title, d.canonical_citation,
                          d.source_kind, d.source_format,
                          d.metadata_json AS document_metadata_json,
                          u.global_key AS unit_global_key,
                          u.legal_citation AS unit_legal_citation, u.breadcrumbs
                   FROM passages AS p JOIN documents AS d USING (document_id)
                   LEFT JOIN structural_units AS u USING (unit_id)
                   WHERE p.document_id = ? AND p.sort_order BETWEEN ? AND ?
                   ORDER BY p.sort_order, p.passage_id""",
                (target["document_id"], target["sort_order"] - max(before, 0), target["sort_order"] + max(after, 0)),
            )
        )

    def get_context(
        self, passage_ids: Sequence[str], before: int = 1, after: int = 1, max_chars: int = 50000,
    ) -> dict[str, Any]:
        """Merge bounded neighborhoods for one or more retrieval passages."""

        if not passage_ids:
            raise ValueError("passage_ids must not be empty")
        if len(passage_ids) > 20:
            raise ValueError("at most 20 passage_ids may be expanded at once")
        before = max(0, min(int(before), 10))
        after = max(0, min(int(after), 10))
        max_chars = max(1, min(int(max_chars), 200000))
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for passage_id in passage_ids:
            for row in self.context_neighbors(passage_id, before, after):
                if row["passage_id"] not in seen:
                    seen.add(row["passage_id"])
                    rows.append(row)
        rows.sort(key=lambda row: (row["document_id"], row["sort_order"], row["passage_id"]))
        kept, characters = [], 0
        for row in rows:
            if characters + len(row["text"]) > max(1, max_chars):
                break
            kept.append(row)
            characters += len(row["text"])
        return {
            "count": len(kept),
            "passages": kept,
            "truncated": len(kept) < len(rows),
        }

    def search_external_register(self, query: str, category: Optional[str] = None, limit: int = 10) -> list[dict[str, Any]]:
        """Search catalogue metadata, without treating unparsed documents as corpus text."""

        if not self.external_register_path or not self.external_register_path.is_file():
            return []
        connection = sqlite3.connect("file:{}?mode=ro".format(self.external_register_path.resolve()), uri=True)
        connection.row_factory = sqlite3.Row
        try:
            sql = """SELECT d.*, max(f.validation_status = 'valid') AS has_valid_local_file
                     FROM documents AS d LEFT JOIN files AS f ON f.document_id = d.id
                     WHERE (lower(d.title) LIKE ? OR lower(coalesce(d.short_title, '')) LIKE ?
                            OR d.id IN (SELECT document_id FROM aliases WHERE normalized_alias = ?))"""
            normalized = _norm(query)
            params: list[Any] = ["%{}%".format(query.casefold()), "%{}%".format(query.casefold()), normalized]
            if category:
                sql += " AND d.category = ?"
                params.append(category)
            sql += " GROUP BY d.id ORDER BY d.priority, d.title COLLATE NOCASE LIMIT ?"
            params.append(limit)
            return self._rows(connection.execute(sql, params))
        finally:
            connection.close()


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE documents (
          document_id TEXT PRIMARY KEY, document_key TEXT, document_global_key TEXT,
          title TEXT NOT NULL, canonical_citation TEXT, abbreviation TEXT,
          full_citation TEXT, source_kind TEXT NOT NULL, source_format TEXT,
          raw_path TEXT, source_path TEXT, is_pdf_fallback INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT NOT NULL
        );
        CREATE TABLE document_aliases (
          document_id TEXT NOT NULL REFERENCES documents(document_id), alias TEXT NOT NULL,
          normalized_alias TEXT NOT NULL, PRIMARY KEY(document_id, normalized_alias)
        );
        CREATE INDEX document_aliases_lookup ON document_aliases(normalized_alias);
        CREATE VIRTUAL TABLE documents_fts USING fts5(document_id UNINDEXED, alias, title);
        CREATE TABLE structural_units (
          unit_id TEXT PRIMARY KEY, global_key TEXT, document_id TEXT NOT NULL REFERENCES documents(document_id),
          parent_unit_id TEXT, unit_type TEXT, label TEXT, number TEXT, title TEXT,
          legal_citation TEXT, display_name TEXT, breadcrumbs TEXT, sequence INTEGER,
          page_start INTEGER, page_end INTEGER, source_order INTEGER, metadata_json TEXT NOT NULL
        );
        CREATE INDEX units_document_sequence ON structural_units(document_id, sequence);
        CREATE TABLE unit_aliases (
          unit_id TEXT NOT NULL REFERENCES structural_units(unit_id), alias TEXT NOT NULL,
          normalized_alias TEXT NOT NULL, PRIMARY KEY(unit_id, normalized_alias)
        );
        CREATE INDEX unit_aliases_lookup ON unit_aliases(normalized_alias);
        CREATE VIRTUAL TABLE units_fts USING fts5(unit_id UNINDEXED, label, title, citation, display_name);
        CREATE TABLE passages (
          passage_pk INTEGER PRIMARY KEY, passage_id TEXT NOT NULL UNIQUE,
          document_id TEXT NOT NULL REFERENCES documents(document_id),
          unit_id TEXT REFERENCES structural_units(unit_id), original_chunk_id TEXT,
          passage_kind TEXT NOT NULL, label TEXT, legal_citation TEXT, text TEXT NOT NULL,
          page_start INTEGER, page_end INTEGER, sort_order INTEGER NOT NULL,
          token_count INTEGER, metadata_json TEXT NOT NULL
        );
        CREATE INDEX passages_document_order ON passages(document_id, sort_order);
        CREATE INDEX passages_unit_order ON passages(unit_id, sort_order);
        CREATE VIRTUAL TABLE passages_fts USING fts5(
          passage_pk UNINDEXED, text, title, citation, tokenize='unicode61 remove_diacritics 2'
        );
        """
    )


def _aliases(document: Mapping[str, Any]) -> list[str]:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), Mapping) else {}
    values: list[Any] = [
        document.get("document_id"), document.get("document_key"), document.get("document_global_key"),
        document.get("title"), document.get("canonical_citation"), document.get("citation_prefix"),
        document.get("abbreviation"), metadata.get("base_celex"), metadata.get("consolidated_celex"),
    ]
    values.extend(metadata.get("citation_aliases") or [])
    return list(dict.fromkeys(value for value in (_text(v) for v in values) if value))


def _insert_document(connection: sqlite3.Connection, document: Mapping[str, Any], raw_path: str, source_kind: str) -> bool:
    document_id = _text(document.get("document_id"))
    if not document_id:
        return False
    title = _text(document.get("title")) or _text(document.get("canonical_citation")) or document_id
    metadata = document.get("metadata") if isinstance(document.get("metadata"), Mapping) else {}
    source_format = _text(metadata.get("source_format")) or _text(document.get("source_format"))
    connection.execute(
        """INSERT OR IGNORE INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (
            document_id, _text(document.get("document_key")), _text(document.get("document_global_key")), title,
            _text(document.get("canonical_citation")), _text(document.get("abbreviation")),
            _text(document.get("full_citation")), source_kind, source_format, raw_path,
            _text(document.get("source_xml")) or _text(document.get("source_pdf")),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    if connection.execute("SELECT changes()").fetchone()[0] == 0:
        return False
    for alias in _aliases(document):
        connection.execute("INSERT OR IGNORE INTO document_aliases VALUES (?, ?, ?)", (document_id, alias, _norm(alias)))
        connection.execute("INSERT INTO documents_fts VALUES (?, ?, ?)", (document_id, alias, title))
    return True


def _insert_structured_document(connection: sqlite3.Connection, document: Mapping[str, Any], raw_path: str, stats: BuildStats) -> None:
    document_id = _text(document.get("document_id"))
    document_metadata = document.get("metadata") if isinstance(document.get("metadata"), Mapping) else {}
    source_kind = "eurlex" if _text(document_metadata.get("base_celex")) else "gii"
    if not _insert_document(connection, document, raw_path, source_kind):
        stats.warnings.append("duplicate document_id skipped: {}".format(document_id))
        return
    stats.documents += 1
    for unit in document.get("structural_units") or []:
        if not isinstance(unit, Mapping) or not _text(unit.get("unit_id")):
            continue
        start, end = _page_range(unit.get("page_range"))
        breadcrumbs = unit.get("breadcrumbs") if isinstance(unit.get("breadcrumbs"), list) else []
        values = (
            _text(unit.get("unit_id")), _text(unit.get("global_key")), document_id,
            _text(unit.get("parent_unit_id")), _text(unit.get("unit_type")),
            _text(unit.get("label")), _text(unit.get("number")), _text(unit.get("title")),
            _text(unit.get("legal_citation")), _text(unit.get("display_name")),
            json.dumps(breadcrumbs, ensure_ascii=False), _integer_order(unit.get("sequence")), start, end,
            _integer_order(unit.get("source_order")), json.dumps(dict(unit), ensure_ascii=False, sort_keys=True),
        )
        connection.execute("INSERT OR IGNORE INTO structural_units VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
        if connection.execute("SELECT changes()").fetchone()[0] == 0:
            continue
        stats.structural_units += 1
        aliases = [
            unit.get("unit_id"), unit.get("global_key"), unit.get("label"),
            unit.get("number"), unit.get("legal_citation"),
            unit.get("display_name"), unit.get("title"),
        ]
        aliases.extend(_generated_unit_aliases(unit))
        for alias in dict.fromkeys(value for value in (_text(x) for x in aliases) if value):
            connection.execute("INSERT OR IGNORE INTO unit_aliases VALUES (?, ?, ?)", (unit["unit_id"], alias, _norm(alias)))
        connection.execute(
            "INSERT INTO units_fts VALUES (?, ?, ?, ?, ?)",
            (unit["unit_id"], _text(unit.get("label")), _text(unit.get("title")), _text(unit.get("legal_citation")), _text(unit.get("display_name"))),
        )
    for fallback, chunk in enumerate(document.get("chunks") or []):
        if not isinstance(chunk, Mapping):
            continue
        text = _text(chunk.get("text"))
        if not text:
            continue
        chunk_id = _text(chunk.get("chunk_id")) or _hash(document_id, str(fallback), text)[:24]
        passage_id = "structured:{}".format(chunk_id)
        start, end = _page_range(chunk.get("page_range"))
        # Extractors append chunks in canonical reading order.  source_order is
        # intentionally not used here because GII represents it as a JSON list.
        order = fallback
        metadata = dict(chunk)
        connection.execute(
            """INSERT INTO passages(passage_id, document_id, unit_id, original_chunk_id, passage_kind, label,
               legal_citation, text, page_start, page_end, sort_order, token_count, metadata_json)
               VALUES (?, ?, ?, ?, 'structured_chunk', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (passage_id, document_id, _text(chunk.get("unit_id")) or None, chunk_id,
             _text(chunk.get("label")), _text(chunk.get("legal_citation")), text, start, end, order,
             chunk.get("token_count") or chunk.get("table_chunk_token_count"),
             json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
        )
        passage_pk = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("INSERT INTO passages_fts VALUES (?, ?, ?, ?)", (passage_pk, text, _text(document.get("title")), _text(chunk.get("legal_citation"))))
        stats.passages += 1


def _documents_from_raw(payload: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    documents = payload.get("documents")
    if isinstance(documents, list):
        yield from (item for item in documents if isinstance(item, Mapping))
    elif _text(payload.get("document_id")):
        yield payload


def _resolve_raw_path(manifest_path: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    return candidate if candidate.is_absolute() else manifest_path.parent / candidate


def _iter_manifest_raw_paths(manifest_path: Path) -> Iterator[Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("entries") or manifest.get("results") or []
    if not isinstance(records, list):
        return
    for record in records:
        if isinstance(record, Mapping) and _text(record.get("output_path")):
            yield _resolve_raw_path(manifest_path, _text(record["output_path"]))


def _tokenizer() -> tuple[callable, callable]:
    """Use the corpus token encoding when available; retain a cheap fallback."""

    try:
        import tiktoken  # type: ignore

        encoding = tiktoken.get_encoding("cl100k_base")
        return encoding.encode, encoding.decode
    except Exception:  # pragma: no cover - exercised only without optional dependency
        def encode(text: str) -> list[str]:
            return re.findall(r"\S+\s*", text)
        return encode, lambda tokens: "".join(tokens)


def _looks_like_heading(value: str) -> bool:
    value = " ".join(value.split()).strip()
    if not value or len(value) > 180:
        return False
    return bool(
        re.match(r"^(?:\d+(?:\.\d+){1,}|(?:Teil|Kapitel|Abschnitt)\s+\w+)", value, re.IGNORECASE)
        or (len(value) >= 4 and value.upper() == value and any(char.isalpha() for char in value))
    )


def _pdf_passages(path: Path, max_tokens: int, overlap: int) -> Iterator[tuple[str, int, int, int, str]]:
    try:
        import fitz  # type: ignore
    except ImportError as exc:  # pragma: no cover - runtime environment normally has fitz
        raise RuntimeError("PyMuPDF (fitz) is required for PDF fallback indexing") from exc
    encode, decode = _tokenizer()
    pdf = fitz.open(str(path))
    try:
        blocks: list[tuple[str, int, str]] = []
        current_heading = ""
        for page_index, page in enumerate(pdf):
            page_number = page_index + 1
            page_blocks = sorted(
                page.get_text("blocks") or [], key=lambda item: (item[1], item[0])
            )
            for block in page_blocks:
                raw = str(block[4] or "").strip()
                for paragraph in re.split(r"\n\s*\n+", raw):
                    text = re.sub(r"[ \t]+", " ", paragraph).strip()
                    if not text:
                        continue
                    first_line = text.splitlines()[0].strip()
                    if _looks_like_heading(first_line):
                        current_heading = first_line
                    blocks.append((text, page_number, current_heading))

        current_parts: list[str] = []
        current_count = 0
        current_start = 0
        current_end = 0
        current_heading = ""

        def emit_current() -> Optional[tuple[str, int, int, int, str]]:
            if not current_parts:
                return None
            return (
                "\n\n".join(current_parts).strip(),
                current_start,
                current_end,
                current_count,
                current_heading,
            )

        for text, page_number, heading in blocks:
            tokens = encode(text)
            if len(tokens) > max_tokens:
                ready = emit_current()
                if ready:
                    yield ready
                current_parts = []
                current_count = 0
                current_start = current_end = 0
                current_heading = ""
                start = 0
                while start < len(tokens):
                    end = min(len(tokens), start + max_tokens)
                    chunk = _text(decode(tokens[start:end]))
                    if chunk:
                        yield chunk, page_number, page_number, end - start, heading
                    if end == len(tokens):
                        break
                    start = max(end - overlap, start + 1)
                continue

            if current_parts and current_count + len(tokens) > max_tokens:
                ready = emit_current()
                if ready:
                    yield ready
                current_parts = []
                current_count = 0
                current_start = current_end = 0
                current_heading = ""

            if not current_parts:
                current_start = page_number
                current_heading = heading
            current_parts.append(text)
            current_count += len(tokens)
            current_end = page_number

        ready = emit_current()
        if ready:
            yield ready
    finally:
        pdf.close()


def _external_pdf_rows(register_path: Path, document_ids: Sequence[str], external_root: Optional[Path]) -> Iterator[sqlite3.Row]:
    if not document_ids:
        return
    connection = sqlite3.connect("file:{}?mode=ro".format(register_path.resolve()), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in document_ids)
        sql = """SELECT d.id, d.title, d.short_title, d.document_type, d.edition, d.language,
                         d.landing_url, f.role, f.local_path, f.storage_root,
                         f.sha256, f.page_count, f.media_type
                  FROM documents AS d JOIN files AS f ON f.document_id = d.id
                  WHERE d.id IN ({}) AND f.validation_status = 'valid'
                    AND lower(coalesce(f.media_type, '')) LIKE '%pdf%'
                  ORDER BY d.id, f.role, f.local_path""".format(placeholders)
        yield from connection.execute(sql, list(document_ids))
    finally:
        connection.close()


def _external_path(row: sqlite3.Row, register_path: Path, external_root: Optional[Path]) -> Path:
    local = Path(row["local_path"])
    if local.is_absolute():
        return local
    if external_root:
        return external_root / local
    # The register convention stores paths relative to external_documents.
    return register_path.parent / local


def _insert_pdf_fallbacks(
    connection: sqlite3.Connection,
    register_path: Path,
    document_ids: Sequence[str],
    external_root: Optional[Path],
    max_tokens: int,
    overlap: int,
    stats: BuildStats,
) -> None:
    for row in _external_pdf_rows(register_path, document_ids, external_root):
        path = _external_path(row, register_path, external_root)
        if not path.is_file():
            stats.warnings.append("selected external PDF unavailable: {}".format(path))
            continue
        external_id = row["id"]
        document_id = "external:{}".format(external_id)
        if connection.execute("SELECT 1 FROM documents WHERE document_id = ?", (document_id,)).fetchone():
            # A document can have multiple selected files; retain each as passages under one document.
            pass
        else:
            metadata = {key: row[key] for key in row.keys() if key not in {"local_path", "storage_root"}}
            connection.execute(
                """INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, 'external_pdf', 'pdf_fallback', ?, ?, 1, ?)""",
                (document_id, external_id, external_id, _text(row["title"]) or external_id,
                 _text(row["short_title"]), "", "", str(path), str(path), json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
            )
            aliases = [external_id, row["title"], row["short_title"]]
            for alias in dict.fromkeys(value for value in (_text(x) for x in aliases) if value):
                connection.execute("INSERT OR IGNORE INTO document_aliases VALUES (?, ?, ?)", (document_id, alias, _norm(alias)))
                connection.execute("INSERT INTO documents_fts VALUES (?, ?, ?)", (document_id, alias, _text(row["title"]) or external_id))
            stats.documents += 1
            stats.pdf_fallback_documents += 1
        file_hash = _text(row["sha256"]) or hashlib.sha256(path.read_bytes()).hexdigest()
        base_order = connection.execute("SELECT coalesce(max(sort_order), -1) + 1 FROM passages WHERE document_id = ?", (document_id,)).fetchone()[0]
        for ordinal, (text, start, end, token_count, heading) in enumerate(
            _pdf_passages(path, max_tokens, overlap)
        ):
            passage_id = "pdf:{}:{}:{:06d}".format(external_id, file_hash[:16], ordinal)
            page_label = str(start) if start == end else "{}–{}".format(start, end)
            citation = "{} S. {}".format(_text(row["short_title"]) or external_id, page_label)
            label = heading or _text(row["role"]) or "PDF-Fallback"
            metadata = {
                "fallback": True,
                "source_file": str(path),
                "source_file_role": _text(row["role"]),
                "file_sha256": file_hash,
                "heading": heading,
                "page_start": start,
                "page_end": end,
            }
            connection.execute(
                """INSERT OR IGNORE INTO passages(passage_id, document_id, unit_id, original_chunk_id,
                   passage_kind, label, legal_citation, text, page_start, page_end, sort_order,
                   token_count, metadata_json) VALUES (?, ?, NULL, NULL, 'pdf_fallback', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (passage_id, document_id, label, citation, text, start, end, base_order + ordinal,
                 token_count, json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
            )
            if connection.execute("SELECT changes()").fetchone()[0]:
                passage_pk = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                connection.execute("INSERT INTO passages_fts VALUES (?, ?, ?, ?)", (passage_pk, text, _text(row["title"]), citation))
                stats.passages += 1
                stats.pdf_fallback_passages += 1


def build_index(
    db_path: Path | str,
    manifests: Sequence[Path | str],
    *,
    external_register_path: Optional[Path | str] = None,
    external_document_ids: Sequence[str] = (),
    external_root: Optional[Path | str] = None,
    pdf_chunk_tokens: int = DEFAULT_PDF_CHUNK_TOKENS,
    pdf_chunk_overlap: int = DEFAULT_PDF_CHUNK_OVERLAP,
) -> BuildStats:
    """Build a complete index in a sibling temporary DB, then atomically replace.

    ``external_document_ids`` is intentionally opt-in: passing no IDs means
    no generic PDFs are read.  This keeps the absence of a special parser
    explicit in the data model.
    """

    if pdf_chunk_tokens < 32 or pdf_chunk_overlap < 0 or pdf_chunk_overlap >= pdf_chunk_tokens:
        raise ValueError("invalid PDF token chunk configuration")
    destination = Path(db_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".{}.".format(destination.name), suffix=".tmp", dir=str(destination.parent))
    os.close(descriptor)
    temporary = Path(temporary_name)
    stats = BuildStats()
    try:
        connection = sqlite3.connect(str(temporary))
        try:
            _create_schema(connection)
            connection.execute("BEGIN")
            for manifest_value in manifests:
                manifest_path = Path(manifest_value)
                stats.manifests += 1
                for raw_path in _iter_manifest_raw_paths(manifest_path):
                    if not raw_path.is_file():
                        stats.skipped_raw_files += 1
                        stats.warnings.append("raw JSON missing: {}".format(raw_path))
                        continue
                    try:
                        payload = json.loads(raw_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        stats.skipped_raw_files += 1
                        stats.warnings.append("raw JSON unreadable {}: {}".format(raw_path, exc))
                        continue
                    for document in _documents_from_raw(payload):
                        _insert_structured_document(connection, document, str(raw_path), stats)
            if external_document_ids:
                if not external_register_path:
                    raise ValueError("external_register_path is required for external_document_ids")
                _insert_pdf_fallbacks(
                    connection, Path(external_register_path), tuple(dict.fromkeys(external_document_ids)),
                    Path(external_root) if external_root else None, pdf_chunk_tokens, pdf_chunk_overlap, stats,
                )
                connection.execute("INSERT INTO metadata VALUES (?, ?)", ("external_register_path", str(Path(external_register_path).resolve())))
            connection.execute("INSERT INTO metadata VALUES (?, ?)", ("schema_version", INDEX_SCHEMA_VERSION))
            connection.execute("INSERT INTO metadata VALUES (?, ?)", ("build_stats", json.dumps(asdict(stats), ensure_ascii=False, sort_keys=True)))
            connection.commit()
            connection.execute("PRAGMA optimize")
        finally:
            connection.close()
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return stats


__all__ = [
    "BuildStats", "DEFAULT_PDF_CHUNK_OVERLAP", "DEFAULT_PDF_CHUNK_TOKENS", "INDEX_SCHEMA_VERSION",
    "RetrievalIndex", "build_index",
]
