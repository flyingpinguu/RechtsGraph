import asyncio
import json

from mcp import Client

from rechtsgraph_mcp.index import build_index
from tests.test_retrieval_index import _raw_document, _write_manifest


def _tool_payload(response):
    structured = response.structured_content
    if structured:
        return structured.get("result", structured)
    assert len(response.content) == 1
    return json.loads(response.content[0].text)


def test_mcp_exposes_bounded_retrieval_tools(tmp_path, monkeypatch):
    database = tmp_path / "index.sqlite"
    build_index(database, [_write_manifest(tmp_path, _raw_document())])
    monkeypatch.setenv("RECHTSGRAPH_INDEX_PATH", str(database))

    from rechtsgraph_mcp.server import mcp

    async def exercise_server():
        async with Client(mcp) as client:
            assert client.instructions is not None
            assert "# RechtsGraph agent guide" in client.instructions
            assert "Call tools directly without progress commentary" in client.instructions

            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {
                "get_service_status",
                "find_documents",
                "get_document_outline",
                "get_structural_unit",
                "search_corpus",
                "get_context",
                "search_external_register",
                "explore_references",
            }
            for name in {
                "find_documents", "get_document_outline", "get_structural_unit",
                "search_corpus", "get_context", "search_external_register",
            }:
                tool = next(tool for tool in listed.tools if tool.name == name)
                assert tool.input_schema["properties"]["detail"]["enum"] == [
                    "compact", "full"
                ]
                assert tool.output_schema is None

            found = await client.call_tool("find_documents", {"query": "DemoG"})
            assert not found.is_error
            assert found.structured_content is None
            payload = _tool_payload(found)
            assert payload["results"][0]["document_ref"] == "DemoG"
            assert "document_id" not in payload["results"][0]
            assert "raw_path" not in payload["results"][0]

            provision = await client.call_tool(
                "get_structural_unit",
                {"document_ref": "DemoG", "unit_ref": "§ 1 Abs. 1"},
            )
            assert not provision.is_error
            assert provision.structured_content is None
            provision_payload = _tool_payload(provision)
            assert provision_payload["requested_subsection"] == "Abs. 1"
            assert provision_payload["unit"] == {
                "unit_ref": "DemoG § 1 Abs. 1", "title": "Details"
            }
            assert provision_payload["passages"][0]["citation"] == "DemoG § 1 Abs. 1"
            assert "matching_passages" not in provision_payload
            assert "passage_id" not in provision_payload["passages"][0]

            hits = await client.call_tool(
                "search_corpus", {"query": "Schutz Wasser", "corpus": "de"}
            )
            assert hits.structured_content is None
            hit_payload = _tool_payload(hits)
            assert hit_payload["results"][0]["passage_id"] == "structured:chunk_one"
            assert hit_payload["results"][0]["document_ref"] == "DemoG"
            assert "snippet" in hit_payload["results"][0]
            assert "text" not in hit_payload["results"][0]
            assert "evidence" not in hit_payload["results"][0]

            status = await client.call_tool(
                "get_service_status", {"component": "retrieval"}
            )
            status_payload = status.structured_content["result"]
            assert status_payload["retrieval"]["documents"] == 1

    asyncio.run(exercise_server())


def test_mcp_preserves_recorded_status_without_claiming_temporal_applicability(tmp_path, monkeypatch):
    raw = _raw_document()
    raw["documents"][0]["metadata"].update({
        "source_url": "https://example.org/official/demo",
        "legal_status": "repealed",
        "consolidation_date": "2007-01-16",
    })
    database = tmp_path / "index.sqlite"
    build_index(database, [_write_manifest(tmp_path, raw)])
    monkeypatch.setenv("RECHTSGRAPH_INDEX_PATH", str(database))

    from rechtsgraph_mcp.server import mcp

    def assert_evidence(record):
        evidence = record["evidence"]
        assert evidence["source_url"] == "https://example.org/official/demo"
        assert evidence["legal_status"] == {"raw": "repealed", "normalized": "not_in_force"}
        assert evidence["consolidation_date"] == "2007-01-16"
        assert evidence["temporal_applicability"] == "undetermined"
        assert evidence["date_coverage"] == "unknown"
        assert "metadata_json" not in record

    async def exercise_server():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_documents", {"query": "DemoG", "detail": "full"}
            )
            assert_evidence(_tool_payload(result)["results"][0])
            result = await client.call_tool(
                "search_corpus", {"query": "Schutz Wasser", "detail": "full"}
            )
            hit = _tool_payload(result)["results"][0]
            assert_evidence(hit)
            result = await client.call_tool("get_context", {
                "passage_ids": [hit["passage_id"]], "before": 0, "after": 1,
                "detail": "full",
            })
            for passage in _tool_payload(result)["passages"]:
                assert_evidence(passage)
            result = await client.call_tool("get_structural_unit", {
                "document_ref": "DemoG", "unit_ref": "§ 1",
            })
            payload = _tool_payload(result)
            assert "evidence" not in payload["document"]
            assert payload["passages"][0]["text"]
            assert "evidence" not in payload["passages"][0]

            result = await client.call_tool("get_structural_unit", {
                "document_ref": "DemoG", "unit_ref": "§ 1", "detail": "full",
            })
            payload = _tool_payload(result)
            assert_evidence(payload["document"])
            for passage in payload["passages"]:
                assert_evidence(passage)

    asyncio.run(exercise_server())
