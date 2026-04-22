"""
agent.py – Observability Agent powered by Gemini 2.5 Flash + MCP.

Architecture:
  agent.py (MCP client)  ──stdio──▶  mcp_server.py (MCP server)
                                           │
                                      tools.py  ·  PostgreSQL  ·  Slack

The agent connects to the MCP server at startup, discovers tools
dynamically, converts their schemas to Gemini function declarations,
and routes every tool call back through MCP — no tool logic lives here.

Severity is assessed by the model using contextual reasoning, not
hardcoded thresholds. See SYSTEM_PROMPT for the agent's guidance.
"""

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ClientError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mock_app.database import log_agent_event

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-2.5-flash"
ANALYSIS_WINDOW_MINUTES = 30

MCP_SERVER = Path(__file__).parent / "mcp_server.py"

SYSTEM_PROMPT = """You are an expert Observability Agent. Analyse application logs, assess service health, and take appropriate incident response actions.

You receive per-service metrics: error rate, request count, average latency, and error count.
Use your judgment to classify the health of each service. Do not apply fixed numeric thresholds.
Instead, reason holistically across these signals:

  Service criticality — payment and auth services affect revenue and user trust more than product or inventory services.
  Error rate in context — 5% errors on a payment service is more alarming than 20% on a health-check endpoint.
  Error volume — 30 errors on 31 requests is a different risk than 30 errors on 3000.
  Latency — elevated latency without errors often signals resource exhaustion before failures appear.
  Error type — 5xx (server-side) are more severe than 4xx (client errors); 503s indicate service unavailability.
  Spread — errors isolated to one service vs degradation across many services changes the blast radius.

Severity levels (use as a guide for reasoning, not a formula):
  SEV-1 (CRITICAL) — Service is severely degraded or effectively down. Widespread user impact. Immediate human intervention required.
  SEV-2 (HIGH)     — Significant degradation. A meaningful portion of users affected. Needs prompt attention.
  SEV-3 (MEDIUM)   — Partial or intermittent degradation. Most users unaffected. Needs investigation.
  SEV-4 (LOW)      — Minor anomaly. System healthy overall. Monitor but no immediate action needed.

Steps (follow in order, no skipping):
1. Call get_logs once.
2. Analyse the full picture: overall health and each service individually.
3. For each service, reason through the signals above and assign a severity.
   a. If SEV-1: call call_on_call_engineer for that service (triggers Slack alert).
   b. Call raise_alert for every service with your assessed severity.
4. Call send_email_notification once, summarising all services, their severities, and your reasoning.
5. Call save_metrics once using the overall stats and the highest severity found.
6. Write a concise incident report explaining your severity assessments and the evidence behind each.
"""

# ---------------------------------------------------------------------------
# MCP schema → Gemini schema conversion
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "string":  types.Type.STRING,
    "integer": types.Type.INTEGER,
    "number":  types.Type.NUMBER,
    "boolean": types.Type.BOOLEAN,
    "object":  types.Type.OBJECT,
    "array":   types.Type.ARRAY,
}


def _convert_schema(json_schema: dict) -> types.Schema:
    """Recursively convert a JSON Schema dict to a Gemini types.Schema."""
    json_type = json_schema.get("type", "string")
    kwargs: dict = {"type": _TYPE_MAP.get(json_type, types.Type.STRING)}

    if desc := json_schema.get("description"):
        kwargs["description"] = desc
    if json_type == "object" and (props := json_schema.get("properties")):
        kwargs["properties"] = {k: _convert_schema(v) for k, v in props.items()}
    if req := json_schema.get("required"):
        kwargs["required"] = req
    if json_type == "array" and (items := json_schema.get("items")):
        kwargs["items"] = _convert_schema(items)

    return types.Schema(**kwargs)


def _mcp_tools_to_gemini(mcp_tools) -> list[types.Tool]:
    """Convert the MCP server's tool list into Gemini FunctionDeclarations."""
    declarations = [
        types.FunctionDeclaration(
            name=tool.name,
            description=tool.description or "",
            parameters=_convert_schema(tool.inputSchema) if tool.inputSchema else types.Schema(type=types.Type.OBJECT),
        )
        for tool in mcp_tools
    ]
    return [types.Tool(function_declarations=declarations)]


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _parse_retry_delay(err_str: str, default: int = 65) -> int:
    m = re.search(r"retryDelay['\": ]+(\d+)s", err_str)
    if m:
        return int(m.group(1)) + 5
    m = re.search(r"retry.after['\": ]+(\d+)", err_str, re.IGNORECASE)
    if m:
        return int(m.group(1)) + 5
    return default


