"""
dynatrace_mcp_server.py – Dedicated Dynatrace MCP Server.

Exposes Dynatrace log operations over the Model Context Protocol (stdio transport).
The agent connects to this server alongside mcp_server.py (Observability MCP),
giving it tools from two independent backends that it must correlate before
assigning any severity.

Tools exposed:
  dt_ingest_log   – write a log entry directly to Dynatrace (utility / testing)
  dt_search_logs  – query aggregated per-service log stats from Dynatrace

Why a separate MCP server (not a tool in the main mcp_server.py)?
  - Clean separation: observability tools own PostgreSQL, this server owns Dynatrace.
  - Independent deployability: the DT server can be started, stopped, or swapped
    without touching the observability MCP.
  - Explicit routing in agent.py: the agent knows which server handled each tool
    call, making the data provenance transparent in logs.
"""

import asyncio
import json
import sys

import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from dynatrace_client import ingest_log_sync as _dt_ingest_sync
from dynatrace_client import query_logs as _dt_query

server = Server("dynatrace-mcp")


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_TOOLS = [
    mcp_types.Tool(
        name="dt_ingest_log",
        description=(
            "Write a single log entry directly to Dynatrace via the Logs Ingest API. "
            "Intended for manual log injection and testing only. "
            "IMPORTANT: automatic log forwarding already happens on every FastAPI request — "
            "do NOT call this tool during a normal analysis run."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "endpoint":         {"type": "string",  "description": "HTTP endpoint path, e.g. /api/payments"},
                "method":           {"type": "string",  "description": "HTTP method, e.g. POST"},
                "status_code":      {"type": "integer", "description": "HTTP status code, e.g. 500"},
                "response_time_ms": {"type": "number",  "description": "Response time in milliseconds"},
                "message":          {"type": "string",  "description": "Log message content"},
                "request_id":       {"type": "string",  "description": "Unique request identifier"},
                "error_detail":     {"type": "string",  "description": "Optional error detail"},
            },
            "required": [
                "endpoint", "method", "status_code",
                "response_time_ms", "message", "request_id",
            ],
        },
    ),
    mcp_types.Tool(
        name="dt_search_logs",
        description=(
            "Fetch aggregated per-service log statistics from Dynatrace for the last N minutes. "
            "Returns source='dynatrace' with the SAME schema fields as get_logs — "
            "error_rate_pct, avg_response_time_ms, total_requests, error_count per service — "
            "so you can directly compare the two sources side-by-side. "
            "\n"
            "ALWAYS call dt_search_logs with the same timeframe_minutes as get_logs before "
            "assigning any severity. The data to correlate will be in the tool response — "
            "do not invent or estimate values not present in the response. "
            "\n"
            "If status='permission_error': the API token is missing logs.read scope — "
            "note this, rely solely on PostgreSQL data, and mention the gap in your report. "
            "If status='not_configured': DT env vars are not set — same fallback applies."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timeframe_minutes": {
                    "type": "integer",
                    "description": "Minutes back to query. Must match get_logs timeframe. Default 30.",
                }
            },
        },
    ),
]


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[mcp_types.Tool]:
    return _TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[mcp_types.TextContent]:
    if name == "dt_ingest_log":
        result = await asyncio.to_thread(_dt_ingest_sync, **arguments)

    elif name == "dt_search_logs":
        window = int(arguments.get("timeframe_minutes", 30))
        raw    = await asyncio.to_thread(_dt_query, window)
        result = json.dumps(raw, default=str)

    else:
        raise ValueError(f"Unknown Dynatrace MCP tool: '{name}'")

    return [mcp_types.TextContent(type="text", text=str(result))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
