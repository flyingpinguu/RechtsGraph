import asyncio
import json

import pytest
from mcp import Client

from rechtsgraph_mcp import server


DOCUMENT = {"document_id": "doc_1", "title": "Demo Act", "abbreviation": "DemoG"}
UNIT = {"unit_id": "unit_1", "document_id": "doc_1", "label": "§ 1"}
PASSAGE = {"passage_id": "passage_1", "document_id": "doc_1", "unit_id": "unit_1", "text": "Actual provision text."}


def _tool_payload(response):
    structured = response.structured_content
    if structured:
        return structured.get("result", structured)
    assert len(response.content) == 1
    return json.loads(response.content[0].text)


@pytest.mark.parametrize(
    ("index_payload", "expected_status", "expected_hint_tool"),
    [
        ({"document": None, "unit": None, "passages": [], "truncated": False},
         "document_not_found", "find_documents"),
        ({"document": DOCUMENT, "unit": None, "passages": [], "truncated": False},
         "unit_not_found", "get_document_outline"),
        ({"document": DOCUMENT, "unit": UNIT, "passages": [], "truncated": False},
         "no_text", "search_corpus"),
    ],
)
def test_structural_lookup_reports_distinct_empty_outcomes(
    monkeypatch, index_payload, expected_status, expected_hint_tool
):
    def fake_index_call(method, *args, **kwargs):
        assert method == "get_structural_unit"
        assert args == ("DemoG", "§ 1")
        return index_payload

    monkeypatch.setattr(server, "_index_call", fake_index_call)

    async def call_tool():
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "get_structural_unit", {"document_ref": "DemoG", "unit_ref": "§ 1"}
            )

    response = asyncio.run(call_tool())
    assert not response.is_error
    assert response.structured_content is None
    result = _tool_payload(response)
    assert result["ok"] is False
    assert result["status"] == expected_status
    assert result["error"]
    assert expected_hint_tool in result["hint"]
    if index_payload["document"] is None:
        assert result["document"] is None
    else:
        assert result["document"]["document_ref"] == "DemoG"
    if index_payload["unit"] is None:
        assert result["unit"] is None
    else:
        assert result["unit"]["unit_ref"] == "§ 1"
    assert result["passages"] == []
    assert result["passage_count"] == 0


def test_structural_lookup_preserves_success_content(monkeypatch):
    payload = {
        "document": DOCUMENT,
        "unit": UNIT,
        "passages": [PASSAGE],
        "truncated": False,
    }
    monkeypatch.setattr(server, "_index_call", lambda *args, **kwargs: payload)

    async def call_tool():
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "get_structural_unit", {"document_ref": "DemoG", "unit_ref": "§ 1"}
            )

    response = asyncio.run(call_tool())
    assert not response.is_error
    result = _tool_payload(response)
    assert result == {
        "ok": True,
        "document": {"document_ref": "DemoG", "title": "Demo Act"},
        "unit": {"unit_ref": "§ 1"},
        "passage_count": 1,
        "passages": [{"text": "Actual provision text."}],
        "truncated": False,
    }


def test_structural_lookup_full_detail_retains_previous_projection(monkeypatch):
    payload = {
        "document": DOCUMENT,
        "unit": UNIT,
        "passages": [PASSAGE],
        "truncated": False,
    }
    monkeypatch.setattr(server, "_index_call", lambda *args, **kwargs: payload)

    async def call_tool():
        async with Client(server.mcp) as client:
            return await client.call_tool(
                "get_structural_unit",
                {"document_ref": "DemoG", "unit_ref": "§ 1", "detail": "full"},
            )

    response = asyncio.run(call_tool())
    assert not response.is_error
    result = _tool_payload(response)
    assert result["document"] == DOCUMENT
    assert result["unit"] == UNIT
    assert result["passages"] == [PASSAGE]
