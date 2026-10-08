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
import threading
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ClientError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from laya_config import load_config as load_laya_config
from laya_triage.evidence import build_evidence_packs
from laya_triage.prompting import (
    LAYA_PROMPT_ADDENDUM,
    advisory_user_message,
    triage_table,
    unavailable_triage,
)
from dynatrace_client import dt_ingest_tool_dry_run
from mock_app.database import fetch_run_alerts, log_agent_event, save_laya_triage
from slack_notify import slack_mode

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "gemini-2.5-flash"
ANALYSIS_WINDOW_MINUTES = 30

OBS_MCP_SERVER  = Path(__file__).parent / "mcp_server.py"
DT_MCP_SERVER   = Path(__file__).parent / "dynatrace_mcp_server.py"
LAYA_MCP_SERVER = Path(__file__).parent / "laya_mcp_server.py"

# Tools that receive this run's id (injected here, never supplied by Gemini) so they
# can look up the run's Laya triage. Only injected when LAYA_ENABLED=true.
RUN_ID_TOOLS = {"raise_alert", "call_on_call_engineer", "send_email_notification"}

# LAYA-MCP: time to start the server, and an outer bound on classify_services
# (checkpoint load + inference) on top of the client's own limits.
LAYA_INIT_TIMEOUT_S = 30
LAYA_CALL_GRACE_S   = 200

CHROMA_PATH = Path(__file__).parent / "chroma_db"
COLLECTION  = "observability_docs"
TOP_K       = 3

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


GEMINI_CALL_TIMEOUT_S = float(os.environ.get("GEMINI_CALL_TIMEOUT_S", "180"))
GEMINI_TRANSIENT_RETRIES = 2


class _BoundedChat:
    """
    A Gemini chat whose history lives here, so a stalled request can be abandoned.

    google-genai's Chat appends to its history when a response arrives; a request
    abandoned after a timeout could still arrive later and corrupt the history of
    the retry. Here every attempt sends a copy of the history and the history only
    advances when the attempt that is still awaited succeeds.
    """

    def __init__(self, client, model: str, config):
        self.client, self.model, self.config = client, model, config
        self.history: list = []

    @staticmethod
    def _as_content(message):
        if isinstance(message, str):
            return types.Content(role="user", parts=[types.Part(text=message)])
        return types.Content(role="user", parts=list(message))

    def send_message(self, message, timeout_s: float = GEMINI_CALL_TIMEOUT_S):
        content = self._as_content(message)
        contents = self.history + [content]
        response = _call_with_deadline(
            lambda: self.client.models.generate_content(
                model=self.model, contents=contents, config=self.config),
            timeout_s,
        )
        candidate = response.candidates[0] if response.candidates else None
        if not (candidate and candidate.content and candidate.content.parts):
            # Seen intermittently: a candidate with no parts. Nothing to act on; retry.
            reason = getattr(candidate, "finish_reason", None) if candidate else "no candidates"
            raise EmptyGeminiResponse(f"Gemini returned an empty response (finish_reason={reason})")
        self.history = contents + [candidate.content]
        return response


def _call_with_deadline(fn, timeout_s: float):
    """Run fn in a daemon thread; raise TimeoutError if it has not returned in time.
    (requests' own timeout only covers silence on the socket, not a stalled response.)"""
    box: dict = {}
    done = threading.Event()

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:     # noqa: BLE001 – re-raised in the caller
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name="gemini-call").start()
    if not done.wait(timeout_s):
        raise TimeoutError(f"Gemini call did not complete within {timeout_s:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["result"]


class EmptyGeminiResponse(RuntimeError):
    """A response with no content parts; treated as transient."""


def _is_transient(exc: BaseException) -> bool:
    import requests
    return isinstance(exc, (TimeoutError, ConnectionError, EmptyGeminiResponse,
                            requests.exceptions.ConnectionError, requests.exceptions.Timeout))


async def _send_with_retry(chat, message, max_retries: int = 8):
    """Send a Gemini chat message with backoff on 429 quota errors, and a bounded
    retry when a call stalls or the connection drops."""
    transient = 0
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
        except Exception as e:
            if _is_transient(e) and transient < GEMINI_TRANSIENT_RETRIES:
                transient += 1
                print(f"[Agent] Gemini call failed ({type(e).__name__}: {e}) – "
                      f"retrying ({transient}/{GEMINI_TRANSIENT_RETRIES})…")
                await asyncio.sleep(5)
            else:
                raise


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def _server_params(script: Path) -> StdioServerParameters:
    """
    Start an MCP server with the agent's own environment. mcp's stdio_client otherwise
    passes only HOME/LOGNAME/PATH/SHELL/TERM/USER, and each server then reads .env by
    itself, so a value set for this run (SLACK_WEBHOOK_URL=, LAYA_MODE=...) never
    reached the servers. os.environ already holds .env, loaded at import, with
    explicit environment values taking precedence.
    """
    return StdioServerParameters(command=sys.executable, args=[str(script)], env=dict(os.environ))


async def _start_laya_session(stack: AsyncExitStack):
    """Spawn LAYA-MCP. Returns (session, None) or (None, error); never raises."""
    params = _server_params(LAYA_MCP_SERVER)
    try:
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=LAYA_INIT_TIMEOUT_S)
        return session, None
    except Exception as exc:   # noqa: BLE001 – Laya must never stop the agent
        return None, f"LAYA-MCP failed to start: {type(exc).__name__}: {exc}"


