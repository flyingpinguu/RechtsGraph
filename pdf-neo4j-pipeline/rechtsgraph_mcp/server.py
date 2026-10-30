"""stdio MCP server exposing RechtsGraph as tools to an existing agent host."""

from __future__ import annotations

import json
import os
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .external_registry import ExternalRegistry
from .index import RetrievalIndex
from .neo4j_store import Neo4jStore, Neo4jUnavailable
from .embeddings import EmbeddingSettings, load_local_env
from .vector_search import Neo4jVectorIndex, search as ranked_search
from .presentation import (
    compact_context,
    compact_document,
    compact_external_search,
    compact_outline,
    compact_search_result,
    compact_structural_unit,
    minimal_document,
)


LOCAL_ERRORS = (
    FileNotFoundError,
    OSError,
    RuntimeError,
    ValueError,
    sqlite3.Error,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_INDEX = PROJECT_ROOT / "output" / "retrieval" / "rechtsgraph.sqlite"
DEFAULT_EXTERNAL_REGISTER = REPO_ROOT / "external_documents" / "register.sqlite"
DEFAULT_AGENT_GUIDE = Path(__file__).with_name("AGENT_GUIDE.md")
FALLBACK_SERVER_INSTRUCTIONS = (
    "RechtsGraph supplies read-only German and EU legal retrieval tools to an "
    "existing agent. Distinguish corpus text, graph connections, external "
    "register metadata, and web evidence."
)


def _load_server_instructions() -> str:
    guide_path = Path(
        os.environ.get("RECHTSGRAPH_AGENT_GUIDE", str(DEFAULT_AGENT_GUIDE))
    )
    try:
        instructions = guide_path.read_text(encoding="utf-8").strip()
    except OSError:
        return FALLBACK_SERVER_INSTRUCTIONS
    return instructions or FALLBACK_SERVER_INSTRUCTIONS


SERVER_INSTRUCTIONS = _load_server_instructions()

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

EMBEDDING_SEARCH = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True,
)

mcp = MCPServer(
    "rechtsgraph",
    title="RechtsGraph Legal Retrieval",
    description=(
        "Search German and EU law by document, provision, content, and graph "
        "reference. Designed to complement, not constrain, the host agent's "
        "own reasoning and web tools."
    ),
    instructions=SERVER_INSTRUCTIONS,
    version="0.4.0",
)


def _index_path() -> Path:
    return Path(os.environ.get("RECHTSGRAPH_INDEX_PATH", str(DEFAULT_INDEX)))


def _external_register_path() -> Path:
    return Path(
        os.environ.get(
            "RECHTSGRAPH_EXTERNAL_REGISTER", str(DEFAULT_EXTERNAL_REGISTER)
        )
    )


def _index() -> RetrievalIndex:
    return RetrievalIndex(_index_path())


def _index_call(method: str, *args: Any, **kwargs: Any) -> Any:
    # Open read-only per call. MCP hosts may execute synchronous tools on
    # different worker threads, and the index may be atomically rebuilt.
    with _index() as index:
        return getattr(index, method)(*args, **kwargs)


@lru_cache(maxsize=1)
def _external_registry() -> ExternalRegistry:
    return ExternalRegistry(_external_register_path())


@lru_cache(maxsize=1)
def _neo4j() -> Neo4jStore:
    return Neo4jStore()


