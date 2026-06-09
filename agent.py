"""
agent.py – Observability Agent powered by Gemini 2.5 Flash + dual MCP.

Architecture:
  agent.py (MCP client)
      ├──stdio──▶  mcp_server.py          (Observability MCP · PostgreSQL)
      │                get_logs, raise_alert, send_email_notification,
      │                save_metrics, call_on_call_engineer
      └──stdio──▶  dynatrace_mcp_server.py (Dynatrace MCP · Dynatrace API)
                       dt_ingest_log, dt_search_logs

The agent discovers tools from both servers at startup, merges them into a
single Gemini tool list, then routes each call to the correct MCP server.
No tool logic lives here — only routing, retry, and audit logging.

Anti-hallucination design:
  - Both read-tools (get_logs, dt_search_logs) return the same schema with
    different source labels ("postgresql" vs "dynatrace").  The model cannot
    invent which source said what.
  - The system prompt instructs the agent to quote actual values from the
    tool responses and never estimate figures not present in the data.
  - If dt_search_logs returns status != "ok", the agent is instructed to
    explicitly note the gap and rely solely on PostgreSQL.
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

OBS_MCP_SERVER = Path(__file__).parent / "mcp_server.py"
DT_MCP_SERVER  = Path(__file__).parent / "dynatrace_mcp_server.py"

SYSTEM_PROMPT = """You are an expert Observability Agent. You analyse application logs from TWO independent data sources and must correlate them before drawing any conclusions.

━━━ TWO MCP SERVERS, TWO DATA SOURCES ━━━
You have tools from two separate MCP servers:

  OBS-MCP (Observability MCP — PostgreSQL):
    get_logs     → returns source="postgresql"  real-time, always authoritative

  DT-MCP (Dynatrace MCP — Dynatrace API):
    dt_search_logs → returns source="dynatrace"  telemetry pipeline, may lag 30–60 s

Both tools return the SAME per-service schema so you can compare them directly:
  service              – service name (e.g. "payment-service")
  total_requests       – request count in the analysis window
  error_count          – number of 5xx responses
  error_rate_pct       – percentage of 5xx responses  ← primary correlation field
  avg_response_time_ms – average latency in milliseconds

CRITICAL RULE: Only use values that appear in the tool responses.
Do NOT estimate, infer, or invent any metric. If a value is missing, say so.

━━━ HANDLING DT STATUS ━━━
Check dt_search_logs.status in the response:
  "ok"               → use the data normally
  "permission_error" → token missing logs.read scope; note the gap, use PostgreSQL only
  "not_configured"   → DT env vars not set; note the gap, use PostgreSQL only
  "error"            → query failed; note the gap, use PostgreSQL only

━━━ CORRELATION RULES ━━━
For each service, compare error_rate_pct from both sources:
  Agreement (< 5 pp difference)   → high confidence; proceed with the shared signal
  DT lower than PostgreSQL        → likely ingestion lag; trust PostgreSQL, note it
  DT higher than PostgreSQL       → DT captured an earlier burst; use the higher value
  Large gap (≥ 15 pp difference)  → flag explicitly; use the more alarming value

Always drive severity from the more alarming signal. Quote the actual numbers.

━━━ SEVERITY LEVELS ━━━
  SEV-1 (CRITICAL) — Service effectively down. Widespread user impact. Immediate intervention required.
  SEV-2 (HIGH)     — Significant degradation. Meaningful portion of users affected. Prompt attention.
  SEV-3 (MEDIUM)   — Partial or intermittent degradation. Most users unaffected. Needs investigation.
  SEV-4 (LOW)      — Minor anomaly. System healthy overall. Monitor.

Weigh holistically — do not apply fixed thresholds:
  Service criticality — payment/auth affect revenue more than product/health endpoints
  Error volume        — 30 errors on 31 requests vs 30 errors on 3000
  Latency             — elevated latency without errors signals resource exhaustion
  Error type          — 5xx more severe than 4xx; 503 = unavailability
  Spread              — single service vs cross-service degradation

