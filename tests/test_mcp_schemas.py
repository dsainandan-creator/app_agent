"""MCP tool schemas: names, required arguments, and run_id staying optional."""

import asyncio
import json


def _tools(module):
    return {t.name: t for t in asyncio.run(module.list_tools())}


def test_laya_mcp_schema():
    import laya_mcp_server
    tools = _tools(laya_mcp_server)
    assert set(tools) == {"classify_services"}
    schema = tools["classify_services"].inputSchema
    assert schema["required"] == ["evidence_packs"]
    assert schema["properties"]["evidence_packs"]["type"] == "array"
    assert {"run_id", "mode"} <= set(schema["properties"])


def test_laya_mcp_records_rows_only_with_run_id(monkeypatch):
    import laya_mcp_server
    saved = []
    monkeypatch.setattr(laya_mcp_server, "save_laya_triage", lambda *a: saved.append(a))
    monkeypatch.setattr(laya_mcp_server.laya_client, "classify", lambda packs, cfg: {
        "status": "ok", "results": [{"service": p["service"], "status": "ok"} for p in packs]})
    packs = [{"service": "a"}, {"service": "b"}]

    out = asyncio.run(laya_mcp_server.call_tool("classify_services", {"evidence_packs": packs}))
    assert json.loads(out[0].text)["status"] == "ok" and saved == []

    asyncio.run(laya_mcp_server.call_tool(
        "classify_services", {"evidence_packs": packs, "run_id": "r1", "mode": "shadow"}))
    assert [(a[0], a[1], a[3]["service"]) for a in saved] == [("r1", "shadow", "a"), ("r1", "shadow", "b")]


def test_dynatrace_mcp_schema():
    import dynatrace_mcp_server
    tools = _tools(dynatrace_mcp_server)
    assert set(tools) == {"dt_ingest_log", "dt_search_logs"}


def test_obs_mcp_schema():
    import mcp_server
    tools = _tools(mcp_server)
    assert set(tools) == {"get_logs", "raise_alert", "send_email_notification",
                          "save_metrics", "call_on_call_engineer"}