async def _logged_call(run_id: str, session: ClientSession, server: str, tool_name: str,
                       tool_args: dict, timeout: float = None) -> str:
    """Call an MCP tool from code (not Gemini), logged like the agent's own tool calls."""
    print(f"[{server} → {tool_name}] (code) {json.dumps(tool_args, default=str)[:200]}")
    log_agent_event(
        run_id=run_id,
        event_type="TOOL_CALL",
        tool_name=tool_name,
        tool_input=json.dumps(tool_args, default=str),
        message=f"Calling {tool_name} via {server} (Laya pre-triage, code)",
    )
    try:
        call = session.call_tool(tool_name, tool_args)
        mcp_resp = await (asyncio.wait_for(call, timeout) if timeout else call)
        result = mcp_resp.content[0].text if mcp_resp.content else ""
    except Exception as exc:
        log_agent_event(run_id=run_id, event_type="TOOL_ERROR", tool_name=tool_name,
                        tool_result=f"{type(exc).__name__}: {exc}",
                        message=f"{tool_name} failed (Laya pre-triage, code)")
        raise
    print(f"[{server} ← {tool_name}] (code) {result[:200]}")
    log_agent_event(
        run_id=run_id,
        event_type="TOOL_RESULT",
        tool_name=tool_name,
        tool_result=result[:1000],
        message=f"{tool_name} completed (Laya pre-triage, code)",
    )
    return result


async def _laya_pretriage(run_id, window_minutes, obs_session, dt_session, laya_session,
                          laya_error, laya_cfg, laya_mode):
    """
    Code, not Gemini: fetch both sources, build evidence packs and ask Laya.
    Returns (pg, dt, packs, triage). Never raises.
    """
    args = {"timeframe_minutes": window_minutes}
    try:
        pg = json.loads(await _logged_call(run_id, obs_session, "OBS-MCP", "get_logs", args))
    except Exception as exc:   # noqa: BLE001
        pg = {"source": "postgresql", "status": "error", "error": str(exc), "services": []}
    try:
        dt = json.loads(await _logged_call(run_id, dt_session, "DT-MCP", "dt_search_logs", args))
    except Exception as exc:   # noqa: BLE001
        dt = {"source": "dynatrace", "status": "error", "note": str(exc), "services": []}

    packs = build_evidence_packs(pg, dt, laya_cfg.critical_services)

    triage = None
    if laya_session is not None:
        try:
            text = await _logged_call(
                run_id, laya_session, "LAYA-MCP", "classify_services",
                {"evidence_packs": packs, "run_id": run_id, "mode": laya_mode},
                timeout=laya_cfg.timeout_s + LAYA_CALL_GRACE_S,
            )
            triage = json.loads(text)
        except Exception as exc:   # noqa: BLE001
            laya_error = f"classify_services failed: {type(exc).__name__}: {exc}"
    if triage is None:
        # LAYA-MCP never answered, so it saved nothing: record the gap here.
        triage = unavailable_triage(packs, laya_cfg.model, laya_error)
        for pack, result in zip(packs, triage["results"]):
            save_laya_triage(run_id, laya_mode, laya_cfg.model, pack, result)
    return pg, dt, packs, triage