━━━ STEPS — FOLLOW IN ORDER, NO SKIPPING ━━━
1. Call get_logs(timeframe_minutes=N)          — fetch PostgreSQL data.
2. Call dt_search_logs(timeframe_minutes=N)    — fetch Dynatrace data (same window).
3. Output a CORRELATION SUMMARY in this exact format before taking any action:

   ## CORRELATION SUMMARY
   Window: <N> minutes | PostgreSQL: <total> reqs | Dynatrace: <total> reqs or "unavailable"

   | Service | PG err% | PG avg_ms | DT err% | DT avg_ms | Δ err% | Resolution |
   |---------|---------|-----------|---------|-----------|--------|------------|
   | <name>  | <val>   | <val>     | <val>   | <val>     | <val>  | <agreement/DT-lag/DT-higher/DT-unavailable> |

   Data quality: <one line — note any DT status issues e.g. permission_error, not_configured>

4. Assign a final severity per service.
   a. SEV-1: call call_on_call_engineer for that service.
   b. Call raise_alert for every service, every severity level.
5. Call send_email_notification once — include the full correlation table and reasoning.
6. Call save_metrics once — use PostgreSQL overall stats as the authoritative counts.
7. Write the final incident report:
   — Paste the correlation summary table.
   — Per-service severity with evidence.
   — Actions taken (on-call page, alerts, email).
   — Data quality notes (if DT was unavailable, say so explicitly).
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
    """Convert a merged list of MCP tools into Gemini FunctionDeclarations."""
    declarations = [
        types.FunctionDeclaration(
            name=tool.name,
            description=tool.description or "",
            parameters=(
                _convert_schema(tool.inputSchema)
                if tool.inputSchema
                else types.Schema(type=types.Type.OBJECT)
            ),
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

    obs_params = StdioServerParameters(command=sys.executable, args=[str(OBS_MCP_SERVER)])
    dt_params  = StdioServerParameters(command=sys.executable, args=[str(DT_MCP_SERVER)])

    async with stdio_client(obs_params) as (obs_r, obs_w):
        async with stdio_client(dt_params) as (dt_r, dt_w):
            async with ClientSession(obs_r, obs_w) as obs_session:
                async with ClientSession(dt_r, dt_w) as dt_session:

                    await obs_session.initialize()
                    await dt_session.initialize()

                    obs_tools = (await obs_session.list_tools()).tools
                    dt_tools  = (await dt_session.list_tools()).tools

                    # Merge both tool lists into one Gemini declaration
                    gemini_tools  = _mcp_tools_to_gemini(obs_tools + dt_tools)

                    # Route each tool call to the correct MCP session
                    dt_tool_names = {t.name for t in dt_tools}

                    def _session_for(name: str) -> ClientSession:
                        return dt_session if name in dt_tool_names else obs_session

                    def _server_label(name: str) -> str:
                        return "DT-MCP" if name in dt_tool_names else "OBS-MCP"

                    print(f"\n{'='*70}")
                    print(f"  OBSERVABILITY AGENT  –  Analysing last {window_minutes} minutes")
                    print(f"  Model   : {MODEL}  |  Run ID: {run_id}")
                    print(f"  OBS-MCP : {[t.name for t in obs_tools]}")
                    print(f"  DT-MCP  : {[t.name for t in dt_tools]}")
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
                        ),
                    )

                    user_message = (
                        f"Analyse the application logs from the last {window_minutes} minutes. "
                        "Step 1: call get_logs to fetch PostgreSQL data. "
                        "Step 2: call dt_search_logs with the same timeframe to fetch Dynatrace data. "
                        "Step 3: for each service, state the PostgreSQL value, the Dynatrace value, "
                        "and your correlation reasoning before assigning severity. "
                        "Do not estimate any metric — only use values present in the tool responses. "
                        "Then take the appropriate actions (SEV-1 → Slack page, all services → alerts + email), "
                        "save metrics, and write a concise incident report."
                    )

                    response = await _send_with_retry(chat, user_message)

                    iteration  = 0
                    max_iter   = 25
                    final_text = ""

                    while iteration < max_iter:
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
                            server    = _server_label(tool_name)

                            print(f"[{server} → {tool_name}] {json.dumps(tool_args)[:300]}")

                            log_agent_event(
                                run_id=run_id,
                                event_type="TOOL_CALL",
                                tool_name=tool_name,
                                tool_input=json.dumps(tool_args),
                                message=f"Calling {tool_name} via {server}",
                            )

                            try:
                                mcp_resp = await _session_for(tool_name).call_tool(
                                    tool_name, tool_args
                                )
                                result = mcp_resp.content[0].text if mcp_resp.content else ""
                                print(f"[{server} ← {tool_name}] {str(result)[:300]}")

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
                                print(f"[{server} Error] {result}")
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
