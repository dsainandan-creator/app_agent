# Observability Agent

An AI agent that monitors a live web service, pulls logs from **PostgreSQL and Dynatrace**, correlates them per service, and takes automated incident response actions — Slack pages for critical failures, DB alerts, and email notifications.

---

## How It Works

```
FastAPI app generates traffic
    ├── logs every request → PostgreSQL
    └── forwards every request → Dynatrace (background thread)

python agent.py
    ├── spawns OBS-MCP  (mcp_server.py)          ← PostgreSQL tools
    ├── spawns DT-MCP   (dynatrace_mcp_server.py) ← Dynatrace tools
    └── Gemini 2.5 Flash reasons over both sources

Agent 7-step protocol:
  1. get_logs          → fetch PostgreSQL data
  2. dt_search_logs    → fetch Dynatrace data (same window)
  3. CORRELATION SUMMARY table (mandatory before any action)
  4. raise_alert       → one per service, every severity
     call_on_call_engineer → Slack page (SEV-1 only)
  5. send_email_notification → once per run
  6. save_metrics      → persist to DB
  7. final incident report printed to terminal
```

---

## Architecture

```
agent.py (Gemini 2.5 Flash · Dual MCP Client)
    │
    ├──stdio──▶ mcp_server.py (OBS-MCP)
    │               get_logs, raise_alert, send_email_notification,
    │               save_metrics, call_on_call_engineer → Slack
    │               └── PostgreSQL (logs · metrics · alerts · agent_runs)
    │
    └──stdio──▶ dynatrace_mcp_server.py (DT-MCP)
                    dt_search_logs  → Grail DQL  (apps.dynatrace.com)
                    dt_ingest_log   → Ingest API (live.dynatrace.com)
                    └── Dynatrace Grail Log Storage
```

The agent has zero tool logic — it only routes, retries, and audit-logs. Both read tools (`get_logs`, `dt_search_logs`) return the same per-service schema (`error_rate_pct`, `avg_response_time_ms`, etc.) tagged with `source="postgresql"` or `source="dynatrace"`, so Gemini can compare them directly.

---

## Correlation Summary

Before taking any action, the agent outputs this table:

```
## CORRELATION SUMMARY
Window: 30 min | PostgreSQL: 619 reqs | Dynatrace: 612 reqs

| Service         | PG err% | PG avg_ms | DT err% | DT avg_ms | Δ err% | Resolution  |
|-----------------|---------|-----------|---------|-----------|--------|-------------|
| payment-service | 20.9    | 354.9     | 18.2    | 341.0     | 2.7    | agreement   |
| order-service   | 13.6    | 158.8     | 9.1     | 142.0     | 4.5    | DT-lag      |
```

| Resolution | Meaning |
|-----------|---------|
| `agreement` | < 5 pp difference — high confidence |
| `DT-lag` | DT lower — ingestion lag, trust PostgreSQL |
| `DT-higher` | DT higher — earlier burst captured, use DT value |
| `DT-unavailable` | DT query failed — PostgreSQL only |

---

## Severity Levels

The agent uses contextual reasoning, not fixed thresholds.

| Level | Label | Actions |
|-------|-------|---------|
| SEV-1 | CRITICAL | Slack page + DB alert + email |
| SEV-2 | HIGH | DB alert + email |
| SEV-3 | MEDIUM | DB alert + email |
| SEV-4 | LOW | DB alert + email |

Factors: service criticality (payments > health), error volume, latency, 5xx vs 4xx, cross-service spread, source agreement.

---

## Project Structure

```
app_agent/
├── agent.py                  # Dual MCP client · Gemini · RAG assistant
├── mcp_server.py             # Observability MCP (PostgreSQL tools)
├── dynatrace_mcp_server.py   # Dynatrace MCP (DQL + ingest tools)
├── dynatrace_client.py       # Dynatrace HTTP client (DQL + ingest)
├── tools.py                  # Tool implementations for OBS-MCP
├── slack_notify.py           # Slack Incoming Webhook (SEV-1)
├── embed_docs.py             # Chunk + embed runbooks into ChromaDB
├── dynatrace-alerts.txt      # Runbook: alert types, severity levels
├── agent-runbook.txt         # Runbook: agent protocol, escalation flow
├── mcp-integration.txt       # Runbook: MCP tools, Dynatrace token model
├── chroma_db/                # ChromaDB vector store (created by embed_docs.py)
├── seed_mock_data.py         # Seed DB with historical test data
├── run_all_severities.py     # Live test: cycles SEV-1 → SEV-4
├── requirements.txt
├── .env
└── mock_app/
    ├── main.py               # FastAPI service + traffic endpoints
    ├── database.py           # PostgreSQL schema + DB helpers + DT ingest
    ├── dashboard.py          # REST API for React dashboard
    └── traffic_simulator.py  # Background traffic generator
└── dashboard/src/            # React dashboard (polls every 10s)
```