def _error(source: str, exc: BaseException) -> Dict[str, Any]:
    return {
        "ok": False,
        "source": source,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _tool_json(payload: Dict[str, Any]) -> str:
    """Serialize one model-facing representation without pretty-print bloat.

    MCP structured tools duplicate a mapping into both ``content`` and
    ``structuredContent`` for backwards compatibility.  Potentially large
    retrieval tools intentionally use compact content-only JSON instead:
    it works with every MCP client, remains machine-readable, and is sent only
    once over the protocol.
    """

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _service_status(component: str = "all") -> Dict[str, Any]:
    component = component.casefold().strip()
    if component not in {"all", "retrieval", "external_register", "neo4j", "vectors"}:
        raise ValueError(
            "component must be all, retrieval, external_register, neo4j, or vectors"
        )
    payload: Dict[str, Any] = {
        "ok": True,
        "server_version": "0.4.0",
        "component": component,
    }
    if component in {"all", "retrieval"}:
        try:
            payload["retrieval"] = _index_call("status")
        except LOCAL_ERRORS as exc:
            payload["retrieval"] = _error("retrieval_index", exc)
    if component in {"all", "external_register"}:
        try:
            payload["external_register"] = _external_registry().status()
        except LOCAL_ERRORS as exc:
            payload["external_register"] = _error("external_register", exc)
    if component in {"all", "neo4j"}:
        payload["neo4j"] = _neo4j().status()
    if component == "vectors":
        try:
            payload["vectors"] = Neo4jVectorIndex(EmbeddingSettings.from_environment(), _neo4j()).status()
        except LOCAL_ERRORS as exc:
            payload["vectors"] = _error("vector_index", exc)
    return payload


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_service_status(component: str = "all") -> Dict[str, Any]:
    """Check retrieval, external-register, or Neo4j availability cheaply.

    For graph questions call this with component `neo4j` before retrieving
    supporting text or invoking `explore_references`. The result is status, not
    legal evidence.
    """

    try:
        return _service_status(component)
    except LOCAL_ERRORS as exc:
        return _error("service_status", exc)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def find_documents(
    query: str,
    corpus: Optional[str] = None,
    limit: int = 5,
    detail: Literal["compact", "full"] = "compact",
) -> str:
    """Find a German, EU, or indexed supplementary document.

    Resolves exact abbreviations, CELEX identifiers, stable keys, and aliases
    before ranked title search. The compact default returns only reusable
    document_ref values and short titles. Use detail="full" only for aliases,
    source/provenance, scores, status, or internal IDs. The agent may also use
    web search before or after this tool whenever useful.
    """

    try:
        limit = max(1, min(int(limit), 8))
        full_results = _index_call(
            "find_documents", query, corpus=corpus, limit=limit
        )
        results = [
            compact_document(result) if detail == "full" else minimal_document(result)
            for result in full_results
        ]
        return _tool_json(
            {"ok": True, "query": query, "count": len(results), "results": results}
        )
    except LOCAL_ERRORS as exc:
        return _tool_json(_error("retrieval_index", exc))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def get_document_outline(
    document_ref: str,
    max_depth: int = 1,
    limit: int = 30,
    detail: Literal["compact", "full"] = "compact",
) -> str:
    """Return the addressable hierarchy of a selected document.

    This is useful before choosing a paragraph, article, annex, or table in an
    unfamiliar act. The compact default returns human-usable document/unit
    references, titles, types, and depths without internal IDs or provenance.
    Use detail="full" only when pages, stable IDs, or source metadata are
    actually needed. Generic PDF passages are never presented as StructuralUnits.
    """

    try:
        max_depth = max(0, min(int(max_depth), 3))
        limit = max(1, min(int(limit), 80))
        payload = _index_call(
            "get_document_outline",
            document_ref,
            max_depth=max_depth,
            limit=limit,
        )
        return _tool_json({"ok": True, **compact_outline(payload, detail=detail)})
    except LOCAL_ERRORS as exc:
        return _tool_json(_error("retrieval_index", exc))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def get_structural_unit(
    document_ref: str,
    unit_ref: str,
    include_descendants: bool = True,
    max_passages: int = 10,
    max_chars: int = 6000,
    detail: Literal["compact", "full"] = "compact",
) -> str:
    """Resolve and return a paragraph, article, annex, table, or stable unit key.

    The unit is always resolved inside the selected document. Examples include
    `§ 6`, `§ 6 Abs. 2`, `Art. 4`, `Anlage 1`, and an address returned by an
    outline. The default response contains legal citations, text, and only
    actionable passage cautions. Use detail="full" when provenance, page
    ranges, graph IDs, or other internal metadata are actually needed.
    """

    try:
        max_passages = max(1, min(int(max_passages), 20))
        max_chars = max(500, min(int(max_chars), 12000))
        payload = _index_call(
            "get_structural_unit",
            document_ref,
            unit_ref,
            include_descendants=include_descendants,
            max_passages=max_passages,
            max_chars=max_chars,
        )
        result = compact_structural_unit(payload, detail=detail)
        if payload.get("document") is None:
            return _tool_json({
                "ok": False,
                "status": "document_not_found",
                "error": f"Document {document_ref!r} was not found in the retrieval index.",
                "hint": "Use find_documents to resolve a document_ref, or search the web for an authoritative source.",
                **result,
            })
        if payload.get("unit") is None:
            return _tool_json({
                "ok": False,
                "status": "unit_not_found",
                "error": f"Unit {unit_ref!r} was not found in the selected document.",
                "hint": "Use get_document_outline to find an addressable unit, or search the web for the provision.",
                **result,
            })
        if not any(passage.get("text", "").strip() for passage in result["passages"]):
            return _tool_json({
                "ok": False,
                "status": "no_text",
                "error": "The unit exists, but no passage text was returned.",
                "hint": "Try search_corpus for the provision, or search the web for an authoritative text.",
                **result,
            })
        return _tool_json({"ok": True, **result})
    except LOCAL_ERRORS as exc:
        return _tool_json(_error("retrieval_index", exc))


@mcp.tool(annotations=EMBEDDING_SEARCH, structured_output=False)
def search_corpus(
    query: str,
    corpus: Optional[str] = None,
    document_ref: Optional[str] = None,
    limit: int = 5,
    snippet_chars: int = 900,
    detail: Literal["compact", "full"] = "compact",
    mode: Literal["auto", "lexical", "semantic", "hybrid"] = "auto",
) -> str:
    """Search legal passages using lexical, semantic or hybrid retrieval.

    The compact default returns the passage ID needed by ``get_context``, a
    reusable document reference, legal citation, and bounded snippet. Use
    detail="full" only when page, provenance, source, or internal graph fields
    are needed. Auto uses RECHTSGRAPH_SEARCH_MODE (default lexical). Semantic
    needs an embedding index and sends the query to OpenRouter; cached queries
    are reused. Hybrid fuses BM25/vector ranks and visibly falls back to lexical
    if vectors are unavailable. Vector coverage can be partial. Neither a hit
    nor stored metadata establishes completeness or temporal applicability.
    """

    try:
        limit = max(1, min(int(limit), 8))
        snippet_chars = max(200, min(int(snippet_chars), 1000))
        load_local_env()
        effective_mode = os.environ.get("RECHTSGRAPH_SEARCH_MODE", "lexical") if mode == "auto" else mode
        if effective_mode == "lexical":
            payload = {"results": _index_call("search_corpus", query, corpus=corpus,
                                             document_ref=document_ref, limit=limit),
                       "mode": "lexical", "warnings": []}
        else:
            with _index() as index:
                payload = ranked_search(index, query, mode=effective_mode, limit=limit,
                                        corpus=corpus, document_ref=document_ref)
        full_results = payload.pop("results")
        results = [
            compact_search_result(result, query, snippet_chars, detail=detail)
            for result in full_results
        ]
        return _tool_json({
            "ok": True,
            "query": query,
            "count": len(results),
            "results": results,
            **payload,
        })
    except LOCAL_ERRORS as exc:
        return _tool_json(_error("retrieval_index", exc))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def get_context(
    passage_ids: List[str],
    before: int = 1,
    after: int = 1,
    max_chars: int = 12000,
    detail: Literal["compact", "full"] = "compact",
) -> str:
    """Expand passage hits with ordered neighboring context and parent units.

    Call this after corpus search when a hit is useful but too narrow. Context
    expansion is explicit and bounded; the host agent decides how much it needs.
    The compact default keeps the text, citation, document_ref, and actionable
    cautions. Use detail="full" for provenance, pages, and internal IDs.
    """

    try:
        passage_ids = passage_ids[:5]
        before = max(0, min(int(before), 2))
        after = max(0, min(int(after), 2))
        max_chars = max(500, min(int(max_chars), 20000))
        payload = _index_call(
            "get_context",
            passage_ids,
            before=before,
            after=after,
            max_chars=max_chars,
        )
        return _tool_json({"ok": True, **compact_context(payload, detail=detail)})
    except LOCAL_ERRORS as exc:
        return _tool_json(_error("retrieval_index", exc))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def search_external_register(
    query: str,
    category: Optional[str] = None,
    limit: int = 5,
    detail: Literal["compact", "full"] = "compact",
) -> str:
    """Search known external documents, including unavailable metadata entries.

    The compact default returns identity, category, availability, and whether
    the document can be queried with ``search_corpus(corpus="external")``.
    Use detail="full" for aliases, files, URLs, licensing, and graph counts.
    This tool is complementary to corpus and web search.
    """

    try:
        limit = max(1, min(int(limit), 8))
        payload = _external_registry().search(
            query, category=category, limit=limit
        )
        for result in payload.get("results") or []:
            result["indexed_for_fulltext"] = False
            matches = _index_call(
                "find_documents", result.get("id") or "", corpus="external", limit=1
            )
            expected_id = "external:{}".format(result.get("id") or "")
            match = next(
                (
                    item
                    for item in matches
                    if item.get("document_id") == expected_id
                    or item.get("document_key") == result.get("id")
                ),
                None,
            )
            if match:
                result["indexed_for_fulltext"] = True
                result["retrieval_document_id"] = match.get("document_id")
        return _tool_json(
            {"ok": True, **compact_external_search(payload, detail=detail)}
        )
    except LOCAL_ERRORS as exc:
        return _tool_json(_error("external_register", exc))


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def explore_references(
    node_id: str,
    direction: str = "outgoing",
    limit: int = 30,
) -> Dict[str, Any]:
    """Inspect bounded incoming or outgoing REFERS_TO edges for a returned node.

    This optional GraphRAG tool uses the live Neo4j database and clearly reports
    when that database or its credentials are unavailable. Continue a traversal
    with the returned ``neighbor_node_id``; explicit edge source/target fields
    retain the true citation direction. It never exposes arbitrary Cypher.
    """

    try:
        limit = max(1, min(int(limit), 50))
        return {
            "ok": True,
            **_neo4j().explore_references(
                node_id, direction=direction, limit=limit
            ),
        }
    except (Neo4jUnavailable, RuntimeError, ValueError) as exc:
        return _error("neo4j", exc)


@mcp.resource(
    "rechtsgraph://status",
    name="rechtsgraph-status",
    title="RechtsGraph corpus and service status",
    description="Current local retrieval index, external register, and Neo4j availability.",
    mime_type="application/json",
)
def rechtsgraph_status() -> str:
    payload = _service_status("all")
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()


__all__ = ["mcp", "main"]