def _apply_policy_after_loop(run_id: str, mode: str, packs: list,
                             paged_by_gemini: set, gemini_severity: dict) -> str:
    """
    After the Gemini loop: decide for every service Gemini did not page (NO_PAGE or
    PAGE_FORCED), send forced pages in enforce mode, then return the decision table
    for this run (the tool already recorded decisions for services Gemini paged).
    """
    import tools as obs_tools
    from policy import PAGE_FORCED

    for pack in packs:
        service = pack["service"]
        if service in paged_by_gemini:
            continue
        ctx = obs_tools.policy_context(run_id, service)
        if ctx is None:
            continue
        decision = ctx["decide"](gemini_pages=False)
        obs_tools.record_decision(run_id, service, decision, mode)
        if mode == "enforce" and decision.outcome == PAGE_FORCED:
            incident = obs_tools.send_forced_page(
                run_id, service, decision, gemini_severity.get(service, "not assessed"))
            print(f"[Policy] PAGE_FORCED {service}: {incident['status']} {incident['incident_id']}")
            log_agent_event(
                run_id=run_id,
                event_type="POLICY_PAGE_FORCED",
                tool_name="call_on_call_engineer",
                tool_input=json.dumps({"service": service, "decision": decision.to_dict()}),
                tool_result=json.dumps(incident)[:1000],
                severity=1,
                message=f"Paging policy forced a page for {service}; Gemini did not page",
            )

    rows = fetch_run_alerts(run_id, obs_tools.POLICY_ALERT_TYPES)
    if not rows:
        return ""
    enforced = mode == "enforce"
    lines = [
        f"## Paging policy decisions (mode: {mode}"
        + ("" if enforced else " — recorded only, not enforced") + ")",
        "",
        "| Service | Gemini SEV | Decision | Detail |",
        "|---------|------------|----------|--------|",
    ]
    for r in rows:
        detail = r["message"].split("] ", 1)[-1].replace("|", "/")
        lines.append(f"| {r['service']} | {gemini_severity.get(r['service'], 'n/a')} "
                     f"| {r['status']} | {detail} |")
    return "\n".join(lines)


async def _run_agent_async(window_minutes: int) -> str:
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY is not set. Add it to your .env file.")
        sys.exit(1)

    client = genai.Client(api_key=GEMINI_API_KEY)
    run_id = str(uuid.uuid4())[:8]

    laya_cfg  = load_laya_config()
    laya_mode = laya_cfg.effective_mode() if laya_cfg.enabled else None

    obs_params = _server_params(OBS_MCP_SERVER)
    dt_params  = _server_params(DT_MCP_SERVER)

    async with AsyncExitStack() as stack:
        obs_r, obs_w = await stack.enter_async_context(stdio_client(obs_params))
        dt_r, dt_w   = await stack.enter_async_context(stdio_client(dt_params))
        obs_session  = await stack.enter_async_context(ClientSession(obs_r, obs_w))
        dt_session   = await stack.enter_async_context(ClientSession(dt_r, dt_w))

        await obs_session.initialize()
        await dt_session.initialize()

        laya_session, laya_error = None, None
        if laya_cfg.enabled:
            laya_session, laya_error = await _start_laya_session(stack)

        obs_tools = (await obs_session.list_tools()).tools
        dt_tools  = (await dt_session.list_tools()).tools

        # Merge both tool lists into one Gemini declaration. LAYA-MCP's tool is
        # called from code only and is deliberately not offered to Gemini.
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
        print(f"  Slack   : {slack_mode()}"
              + ("  (payloads logged, no HTTP call; SLACK_DRY_RUN=false to post)"
                 if slack_mode() == "DRY_RUN" else "  (posts to SLACK_WEBHOOK_URL)"))
        print(f"  DT tool : dt_ingest_log {'DRY_RUN' if dt_ingest_tool_dry_run() else 'LIVE'}")
        if laya_cfg.enabled:
            status = "['classify_services']" if laya_session else f"unavailable ({laya_error})"
            print(f"  LAYA-MCP: {status}  |  mode={laya_mode}  model={laya_cfg.model}")
        print(f"{'='*70}\n")

        log_agent_event(
            run_id=run_id,
            event_type="RUN_START",
            message=f"Agent run started. Analysing last {window_minutes} minutes."
                    + (f" Laya mode={laya_mode}." if laya_cfg.enabled else ""),
        )

        system_prompt = SYSTEM_PROMPT
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

        packs: list = []
        paged_by_gemini: set = set()
        gemini_severity: dict = {}

        if laya_cfg.enabled:
            pg, dt, packs, triage = await _laya_pretriage(
                run_id, window_minutes, obs_session, dt_session, laya_session,
                laya_error, laya_cfg, laya_mode)
            print(triage_table(triage) + "\n")
            if laya_mode in ("advisory", "enforce"):
                system_prompt = SYSTEM_PROMPT + LAYA_PROMPT_ADDENDUM
                user_message = advisory_user_message(window_minutes, pg, dt, packs, triage)

        chat = _BoundedChat(
            client,
            MODEL,
            types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=gemini_tools,
            ),
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

                # run_id comes from the agent, never from Gemini.
                tool_args.pop("run_id", None)
                if laya_cfg.enabled and tool_name in RUN_ID_TOOLS:
                    tool_args["run_id"] = run_id

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

                    if tool_name == "call_on_call_engineer":
                        paged_by_gemini.add(tool_args.get("service"))
                    elif tool_name == "raise_alert" and not getattr(mcp_resp, "isError", False):
                        try:
                            gemini_severity[tool_args.get("service")] = f"SEV-{int(tool_args['severity'])}"
                        except (KeyError, TypeError, ValueError):
                            pass

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

    if laya_cfg.enabled and packs:
        policy_table = _apply_policy_after_loop(
            run_id, laya_mode, packs, paged_by_gemini, gemini_severity)
        if policy_table:
            print(f"\n{policy_table}\n")
            # Shadow mode leaves the report unchanged; advisory/enforce append the decisions.
            if laya_mode in ("advisory", "enforce"):
                final_text = f"{final_text}\n\n{policy_table}" if final_text else policy_table

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
# RAG — retrieval-augmented generation over the runbook knowledge base
# ---------------------------------------------------------------------------

