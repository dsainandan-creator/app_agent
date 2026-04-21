"""
on_call.py – Mock on-call engineer notification system.

Simulates raising a P1/Sev-1 incident page to an on-call engineer,
similar to PagerDuty or OpsGenie. In production this would fire a webhook.
"""

import datetime
import random
import time


# Simulate on-call roster
_ON_CALL_ROSTER = [
    {"name": "Alice Chen",    "phone": "+1-555-0101", "team": "Platform SRE"},
    {"name": "Bob Martinez",  "phone": "+1-555-0102", "team": "Backend SRE"},
    {"name": "Priya Sharma",  "phone": "+1-555-0103", "team": "Infra On-Call"},
]

_INCIDENT_COUNTER = 0


def call_on_call_engineer(service: str, message: str) -> dict:
    """
    Simulate paging the on-call engineer with a critical incident.

    Args:
        service:  The name of the failing service / component.
        message:  Human-readable description of the critical failure.

    Returns:
        A dict describing the incident that was raised.
    """
    global _INCIDENT_COUNTER
    _INCIDENT_COUNTER += 1

    engineer = random.choice(_ON_CALL_ROSTER)
    incident_id = f"INC-{datetime.datetime.now().strftime('%Y%m%d')}-{_INCIDENT_COUNTER:04d}"
    ts = datetime.datetime.now().isoformat(timespec="seconds")

    # Simulate network call latency
    time.sleep(0.3)

    incident = {
        "incident_id":    incident_id,
        "severity":       "SEV-1 / P1 – CRITICAL",
        "service":        service,
        "message":        message,
        "timestamp":      ts,
        "on_call_engineer": engineer["name"],
        "team":           engineer["team"],
        "contact":        engineer["phone"],
        "status":         "PAGED",
        "acknowledgment_url": f"https://oncall.internal/incidents/{incident_id}/ack",
    }

    _print_incident_banner(incident)
    return incident


def _print_incident_banner(incident: dict):
    border = "=" * 70
    print(f"\n{border}")
    print("  *** SEV-1 CRITICAL INCIDENT RAISED ***")
    print(border)
    print(f"  Incident ID  : {incident['incident_id']}")
    print(f"  Service      : {incident['service']}")
    print(f"  Time         : {incident['timestamp']}")
    print(f"  Message      : {incident['message']}")
    print(f"  On-Call      : {incident['on_call_engineer']} ({incident['team']})")
    print(f"  Contact      : {incident['contact']}")
    print(f"  Ack URL      : {incident['acknowledgment_url']}")
    print(f"  Status       : {incident['status']}")
    print(f"{border}\n")