async def _send_with_retry(chat, message, max_retries: int = 8):
    """Send a Gemini chat message with backoff on 429 quota errors."""
    for attempt in range(max_retries):
        try:
            return await asyncio.to_thread(chat.send_message, message)
        except ClientError as e:
            err = str(e)
            if ("429" in err or "RESOURCE_EXHAUSTED" in err) and attempt < max_retries - 1:
                delay = _parse_retry_delay(err)
                print(f"[Agent] Rate limited – waiting {delay}s (attempt {attempt + 1}/{max_retries})…")
                await asyncio.sleep(delay)
            else:
                raise


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def _run_agent_async(window_minutes: int) -> str:
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY is not set. Add it to your .env file.")
        sys.exit(1)

    client = genai.Client(api_key=GEMINI_API_KEY)
    run_id = str(uuid.uuid4())[:8]

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(MCP_SERVER)],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Discover tools from the MCP server and convert to Gemini format
            mcp_result = await session.list_tools()
            gemini_tools = _mcp_tools_to_gemini(mcp_result.tools)

            print(f"\n{'='*70}")
            print(f"  OBSERVABILITY AGENT  –  Analysing last {window_minutes} minutes")
            print(f"  Model: {MODEL}  |  Run ID: {run_id}")
            print(f"  MCP tools loaded: {[t.name for t in mcp_result.tools]}")
            print(f"{'='*70}\n")

            log_agent_event(
                run_id=run_id,
                event_type="RUN_START",
                message=f"Agent run started. Analysing last {window_minutes} minutes.",
            )

            chat = await asyncio.to_thread(
                client.chats.create,
                model=MODEL,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=gemini_tools,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )

            user_message = (
                f"Analyse the application logs from the last {window_minutes} minutes. "
                "Classify the severity for each service using your judgment, take appropriate "
                "action (Slack page if SEV-1, alerts + email for all), save metrics, and "
                "provide a brief incident report explaining your reasoning."
            )

            response = await _send_with_retry(chat, user_message)

            iteration = 0
            max_iterations = 20
            final_text = ""

            while iteration < max_iterations:
                iteration += 1
                print(f"[Agent] Iteration {iteration}")

                function_calls = []
                for part in response.candidates[0].content.parts:
                    if part.function_call and part.function_call.name:
                        function_calls.append(part.function_call)
                    elif part.text:
                        print(f"\n[Agent Response]\n{part.text}\n")
                        final_text = part.text

                if not function_calls:
                    print("[Agent] Analysis complete.")
                    break

                tool_response_parts = []
                for fc in function_calls:
                    tool_name = fc.name
                    tool_args = dict(fc.args)

                    print(f"[MCP → {tool_name}] {json.dumps(tool_args)[:300]}")

                    log_agent_event(
                        run_id=run_id,
                        event_type="TOOL_CALL",
                        tool_name=tool_name,
                        tool_input=json.dumps(tool_args),
                        message=f"Calling {tool_name}",
                    )

                    try:
                        mcp_response = await session.call_tool(tool_name, tool_args)
                        result = mcp_response.content[0].text if mcp_response.content else ""
                        print(f"[MCP ← {tool_name}] {str(result)[:300]}")

                        sev = None
                        if tool_name == "save_metrics" and "severity_assessment" in tool_args:
                            sev = int(tool_args["severity_assessment"])
                        elif tool_name == "call_on_call_engineer":
                            sev = 1

                        log_agent_event(
                            run_id=run_id,
                            event_type="TOOL_RESULT",
                            tool_name=tool_name,
                            tool_result=str(result)[:1000],
                            severity=sev,
                            message=f"{tool_name} completed",
                        )
                    except Exception as exc:
                        result = f"Tool execution error: {exc}"
                        print(f"[MCP Error] {result}")
                        log_agent_event(
                            run_id=run_id,
                            event_type="TOOL_ERROR",
                            tool_name=tool_name,
                            tool_result=result,
                            message=result,
                        )

                    tool_response_parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=tool_name,
                                response={"result": result},
                            )
                        )
                    )

                await asyncio.sleep(2)
                response = await _send_with_retry(chat, tool_response_parts)

    log_agent_event(
        run_id=run_id,
        event_type="FINAL_REPORT",
        message=final_text[:2000] if final_text else "Run complete (no text output).",
    )

    print(f"\n{'='*70}")
    print("  AGENT RUN COMPLETE")
    print(f"{'='*70}\n")

    return final_text


def run_agent(window_minutes: int = ANALYSIS_WINDOW_MINUTES) -> str:
    """Synchronous entry point — wraps the async agent loop."""
    return asyncio.run(_run_agent_async(window_minutes))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    window = int(sys.argv[1]) if len(sys.argv) > 1 else ANALYSIS_WINDOW_MINUTES
    result = run_agent(window_minutes=window)
    print("\n--- FINAL INCIDENT REPORT ---")
    print(result)