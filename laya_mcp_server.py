"""
laya_mcp_server.py – Laya MCP Server (severity triage).

Exposes Laya's first-pass severity triage over the Model Context Protocol
(stdio transport), in the same style as mcp_server.py and dynatrace_mcp_server.py.
Uses mcp 1.x; laya's own bundled MCP server (laya[mcp]) needs mcp>=2.2 and is
deliberately not used.

agent.py starts this server only when LAYA_ENABLED=true, and calls the tool from
code before the Gemini chat starts; Gemini itself is not given this tool.

Tools exposed:
  classify_services – answer the four triage questions (severity, user_impact,
                      failure_mode, page_now) for every service's evidence pack in
                      one batched Laya call; optionally record the results in the
                      laya_triage table
"""

import asyncio
import json

import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from laya_config import load_config
from laya_triage import client as laya_client
from mock_app.database import save_laya_triage

server = Server("laya-mcp")
CONFIG = load_config()

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_TOOLS = [
    mcp_types.Tool(
        name="classify_services",
        description=(
            "Run Laya's first-pass triage on per-service evidence packs (built by "
            "laya_triage.evidence.build_evidence_packs). Answers severity (sev1-sev4), "
            "user_impact, failure_mode and page_now for every service in one batched call. "
            "Returns status 'ok' or 'unavailable' plus one result per service; never fails "
            "because of Laya itself."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "evidence_packs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "One evidence pack per service.",
                },
                "run_id": {
                    "type": "string",
                    "description": "Optional agent run id. When given, each result is saved to laya_triage.",
                },
                "mode": {
                    "type": "string",
                    "description": "Optional effective LAYA_MODE, recorded with each saved row.",
                },
            },
            "required": ["evidence_packs"],
        },
    ),
]


def _classify(evidence_packs: list, run_id: str = None, mode: str = None) -> dict:
    out = laya_client.classify(evidence_packs, CONFIG)
    if run_id:
        for pack, result in zip(evidence_packs, out["results"]):
            save_laya_triage(run_id, mode or CONFIG.mode, CONFIG.model, pack, result)
    return out


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[mcp_types.Tool]:
    return _TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[mcp_types.TextContent]:
    if name != "classify_services":
        raise ValueError(f"Unknown Laya MCP tool: '{name}'")
    result = await asyncio.to_thread(
        _classify,
        arguments.get("evidence_packs") or [],
        arguments.get("run_id"),
        arguments.get("mode"),
    )
    return [mcp_types.TextContent(type="text", text=json.dumps(result, default=str))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    laya_client.warmup(CONFIG)          # start building the checkpoint while the agent fetches logs
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
