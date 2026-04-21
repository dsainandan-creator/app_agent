# Observability Agent

An AI-powered observability system that monitors a live web service, analyses logs per service, classifies incidents by severity, and takes autonomous action — paging on-call engineers for critical failures and sending email alerts for all severity levels.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Mock FastAPI Service                          │
│                         mock_app/main.py                             │
│                                                                      │
│  /api/payments   /api/orders   /api/users   /api/products   /health  │
│       │                │             │            │                  │
│       └────────────────┴─────────────┴────────────┘                 │
│                   HTTP middleware auto-logs every request            │
│                                  │                                   │
│                          PostgreSQL: logs table                      │
│                                  │                                   │
│          Background: traffic_simulator.py (auto-generates load)      │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │ fetch_logs()
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Observability Agent                            │
│                    agent.py  ·  Gemini 2.5 Flash                     │
│                                                                      │
│   1. get_logs()          → aggregated stats + per-service breakdown  │
│   2. (per service)       → classify SEV-1 / 2 / 3 / 4               │
│   3. call_on_call_engineer()  → SEV-1 services only                 │
│   4. raise_alert()            → every service                        │
│   5. send_email_notification() → one summary email                  │
│   6. save_metrics()           → persist run results                 │
│   7. Final incident report                                           │
└─────────────┬────────────────────────────────┬───────────────────────┘
              │ SEV-1 only                      │ all severities
              ▼                                 ▼
   ┌─────────────────────┐          ┌──────────────────────────┐
   │     on_call.py      │          │       PostgreSQL          │
   │  Mock PagerDuty /   │          │  metrics · alerts ·      │
   │  OpsGenie pager     │          │  agent_runs tables       │
   │  Incident raised    │          └──────────────────────────┘
   │  Engineer paged     │                     │
   └─────────────────────┘                     │ REST API (/dashboard/*)
                                               ▼
                                  ┌────────────────────────┐
                                  │   React Dashboard      │
                                  │   dashboard/src/       │
                                  │   Vite · localhost:5173│
                                  └────────────────────────┘
```

---

## Project Structure

```
app_agent/
├── agent.py                  # Gemini 2.5 Flash agent — reasoning + action loop
├── on_call.py                # Mock PagerDuty/OpsGenie on-call pager
├── tools.py                  # 5 agent tools + Gemini function declarations
├── seed_mock_data.py         # Seed historical data (one run per severity)
├── run_all_severities.py     # Live test runner: cycles through all 4 severities
├── requirements.txt
└── mock_app/
    ├── __init__.py
    ├── main.py               # FastAPI service — endpoints + request logging middleware
    ├── database.py           # PostgreSQL schema + all DB helpers
    ├── dashboard.py          # REST API router that feeds the React dashboard
    └── traffic_simulator.py  # Background async traffic generator
└── dashboard/
    └── src/
        ├── App.jsx           # React dashboard — metrics, alerts, agent activity
        ├── main.jsx
        └── index.css
```

---

## How It Works

### 1. Mock FastAPI Service (`mock_app/main.py`)
Simulates a real production service with multiple endpoints. Every request is automatically logged to PostgreSQL via HTTP middleware. The service randomly produces both successful and error responses at configurable rates.

**Endpoints:**

| Method | Path | Simulated Error Rate |
|--------|------|---------------------|
| GET | `/health` | 0% |
| GET | `/api/users` | 20% (500) |
| GET | `/api/users/{id}` | 10% (500) + 15% (404) |
| POST | `/api/orders` | 25% (400/500/503) |
| GET | `/api/orders/{id}` | 18% (500/503) + 12% (404) |
| GET | `/api/payments/{id}` | 30% (500/503/502) + 10% (404) |
| POST | `/api/payments` | 35% (500/503/422) |
| GET | `/api/products` | 8% (500) |
| GET | `/api/inventory/{id}` | 22% (500/503) |
| POST | `/simulate/spike-errors` | Injects 10 error logs directly |
| POST | `/simulate/critical` | Injects 20 Sev-1 critical logs directly |

**Traffic simulator** runs as a background async task, cycling through four phases:

| Phase | Req/s | Description |
|-------|------:|-------------|
| `NORMAL` | 0.3 | Healthy baseline |
| `DEGRADED` | 0.3 | Elevated errors, slower responses |
| `SPIKE` | 0.8 | Heavy burst with many failures |
| `RECOVERY` | 0.2 | Errors subsiding |

### 2. Observability Agent (`agent.py`)
Powered by **Gemini 2.5 Flash**, the agent runs an autonomous reasoning + action loop. Each run is fully logged to `agent_runs` for audit.

**Agent loop steps:**
1. Call `get_logs` once — returns overall stats plus a per-service breakdown
2. For each service in the breakdown, classify its severity independently
3. For any SEV-1 service, call `call_on_call_engineer`
4. Call `raise_alert` for every service with its individual severity
5. Call `send_email_notification` once, summarising all services
6. Call `save_metrics` once with the overall stats and highest severity found
7. Write a concise incident report

### 3. Severity Classification

Classification is done **per service**, independently:

| Level | Label | Trigger Condition |
|-------|-------|-------------------|
| **SEV-1** | CRITICAL | error_rate > 30% **OR** avg latency > 2000ms |
| **SEV-2** | HIGH | error_rate 15–30% **OR** avg latency 1000–2000ms |
| **SEV-3** | MEDIUM | error_rate 5–15% **OR** avg latency 500–1000ms |
| **SEV-4** | LOW | error_rate < 5% **AND** avg latency < 500ms |

### 4. Actions by Severity

| Severity | On-Call Page | Raise Alert | Send Email |
|----------|:-----------:|:-----------:|:----------:|
| SEV-1 CRITICAL | ✅ | ✅ | ✅ (included in summary) |
| SEV-2 HIGH | — | ✅ | ✅ (included in summary) |
| SEV-3 MEDIUM | — | ✅ | ✅ (included in summary) |
| SEV-4 LOW | — | ✅ | ✅ (included in summary) |

### 5. On-Call Engineer (`on_call.py`)
Simulates PagerDuty/OpsGenie. On a SEV-1 trigger it:
- Randomly selects an engineer from the mock roster (Alice Chen, Bob Martinez, Priya Sharma)
- Generates a timestamped incident ID (`INC-YYYYMMDD-NNNN`)
- Prints a full incident banner to the terminal
- Persists the escalation to the `alerts` table with `alert_type = 'ONCALL'`
- Returns structured incident data (incident ID, engineer name, contact, ack URL)

### 6. Dashboard API (`mock_app/dashboard.py`)
FastAPI router mounted at `/dashboard/*`. The React frontend polls these endpoints every 10 seconds:

| Endpoint | Returns |
|----------|---------|
| `GET /dashboard/summary` | Header stats: error rate, avg latency, on-call pages, latest severity |
| `GET /dashboard/logs` | Recent log rows (last 60 min, up to 100) |
| `GET /dashboard/metrics` | Agent analysis history (last 20 runs) |
| `GET /dashboard/alerts` | All raised alerts, emails, and on-call pages (last 50) |
| `GET /dashboard/agent-runs` | Full agent activity audit trail (last 100 events) |

---

## Database Schema

Four tables are created automatically on first run in the `observability` PostgreSQL database.

**`logs`** — every request from the FastAPI service
```
id | timestamp | endpoint | method | status_code | response_time_ms
   | log_level | message | request_id | error_detail
```

**`metrics`** — each agent analysis run result
```
id | timestamp | window_minutes | total_requests | error_count | success_count
   | avg_response_time_ms | error_rate | severity_assessment | analysis_summary
```

**`alerts`** — all raised alerts, emails, and on-call pages
```
id | timestamp | severity | service | message | alert_type (RAISE_ALERT | EMAIL | ONCALL) | status
```

**`agent_runs`** — full audit trail of every agent action
```
id | timestamp | run_id | event_type (RUN_START | TOOL_CALL | TOOL_RESULT | FINAL_REPORT)
   | tool_name | tool_input | tool_result | severity | message
```

---

## Prerequisites

- Python 3.10+
- Node.js 18+ (for the React dashboard)
- PostgreSQL running locally
- A Gemini API key — get one free at [aistudio.google.com](https://aistudio.google.com/app/apikey)

---

## Setup & Running

### Step 1 — Create the PostgreSQL database

```bash
psql -U postgres -c "CREATE DATABASE observability;"
```

### Step 2 — Install Python dependencies

```bash
cd app_agent
pip install -r requirements.txt
```

### Step 3 — Configure your Gemini API key

Open [agent.py](agent.py) and set your key on line 40:

```python
GEMINI_API_KEY = "your_key_here"
```

Or export it as an environment variable (takes precedence over the file):

```bash
export GEMINI_API_KEY="your_key_here"
```

### Step 4 — Start the mock FastAPI service

```bash
uvicorn mock_app.main:app --reload --port 8000
```

On startup this will:
- Create all four DB tables if they don't exist
- Start the background traffic simulator automatically

### Step 5 — Run the agent

In a new terminal:

```bash
# Analyse the last 30 minutes (default)
python agent.py

# Analyse a custom window
python agent.py 60
```

### Step 6 — Start the React dashboard (optional)

```bash
cd dashboard
npm install
npm run dev
```

Open `http://localhost:5173` in your browser. The dashboard auto-refreshes every 10 seconds.

---

## Testing

### Option A — Seed historical mock data (no agent run needed)

Inserts pre-built historical records for all four severities directly into `metrics`, `alerts`, and `agent_runs`. Useful for populating the dashboard without running the agent live.

```bash
python seed_mock_data.py
```

This inserts:
- **4 metrics rows** — SEV-1 @ 200 min ago, SEV-2 @ 150 min, SEV-3 @ 100 min, SEV-4 @ 60 min
- **9 alert rows** — ONCALL + RAISE_ALERT + EMAIL for SEV-1; RAISE_ALERT + EMAIL for SEV-2/3/4
- **42 agent_run rows** — full audit trail for all four historical runs

### Option B — Run all four severity scenarios live

Seeds fresh log data for each severity level and runs the agent against it sequentially.

```bash
python run_all_severities.py
```

This will:
1. Truncate the `logs` table
2. Insert log rows calibrated for SEV-1 (40% error rate, ~2470ms avg latency)
3. Run the agent — expect one on-call page + alerts + email
4. Repeat for SEV-2, SEV-3, SEV-4 with a 5-second pause between each

### Option C — Trigger a critical scenario against the live service

With the FastAPI service running, inject 20 critical error logs instantly:

```bash
curl -X POST http://localhost:8000/simulate/critical
```

Then run the agent:

```bash
python agent.py
```

---

## Verifying On-Call Is Triggered

The on-call page fires **only for SEV-1** (error rate > 30% **or** avg latency > 2000ms on any single service).

### What to look for in the terminal

When the agent runs against a SEV-1 condition, you will see this banner printed mid-run:

```
======================================================================
  *** SEV-1 CRITICAL INCIDENT RAISED ***
======================================================================
  Incident ID  : INC-20260415-0001
  Service      : payment-service
  Time         : 2026-04-15T22:23:48
  Message      : CRITICAL: Payment service error rate is 35.0% and average
                 response time is 2800ms. Immediate action required.
  On-Call      : Priya Sharma (Infra On-Call)
  Contact      : +1-555-0103
  Ack URL      : https://oncall.internal/incidents/INC-20260415-0001/ack
  Status       : PAGED
======================================================================
```

### What to check in the database

```sql
-- Confirm the on-call escalation was persisted
SELECT id, timestamp, severity, service, message, alert_type
FROM   alerts
WHERE  alert_type = 'ONCALL'
ORDER  BY timestamp DESC
LIMIT  5;

-- Check the agent correctly classified the run as SEV-1
SELECT timestamp, severity_assessment, error_rate, avg_response_time_ms, analysis_summary
FROM   metrics
ORDER  BY timestamp DESC
LIMIT  5;

-- Review the agent's full tool-call audit trail for the run
SELECT event_type, tool_name, severity, message
FROM   agent_runs
WHERE  run_id = '<run-id-from-terminal>'
ORDER  BY timestamp;
```

### What to check in the dashboard

- **Header cards** — "On-Call Pages Total" counter increments
- **Alerts tab** — a row with `alert_type = ONCALL` appears at the top
- **Agent Activity tab** — shows `TOOL_CALL call_on_call_engineer` in the audit trail for that run

### Confirm no false pages (non-SEV-1 runs)

```sql
-- Should return 0 rows for any run that wasn't SEV-1
SELECT a.alert_type, a.severity, a.service, a.timestamp
FROM   alerts a
WHERE  a.alert_type = 'ONCALL'
  AND  a.severity  != 1;
```

---

## Example Agent Output (Full SEV-1 Run)

```
======================================================================
  OBSERVABILITY AGENT  –  Analysing last 30 minutes
  Model: gemini-2.5-flash  |  Run ID: 7a09af74
======================================================================

[Agent] Iteration 1
[Tool Call] get_logs({"timeframe_minutes": 30})
[Tool Result] {
  "overall": {"total_requests": 26, "error_count": 8, "error_rate_pct": 30.8, "avg_response_time_ms": 1850.0},
  "services": [
    {"service": "payment-service", "error_rate_pct": 40.0, "avg_response_time_ms": 2470.0},
    {"service": "order-service",   "error_rate_pct": 22.2, "avg_response_time_ms": 1378.0},
    {"service": "user-service",    "error_rate_pct": 11.1, "avg_response_time_ms": 596.0},
    {"service": "product-service", "error_rate_pct":  0.0, "avg_response_time_ms": 200.0}
  ]
}

[Agent] Iteration 2
[Tool Call] call_on_call_engineer({"service": "payment-service", "message": "..."})

======================================================================
  *** SEV-1 CRITICAL INCIDENT RAISED ***
======================================================================
  Incident ID  : INC-20260415-0001
  Service      : payment-service
  On-Call      : Priya Sharma (Infra On-Call)
  Contact      : +1-555-0103
  Status       : PAGED
======================================================================

[Tool Call] raise_alert({"service": "payment-service", "severity": 1, ...})
[Tool Call] raise_alert({"service": "order-service",   "severity": 2, ...})
[Tool Call] raise_alert({"service": "user-service",    "severity": 3, ...})
[Tool Call] raise_alert({"service": "product-service", "severity": 4, ...})

[Agent] Iteration 3
[Tool Call] send_email_notification({...})   # one summary email for all services
[Tool Call] save_metrics({..., "severity_assessment": 1})

[Agent] Iteration 4
[Agent Response]
  **Incident Report — payment-service (SEV-1 CRITICAL):** 40% error rate, 2470ms avg latency. On-call paged.
  **order-service (SEV-2 HIGH):** 22% error rate, 1378ms avg latency.
  **user-service (SEV-3 MEDIUM):** 11% error rate, 596ms avg latency.
  **product-service (SEV-4 LOW):** Healthy — 0% errors, 200ms latency.

======================================================================
  AGENT RUN COMPLETE
======================================================================
```

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` | Mock web service |
| `uvicorn` | ASGI server for FastAPI |
| `psycopg2-binary` | PostgreSQL driver |
| `google-genai` | Gemini 2.5 Flash SDK |
| `httpx` | HTTP client |
| `react` + `vite` | Dashboard frontend |