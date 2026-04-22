"""
mcp_server.py – Observability MCP Server.

Exposes all agent tools over the Model Context Protocol (stdio transport).
The agent connects to this server as an MCP client and discovers tools
dynamically — no tool schemas are hardcoded in the agent.

Tools exposed:
  get_logs                – Fetch aggregated log stats from PostgreSQL
  raise_alert             – Persist a severity-assessed alert to the DB
  send_email_notification – Mock email sender (logs to DB)
  save_metrics            – Persist analysis metrics to the DB
  call_on_call_engineer   – SEV-1 Slack alert via Incoming Webhook
"""

import asyncio
import sys

import mcp.types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from tools import (
    call_on_call_engineer as _call_oncall,
    get_logs as _get_logs,
    raise_alert as _raise_alert,
    save_metrics as _save_metrics,
    send_email_notification as _send_email,
)

server = Server("observability-mcp")

# ---------------------------------------------------------------------------
# Tool schemas  (JSON Schema – converted to Gemini format by the agent)
# ---------------------------------------------------------------------------

_TOOLS = [
    mcp_types.Tool(
        name="get_logs",
        description=(
            "Get overall + per-service aggregated stats (error rate, latency) "
            "for the last N minutes. Call once at the start of every analysis."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "timeframe_minutes": {
                    "type": "integer",
                    "description": "How many minutes back to fetch logs. Default 30.",
                }
            },
        },
    ),
    mcp_types.Tool(
        name="raise_alert",
        description=(
            "Persist your severity assessment for a service to the database. "
            "Call for every service regardless of severity level. "
            "Include your reasoning in the message field."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service":  {"type": "string",  "description": "Affected service name."},
                "severity": {"type": "integer", "description": "Assessed severity: 1=CRITICAL, 2=HIGH, 3=MEDIUM, 4=LOW."},
                "message":  {"type": "string",  "description": "What you observed, why you chose this severity, and key metrics."},
            },
            "required": ["service", "severity", "message"],
        },
    ),
    mcp_types.Tool(
        name="send_email_notification",
        description=(
            "Send a mock email summarising all service severities. "
            "Call once after assessing all services. Use the highest severity found."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient": {"type": "string",  "description": "Recipient email address."},
                "severity":  {"type": "integer", "description": "Highest severity across all services (1–4)."},
                "subject":   {"type": "string",  "description": "Email subject line."},
                "body":      {"type": "string",  "description": "Full summary: all services, severities, and your reasoning."},
            },
            "required": ["recipient", "severity", "subject", "body"],
        },
    ),
    mcp_types.Tool(
        name="save_metrics",
        description=(
            "Persist the overall analysis metrics and highest severity to the database. "
            "Call once after completing analysis of all services."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "window_minutes":       {"type": "integer", "description": "Analysis window in minutes."},
                "total_requests":       {"type": "integer", "description": "Total request count across all services."},
                "error_count":          {"type": "integer", "description": "Total 5xx error count."},
                "success_count":        {"type": "integer", "description": "Total 2xx success count."},
                "avg_response_time_ms": {"type": "number",  "description": "Overall average latency in ms."},
                "error_rate":           {"type": "number",  "description": "Overall error rate 0–100."},
                "severity_assessment":  {"type": "integer", "description": "Highest severity you found across all services (1–4)."},
                "analysis_summary":     {"type": "string",  "description": "Brief plain-text summary of what you found and what actions were taken."},
            },
            "required": [
                "window_minutes", "total_requests", "error_count", "success_count",
                "avg_response_time_ms", "error_rate", "severity_assessment", "analysis_summary",
            ],
        },
    ),
    mcp_types.Tool(
        name="call_on_call_engineer",
        description=(
            "Send a SEV-1 CRITICAL alert to Slack via Incoming Webhook. "
            "Call this for any service you assess as SEV-1 — severely degraded "
            "or effectively down, requiring immediate human intervention."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "The failing service name."},
                "message": {"type": "string", "description": "Critical failure description — include error rate, latency, and user impact."},
            },
            "required": ["service", "message"],
        },
    ),
]

_TOOL_FN_MAP = {
    "get_logs":                _get_logs,
    "raise_alert":             _raise_alert,
    "send_email_notification": _send_email,
    "save_metrics":            _save_metrics,
    "call_on_call_engineer":   _call_oncall,
}


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[mcp_types.Tool]:
    return _TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[mcp_types.TextContent]:
    fn = _TOOL_FN_MAP.get(name)
    if fn is None:
        raise ValueError(f"Unknown tool: '{name}'")

    result = await asyncio.to_thread(fn, **arguments)
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