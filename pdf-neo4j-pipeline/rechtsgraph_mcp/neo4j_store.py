"""Narrow, read-only Neo4j access for graph-context MCP tools.

The retrieval tools do not require Neo4j to be running.  This adapter is kept
deliberately small: callers can inspect legal references, but cannot submit
arbitrary Cypher through the MCP server.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class Neo4jUnavailable(RuntimeError):
    """Raised when the configured Neo4j database cannot be queried."""


@dataclass(frozen=True)
class Neo4jSettings:
    url: str = "http://localhost:7474"
    database: str = "gii-xml-chunked"
    username: Optional[str] = None
    password: Optional[str] = None
    timeout_seconds: float = 5.0

    @classmethod
    def from_environment(cls) -> "Neo4jSettings":
        return cls(
            url=os.environ.get("RECHTSGRAPH_NEO4J_URL", "http://localhost:7474"),
            database=os.environ.get("RECHTSGRAPH_NEO4J_DATABASE", "gii-xml-chunked"),
            username=os.environ.get("RECHTSGRAPH_NEO4J_USERNAME") or None,
            password=os.environ.get("RECHTSGRAPH_NEO4J_PASSWORD") or None,
            timeout_seconds=float(os.environ.get("RECHTSGRAPH_NEO4J_TIMEOUT", "5")),
        )


class Neo4jStore:
    """Execute fixed, parameterized read queries against RechtsGraph."""

    def __init__(self, settings: Optional[Neo4jSettings] = None) -> None:
        self.settings = settings or Neo4jSettings.from_environment()

    def _endpoint(self) -> str:
        return "{}/db/{}/tx/commit".format(
            self.settings.url.rstrip("/"), self.settings.database
        )

    def _query(self, cypher: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        body = json.dumps(
            {"statements": [{"statement": cypher, "parameters": parameters or {}}]}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.settings.username is not None:
            raw = "{}:{}".format(
                self.settings.username, self.settings.password or ""
            ).encode("utf-8")
            headers["Authorization"] = "Basic {}".format(
                base64.b64encode(raw).decode("ascii")
            )
        request = Request(self._endpoint(), data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.settings.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")[:500]
            raise Neo4jUnavailable(
                "Neo4j HTTP {}: {}".format(exc.code, message)
            ) from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise Neo4jUnavailable("Neo4j is unavailable: {}".format(exc)) from exc

        errors = payload.get("errors") or []
        if errors:
            first = errors[0]
            raise Neo4jUnavailable(
                "Neo4j query failed: {} ({})".format(
                    first.get("message") or "unknown error",
                    first.get("code") or "unknown code",
                )
            )
        results = payload.get("results") or []
        if not results:
            return []
        columns = results[0].get("columns") or []
        return [
            dict(zip(columns, item.get("row") or []))
            for item in results[0].get("data") or []
        ]

    def status(self) -> Dict[str, Any]:
        try:
            rows = self._query(
                "MATCH (d:Document) RETURN count(d) AS documents"
            )
        except Neo4jUnavailable as exc:
            return {
                "available": False,
                "database": self.settings.database,
                "url": self.settings.url,
                "error": str(exc),
            }
        return {
            "available": True,
            "database": self.settings.database,
            "url": self.settings.url,
            "documents": int(rows[0]["documents"]) if rows else 0,
        }

    def explore_references(
        self,
        node_id: str,
        *,
        direction: str = "outgoing",
        limit: int = 30,
    ) -> Dict[str, Any]:
        if direction not in {"outgoing", "incoming"}:
            raise ValueError("direction must be 'outgoing' or 'incoming'")
        if not node_id or len(node_id) > 500:
            raise ValueError("node_id must be a non-empty graph identifier")
        limit = max(1, min(int(limit), 100))
        arrow = "-[r:REFERS_TO]->" if direction == "outgoing" else "<-[r:REFERS_TO]-"
        edge_source = "origin" if direction == "outgoing" else "other"
        edge_target = "other" if direction == "outgoing" else "origin"
        cypher = """
MATCH (origin:GraphNode)
WHERE origin.graph_id = $node_id OR origin.global_key = $node_id
MATCH (origin){}(other:GraphNode)
RETURN origin.graph_id AS queried_node_id,
       origin.global_key AS queried_node_global_key,
       origin.display_name AS queried_node_display_name,
       origin.legal_citation AS queried_node_legal_citation,
       other.graph_id AS neighbor_node_id,
       other.global_key AS neighbor_node_global_key,
       other.display_name AS neighbor_node_display_name,
       other.legal_citation AS neighbor_node_legal_citation,
       labels(other) AS neighbor_node_labels,
       {edge_source}.graph_id AS edge_source_id,
       {edge_source}.global_key AS edge_source_global_key,
       {edge_target}.graph_id AS edge_target_id,
       {edge_target}.global_key AS edge_target_global_key,
       r.mention_text AS mention_text,
       r.resolution_status AS resolution_status,
       r.resolution_method AS resolution_method,
       r.target_document_key AS target_document_key,
       r.requested_target_global_key AS requested_target_global_key
LIMIT $limit
""".format(
            arrow,
            edge_source=edge_source,
            edge_target=edge_target,
        )
        rows = self._query(cypher, {"node_id": node_id, "limit": limit})
        return {
            "node_id": node_id,
            "direction": direction,
            "continuation_node_id_field": "neighbor_node_id",
            "count": len(rows),
            "references": rows,
        }


__all__ = ["Neo4jSettings", "Neo4jStore", "Neo4jUnavailable"]
