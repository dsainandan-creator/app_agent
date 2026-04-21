"""
run_all_severities.py – Run the agent against all 4 severity scenarios.

For each severity (1 → 4):
  1. Truncate the logs table
  2. Insert mock log rows that will trigger exactly that severity
  3. Run the agent – it scans, classifies, alerts, saves metrics
  4. Short pause, then move to next scenario

Severity thresholds (from agent.py):
  SEV-1: error_rate > 30%  OR  avg_latency > 2000ms
  SEV-2: error_rate 15-30% OR  avg_latency 1000-2000ms
  SEV-3: error_rate 5-15%  OR  avg_latency 500-1000ms
  SEV-4: error_rate < 5%   AND avg_latency < 500ms

Run:
  python run_all_severities.py
"""

import time
from datetime import datetime, timedelta

import psycopg2

DB_CONFIG = {
    "dbname": "observability",
    "user": "postgres",
    "host": "localhost",
    "port": 5432,
}

# ---------------------------------------------------------------------------
# Log scenarios  –  (endpoint, method, status_code, response_time_ms, error_detail)
# ---------------------------------------------------------------------------

# SEV-1: 40% error rate, ~2470ms avg latency  →  both thresholds breached
SEV1 = [
    ("/api/payments",    "POST", 503, 3100, "Payment gateway unreachable"),
    ("/api/payments",    "POST", 500, 2800, "Internal server error: DB pool exhausted"),
    ("/api/orders",      "POST", 503, 2900, "Upstream service unavailable"),
    ("/api/users",       "GET",  200, 2200, None),
    ("/api/payments",    "POST", 500, 2700, "Connection timeout after 2500ms"),
    ("/api/orders",      "POST", 200, 2100, None),
    ("/api/users",       "GET",  200, 2300, None),
    ("/api/payments",    "POST", 503, 3200, "Service timeout: no response in 3s"),
    ("/api/products",    "GET",  200, 2000, None),
    ("/health",          "GET",  200, 2100, None),
    ("/api/orders",      "POST", 500, 2600, "DB connection refused"),
    ("/api/users/1",     "GET",  200, 2200, None),
    ("/api/payments",    "POST", 503, 2900, "Payment service down"),
    ("/api/products",    "GET",  200, 2100, None),
    ("/api/orders",      "POST", 500, 2800, "Null pointer in checkout flow"),
    ("/health",          "GET",  200, 2000, None),
    ("/api/users",       "GET",  200, 2300, None),
    ("/api/payments",    "POST", 503, 3000, "Upstream payment gateway timeout"),
    ("/api/inventory/1", "GET",  200, 2200, None),
    ("/api/users/2",     "GET",  200, 2100, None),
]
# 8 errors / 20 total = 40%  |  avg ≈ (8×2912 + 12×2183)/20 ≈ 2470ms

# SEV-2: 22% error rate, ~1378ms avg latency
SEV2 = [
    ("/api/payments",    "POST", 503, 2100, "Payment service degraded"),
    ("/api/users",       "GET",  200, 1100, None),
    ("/api/products",    "GET",  200,  980, None),
    ("/api/orders",      "POST", 500, 1900, "Order service: upstream timeout"),
    ("/api/users/1",     "GET",  200, 1250, None),
    ("/api/inventory/1", "GET",  200,  950, None),
    ("/api/payments",    "POST", 200, 1400, None),
    ("/api/orders",      "POST", 500, 2000, "DB pool exhausted under load"),
    ("/api/products",    "GET",  200, 1050, None),
    ("/health",          "GET",  200,  130, None),
    ("/api/users",       "GET",  200, 1320, None),
    ("/api/payments",    "POST", 503, 1950, "Intermittent gateway failure"),
    ("/api/orders",      "POST", 200, 1600, None),
    ("/api/users/2",     "GET",  200, 1150, None),
    ("/api/products",    "GET",  200, 1080, None),
    ("/api/inventory/2", "GET",  200, 1200, None),
    ("/api/orders",      "POST", 200, 1500, None),
    ("/health",          "GET",  200,  120, None),
]
# 4 errors / 18 total = 22.2%  |  avg ≈ (4×1987 + 14×1167)/18 ≈ 1353ms