_RAG_SYSTEM_PROMPT = (
    "You are a precise assistant for the Observability Agent project. "
    "Answer the user's question using ONLY the context chunks provided. "
    "If the answer is not present in the context, say so clearly — do not invent information. "
    "Be concise. Quote specific details from the context where they are relevant."
)


def _rag_retrieve(question: str) -> list[dict]:
    """Embed the question and return the top-k closest chunks from ChromaDB."""
    ef         = embedding_functions.DefaultEmbeddingFunction()
    chroma     = chromadb.PersistentClient(path=str(CHROMA_PATH))
    collection = chroma.get_collection(COLLECTION, embedding_function=ef)

    results = collection.query(
        query_texts=[question],
        n_results=TOP_K,
        include=["documents", "metadatas", "distances"],
    )

    return [
        {
            "text":        doc,
            "source":      meta["source"],
            "chunk_index": meta["chunk_index"],
            "distance":    dist,
        }
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )
    ]


def _rag_build_prompt(question: str, chunks: list[dict]) -> str:
    """Combine retrieved chunks into an augmented prompt."""
    context_blocks = []
    for i, chunk in enumerate(chunks, 1):
        context_blocks.append(
            f"[Context {i} — {chunk['source']}, chunk {chunk['chunk_index']} "
            f"(cosine distance: {chunk['distance']:.4f})]\n{chunk['text']}"
        )
    context = "\n\n---\n\n".join(context_blocks)
    return f"Context from the observability runbooks:\n\n{context}\n\nQuestion: {question}"


def run_rag() -> None:
    """
    Interactive RAG loop.

    Flow per question:
      1. Embed the question using all-MiniLM-L6-v2 (ChromaDB built-in, local)
      2. Retrieve top-3 closest chunks from the 'observability_docs' collection
      3. Build an augmented prompt: context chunks + question
      4. Send to Gemini 2.5 Flash and print the answer
    """
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY is not set in .env")
        sys.exit(1)

    if not CHROMA_PATH.exists():
        print("ERROR: chroma_db/ not found — run  python embed_docs.py  first.")
        sys.exit(1)

    client = genai.Client(api_key=GEMINI_API_KEY)

    print(f"\n{'='*70}")
    print("  OBSERVABILITY RAG ASSISTANT")
    print("  Knowledge base: dynatrace-alerts | agent-runbook | mcp-integration")
    print(f"  Model: {MODEL}  |  Top-k: {TOP_K}  |  Type 'exit' to quit")
    print(f"{'='*70}\n")

    while True:
        try:
            question = input("Ask a question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit", "q"):
            print("Goodbye.")
            break

        # Step 1 — retrieve
        print(f"\n[RAG] Retrieving top {TOP_K} chunks...")
        chunks = _rag_retrieve(question)
        for i, chunk in enumerate(chunks, 1):
            print(f"  [{i}] {chunk['source']}  chunk={chunk['chunk_index']}  distance={chunk['distance']:.4f}")

        # Step 2 — build augmented prompt
        augmented_prompt = _rag_build_prompt(question, chunks)

        # Step 3 — generate answer
        print("\n[RAG] Generating answer...\n")
        response = client.models.generate_content(
            model=MODEL,
            contents=augmented_prompt,
            config=types.GenerateContentConfig(
                system_instruction=_RAG_SYSTEM_PROMPT,
            ),
        )

        print(f"Answer:\n{response.text}")
        print(f"\n{'─'*70}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--rag" in sys.argv:
        run_rag()
    else:
        window = int(sys.argv[1]) if len(sys.argv) > 1 else ANALYSIS_WINDOW_MINUTES
        result = run_agent(window_minutes=window)
        print("\n--- FINAL INCIDENT REPORT ---")
        print(result)
