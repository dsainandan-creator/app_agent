"""
dashboard.py – FastAPI router that serves data to the React dashboard.
All routes return JSON; the React app polls them every 10 seconds.
"""

import psycopg2.extras
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from mock_app.database import get_connection

router = APIRouter(prefix="/dashboard")


def _query(sql: str, params=None) -> list[dict]:
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@router.get("/logs")
def get_logs(limit: int = 100, minutes: int = 60):
    rows = _query(
        """
        SELECT id, timestamp, endpoint, method, status_code,
               response_time_ms, log_level, message, error_detail
        FROM   logs
        WHERE  timestamp >= NOW() - INTERVAL '1 minute' * %s
        ORDER  BY timestamp DESC
        LIMIT  %s
        """,
        (minutes, limit),
    )
    for r in rows:
        if r["timestamp"]:
            r["timestamp"] = r["timestamp"].isoformat()
    return rows


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@router.get("/metrics")
def get_metrics(limit: int = 20):
    rows = _query(
        """
        SELECT id, timestamp, window_minutes, total_requests,
               error_count, success_count, avg_response_time_ms,
               error_rate, severity_assessment, analysis_summary
        FROM   metrics
        ORDER  BY timestamp DESC
        LIMIT  %s
        """,
        (limit,),
    )
    for r in rows:
        if r["timestamp"]:
            r["timestamp"] = r["timestamp"].isoformat()
    return rows


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

@router.get("/alerts")
def get_alerts(limit: int = 50):
    rows = _query(
        """
        SELECT id, timestamp, severity, service, message, alert_type, status
        FROM   alerts
        ORDER  BY timestamp DESC
        LIMIT  %s
        """,
        (limit,),
    )
    for r in rows:
        if r["timestamp"]:
            r["timestamp"] = r["timestamp"].isoformat()
    return rows


# ---------------------------------------------------------------------------
# Agent activity
# ---------------------------------------------------------------------------

@router.get("/agent-runs")
def get_agent_runs(limit: int = 100):
    rows = _query(
        """
        SELECT id, timestamp, run_id, event_type,
               tool_name, tool_input, tool_result, severity, message
        FROM   agent_runs
        ORDER  BY timestamp DESC
        LIMIT  %s
        """,
        (limit,),
    )
    for r in rows:
        if r["timestamp"]:
            r["timestamp"] = r["timestamp"].isoformat()
    return rows


# ---------------------------------------------------------------------------
# Summary stats (for header cards)
# ---------------------------------------------------------------------------

@router.get("/summary")
def get_summary():
    stats = _query(
        """
        SELECT
            COUNT(*)                                             AS total_requests,
            COUNT(*) FILTER (WHERE status_code >= 500)          AS total_errors,
            ROUND(
                COUNT(*) FILTER (WHERE status_code >= 500)
                * 100.0 / NULLIF(COUNT(*), 0), 2
            )                                                    AS error_rate_pct,
            ROUND(AVG(response_time_ms)::numeric, 1)             AS avg_latency_ms
        FROM logs
        WHERE timestamp >= NOW() - INTERVAL '30 minutes'
        """
    )
    oncall_count = _query(
        "SELECT COUNT(*) AS n FROM alerts WHERE alert_type = 'ONCALL' AND status = 'SENT'"
    )
    latest_severity = _query(
        "SELECT severity_assessment FROM metrics ORDER BY timestamp DESC LIMIT 1"
    )

    s = stats[0] if stats else {}
    return {
        "total_requests_30m":  s.get("total_requests", 0),
        "total_errors_30m":    s.get("total_errors", 0),
        "error_rate_pct":      s.get("error_rate_pct", 0),
        "avg_latency_ms":      s.get("avg_latency_ms", 0),
        "oncall_pages_total":  oncall_count[0]["n"] if oncall_count else 0,
        "latest_severity":     latest_severity[0]["severity_assessment"] if latest_severity else None,
    }