---

## Setup

### 1. PostgreSQL

```bash
psql -U postgres -c "CREATE DATABASE observability;"
```

### 2. Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment variables

Create a `.env` file:

```env
GEMINI_API_KEY=your_gemini_key
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

# Dynatrace — OAuth platform token (dt0s16.*) — scope: logs.read
DYNATRACE_API_KEY=dt0s16.XXXX.XXXX

# Dynatrace — Classic API token (dt0c01.*) — scope: logs.ingest
# Optional: if not set, falls back to DYNATRACE_API_KEY
DYNATRACE_INGEST_KEY=dt0c01.XXXX.XXXX

DYNATRACE_ENV_URL=https://your-env-id.apps.dynatrace.com
```

**Dynatrace tokens** (both created at Dynatrace → Access Tokens → Generate new token):
- `DYNATRACE_API_KEY` — Platform token (`dt0s16.*`) with `logs.read` scope
- `DYNATRACE_INGEST_KEY` — Classic token (`dt0c01.*`) with `logs.ingest` scope. If omitted, the platform token is used as fallback.

Dynatrace is optional — the agent works on PostgreSQL alone and notes the gap in the report.

### 4. Start the FastAPI service

```bash
uvicorn mock_app.main:app --reload --port 8000
```

This creates DB tables, starts the traffic simulator, and begins forwarding logs to Dynatrace. Leave it running.

### 5. Run the observability agent

```bash
python agent.py        # analyse last 30 minutes
python agent.py 60     # custom window (minutes)
```

The agent spawns both MCP servers automatically — no manual setup needed.

### 6. RAG assistant (ask questions about this project)

```bash
python agent.py --rag
```

```
Ask a question: what happens during a SEV-1 incident?

[RAG] Retrieving top 3 chunks...
  [1] agent-runbook.txt  chunk=3  distance=0.51
  [2] dynatrace-alerts.txt  chunk=1  distance=0.54

[RAG] Generating answer...

Answer:
For a SEV-1, the agent triggers a Slack page + DB alert + email ...
```

Type your question at the prompt and press Enter. Type `exit` to quit.
The knowledge base covers: alert types, agent protocol, MCP tools, and the Dynatrace token model.

To rebuild the knowledge base after editing the runbook files:
```bash
python embed_docs.py
```

### 7. React dashboard (optional)

```bash
cd dashboard && npm run dev
```

Open `http://localhost:5173` — auto-refreshes every 10 seconds.

---

## Database Tables

| Table | What it stores |
|-------|---------------|
| `logs` | Every FastAPI request (endpoint, status, latency, error detail) |
| `metrics` | Each agent run result (error rate, severity, summary) |
| `alerts` | All raised alerts, emails, and Slack on-call pages |
| `agent_runs` | Full tool-call audit trail (input, output, timing per tool) |

Tables are created automatically on first `uvicorn` start.

---

## MCP Tools

| Server | Tool | Purpose |
|--------|------|---------|
| OBS-MCP | `get_logs` | Fetch per-service stats from PostgreSQL |
| OBS-MCP | `raise_alert` | Persist severity assessment to DB |
| OBS-MCP | `call_on_call_engineer` | SEV-1 Slack page |
| OBS-MCP | `send_email_notification` | Summary email (logged to DB) |
| OBS-MCP | `save_metrics` | Persist analysis metrics to DB |
| DT-MCP | `dt_search_logs` | Query Dynatrace Grail via DQL |
| DT-MCP | `dt_ingest_log` | Write a log entry to Dynatrace (testing) |

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `google-genai` | Gemini 2.5 Flash |
| `mcp` | Model Context Protocol |
| `fastapi` + `uvicorn` | Mock web service |
| `psycopg2-binary` | PostgreSQL |
| `httpx` | Dynatrace + Slack HTTP calls |
| `chromadb` | Local vector store (RAG) |
| `python-dotenv` | `.env` support |
| `react` + `vite` | Dashboard |
