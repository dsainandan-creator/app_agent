"""
tools.py – Agent tool implementations.

These are the concrete Python functions behind each Claude tool call.
Tools available to the agent:
  1. get_logs                 – fetch recent logs from PostgreSQL
  2. raise_alert              – persist an alert record in the DB
  3. send_email_notification  – mock email sender
  4. save_metrics             – save analysis metrics to DB
  5. call_on_call_engineer    – escalate Sev-1 via Slack Incoming Webhook
"""

import datetime
import json
from typing import Any

from mock_app.database import (
    fetch_logs,
    save_alert,
    save_metrics_record,
)
from slack_notify import notify_slack


# ---------------------------------------------------------------------------
# Tool 1 – Get logs
# ---------------------------------------------------------------------------

def _infer_service(endpoint: str) -> str:
    """Map an endpoint path to a logical service name."""
    if not endpoint:
        return "unknown"
    if "/payments" in endpoint:
        return "payment-service"
    if "/orders" in endpoint:
        return "order-service"
    if "/users" in endpoint:
        return "user-service"
    if "/products" in endpoint or "/inventory" in endpoint:
        return "product-service"
    return "api-gateway"


def get_logs(timeframe_minutes: int = 30) -> str:
    """
    Retrieve aggregated log statistics from the last N minutes.
    Returns overall stats plus a per-service breakdown — no raw rows.
    """
    rows = fetch_logs(window_minutes=timeframe_minutes)

    total = len(rows)
    errors = [r for r in rows if r.get("status_code", 0) >= 500]
    successes = [r for r in rows if r.get("status_code", 0) < 400]

    overall_error_rate = (len(errors) / total * 100) if total else 0.0
    overall_avg_rt = (
        sum(r["response_time_ms"] for r in rows if r.get("response_time_ms")) / total
        if total else 0.0
    )

    # Per-service aggregation
    from collections import defaultdict
    svc_buckets: dict = defaultdict(lambda: {"total": 0, "errors": 0, "latencies": []})
    for r in rows:
        svc = _infer_service(r.get("endpoint", ""))
        svc_buckets[svc]["total"] += 1
        if r.get("response_time_ms"):
            svc_buckets[svc]["latencies"].append(r["response_time_ms"])
        if r.get("status_code", 0) >= 500:
            svc_buckets[svc]["errors"] += 1

    services = []
    for svc, b in sorted(svc_buckets.items()):
        t = b["total"]
        e = b["errors"]
        avg = sum(b["latencies"]) / len(b["latencies"]) if b["latencies"] else 0.0
        services.append({
            "service": svc,
            "total_requests": t,
            "error_count": e,
            "error_rate_pct": round(e / t * 100, 1) if t else 0.0,
            "avg_response_time_ms": round(avg, 1),
        })

    result = {
        "source": "postgresql",
        "status": "ok",
        "window_minutes": timeframe_minutes,
        "overall": {
            "total_requests": total,
            "error_count": len(errors),
            "success_count": len(successes),
            "error_rate_pct": round(overall_error_rate, 1),
            "avg_response_time_ms": round(overall_avg_rt, 1),
        },
        "services": services,
    }
    return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Tool 2 – Raise alert (DB record)
# ---------------------------------------------------------------------------


def raise_alert(service: str, severity: int, message: str) -> str:
    """
    Persist an alert record in the alerts table and print it.

    Args:
        service:  Affected service name.
        severity: 1 (critical) → 4 (low).
        message:  Alert description.

    Returns:
        Confirmation string.
    """
    level_labels = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}
    label = level_labels.get(severity, "UNKNOWN")

    save_alert(
        severity=severity,
        service=service,
        message=message,
        alert_type="RAISE_ALERT",
    )

    banner = (
        f"\n[ALERT][SEV-{severity}/{label}] Service: {service}\n"
        f"  {message}\n"
        f"  Recorded at {datetime.datetime.now().isoformat(timespec='seconds')}"
    )
    print(banner)

    return f"Alert SEV-{severity}/{label} raised for '{service}': {message}"


# ---------------------------------------------------------------------------
# Tool 4 – Send email notification (mock)
# ---------------------------------------------------------------------------

def send_email_notification(
    recipient: str,
    severity: int,
    subject: str,
    body: str,
) -> str:
    """
    Send a mock email alert. Prints to console and persists to DB.

    Args:
        recipient: Email address of the recipient.
        severity:  Severity level 1–4.
        subject:   Email subject.
        body:      Email body text.

    Returns:
        Confirmation string.
    """
    level_labels = {1: "CRITICAL", 2: "HIGH", 3: "MEDIUM", 4: "LOW"}
    label = level_labels.get(severity, "UNKNOWN")
    ts = datetime.datetime.now().isoformat(timespec="seconds")

    # Persist in alerts table as EMAIL type
    save_alert(
        severity=severity,
        service="email-notification",
        message=f"To: {recipient} | Subject: {subject} | {body[:200]}",
        alert_type="EMAIL",
    )

    print(
        f"\n{'─'*60}\n"
        f"  [EMAIL SENT]  SEV-{severity}/{label}\n"
        f"  To      : {recipient}\n"
        f"  Subject : {subject}\n"
        f"  Time    : {ts}\n"
        f"  Body    :\n{body}\n"
        f"{'─'*60}\n"
    )

    return f"Email sent to {recipient} | SEV-{severity}/{label} | Subject: {subject}"


# ---------------------------------------------------------------------------
# Tool 5 – Save metrics
# ---------------------------------------------------------------------------

