"""Read-only search over the supplementary-document register."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional


def normalize_alias(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


class ExternalRegistry:
    """Search metadata without claiming that unavailable text is in the corpus."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError("external register is missing: {}".format(self.path))
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(self.path.resolve()), uri=True
        )
        connection.row_factory = sqlite3.Row
        return connection

    def status(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {"available": False, "path": str(self.path), "documents": 0}
        with self._connect() as connection:
            documents = connection.execute("SELECT count(*) FROM documents").fetchone()[0]
            valid_files = connection.execute(
                "SELECT count(*) FROM files WHERE validation_status = 'valid'"
            ).fetchone()[0]
        return {
            "available": True,
            "path": str(self.path),
            "documents": int(documents),
            "valid_local_files": int(valid_files),
        }

    def search(
        self,
        query: str,
        *,
        category: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        normalized = normalize_alias(query)
        if not normalized:
            raise ValueError("query must contain searchable text")
        limit = max(1, min(int(limit), 50))
        like = "%{}%".format(normalized)
        with self._connect() as connection:
            category_clause = "AND d.category = :category" if category else ""
            rows = connection.execute(
                """
                WITH candidates AS (
                  SELECT d.id, 0 AS rank
                  FROM aliases a JOIN documents d ON d.id = a.document_id
                  WHERE a.normalized_alias = :normalized {category_clause}
                  UNION
                  SELECT d.id, 1 AS rank
                  FROM documents d
                  WHERE (
                    lower(d.id) LIKE :like OR
                    lower(coalesce(d.short_title, '')) LIKE :like OR
                    lower(d.title) LIKE :like
                  ) {category_clause}
                  UNION
                  SELECT d.id, 2 AS rank
                  FROM aliases a JOIN documents d ON d.id = a.document_id
                  WHERE a.normalized_alias LIKE :like {category_clause}
                )
                SELECT d.*, min(c.rank) AS match_rank
                FROM candidates c JOIN documents d ON d.id = c.id
                GROUP BY d.id
                ORDER BY match_rank, d.graph_mention_count DESC, d.title
                LIMIT :limit
                """.format(category_clause=category_clause),
                {
                    "normalized": normalized,
                    "like": like,
                    "category": category,
                    "limit": limit,
                },
            ).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                document_id = str(row["id"])
                aliases = [
                    item[0]
                    for item in connection.execute(
                        "SELECT alias FROM aliases WHERE document_id = ? ORDER BY alias",
                        (document_id,),
                    ).fetchall()
                ]
                files = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT role, local_path, media_type, size_bytes, page_count,
                               validation_status, download_status, download_url, final_url
                        FROM files WHERE document_id = ? ORDER BY role, local_path
                        """,
                        (document_id,),
                    ).fetchall()
                ]
                data = dict(row)
                data["aliases"] = aliases
                data["files"] = files
                data["has_valid_local_text"] = any(
                    item.get("validation_status") == "valid" and item.get("local_path")
                    for item in files
                )
                results.append(data)
        return {"query": query, "count": len(results), "results": results}


__all__ = ["ExternalRegistry", "normalize_alias"]
