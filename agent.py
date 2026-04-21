"""
agent.py – Observability Agent powered by Google Gemini 2.5 Flash.

The agent follows a Reasoning → Action loop:
  1. Call get_logs to retrieve aggregated log statistics.
  2. Analyse the data and classify the overall severity (1–4).
  3. Take the appropriate action based on severity:
       Sev-1 (CRITICAL) → call on-call engineer + raise_alert + send_email
       Sev-2 (HIGH)     → raise_alert + send_email
       Sev-3 (MEDIUM)   → raise_alert + send_email
       Sev-4 (LOW)      → raise_alert + send_email (lower priority)
  4. Save analysis metrics to the database.
  5. Produce a final summary.

Severity classification rules:
  Sev-1: error rate > 30% OR avg latency > 2000 ms
  Sev-2: error rate 15–30% OR avg latency 1000–2000 ms
  Sev-3: error rate 5–15% OR avg latency 500–1000 ms
  Sev-4: error rate < 5% and latency < 500 ms
"""

import json
import os
import re
import sys
import time
import uuid

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ClientError

from tools import TOOL_FUNCTIONS
from mock_app.database import log_agent_event

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

MODEL = "gemini-3-flash-preview"

ANALYSIS_WINDOW_MINUTES = 30
ALERT_EMAIL = "oncall-team@company.internal"

SYSTEM_PROMPT = """You are an Observability Agent. Analyse logs per service, classify severity, and alert.

get_logs returns an overall summary AND a per-service breakdown.
Classify each service independently using these rules:
  SEV-1: error_rate_pct > 30  OR  avg_response_time_ms > 2000
  SEV-2: error_rate_pct 15-30 OR  avg_response_time_ms 1000-2000
  SEV-3: error_rate_pct 5-15  OR  avg_response_time_ms 500-1000
  SEV-4: error_rate_pct < 5   AND avg_response_time_ms < 500

Steps (no skipping):
1. Call get_logs once.
2. For each service in the breakdown:
   a. Classify its severity using the rules above.
   b. If SEV-1: call call_on_call_engineer for that service.
   c. Call raise_alert for that service with its severity.
3. Call send_email_notification once, summarising all services and their severities.
4. Call save_metrics once using the overall stats and the highest severity found.
5. Write a concise incident report covering all services.
"""

# ---------------------------------------------------------------------------
# Gemini tool definitions
# ---------------------------------------------------------------------------

GEMINI_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="get_logs",
                description="Get overall + per-service aggregated stats (error rate, latency) for the last N minutes. Call once at the start.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "timeframe_minutes": types.Schema(
                            type=types.Type.INTEGER,
                            description="Minutes to look back. Default 30.",
                        )
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="raise_alert",
                description="Save a formal alert to the DB. Call for every severity level.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "service": types.Schema(type=types.Type.STRING, description="Affected service name."),
                        "severity": types.Schema(type=types.Type.INTEGER, description="1=CRITICAL,2=HIGH,3=MEDIUM,4=LOW."),
                        "message": types.Schema(type=types.Type.STRING, description="Alert description."),
                    },
                    required=["service", "severity", "message"],
                ),
            ),
            types.FunctionDeclaration(
                name="send_email_notification",
                description="Send a mock email alert. Always call this for every severity.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "recipient": types.Schema(type=types.Type.STRING, description="Recipient email."),
                        "severity": types.Schema(type=types.Type.INTEGER, description="1-4."),
                        "subject": types.Schema(type=types.Type.STRING, description="Email subject."),
                        "body": types.Schema(type=types.Type.STRING, description="Email body with metrics."),
                    },
                    required=["recipient", "severity", "subject", "body"],
                ),
            ),
            types.FunctionDeclaration(
                name="save_metrics",
                description="Persist analysis metrics to DB. Always call after analysis.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "window_minutes": types.Schema(type=types.Type.INTEGER, description="Analysis window minutes."),
                        "total_requests": types.Schema(type=types.Type.INTEGER, description="Total requests."),
                        "error_count": types.Schema(type=types.Type.INTEGER, description="5xx error count."),
                        "success_count": types.Schema(type=types.Type.INTEGER, description="2xx count."),
                        "avg_response_time_ms": types.Schema(type=types.Type.NUMBER, description="Avg latency ms."),
                        "error_rate": types.Schema(type=types.Type.NUMBER, description="Error rate 0-100."),
                        "severity_assessment": types.Schema(type=types.Type.INTEGER, description="Severity 1-4."),
                        "analysis_summary": types.Schema(type=types.Type.STRING, description="Brief summary."),
                    },
                    required=[
                        "window_minutes", "total_requests", "error_count", "success_count",
                        "avg_response_time_ms", "error_rate", "severity_assessment", "analysis_summary",
                    ],
                ),
            ),
            types.FunctionDeclaration(
                name="call_on_call_engineer",
                description="Page on-call engineer. ONLY for SEV-1 (error_rate > 30% or latency > 2000ms).",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "service": types.Schema(type=types.Type.STRING, description="Failing service name."),
                        "message": types.Schema(type=types.Type.STRING, description="Critical failure description with error rate and impact."),
                    },
                    required=["service", "message"],
                ),
            ),
        ]
    )
]

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


