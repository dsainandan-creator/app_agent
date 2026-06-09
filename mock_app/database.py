"""
PostgreSQL database connection and schema management for the observability system.
DB: observability, User: postgres (no password)
"""

import psycopg2
import psycopg2.extras

DB_CONFIG = {
    "dbname": "observability",
    "user": "postgres",
    "host": "localhost",
    "port": 5432,
}


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def init_db():
    """Create schema if not exists."""
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id              SERIAL PRIMARY KEY,
                timestamp       TIMESTAMPTZ DEFAULT NOW(),
                endpoint        VARCHAR(255),
                method          VARCHAR(10),
                status_code     INTEGER,
                response_time_ms FLOAT,
                log_level       VARCHAR(20),
                message         TEXT,
                request_id      VARCHAR(50),
                error_detail    TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id                  SERIAL PRIMARY KEY,
                timestamp           TIMESTAMPTZ DEFAULT NOW(),
                window_minutes      INTEGER,
                total_requests      INTEGER,
                error_count         INTEGER,
                success_count       INTEGER,
                avg_response_time_ms FLOAT,
                error_rate          FLOAT,
                severity_assessment INTEGER,
                analysis_summary    TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id          SERIAL PRIMARY KEY,
                timestamp   TIMESTAMPTZ DEFAULT NOW(),
                severity    INTEGER,
                service     VARCHAR(255),
                message     TEXT,
                alert_type  VARCHAR(50),
                status      VARCHAR(50) DEFAULT 'SENT'
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                id          SERIAL PRIMARY KEY,
                timestamp   TIMESTAMPTZ DEFAULT NOW(),
                run_id      VARCHAR(50),
                event_type  VARCHAR(50),
                tool_name   VARCHAR(100),
                tool_input  TEXT,
                tool_result TEXT,
                severity    INTEGER,
                message     TEXT
            )
        """)

        conn.commit()
        print("[DB] Schema initialised.")
    except Exception as exc:
        conn.rollback()
        print(f"[DB] Init error: {exc}")
        raise
    finally:
        cur.close()
        conn.close()


def log_request(
    endpoint: str,
    method: str,
    status_code: int,
    response_time_ms: float,
    message: str,
    request_id: str,
    error_detail: str = None,
):
    """Persist a single request log entry to PostgreSQL and forward to Dynatrace."""
    if status_code >= 500:
        log_level = "ERROR"
    elif status_code >= 400:
        log_level = "WARNING"
    else:
        log_level = "INFO"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO logs
                (endpoint, method, status_code, response_time_ms,
                 log_level, message, request_id, error_detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                endpoint,
                method,
                status_code,
                response_time_ms,
                log_level,
                message,
                request_id,
                error_detail,
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[DB] Log error: {exc}")
    finally:
        cur.close()
        conn.close()

    # Forward to Dynatrace (fire-and-forget — never blocks the caller)
    try:
        from dynatrace_client import ingest_log as _dt_ingest
        _dt_ingest(endpoint, method, status_code, response_time_ms, message, request_id, error_detail)
    except Exception:
        pass


def fetch_logs(window_minutes: int = 30) -> list[dict]:
    """Return all log rows from the last N minutes."""
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT *
            FROM   logs
            WHERE  timestamp >= NOW() - INTERVAL '%s minutes'
            ORDER  BY timestamp DESC
            """,
            (window_minutes,),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def save_alert(severity: int, service: str, message: str, alert_type: str):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO alerts (severity, service, message, alert_type)
            VALUES (%s, %s, %s, %s)
            """,
            (severity, service, message, alert_type),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[DB] Alert save error: {exc}")
    finally:
        cur.close()
        conn.close()


def log_agent_event(
    run_id: str,
    event_type: str,
    message: str,
    tool_name: str = None,
    tool_input: str = None,
    tool_result: str = None,
    severity: int = None,
):
    """Write one agent activity event to agent_runs."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_runs
                (run_id, event_type, tool_name, tool_input, tool_result, severity, message)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, event_type, tool_name, tool_input, tool_result, severity, message),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[DB] agent_runs insert error: {exc}")
    finally:
        cur.close()
        conn.close()


def save_metrics_record(
    window_minutes: int,
    total_requests: int,
    error_count: int,
    success_count: int,
    avg_response_time_ms: float,
    error_rate: float,
    severity_assessment: int,
    analysis_summary: str,
):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO metrics
                (window_minutes, total_requests, error_count, success_count,
                 avg_response_time_ms, error_rate, severity_assessment, analysis_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                window_minutes,
                total_requests,
                error_count,
                success_count,
                avg_response_time_ms,
                error_rate,
                severity_assessment,
                analysis_summary,
            ),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"[DB] Metrics save error: {exc}")
    finally:
        cur.close()
        conn.close()
