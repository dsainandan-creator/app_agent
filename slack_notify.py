"""
slack_notify.py – Send SEV-1 critical incident alerts to a Slack channel.

Uses an Incoming Webhook URL stored in SLACK_WEBHOOK_URL env var / .env file.
Only called for SEV-1 incidents; all other severities are handled via DB logging.
"""

import datetime
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def notify_slack(service: str, message: str) -> dict:
    """
    Post a SEV-1 critical incident alert to Slack via Incoming Webhook.

    Args:
        service: The failing service name.
        message: Description of the critical failure (error rate, latency, impact).

    Returns:
        Dict with incident_id, status, and delivery details.
    """
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError(
            "SLACK_WEBHOOK_URL is not set. Add it to your .env file."
        )

    ts = datetime.datetime.now()
    incident_id = f"INC-{ts.strftime('%Y%m%d-%H%M%S')}"

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚨 SEV-1 CRITICAL INCIDENT",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Incident ID*\n{incident_id}"},
                    {"type": "mrkdwn", "text": f"*Service*\n`{service}`"},
                    {"type": "mrkdwn", "text": f"*Severity*\nSEV-1 / P1 – CRITICAL"},
                    {"type": "mrkdwn", "text": f"*Time*\n{ts.strftime('%Y-%m-%d %H:%M:%S')}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Details*\n{message}"},
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Sent by *Observability Agent* · Gemini 2.5 Flash",
                    }
                ],
            },
        ]
    }

    response = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    response.raise_for_status()

    result = {
        "incident_id": incident_id,
        "severity": "SEV-1 / P1 – CRITICAL",
        "service": service,
        "message": message,
        "timestamp": ts.isoformat(timespec="seconds"),
        "channel": "slack",
        "status": "SENT",
    }

    _print_incident_banner(result)
    return result


def _print_incident_banner(incident: dict):
    border = "=" * 70
    print(f"\n{border}")
    print("  *** SEV-1 CRITICAL — SLACK ALERT SENT ***")
    print(border)
    print(f"  Incident ID : {incident['incident_id']}")
    print(f"  Service     : {incident['service']}")
    print(f"  Time        : {incident['timestamp']}")
    print(f"  Message     : {incident['message']}")
    print(f"  Channel     : Slack (Incoming Webhook)")
    print(f"  Status      : {incident['status']}")
    print(f"{border}\n")