# SEV-3: 11% error rate, ~608ms avg latency
SEV3 = [
    ("/api/users",       "GET",  200,  480, None),
    ("/api/products",    "GET",  200,  520, None),
    ("/api/orders",      "POST", 500,  950, "Intermittent DB query timeout"),
    ("/api/users/1",     "GET",  200,  440, None),
    ("/api/payments/1",  "GET",  200,  610, None),
    ("/api/inventory/1", "GET",  200,  390, None),
    ("/api/products",    "GET",  200,  500, None),
    ("/api/orders",      "POST", 200,  720, None),
    ("/health",          "GET",  200,   90, None),
    ("/api/users",       "GET",  200,  550, None),
    ("/api/payments",    "POST", 500, 1100, "Occasional payment validation failure"),
    ("/api/orders",      "POST", 200,  680, None),
    ("/api/users/2",     "GET",  200,  420, None),
    ("/api/products",    "GET",  200,  490, None),
    ("/api/inventory/2", "GET",  200,  460, None),
    ("/health",          "GET",  200,   85, None),
    ("/api/users",       "GET",  200,  530, None),
    ("/api/orders",      "POST", 200,  700, None),
]
# 2 errors / 18 total = 11.1%  |  avg ≈ (2×1025 + 16×518)/18 ≈ 574ms

# SEV-4: 4% error rate, ~207ms avg latency
SEV4 = [
    ("/health",          "GET",  200,   85, None),
    ("/api/users",       "GET",  200,  210, None),
    ("/api/products",    "GET",  200,  195, None),
    ("/api/users/1",     "GET",  200,  180, None),
    ("/api/orders",      "POST", 200,  320, None),
    ("/api/products",    "GET",  200,  200, None),
    ("/api/inventory/1", "GET",  200,  170, None),
    ("/health",          "GET",  200,   90, None),
    ("/api/users",       "GET",  200,  220, None),
    ("/api/payments/1",  "GET",  200,  250, None),
    ("/api/orders",      "POST", 200,  310, None),
    ("/api/products",    "GET",  200,  190, None),
    ("/api/users/2",     "GET",  200,  175, None),
    ("/api/inventory/2", "GET",  200,  165, None),
    ("/health",          "GET",  200,   80, None),
    ("/api/orders",      "POST", 200,  295, None),
    ("/api/users",       "GET",  200,  215, None),
    ("/api/products",    "GET",  200,  185, None),
    ("/api/payments",    "POST", 200,  330, None),
    ("/api/orders",      "POST", 200,  300, None),
    ("/api/users/3",     "GET",  200,  190, None),
    ("/api/inventory/3", "GET",  200,  160, None),
    ("/health",          "GET",  200,   88, None),
    ("/api/products",    "GET",  200,  205, None),
    ("/api/payments",    "POST", 500,  350, "Isolated card validation error"),
]
# 1 error / 25 total = 4%  |  avg ≈ (1×350 + 24×208)/25 ≈ 214ms


SCENARIOS = [
    (1, "CRITICAL", SEV1),
    (2, "HIGH",     SEV2),
    (3, "MEDIUM",   SEV3),
    (4, "LOW",      SEV4),
]

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def clear_logs(conn):
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE logs RESTART IDENTITY;")
    conn.commit()
    cur.close()


def seed_logs(conn, rows):
    """Insert rows spaced evenly across the last 29 minutes so all fall in the 30-min window."""
    now = datetime.now()
    interval = timedelta(minutes=29) / max(len(rows) - 1, 1)

    records = []
    for i, (endpoint, method, status, ms, err_detail) in enumerate(rows):
        ts = now - timedelta(minutes=29) + interval * i
        level = "ERROR" if status >= 500 else ("WARNING" if status >= 400 else "INFO")
        msg = f"{method} {endpoint} {status}"
        records.append((ts, endpoint, method, status, ms, level, msg, f"req-{i+1:04d}", err_detail))

    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO logs
            (timestamp, endpoint, method, status_code, response_time_ms,
             log_level, message, request_id, error_detail)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        records,
    )
    conn.commit()
    cur.close()


def print_log_summary(rows):
    total = len(rows)
    errors = sum(1 for r in rows if r[2] >= 500)
    avg_ms = sum(r[3] for r in rows) / total
    print(f"  Logs seeded: {total} requests | {errors} errors "
          f"({errors/total*100:.1f}%) | avg latency {avg_ms:.0f}ms")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    from agent import run_agent   # imported here to avoid circular issues at module load

    conn = psycopg2.connect(**DB_CONFIG)

    for sev, label, scenario_rows in SCENARIOS:
        print(f"\n{'='*70}")
        print(f"  SCENARIO: SEV-{sev} {label}")
        print(f"{'='*70}")

        clear_logs(conn)
        seed_logs(conn, scenario_rows)
        print_log_summary(scenario_rows)
        print()

        run_agent()

        if sev < 4:
            print(f"\n  ── SEV-{sev} complete. Next scenario in 5s… ──\n")
            time.sleep(5)

    conn.close()

    print(f"\n{'='*70}")
    print("  ALL 4 SEVERITY SCENARIOS COMPLETE")
    print("  Check the dashboard – metrics, alerts, and agent_runs should")
    print("  now show one run for each severity level.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()