def send_with_retry(chat, message, max_retries: int = 8):
    """Send a chat message, retrying on 429 quota errors with backoff."""
    for attempt in range(max_retries):
        try:
            return chat.send_message(message)
        except ClientError as e:
            err = str(e)
            is_quota = "429" in err or "RESOURCE_EXHAUSTED" in err
            if is_quota and attempt < max_retries - 1:
                delay = _parse_retry_delay(err)
                print(f"[Agent] Rate limited – waiting {delay}s (attempt {attempt + 1}/{max_retries})…")
                time.sleep(delay)
            else:
                raise

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent(window_minutes: int = ANALYSIS_WINDOW_MINUTES) -> str:
    """Run one full analysis cycle. Returns the agent's final incident report."""
    api_key = GEMINI_API_KEY
    if not api_key:
        print("ERROR: GEMINI_API_KEY is not set.")
        print("  Add it to your .env file or export it as an environment variable.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)

    chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=GEMINI_TOOLS,
        ),
    )

    run_id = str(uuid.uuid4())[:8]

    print(f"\n{'='*70}")
    print(f"  OBSERVABILITY AGENT  –  Analysing last {window_minutes} minutes")
    print(f"  Model: {MODEL}  |  Run ID: {run_id}")
    print(f"{'='*70}\n")

    log_agent_event(
        run_id=run_id,
        event_type="RUN_START",
        message=f"Agent run started. Analysing last {window_minutes} minutes.",
    )

    user_message = (
        f"Analyse the application logs from the last {window_minutes} minutes. "
        "Classify the severity, take appropriate action (on-call page if Sev-1, "
        "email + alert for all severities), save metrics, and provide a brief "
        "incident report."
    )

    iteration = 0
    max_iterations = 20
    final_text = ""

    response = send_with_retry(chat, user_message)

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

            print(f"[Tool Call] {tool_name}({json.dumps(tool_args, indent=2)[:300]})")

            log_agent_event(
                run_id=run_id,
                event_type="TOOL_CALL",
                tool_name=tool_name,
                tool_input=json.dumps(tool_args),
                message=f"Calling {tool_name}",
            )

            fn = TOOL_FUNCTIONS.get(tool_name)
            if fn is None:
                result = f"Error: unknown tool '{tool_name}'"
                print(f"[Tool Error] {result}")
                log_agent_event(run_id=run_id, event_type="TOOL_ERROR",
                                tool_name=tool_name, tool_result=result, message=result)
            else:
                try:
                    result = fn(**tool_args)
                    print(f"[Tool Result] {str(result)[:300]}")

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
                    print(f"[Tool Error] {result}")
                    log_agent_event(run_id=run_id, event_type="TOOL_ERROR",
                                    tool_name=tool_name, tool_result=result, message=result)

            tool_response_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=tool_name,
                        response={"result": result},
                    )
                )
            )

        time.sleep(2)
        response = send_with_retry(chat, tool_response_parts)

    log_agent_event(
        run_id=run_id,
        event_type="FINAL_REPORT",
        message=final_text[:2000] if final_text else "Run complete (no text output).",
    )

    print(f"\n{'='*70}")
    print("  AGENT RUN COMPLETE")
    print(f"{'='*70}\n")

    return final_text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    window = int(sys.argv[1]) if len(sys.argv) > 1 else ANALYSIS_WINDOW_MINUTES
    result = run_agent(window_minutes=window)
    print("\n--- FINAL INCIDENT REPORT ---")
    print(result)