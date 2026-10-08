"""
slack_notify.py – Send SEV-1 critical incident alerts to a Slack channel.

Uses an Incoming Webhook URL stored in SLACK_WEBHOOK_URL env var / .env file.
Only called for SEV-1 incidents; all other severities are handled via DB logging.

Dry run is ON by default. Unless SLACK_DRY_RUN=false is set explicitly,
notify_slack builds the full payload, logs it (stderr plus
logs/slack_dry_run.jsonl) and returns status "DRY_RUN" without any HTTP call,
even when SLACK_WEBHOOK_URL is set.
"""

import datetime
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
DRY_RUN_LOG = Path(__file__).parent / "logs" / "slack_dry_run.jsonl"


def slack_dry_run() -> bool:
    """True unless SLACK_DRY_RUN is explicitly false. Read on every call."""
    return os.environ.get("SLACK_DRY_RUN", "true").strip().lower() not in ("false", "0", "no", "off")


def slack_mode() -> str:
    return "DRY_RUN" if slack_dry_run() else "LIVE"


def _log_dry_run(payload: dict, result: dict) -> None:
    record = {"logged_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "incident_id": result["incident_id"], "service": result["service"], "payload": payload}
    # stderr, not stdout: inside an MCP stdio server, stdout is the protocol channel.
    print(f"[Slack DRY_RUN] would POST to webhook:\n{json.dumps(payload, indent=2, ensure_ascii=False)}",
          file=sys.stderr)
    try:
        DRY_RUN_LOG.parent.mkdir(exist_ok=True)
        with open(DRY_RUN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[Slack DRY_RUN] could not write {DRY_RUN_LOG}: {exc}", file=sys.stderr)


def notify_slack(service: str, message: str) -> dict:
    """
    Post a SEV-1 critical incident alert to Slack via Incoming Webhook.

    Args:
        service: The failing service name.
        message: Description of the critical failure (error rate, latency, impact).

    Returns:
        Dict with incident_id, status ("SENT" or "DRY_RUN"), and delivery details.
    """
    dry_run = slack_dry_run()
    if not dry_run and not SLACK_WEBHOOK_URL:
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

    result = {
        "incident_id": incident_id,
        "severity": "SEV-1 / P1 – CRITICAL",
        "service": service,
        "message": message,
        "timestamp": ts.isoformat(timespec="seconds"),
        "channel": "slack",
        "status": "DRY_RUN" if dry_run else "SENT",
    }

    if dry_run:
        _log_dry_run(payload, result)
    else:
        response = httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()

    _print_incident_banner(result)
    return result


def _print_incident_banner(incident: dict):
    border = "=" * 70
    print(f"\n{border}")
    if incident["status"] == "DRY_RUN":
        print("  *** SEV-1 CRITICAL — SLACK DRY RUN (NOT SENT) ***")
    else:
        print("  *** SEV-1 CRITICAL — SLACK ALERT SENT ***")
    print(border)
    print(f"  Incident ID : {incident['incident_id']}")
    print(f"  Service     : {incident['service']}")
    print(f"  Time        : {incident['timestamp']}")
    print(f"  Message     : {incident['message']}")
    print(f"  Channel     : Slack (Incoming Webhook)")
    print(f"  Status      : {incident['status']}")
    print(f"{border}\n")