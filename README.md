# Observability Agent

An AI-powered observability system that monitors a live web service, analyses logs per service using contextual reasoning, and takes autonomous incident response actions — paging on-call engineers via Slack for critical failures and logging all events to PostgreSQL.

---

## High-Level Architecture Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         USER / OPERATOR                             │
│                              │    ▲                                 │
│                  python agent.py  │  React Dashboard                │
│                              │    │  localhost:5173                 │
└──────────────────────────────┼────┼─────────────────────────────────┘
                               │    │
           ┌───────────────────▼────┴────────────────────┐
           │              agent.py                        │
           │          (MCP Client · Gemini 2.5 Flash)     │
           │                                              │
           │  1. Spawns mcp_server.py via stdio           │
           │  2. Discovers tools dynamically              │
           │  3. Sends prompt + tools to Gemini           │
           │  4. Gemini reasons → returns tool call       │
           │  5. Forwards call to MCP server              │
           │  6. Returns result to Gemini                 │
           │  7. Repeats until final report               │
           └──────────────────┬───────────────────────────┘
                    stdio (MCP Protocol)
           ┌──────────────────▼───────────────────────────┐
           │            mcp_server.py                     │
           │            (MCP Server)                      │
           │                                              │
           │  Tools:                                      │
           │  ┌─────────────────────────────────────┐    │
           │  │ get_logs          → reads PostgreSQL │    │
           │  │ raise_alert       → writes alerts    │    │
           │  │ save_metrics      → writes metrics   │    │
           │  │ send_email        → writes alerts    │    │
           │  │ call_on_call  ────────────────────────────┼──▶ Slack
           │  └─────────────────────────────────────┘    │   (SEV-1 only)
           └──────────────────┬───────────────────────────┘
                              │ reads / writes
           ┌──────────────────▼───────────────────────────┐
           │               PostgreSQL                     │
           │           "observability" DB                 │
           │                                              │
           │  logs         ← FastAPI service traffic      │
           │  metrics      ← agent analysis results       │
           │  alerts       ← every severity assessment    │
           │  agent_runs   ← full MCP tool-call audit     │
           └──────────────────┬───────────────────────────┘
                              │ /dashboard/* REST API
           ┌──────────────────▼───────────────────────────┐
           │  mock_app/main.py  +  mock_app/dashboard.py  │
           │  FastAPI · localhost:8000                     │
           │                                              │
           │  Generates live traffic → logs table         │
           │  Serves dashboard data → React frontend      │
           └──────────────────────────────────────────────┘
```

### End-to-End Flow

```
FastAPI service generates traffic
        │
        ▼
HTTP middleware logs every request → PostgreSQL (logs table)
        │
        ▼
Operator runs: python agent.py
        │
        ├─ agent spawns mcp_server.py (stdio)
        ├─ agent discovers tools via MCP list_tools()
        ├─ agent sends prompt to Gemini 2.5 Flash
        │
        ▼
Gemini reasons over log data
        │
        ├─ calls get_logs via MCP ──────────────▶ PostgreSQL (reads logs)
        │
        ├─ assesses each service (contextual, not threshold-based)
        │
        ├─ SEV-1 service? ──── call_on_call via MCP ──▶ Slack alert sent
        │
        ├─ every service ────── raise_alert via MCP ──▶ PostgreSQL (alerts)
        │
        ├─ once ─────────────── send_email via MCP ───▶ PostgreSQL (alerts)
        │
        ├─ once ─────────────── save_metrics via MCP ─▶ PostgreSQL (metrics)
        │
        └─ writes final incident report to terminal
                │
                ▼
        React Dashboard polls /dashboard/* every 10s
        and displays metrics, alerts, and agent activity
```

---

## What Is MCP and Why Is It Used Here?

**MCP (Model Context Protocol)** is an open standard for connecting AI models to external tools and data sources. Instead of hardcoding tool functions inside the agent, MCP separates them into a standalone server process that the agent connects to at runtime.

**Before MCP (old approach):**
```
agent.py  →  import tools.py  →  call function directly
```
The agent knew everything about the tools: their Python code, their schemas. Tightly coupled.

**With MCP (current approach):**
```
agent.py (MCP client)  ──stdio──▶  mcp_server.py (MCP server)  →  tools.py
```
The agent connects to the MCP server, asks "what tools do you have?", gets back schemas, and routes all calls through the protocol. The agent has no tool logic — it just sends requests and receives results.

**Why this matters:**
- Tools are decoupled from the agent — you can update, add, or swap tools without touching the agent
- The MCP server can be shared with any MCP-compatible client (Claude Desktop, other agents)
- Tool schemas are the single source of truth — defined once in the server, discovered dynamically
- Clean separation: agent owns reasoning, MCP server owns execution

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Mock FastAPI Service                          │
│                         mock_app/main.py                             │
│                                                                      │
│  /api/payments  /api/orders  /api/users  /api/products  /health      │
│       └──────────────────────┬───────────────────────────┘          │
│              HTTP middleware auto-logs every request                 │
│                              │                                       │
│                     PostgreSQL: logs table                           │
│              Background: traffic_simulator.py                        │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               │
┌──────────────────────────────▼───────────────────────────────────────┐
│                     agent.py  (MCP Client)                           │
│                   Gemini 2.5 Flash · ReAct Loop                      │
│                                                                      │
│  1. Connect to MCP server via stdio                                  │
│  2. list_tools()  →  discover schemas dynamically                    │
│  3. Convert MCP schemas → Gemini FunctionDeclarations                │
│  4. Send logs + prompt to Gemini                                     │
│  5. Gemini reasons → returns function call                           │
│  6. call_tool(name, args) via MCP → get result → send back to Gemini │
│  7. Repeat until Gemini produces final report                        │
└──────────────────────┬───────────────────────────────────────────────┘
           stdio (MCP) │
┌──────────────────────▼───────────────────────────────────────────────┐
│                   mcp_server.py  (MCP Server)                        │
│                  "observability-mcp"                                 │
│                                                                      │
│  Tools exposed:                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  get_logs               fetch aggregated stats from Postgres │   │
│  │  raise_alert            persist severity assessment to DB    │   │
│  │  send_email_notification  mock email (logged to DB)          │   │
│  │  save_metrics           persist analysis metrics to DB       │   │
│  │  call_on_call_engineer  SEV-1 → Slack Incoming Webhook       │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                    │                         │                       │
│               tools.py               slack_notify.py                │
└──────────────────────────────────────────────────────────────────────┘
                       │                         │
            ┌──────────▼──────────┐    ┌─────────▼────────┐
            │     PostgreSQL      │    │   Slack Channel   │
            │ logs · metrics ·    │    │  (SEV-1 only)     │
            │ alerts · agent_runs │    └──────────────────┘
            └──────────┬──────────┘
                       │  REST API (/dashboard/*)
            ┌──────────▼──────────┐
            │   React Dashboard   │
            │  dashboard/src/     │
            │  localhost:5173     │
            └─────────────────────┘
```

---

## How the MCP Flow Works (Step by Step)

```
agent.py                        mcp_server.py                   tools.py / Slack
   │                                 │                               │
   │── stdio_client.connect() ──────▶│                               │
   │◀─ session.initialize() ────────│                               │
   │                                 │                               │
   │── session.list_tools() ────────▶│                               │
   │◀─ [get_logs, raise_alert, ...]──│                               │
   │                                 │                               │
   │  (convert schemas → Gemini)     │                               │
   │  (send prompt to Gemini)        │                               │
   │  (Gemini returns: call get_logs)│                               │
   │                                 │                               │
   │── session.call_tool(            │                               │
   │     "get_logs", {minutes: 30}) ▶│── _get_logs(30) ────────────▶│
   │                                 │◀─ aggregated JSON ────────────│
   │◀─ TextContent(json) ────────────│                               │
   │                                 │                               │
   │  (send result back to Gemini)   │                               │
   │  (Gemini reasons → SEV-1 found) │                               │
   │  (Gemini returns: call_on_call) │                               │
   │                                 │                               │
   │── session.call_tool(            │                               │
   │     "call_on_call_engineer",...▶│── notify_slack(service) ─────▶│
   │                                 │                               │──▶ Slack POST
   │◀─ incident JSON ────────────────│◀──────────────────────────────│
   │                                 │                               │
   │  ... (raise_alert × N,          │                               │
   │        send_email, save_metrics)│                               │
   │                                 │                               │
   │  (Gemini writes final report)   │                               │
   │── session closes ───────────────│                               │
```

---

## Severity Classification

The agent uses **contextual reasoning**, not fixed numeric thresholds. It weighs:

| Signal | What the agent considers |
|--------|--------------------------|
| **Service criticality** | Payment and auth services have higher stakes than product or health endpoints |
| **Error rate in context** | 5% errors on payments is more alarming than 20% on a health check |
| **Error volume** | 30 errors on 31 requests vs 30 errors on 3000 requests |
| **Latency** | Elevated latency without errors often signals resource exhaustion before failures appear |
| **Error type** | 5xx (server-side) are more severe than 4xx (client errors); 503s indicate unavailability |
| **Spread** | Errors in one service vs degradation across many services changes the blast radius |

**Severity levels (guide, not formula):**

| Level | Label | Meaning |
|-------|-------|---------|
| **SEV-1** | CRITICAL | Service effectively down. Widespread user impact. Immediate intervention required. |
| **SEV-2** | HIGH | Significant degradation. Meaningful portion of users affected. Prompt attention needed. |
| **SEV-3** | MEDIUM | Partial/intermittent degradation. Most users unaffected. Needs investigation. |
| **SEV-4** | LOW | Minor anomaly. System healthy overall. Monitor. |

**Actions by severity:**

| Severity | Slack (on-call page) | DB Alert | Email |
|----------|:-------------------:|:--------:|:-----:|
| SEV-1 CRITICAL | ✅ | ✅ | ✅ |
| SEV-2 HIGH | — | ✅ | ✅ |
| SEV-3 MEDIUM | — | ✅ | ✅ |
| SEV-4 LOW | — | ✅ | ✅ |

---

## Project Structure

```
app_agent/
├── agent.py              # MCP client · Gemini 2.5 Flash · async agent loop
├── mcp_server.py         # MCP server · exposes all tools over stdio
├── tools.py              # Tool implementations (imported by mcp_server.py)
├── slack_notify.py       # SEV-1 Slack Incoming Webhook sender
├── seed_mock_data.py     # Seed historical data (one run per severity level)
├── run_all_severities.py # Live test: cycles through all 4 severity scenarios
├── requirements.txt
├── .env                  # GEMINI_API_KEY + SLACK_WEBHOOK_URL (git-ignored)
└── mock_app/
    ├── main.py           # FastAPI service · endpoints · request logging middleware
    ├── database.py       # PostgreSQL schema + all DB helpers
    ├── dashboard.py      # REST API router for the React dashboard
    └── traffic_simulator.py  # Background async traffic generator
└── dashboard/
    └── src/
        ├── App.jsx       # React dashboard · auto-refreshes every 10s
        ├── main.jsx
        └── index.css
```

---

## Database Schema

Four tables created automatically on first run.

**`logs`** — every request from the FastAPI service
```
id | timestamp | endpoint | method | status_code | response_time_ms
   | log_level | message  | request_id | error_detail
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

**`agent_runs`** — full MCP tool-call audit trail
```
id | timestamp | run_id | event_type (RUN_START | TOOL_CALL | TOOL_RESULT | FINAL_REPORT)
   | tool_name | tool_input | tool_result | severity | message
```

---

## Prerequisites

- Python 3.10+
- Node.js 18+ (React dashboard)
- PostgreSQL running locally
- Gemini API key — [aistudio.google.com](https://aistudio.google.com/app/apikey)
- Slack Incoming Webhook URL — [api.slack.com/apps](https://api.slack.com/apps)

---

## Setup & Running

### Step 1 — Create the PostgreSQL database

```bash
psql -U postgres -c "CREATE DATABASE observability;"
```

### Step 2 — Install Python dependencies

```bash
pip install -r requirements.txt
```

### Step 3 — Configure environment variables

Edit [.env](.env):
```
GEMINI_API_KEY=your_gemini_key_here
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

### Step 4 — Start the mock FastAPI service

```bash
uvicorn mock_app.main:app --reload --port 8000
```

This creates DB tables, starts the traffic simulator in the background, and serves the dashboard API at `http://localhost:8000/dashboard/*`.

### Step 5 — Run the agent

```bash
# Analyse the last 30 minutes (default)
python agent.py

# Custom window
python agent.py 60
```

**What happens internally:**
1. `agent.py` launches `mcp_server.py` as a subprocess (stdio)
2. Calls `list_tools()` — receives all 5 tool schemas from the MCP server
3. Converts schemas to Gemini function declarations dynamically
4. Sends the prompt to Gemini with the discovered tools
5. For each tool call Gemini makes, forwards it to the MCP server via `call_tool()`
6. MCP server executes the tool and returns the result
7. Result is sent back to Gemini to continue reasoning

You do **not** need to start `mcp_server.py` manually — the agent starts and stops it automatically.

### Step 6 — Start the React dashboard (optional)

```bash
cd dashboard
npm run dev          # node_modules already installed
```

Open `http://localhost:5173`. The dashboard polls `http://localhost:8000/dashboard/*` every 10 seconds.

---

## Testing

### Option A — Seed historical data

Inserts pre-built records for all four severities without running the agent live:

```bash
python seed_mock_data.py
```

Inserts: 4 metrics rows · 9 alert rows · 42 agent_run rows.

### Option B — Live four-scenario test

```bash
python run_all_severities.py
```

Seeds log data for each severity level and runs the agent sequentially (SEV-1 → SEV-2 → SEV-3 → SEV-4).

### Option C — Force a SEV-1 via the live service

```bash
# Inject 20 critical errors
curl -X POST http://localhost:8000/simulate/critical

# Run the agent — should trigger Slack alert
python agent.py
```

---

## Verifying the Slack On-Call Alert

The Slack alert fires **only for SEV-1** services (agent's judgment, not a fixed threshold).

**Terminal — look for this banner mid-run:**
```
======================================================================
  *** SEV-1 CRITICAL — SLACK ALERT SENT ***
======================================================================
  Incident ID : INC-20260421-212328
  Service     : payment-service
  Time        : 2026-04-21T21:23:28
  Message     : CRITICAL: Payment service ...
  Channel     : Slack (Incoming Webhook)
  Status      : SENT
======================================================================
```

**Database — confirm the on-call page was persisted:**
```sql
-- See all on-call escalations
SELECT id, timestamp, severity, service, message, alert_type
FROM   alerts
WHERE  alert_type = 'ONCALL'
ORDER  BY timestamp DESC
LIMIT  5;

-- Confirm SEV-1 assessment was saved
SELECT timestamp, severity_assessment, error_rate, analysis_summary
FROM   metrics
ORDER  BY timestamp DESC
LIMIT  3;

-- Inspect the full MCP tool-call audit trail for a run
SELECT event_type, tool_name, severity, message
FROM   agent_runs
WHERE  run_id = '<run-id-shown-in-terminal>'
ORDER  BY timestamp;
```

**Dashboard — check:**
- Header card "On-Call Pages Total" incremented
- Alerts tab shows `alert_type = ONCALL` row at the top
- Agent Activity tab shows `TOOL_CALL call_on_call_engineer` in the run

---

## MCP Tool Summary

| Tool | When called | What it does |
|------|-------------|--------------|
| `get_logs` | Once at start | Fetches per-service error rate + latency from PostgreSQL |
| `raise_alert` | Once per service | Persists the agent's severity assessment + reasoning to DB |
| `call_on_call_engineer` | SEV-1 services only | Posts formatted Slack block message via Incoming Webhook |
| `send_email_notification` | Once, after all alerts | Sends summary email (mock, logged to DB) |
| `save_metrics` | Once at end | Persists overall analysis metrics and highest severity to DB |

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` | Mock web service |
| `uvicorn` | ASGI server |
| `psycopg2-binary` | PostgreSQL driver |
| `google-genai` | Gemini 2.5 Flash SDK |
| `mcp` | Model Context Protocol — tool server + client |
| `python-dotenv` | `.env` file support |
| `httpx` | Slack webhook HTTP client |
| `react` + `vite` | Dashboard frontend |