"""
seed_mock_data.py – Seed historical dashboard data (one record per severity).

Tables populated:
  metrics     – 4 historical agent-run summaries (one per sev level)
  alerts      – 9 alert records matching those runs
  agent_runs  – full audit trail for each of the 4 historical runs

Timestamps are always relative to NOW() so the data stays fresh.

Run:
  python seed_mock_data.py
"""

import psycopg2
from datetime import datetime, timedelta

DB_CONFIG = {
    "dbname": "observability",
    "user": "postgres",
    "host": "localhost",
    "port": 5432,
}


def ts(mins_ago, secs_offset=0):
    """Return a datetime relative to now — always fresh."""
    return datetime.now() - timedelta(minutes=mins_ago, seconds=secs_offset)


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    try:
        # ──────────────────────────────────────────────────────────────────────
        # METRICS  –  4 historical runs, one per severity
        # Spaced at 200 / 150 / 100 / 60 minutes ago
        # ──────────────────────────────────────────────────────────────────────
        metrics = [
            (ts(200), 30, 100, 35, 65, 2500.0, 35.0, 1,
             "CRITICAL: 35% error rate on payment-service. Avg latency 2500ms exceeds 2000ms "
             "threshold. Multiple 503 responses from payment gateway. On-call engineer paged."),

            (ts(150), 30,  80, 18, 62, 1400.0, 22.5, 2,
             "HIGH: 22.5% error rate above 15% threshold. Payment and order services degraded. "
             "Avg latency 1400ms. Alert raised and team notified by email."),

            (ts(100), 30,  60,  6, 54,  700.0, 10.0, 3,
             "MEDIUM: 10% error rate within 5-15% range. Order service intermittently failing. "
             "Avg latency 700ms. Alert raised."),

            (ts(60),  30,  50,  1, 49,  200.0,  2.0, 4,
             "LOW: 2% error rate below 5% threshold. Single isolated failure on /api/inventory. "
             "Avg latency 200ms. System healthy overall."),
        ]

        cur.executemany(
            """
            INSERT INTO metrics
                (timestamp, window_minutes, total_requests, error_count, success_count,
                 avg_response_time_ms, error_rate, severity_assessment, analysis_summary)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            metrics,
        )
        print(f"[metrics]    {len(metrics)} rows inserted")

        # ──────────────────────────────────────────────────────────────────────
        # ALERTS  –  one RAISE_ALERT + EMAIL per run; extra ONCALL for SEV-1
        # ──────────────────────────────────────────────────────────────────────
        alerts = [
            # SEV-1  (3 alerts: ONCALL + RAISE_ALERT + EMAIL)
            (ts(200,  0), 1, "payment-service",
             "[ONCALL] INC-001: 35% error rate – payment gateway down. "
             "Avg latency 2500ms. Immediate escalation required.",
             "ONCALL", "SENT"),
            (ts(200, -5), 1, "payment-service",
             "CRITICAL: 35% error rate on payment-service. Avg latency 2500ms. "
             "Multiple 503 errors from payment gateway.",
             "RAISE_ALERT", "SENT"),
            (ts(200, -10), 1, "email-notification",
             "To: oncall-team@company.internal | Subject: [SEV-1 CRITICAL] payment-service DOWN | "
             "35% errors, 2500ms avg latency. On-call paged.",
             "EMAIL", "SENT"),

            # SEV-2  (2 alerts)
            (ts(150,  0), 2, "payment-service",
             "HIGH: 22.5% error rate on payment-service. Avg latency 1400ms. Degraded performance.",
             "RAISE_ALERT", "SENT"),
            (ts(150, -5), 2, "email-notification",
             "To: oncall-team@company.internal | Subject: [SEV-2 HIGH] payment-service degraded | "
             "22.5% errors, 1400ms avg latency.",
             "EMAIL", "SENT"),

            # SEV-3  (2 alerts)
            (ts(100,  0), 3, "order-service",
             "MEDIUM: 10% error rate on order-service. Avg latency 700ms. Intermittent failures.",
             "RAISE_ALERT", "SENT"),
            (ts(100, -5), 3, "email-notification",
             "To: oncall-team@company.internal | Subject: [SEV-3 MEDIUM] order-service failures | "
             "10% errors, 700ms avg latency.",
             "EMAIL", "SENT"),

            # SEV-4  (2 alerts)
            (ts(60,  0), 4, "product-service",
             "LOW: 2% error rate on product-service. Single isolated failure. System healthy.",
             "RAISE_ALERT", "SENT"),
            (ts(60, -5), 4, "email-notification",
             "To: oncall-team@company.internal | Subject: [SEV-4 LOW] isolated failure | "
             "2% errors, 200ms avg latency.",
             "EMAIL", "SENT"),
        ]

        cur.executemany(
            """
            INSERT INTO alerts (timestamp, severity, service, message, alert_type, status)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            alerts,
        )
        print(f"[alerts]     {len(alerts)} rows inserted")

        # ──────────────────────────────────────────────────────────────────────
        # AGENT_RUNS  –  full audit trail for each of the 4 historical runs
        # ──────────────────────────────────────────────────────────────────────
        runs = []

        def add_run(run_id, events):
            for ev in events:
                runs.append((ev[0], run_id, ev[1], ev[2], ev[3], ev[4], ev[5], ev[6]))

        # SEV-1 run  (200 min ago)
        add_run("sev1-ab12", [
            (ts(200, 0),  "RUN_START",            None,                    None, None, None,
             "Agent run started. Analysing last 30 minutes."),
            (ts(200, -1), "TOOL_CALL",            "get_logs",              '{"timeframe_minutes":30}', None, None,
             "Calling get_logs"),
            (ts(200, -2), "TOOL_RESULT",          "get_logs",              None,
             '{"overall":{"total_requests":100,"error_count":35,"error_rate_pct":35.0,"avg_response_time_ms":2500}}',
             None, "get_logs completed"),
            (ts(200, -3), "TOOL_CALL",            "call_on_call_engineer", '{"service":"payment-service","message":"35% error rate, 2500ms latency"}', None, 1,
             "Calling call_on_call_engineer"),
            (ts(200, -4), "TOOL_RESULT",          "call_on_call_engineer", None,
             '{"incident_id":"INC-001","engineer":"Alice Chen","status":"PAGED"}',
             1, "call_on_call_engineer completed"),
            (ts(200, -5), "TOOL_CALL",            "raise_alert",           '{"service":"payment-service","severity":1,"message":"CRITICAL: 35% error rate"}', None, 1,
             "Calling raise_alert"),
            (ts(200, -6), "TOOL_RESULT",          "raise_alert",           None,
             "Alert SEV-1/CRITICAL raised for 'payment-service'", 1, "raise_alert completed"),
            (ts(200, -7), "TOOL_CALL",            "send_email_notification",
             '{"recipient":"oncall-team@company.internal","severity":1,"subject":"[SEV-1 CRITICAL] payment-service DOWN"}',
             None, 1, "Calling send_email_notification"),
            (ts(200, -8), "TOOL_RESULT",          "send_email_notification", None,
             "Email sent to oncall-team@company.internal | SEV-1/CRITICAL", 1,
             "send_email_notification completed"),
            (ts(200, -9), "TOOL_CALL",            "save_metrics",
             '{"window_minutes":30,"total_requests":100,"error_count":35,"error_rate":35.0,"severity_assessment":1}',
             None, 1, "Calling save_metrics"),
            (ts(200,-10), "TOOL_RESULT",          "save_metrics",          None,
             "Metrics saved: 100 reqs | 35.0% errors | SEV-1", 1, "save_metrics completed"),
            (ts(200,-11), "FINAL_REPORT",         None,                    None, None, 1,
             "SEV-1 CRITICAL: payment-service 35% error rate, 2500ms avg latency. "
             "On-call engineer Alice Chen paged. Alert and email sent."),
        ])

        # SEV-2 run  (150 min ago)
        add_run("sev2-cd34", [
            (ts(150, 0),  "RUN_START",   None,       None, None, None,
             "Agent run started. Analysing last 30 minutes."),
            (ts(150, -1), "TOOL_CALL",   "get_logs", '{"timeframe_minutes":30}', None, None,
             "Calling get_logs"),
            (ts(150, -2), "TOOL_RESULT", "get_logs", None,
             '{"overall":{"total_requests":80,"error_count":18,"error_rate_pct":22.5,"avg_response_time_ms":1400}}',
             None, "get_logs completed"),
            (ts(150, -3), "TOOL_CALL",   "raise_alert",
             '{"service":"payment-service","severity":2,"message":"HIGH: 22.5% error rate"}',
             None, 2, "Calling raise_alert"),
            (ts(150, -4), "TOOL_RESULT", "raise_alert", None,
             "Alert SEV-2/HIGH raised for 'payment-service'", 2, "raise_alert completed"),
            (ts(150, -5), "TOOL_CALL",   "send_email_notification",
             '{"recipient":"oncall-team@company.internal","severity":2,"subject":"[SEV-2 HIGH] payment-service degraded"}',
             None, 2, "Calling send_email_notification"),
            (ts(150, -6), "TOOL_RESULT", "send_email_notification", None,
             "Email sent to oncall-team@company.internal | SEV-2/HIGH", 2,
             "send_email_notification completed"),
            (ts(150, -7), "TOOL_CALL",   "save_metrics",
             '{"window_minutes":30,"total_requests":80,"error_count":18,"error_rate":22.5,"severity_assessment":2}',
             None, 2, "Calling save_metrics"),
            (ts(150, -8), "TOOL_RESULT", "save_metrics", None,
             "Metrics saved: 80 reqs | 22.5% errors | SEV-2", 2, "save_metrics completed"),
            (ts(150, -9), "FINAL_REPORT", None, None, None, 2,
             "SEV-2 HIGH: payment-service 22.5% error rate, 1400ms avg latency. Alert and email sent."),
        ])

        # SEV-3 run  (100 min ago)
        add_run("sev3-ef56", [
            (ts(100, 0),  "RUN_START",   None,       None, None, None,
             "Agent run started. Analysing last 30 minutes."),
            (ts(100, -1), "TOOL_CALL",   "get_logs", '{"timeframe_minutes":30}', None, None,
             "Calling get_logs"),
            (ts(100, -2), "TOOL_RESULT", "get_logs", None,
             '{"overall":{"total_requests":60,"error_count":6,"error_rate_pct":10.0,"avg_response_time_ms":700}}',
             None, "get_logs completed"),
            (ts(100, -3), "TOOL_CALL",   "raise_alert",
             '{"service":"order-service","severity":3,"message":"MEDIUM: 10% error rate"}',
             None, 3, "Calling raise_alert"),
            (ts(100, -4), "TOOL_RESULT", "raise_alert", None,
             "Alert SEV-3/MEDIUM raised for 'order-service'", 3, "raise_alert completed"),
            (ts(100, -5), "TOOL_CALL",   "send_email_notification",
             '{"recipient":"oncall-team@company.internal","severity":3,"subject":"[SEV-3 MEDIUM] order-service failures"}',
             None, 3, "Calling send_email_notification"),
            (ts(100, -6), "TOOL_RESULT", "send_email_notification", None,
             "Email sent to oncall-team@company.internal | SEV-3/MEDIUM", 3,
             "send_email_notification completed"),
            (ts(100, -7), "TOOL_CALL",   "save_metrics",
             '{"window_minutes":30,"total_requests":60,"error_count":6,"error_rate":10.0,"severity_assessment":3}',
             None, 3, "Calling save_metrics"),
            (ts(100, -8), "TOOL_RESULT", "save_metrics", None,
             "Metrics saved: 60 reqs | 10.0% errors | SEV-3", 3, "save_metrics completed"),
            (ts(100, -9), "FINAL_REPORT", None, None, None, 3,
             "SEV-3 MEDIUM: order-service 10% error rate, 700ms avg latency. Alert and email sent."),
        ])

        # SEV-4 run  (60 min ago)
        add_run("sev4-gh78", [
            (ts(60, 0),  "RUN_START",   None,       None, None, None,
             "Agent run started. Analysing last 30 minutes."),
            (ts(60, -1), "TOOL_CALL",   "get_logs", '{"timeframe_minutes":30}', None, None,
             "Calling get_logs"),
            (ts(60, -2), "TOOL_RESULT", "get_logs", None,
             '{"overall":{"total_requests":50,"error_count":1,"error_rate_pct":2.0,"avg_response_time_ms":200}}',
             None, "get_logs completed"),
            (ts(60, -3), "TOOL_CALL",   "raise_alert",
             '{"service":"product-service","severity":4,"message":"LOW: 2% error rate, isolated failure"}',
             None, 4, "Calling raise_alert"),
            (ts(60, -4), "TOOL_RESULT", "raise_alert", None,
             "Alert SEV-4/LOW raised for 'product-service'", 4, "raise_alert completed"),
            (ts(60, -5), "TOOL_CALL",   "send_email_notification",
             '{"recipient":"oncall-team@company.internal","severity":4,"subject":"[SEV-4 LOW] isolated failure"}',
             None, 4, "Calling send_email_notification"),
            (ts(60, -6), "TOOL_RESULT", "send_email_notification", None,
             "Email sent to oncall-team@company.internal | SEV-4/LOW", 4,
             "send_email_notification completed"),
            (ts(60, -7), "TOOL_CALL",   "save_metrics",
             '{"window_minutes":30,"total_requests":50,"error_count":1,"error_rate":2.0,"severity_assessment":4}',
             None, 4, "Calling save_metrics"),
            (ts(60, -8), "TOOL_RESULT", "save_metrics", None,
             "Metrics saved: 50 reqs | 2.0% errors | SEV-4", 4, "save_metrics completed"),
            (ts(60, -9), "FINAL_REPORT", None, None, None, 4,
             "SEV-4 LOW: product-service 2% error rate, 200ms avg latency. System healthy. Alert and email sent."),
        ])

        cur.executemany(
            """
            INSERT INTO agent_runs
                (timestamp, run_id, event_type, tool_name, tool_input, tool_result, severity, message)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            runs,
        )
        print(f"[agent_runs] {len(runs)} rows inserted")

        conn.commit()

        print("\n✓ Historical mock data seeded successfully.")
        print("  metrics:     4 rows  (SEV-1 @ 200m ago, SEV-2 @ 150m, SEV-3 @ 100m, SEV-4 @ 60m)")
        print("  alerts:      9 rows  (ONCALL+RAISE+EMAIL for SEV-1 | RAISE+EMAIL for SEV-2/3/4)")
        print("  agent_runs: 42 rows  (full audit trail for all 4 historical runs)")

    except Exception as exc:
        conn.rollback()
        print(f"[ERROR] {exc}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()