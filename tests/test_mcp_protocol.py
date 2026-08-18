"""MCP 2 in-memory protocol regressions for the public tool surface.

Ported from superbrian/master commit eda5493 (branch
001-mcp2-migration-and-regression-tests, T006). Two of the three
original cases depend on superbrian's flattened ``RenderDocClient``
(``server.client.RenderDocClient``) and its ``_serialized_operation``
lock, which we do not adopt -- our server uses the alias-routed
``ConnectionPool`` instead. Per docs/SYNC_SUPERBRIAN.md Stage 4, those
two cases are skipped here and deferred to Stage 5, which ports the
operation-locking behavior onto ``ConnectionPool`` and re-points the
``fake_renderdoc_client`` fixture at the pool interface. The
tool-name/schema stability case needs only the MCP SDK v2 swap and so
runs today.
"""

import json
import threading

import anyio
import pytest
from mcp import Client

from server.app import mcp


def _model_payload(model) -> dict:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _json_text_result(payload: dict):
    assert payload["isError"] is False
    assert payload["content"][0]["type"] == "text"
    return json.loads(payload["content"][0]["text"])


@pytest.mark.anyio
async def test_tool_names_and_input_schemas_are_stable():
    async with Client(mcp) as client:
        listing = _model_payload(await client.list_tools())

    tools = {tool["name"]: tool for tool in listing["tools"]}
    # Adapted from superbrian: our dev branch ships an additional "Task"
    # tool (manages Eval(async_mode=True) async tasks) that superbrian's
    # fork never built. The four superbrian tools must still be present
    # with stable schemas; Task is our own superset addition.
    assert set(tools) == {"Eval", "Search-API", "Get-Texture", "Instance", "Task"}

    eval_schema = tools["Eval"]["inputSchema"]
    assert eval_schema["required"] == ["code"]
    assert eval_schema["properties"]["code"]["type"] == "string"

    search_schema = tools["Search-API"]["inputSchema"]
    assert search_schema["required"] == ["query"]
    assert search_schema["properties"]["query"]["type"] == "string"

    texture_schema = tools["Get-Texture"]["inputSchema"]
    assert texture_schema["required"] == ["resource_id"]
    assert texture_schema["properties"]["mip"]["default"] == 0
    assert texture_schema["properties"]["max_size"]["default"] == 2048

    instance_schema = tools["Instance"]["inputSchema"]
    assert instance_schema["required"] == ["action"]
    assert instance_schema["properties"]["force"]["default"] is False

    task_schema = tools["Task"]["inputSchema"]
    assert task_schema["required"] == ["action"]


@pytest.mark.skip(
    reason="deferred to Stage 5: needs the conftest fake_renderdoc_client "
    "fixture re-pointed from superbrian's flattened RenderDocClient to our "
    "ConnectionPool (docs/SYNC_SUPERBRIAN.md Stage 5)"
)
@pytest.mark.anyio
async def test_tool_results_preserve_dict_text_and_image_content(fake_renderdoc_client):
    async with Client(mcp) as client:
        eval_result = _model_payload(
            await client.call_tool("Eval", {"code": "result = 42"})
        )
        search_result = _model_payload(
            await client.call_tool("Search-API", {"query": "SetFrameEvent"})
        )
        texture_result = _model_payload(
            await client.call_tool("Get-Texture", {"resource_id": "ResourceId::1"})
        )
        instance_result = _model_payload(
            await client.call_tool("Instance", {"action": "connect", "port": 19876})
        )

    assert _json_text_result(eval_result) == {
        "ok": True,
        "data": {"result": 42},
    }
    assert _json_text_result(search_result)["data"][0]["name"] == (
        "ReplayController.SetFrameEvent"
    )

    assert texture_result["isError"] is False
    assert [item["type"] for item in texture_result["content"]] == ["text", "image"]
    assert texture_result["content"][1]["mimeType"] == "image/png"
    assert texture_result["content"][1]["data"]

    assert _json_text_result(instance_result)["data"]["port"] == 19876
    assert fake_renderdoc_client.calls == [
        ("eval", {"code": "result = 42"}),
        ("api_index", {"query": "SetFrameEvent"}),
        (
            "get_texture",
            {
                "resource_id": "ResourceId::1",
                "event_id": None,
                "mip": 0,
                "slice": 0,
                "sample": 0,
            },
        ),
        ("instance_info", {}),
    ]


@pytest.mark.skip(
    reason="deferred to Stage 5: imports superbrian's flattened "
    "server.client.RenderDocClient and its _serialized_operation lock, "
    "which we do not adopt; locking is ported onto ConnectionPool in "
    "Stage 5 (docs/SYNC_SUPERBRIAN.md Stage 5)"
)
@pytest.mark.anyio
async def test_concurrent_protocol_calls_are_serialized(monkeypatch):
    from server.client import RenderDocClient
    from server import tools

    renderdoc = RenderDocClient()
    renderdoc._sock = object()
    renderdoc._port = 19876
    entered = []
    eval_started = threading.Event()
    release_eval = threading.Event()

    def fake_send(cmd, params, read_timeout):
        entered.append(cmd)
        if cmd == "eval":
            eval_started.set()
            assert release_eval.wait(timeout=2)
        return {"ok": True, "data": {"command": cmd}}

    renderdoc._do_send = fake_send
    monkeypatch.setattr(tools, "_client", renderdoc)

    results = {}

    async def call(client, tool_name, arguments):
        results[tool_name] = _model_payload(
            await client.call_tool(tool_name, arguments)
        )

    async with Client(mcp) as client:
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(call, client, "Eval", {"code": "result = 1"})
                assert await anyio.to_thread.run_sync(eval_started.wait, 1)
                tasks.start_soon(call, client, "Search-API", {"query": "texture"})
                await anyio.sleep(0.1)
                assert entered == ["eval"]
                release_eval.set()
        finally:
            release_eval.set()

    assert entered == ["eval", "api_index"]
    assert _json_text_result(results["Eval"])["data"]["command"] == "eval"
    assert _json_text_result(results["Search-API"])["data"]["command"] == "api_index"