def save_metrics(
    window_minutes: int,
    total_requests: int,
    error_count: int,
    success_count: int,
    avg_response_time_ms: float,
    error_rate: float,
    severity_assessment: int,
    analysis_summary: str,
) -> str:
    """
    Persist the agent's analysis metrics to the metrics table.

    Args:
        window_minutes:       Analysis time window.
        total_requests:       Total requests in window.
        error_count:          Number of 5xx errors.
        success_count:        Number of 2xx responses.
        avg_response_time_ms: Average latency.
        error_rate:           Error rate as a percentage (0–100).
        severity_assessment:  Agent's overall severity rating (1–4).
        analysis_summary:     Human-readable summary from the agent.

    Returns:
        Confirmation string.
    """
    save_metrics_record(
        window_minutes=window_minutes,
        total_requests=total_requests,
        error_count=error_count,
        success_count=success_count,
        avg_response_time_ms=avg_response_time_ms,
        error_rate=error_rate,
        severity_assessment=severity_assessment,
        analysis_summary=analysis_summary,
    )
    return (
        f"Metrics saved: {total_requests} reqs | "
        f"{error_rate:.1f}% errors | SEV-{severity_assessment} | "
        f"{analysis_summary[:80]}"
    )


# ---------------------------------------------------------------------------
# Tool 6 – Call on-call engineer
# ---------------------------------------------------------------------------

def call_on_call_engineer(service: str, message: str) -> str:
    """
    Escalate a Sev-1 critical incident via Slack Incoming Webhook.
    Also persists the escalation in the alerts DB.

    Args:
        service: The failing service or component.
        message: Description of the critical failure.

    Returns:
        Incident details as a JSON string.
    """
    incident = notify_slack(service=service, message=message)

    save_alert(
        severity=1,
        service=service,
        message=f"[SLACK] {incident['incident_id']}: {message}",
        alert_type="ONCALL",
    )

    return json.dumps(incident)


# ---------------------------------------------------------------------------
# Tool registry (used by the agent)
# ---------------------------------------------------------------------------

TOOL_FUNCTIONS: dict[str, Any] = {
    "get_logs":                get_logs,
    "raise_alert":             raise_alert,
    "send_email_notification": send_email_notification,
    "save_metrics":            save_metrics,
    "call_on_call_engineer":   call_on_call_engineer,
}

# Claude tool definitions (JSON schema)
TOOL_DEFINITIONS = [
    {
        "name": "get_logs",
        "description": (
            "Retrieve application logs from the observability database for the last N minutes. "
            "Returns a summary (total requests, error count, success count, error rate, "
            "avg response time) plus up to 100 individual log rows. "
            "Use this as your first step to gather data before classifying severity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timeframe_minutes": {
                    "type": "integer",
                    "description": "How many minutes back to look for logs. Default is 30.",
                    "default": 30,
                }
            },
            "required": [],
        },
    },
    {
        "name": "raise_alert",
        "description": (
            "Persist a formal alert record in the database for the affected service. "
            "Call this for every severity level to create an audit trail. "
            "Severity: 1=CRITICAL, 2=HIGH, 3=MEDIUM, 4=LOW."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Name of the affected service or component.",
                },
                "severity": {
                    "type": "integer",
                    "description": "Severity level: 1 (critical) to 4 (low).",
                    "enum": [1, 2, 3, 4],
                },
                "message": {
                    "type": "string",
                    "description": "Clear description of the alert condition.",
                },
            },
            "required": ["service", "severity", "message"],
        },
    },
    {
        "name": "send_email_notification",
        "description": (
            "Send a mock email alert to a recipient. "
            "Use for Sev-2, Sev-3, and Sev-4 issues to notify the team. "
            "Also use for Sev-1 in addition to the on-call page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {
                    "type": "string",
                    "description": "Email address of the recipient.",
                },
                "severity": {
                    "type": "integer",
                    "description": "Severity level: 1 (critical) to 4 (low).",
                    "enum": [1, 2, 3, 4],
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line.",
                },
                "body": {
                    "type": "string",
                    "description": "Full email body with metrics and recommended actions.",
                },
            },
            "required": ["recipient", "severity", "subject", "body"],
        },
    },
    {
        "name": "save_metrics",
        "description": (
            "Save the agent's analysis results and computed metrics to the database. "
            "Always call this after completing your analysis so results are persisted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_minutes": {
                    "type": "integer",
                    "description": "Analysis window in minutes.",
                },
                "total_requests": {
                    "type": "integer",
                    "description": "Total number of requests in the window.",
                },
                "error_count": {
                    "type": "integer",
                    "description": "Number of 5xx error responses.",
                },
                "success_count": {
                    "type": "integer",
                    "description": "Number of successful (2xx) responses.",
                },
                "avg_response_time_ms": {
                    "type": "number",
                    "description": "Average response time in milliseconds.",
                },
                "error_rate": {
                    "type": "number",
                    "description": "Error rate as a percentage (0–100).",
                },
                "severity_assessment": {
                    "type": "integer",
                    "description": "Overall severity assessment: 1–4.",
                    "enum": [1, 2, 3, 4],
                },
                "analysis_summary": {
                    "type": "string",
                    "description": "Human-readable summary of the analysis and actions taken.",
                },
            },
            "required": [
                "window_minutes",
                "total_requests",
                "error_count",
                "success_count",
                "avg_response_time_ms",
                "error_rate",
                "severity_assessment",
                "analysis_summary",
            ],
        },
    },
    {
        "name": "call_on_call_engineer",
        "description": (
            "Page the on-call engineer immediately for a Sev-1 CRITICAL incident. "
            "ONLY call this when the overall severity is 1 (critical). "
            "This simulates raising a P1 incident in PagerDuty/OpsGenie."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "The failing service or component name.",
                },
                "message": {
                    "type": "string",
                    "description": (
                        "Detailed description of the critical failure, including "
                        "error rate, affected endpoints, and impact."
                    ),
                },
            },
            "required": ["service", "message"],
        },
    },
]
