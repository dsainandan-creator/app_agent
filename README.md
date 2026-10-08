# Observability Agent

An AI agent that monitors a live web service, pulls logs from **PostgreSQL and Dynatrace**, correlates them per service, and takes incident-response actions: Slack pages for critical failures, DB alerts, and email notifications.

An optional **Laya triage layer** adds a fast first-pass severity classifier and a **deterministic paging policy** in code, so whether a Slack page goes out no longer rests on Gemini alone.

---

## How It Works

```
FastAPI app generates traffic
    ├── logs every request → PostgreSQL
    └── forwards every request → Dynatrace (background thread)

python agent.py
    ├── spawns OBS-MCP   (mcp_server.py)           ← PostgreSQL tools
    ├── spawns DT-MCP    (dynatrace_mcp_server.py) ← Dynatrace tools
    ├── spawns LAYA-MCP  (laya_mcp_server.py)      ← Laya triage (only when LAYA_ENABLED=true)
    └── Gemini 2.5 Flash reasons over the data

With Laya enabled, code (not Gemini) runs first:
  0. get_logs + dt_search_logs → evidence packs → classify_services (Laya)

Agent protocol:
  1. get_logs          → fetch PostgreSQL data
  2. dt_search_logs    → fetch Dynatrace data (same window)
  3. CORRELATION SUMMARY table (mandatory before any action)
  4. raise_alert       → one per service, every severity
     call_on_call_engineer → Slack page (SEV-1), checked by the paging policy
  5. send_email_notification → once per run
  6. save_metrics      → persist to DB
  7. final incident report (+ paging-policy decisions in advisory/enforce mode)
```

---

## Architecture

```
agent.py (Gemini 2.5 Flash · MCP client)
    │
    ├──stdio──▶ mcp_server.py (OBS-MCP)
    │               get_logs, raise_alert, send_email_notification,
    │               save_metrics, call_on_call_engineer ──▶ policy.py ──▶ Slack
    │               └── PostgreSQL (logs · metrics · alerts · agent_runs · laya_triage)
    │
    ├──stdio──▶ dynatrace_mcp_server.py (DT-MCP)
    │               dt_search_logs  → Grail DQL  (apps.dynatrace.com, summarised per service)
    │               dt_ingest_log   → Ingest API (live.dynatrace.com, dry run by default)
    │
    └──stdio──▶ laya_mcp_server.py (LAYA-MCP, optional)
                    classify_services → laya_triage/client.py
                        ├── in-process laya.Router (default), or
                        └── laya-serve over HTTP (LAYA_BASE_URL, e.g. :8100)
```

Diagram: [`docs/architecture-laya.excalidraw`](docs/architecture-laya.excalidraw) (PNG: [`docs/architecture-laya.png`](docs/architecture-laya.png)).

The agent has no tool logic: it routes, retries and audit-logs. Both read tools return the same per-service schema tagged `source="postgresql"` or `source="dynatrace"`, so Gemini compares them directly. `classify_services` is called by code only; Gemini is never given it.

---

## Correlation Summary

Before taking any action, the agent outputs this table (advisory/enforce mode adds the two Laya columns):

```
| Service         | PG err% | PG avg_ms | DT err% | DT avg_ms | Δ err% | Resolution | Laya SEV (p) | Agent SEV |
|-----------------|---------|-----------|---------|-----------|--------|------------|--------------|-----------|
| payment-service | 57.1    | 1877.9    | 14.3    | 416.8     | -42.8  | DT-lag     | sev2 (0.26)  | SEV-1     |
```

| Resolution | Meaning |
|-----------|---------|
| `agreement` | < 5 pp difference — high confidence |
| `DT-lag` | DT lower — ingestion lag, trust PostgreSQL |
| `DT-higher` | DT higher — earlier burst captured, use DT value |
| `DT-unavailable` | DT query failed — PostgreSQL only |

---

## Severity Levels

Gemini assigns severity by contextual reasoning, not fixed thresholds.

| Level | Label | Actions |
|-------|-------|---------|
| SEV-1 | CRITICAL | Slack page (subject to the paging policy) + DB alert + email |
| SEV-2 | HIGH | DB alert + email |
| SEV-3 | MEDIUM | DB alert + email |
| SEV-4 | LOW | DB alert + email |

`raise_alert` rejects any severity outside 1–4 with an error Gemini sees.

---

## Laya triage layer

[Laya](https://huggingface.co/convaiinnovations/laya) (Apache-2.0, pip `laya`) is a fast, non-autoregressive decision model: give it a state and typed questions, and one forward pass returns probabilities for each answer.

**What it does here**

- Code builds one **evidence pack** per service (`laya_triage/evidence.py`). Laya sees **text bands, not numbers** (e.g. "severe (30% or more of requests fail with 5xx)"), plus criticality, the PostgreSQL-vs-Dynatrace resolution and up to 5 recent `error_detail` texts. Raw numbers stay in the pack for Gemini and the hard floor. Thresholds and wording live in `laya_triage/triage_config.json`.
- Laya answers four questions in one batched call: `severity` (sev1–sev4), `user_impact`, `failure_mode`, `page_now`. Code derives `expected_sev = Σ i·p(sev_i)` and the top-2 margin.
- Results are stored in the `laya_triage` table and, in advisory/enforce mode, given to Gemini as a **prior** to confirm or override with an evidence-based reason.
- `policy.py` decides, in code, whether a Slack page actually goes out.

**What it does not do**

- It does not replace Gemini, which still assesses every service, raises alerts, sends the email and writes the report.
- It is not accurate out of the box: zero-shot, it never predicts SEV-1 on our test data (see `docs/laya_eval_zero_shot.md`). It needs fine-tuning and calibration before it should influence paging.
- It never sees raw numbers, and it never sends anything itself.

### Modes and rollout

| `LAYA_MODE` | Gemini sees Laya? | Paging policy |
|-------------|-------------------|---------------|
| `shadow` | No — prompt and flow unchanged | Decisions recorded (`POLICY_SHADOW` alerts); pages go out as before |
| `advisory` | Yes — prior in the first message; report gets `Laya SEV (p)` / `Agent SEV` columns | Recorded and shown in the report; pages go out as before |
| `enforce` | Yes | **Applied**: held pages are not sent, forced pages are sent |

`enforce` needs `LAYA_CALIBRATION_FILE` to exist; without it the agent logs a warning and runs as `advisory`.

**Recommended rollout:** `shadow` → `advisory` → `enforce`, and `enforce` only after fine-tuning, calibration and an evaluation that meets agreed thresholds (SEV-1 recall, false-page rate). Use the shadow report to compare Laya with Gemini on real runs in between.

### Paging policy (`policy.py`)

Checked in this order:

| # | Condition | Gemini called page | Gemini did not page |
|---|-----------|--------------------|---------------------|
| 1 | **Hard floor**: service in `CRITICAL_SERVICES`, PostgreSQL 5xx ≥ `HARD_FLOOR_ERR_PCT`, Dynatrace `agreement` / `DT-higher` / unavailable | PAGE | PAGE_FORCED |
| 2 | **Minimum volume**: fewer than `PAGE_MIN_REQUESTS` PostgreSQL requests | PAGE_HELD | NO_PAGE |
| 3 | Laya unavailable | PAGE (fail open, labelled "Laya unavailable") | NO_PAGE |
| 4 | Laya P(sev1) ≥ `LAYA_PAGE_HIGH` | PAGE | PAGE_FORCED |
| 5 | `LAYA_PAGE_LOW` ≤ P(sev1) < `LAYA_PAGE_HIGH` | PAGE ("model disagreement") | NO_PAGE |
| 6 | P(sev1) < `LAYA_PAGE_LOW` | PAGE_HELD | NO_PAGE |

- **PAGE_HELD** (enforce): no Slack page; a SEV-2 `PAGE_HELD` alert "held page, needs human review", listed in that run's email.
- **PAGE_FORCED** (enforce): after the Gemini loop, `agent.py` pages the service; the message says Gemini did not page.
- Every decision is stored as an `alerts` row (`POLICY_SHADOW` in shadow/advisory, `POLICY_ENFORCED` in enforce) with the run's `run_id`.
- Slack pages carry Laya's P(sev1), Gemini's severity and the policy reason. Incident ids are collision-safe (`INC-20261008-001712-745-b5f419cf`).

---

## Safety switches

| Switch | Default | Effect |
|--------|---------|--------|
| `SLACK_DRY_RUN` | **true** | Pages are logged (stderr and `logs/slack_dry_run.jsonl`) and never posted. Set `SLACK_DRY_RUN=false` explicitly to really page. The agent prints `Slack: DRY_RUN` or `LIVE` at startup. |
| `DT_INGEST_TOOL_DRY_RUN` | **true** | The `dt_ingest_log` tool only logs its payload. The mock app's automatic forwarding to Dynatrace is not affected. |
| `LAYA_ENABLED` | **false** | Without it the agent behaves as before Laya. |

The MCP servers inherit the agent's environment, so a value set for one run (`SLACK_DRY_RUN=true python agent.py`) applies to every server. Gemini calls have a hard deadline (`GEMINI_CALL_TIMEOUT_S`, default 180 s) and are retried twice if they stall, drop or come back empty.

---

## Project Structure

```
app_agent/
├── agent.py                  # MCP client · Gemini · Laya pre-triage · RAG assistant
├── mcp_server.py             # Observability MCP (PostgreSQL tools)
├── dynatrace_mcp_server.py   # Dynatrace MCP (DQL + ingest tools)
├── laya_mcp_server.py        # Laya MCP (classify_services)
├── dynatrace_client.py       # Dynatrace HTTP client (DQL summarise + ingest)
├── tools.py                  # Tool implementations for OBS-MCP (+ paging policy plumbing)
├── policy.py                 # Deterministic paging policy
├── laya_config.py            # LAYA_* settings
├── slack_notify.py           # Slack Incoming Webhook (dry run by default)
├── laya_triage/
│   ├── evidence.py           # Evidence packs: bands, resolution, token budget
│   ├── client.py             # In-process Router or laya-serve; calibration; never raises
│   ├── prompting.py          # Advisory prompt addendum and first message
│   ├── triage_config.json    # Band thresholds, Laya wording, questions
│   └── training/             # Scenario generator, rubric, calibrate, evaluate, shadow report
├── scripts/laya_smoke.py     # Laya smoke test
├── tests/                    # pytest suite
├── docs/                     # Architecture diagrams, laya-serve guide, eval reports
├── embed_docs.py             # Chunk + embed runbooks into ChromaDB
├── *.txt                     # Runbooks for the RAG assistant
├── requirements.txt / requirements-dev.txt / .env.example
└── mock_app/                 # FastAPI service, DB schema, dashboard API, traffic simulator
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
pip install -r requirements.txt           # runtime (includes laya[serve], torch, transformers)
pip install -r requirements-dev.txt       # + pytest
```

Laya pins: `laya[serve]==0.3.22`, `torch==2.14.1`, `transformers==5.18.0` (Python 3.10+; this repo uses 3.12). Do **not** install `laya[mcp]`: it needs `mcp>=2.2`, which conflicts with this repo's mcp 1.x servers.

**Model download:** the `typed-decisions` checkpoint is **842.6 MB** (804 MiB), downloaded to `~/.cache/huggingface` on first use. Loading it takes about 2–20 s; on an Apple-silicon Mac it runs on MPS.

If `laya.load()` hangs, set **`USE_TF=0`** (Laya's README: a TensorFlow install can deadlock model construction). The project's Laya entry points set it by default.

### 3. Environment variables

Copy [`.env.example`](.env.example) to `.env` and fill in the keys.

| Variable | Default | Purpose |
|----------|---------|---------|
| `GEMINI_API_KEY` | — | Gemini API key |
| `SLACK_WEBHOOK_URL` | — | Slack Incoming Webhook |
| `SLACK_DRY_RUN` | `true` | Only `false` really posts to Slack |
| `GEMINI_CALL_TIMEOUT_S` | `180` | Hard deadline per Gemini call |
| `DYNATRACE_API_KEY` | — | Platform token (`dt0s16.*`), `logs.read` |
| `DYNATRACE_INGEST_KEY` | — | Classic token (`dt0c01.*`), `logs.ingest`; falls back to the API key |
| `DYNATRACE_ENV_URL` | — | `https://<env-id>.apps.dynatrace.com` |
| `DT_INGEST_TOOL_DRY_RUN` | `true` | Only `false` lets the `dt_ingest_log` tool write |
| `LAYA_ENABLED` | `false` | Turn the Laya layer on |
| `LAYA_MODE` | `shadow` | `shadow` \| `advisory` \| `enforce` |
| `LAYA_MODEL` | `typed-decisions` | `english` \| `multilingual` \| `typed-decisions` |
| `LAYA_MODEL_PATH` | — | Local checkpoint directory (e.g. fine-tuned); in-process only |
| `LAYA_BASE_URL` | — | Empty = in-process; else a laya-serve URL, e.g. `http://localhost:8100` |
| `LAYA_API_KEY` | — | Bearer token if laya-serve uses one |
| `LAYA_TIMEOUT_S` | `5` | Laya inference timeout (model load has its own 180 s bound) |
| `LAYA_PAGE_HIGH` | `0.80` | P(sev1) at or above which a page is forced / agreed |
| `LAYA_PAGE_LOW` | `0.40` | P(sev1) below which a Gemini page is held |
| `LAYA_CALIBRATION_FILE` | `laya_triage/calibration.json` | Fitted temperatures; its presence unlocks `enforce` |
| `HARD_FLOOR_ERR_PCT` | `50` | Hard-floor 5xx % for critical services |
| `PAGE_MIN_REQUESTS` | `20` | No page below this many requests (unless hard floor) |
| `CRITICAL_SERVICES` | `payment-service` | Comma-separated |

**Dynatrace tokens** (Dynatrace → Access Tokens → Generate new token): `DYNATRACE_API_KEY` is a platform token with `logs.read`; `DYNATRACE_INGEST_KEY` is a classic token with `logs.ingest`. Dynatrace is optional; the agent works on PostgreSQL alone and notes the gap.

### 4. Start the FastAPI service

```bash
uvicorn mock_app.main:app --reload --port 8000
```

Creates the DB tables (including `laya_triage`), starts the traffic simulator and forwards logs to Dynatrace. Leave it running.

### 5. (Optional) run Laya as a service on port 8100

`laya-serve` has no CLI flags; it reads environment variables. Its default port, 8000, collides with the mock app:

```bash
USE_TF=0 LAYA_PORT=8100 LAYA_HOST=127.0.0.1 LAYA_MODELS=typed-decisions LAYA_MAX_LOADED=1 laya-serve
curl -s http://127.0.0.1:8100/health
```

Then set `LAYA_BASE_URL=http://localhost:8100`. Details: [`docs/laya-serve.md`](docs/laya-serve.md).

---

## Commands

### Run the agent

```bash
python agent.py                                   # Laya off (as before), last 30 minutes
python agent.py 60                                # custom window (minutes)
LAYA_ENABLED=true LAYA_MODE=shadow   python agent.py 5
LAYA_ENABLED=true LAYA_MODE=advisory python agent.py 5
LAYA_ENABLED=true LAYA_MODE=enforce  python agent.py 5    # advisory until calibrated
```

Force a SEV-1 to test paging (pages stay dry runs unless `SLACK_DRY_RUN=false`):

```bash
curl -X POST http://localhost:8000/simulate/critical
LAYA_ENABLED=true LAYA_MODE=advisory python agent.py 5
```

### Laya

```bash
python scripts/laya_smoke.py                                  # load + README example + latency
python -m laya_triage.training.generate_scenarios             # synthetic train/val/test JSONL (no DB writes)
python -m laya_triage.training.calibrate                      # fit temperatures → training/out/calibration.json
python -m laya_triage.training.evaluate --out-md docs/laya_eval.md   # accuracy, ECE, paging rates, thresholds
python -m laya_triage.training.shadow_report --days 7         # Laya vs Gemini on real runs (read-only)
```

`calibrate` writes to `laya_triage/training/out/` on purpose. Copy the file to `laya_triage/calibration.json` only when you are ready for `enforce`. Fine-tuning runs on Kaggle: [`laya_triage/training/FINETUNE.md`](laya_triage/training/FINETUNE.md). Labelling rules: [`RUBRIC.md`](laya_triage/training/RUBRIC.md).

### Tests

```bash
pytest -m "not slow"       # everything except the real-checkpoint test (no network, Slack and Gemini mocked)
pytest -m slow             # loads the real Laya checkpoint
```

Database tests use a separate `observability_test` database and skip if it does not exist.

### RAG assistant

```bash
python agent.py --rag        # ask questions about the runbooks; type exit to quit
python embed_docs.py         # rebuild the knowledge base after editing the runbooks
```

### React dashboard (optional)

```bash
cd dashboard && npm run dev      # http://localhost:5173, refreshes every 10 s
```

---

## Database Tables

| Table | What it stores |
|-------|---------------|
| `logs` | Every FastAPI request (endpoint, status, latency, error detail) |
| `metrics` | Each agent run result (error rate, severity, summary) |
| `alerts` | Alerts, emails, on-call pages (`status` SENT / DRY_RUN), held pages, paging-policy decisions; `run_id` links them to a run |
| `agent_runs` | Full tool-call audit trail, including the code-run Laya pre-triage calls |
| `laya_triage` | One row per service per run: evidence, Laya's answers, expected severity, confidence, latency |

Tables and new columns are created automatically (additively) on first `uvicorn` start or first use.

---

## MCP Tools

| Server | Tool | Purpose |
|--------|------|---------|
| OBS-MCP | `get_logs` | Per-service stats from PostgreSQL (+ 4xx rate and recent error samples) |
| OBS-MCP | `raise_alert` | Persist severity assessment (1–4 only) |
| OBS-MCP | `call_on_call_engineer` | SEV-1 Slack page, through the paging policy |
| OBS-MCP | `send_email_notification` | Summary email (mock, logged to DB; lists held pages) |
| OBS-MCP | `save_metrics` | Persist analysis metrics |
| DT-MCP | `dt_search_logs` | Dynatrace per-service stats (DQL `summarize`, raw-record fallback) |
| DT-MCP | `dt_ingest_log` | Write a log entry to Dynatrace (dry run by default) |
| LAYA-MCP | `classify_services` | Laya triage for all services in one call (code only) |

---

## Limitations

- **Zero-shot Laya is not usable for paging.** On the synthetic test split ([`docs/laya_eval_zero_shot.md`](docs/laya_eval_zero_shot.md)) the shipped `typed-decisions` checkpoint gets severity right 49% of the time (chance 25%) and **never predicts SEV-1**: every SEV-1 service comes out as SEV-2, so at the default thresholds it misses 100% of SEV-1s and `enforce` would hold every Gemini page that the hard floor does not cover. Fine-tune first.
- **Synthetic labels.** Training, calibration and evaluation data come from `generate_scenarios.py` and encode the rules in `RUBRIC.md`, not on-call engineers' judgement. Use the shadow report on real runs before trusting anything.
- **Numbers are banded in code.** Laya only sees the bands in `triage_config.json`; changes to the bands change what it can distinguish, and invalidate a fine-tuned checkpoint and its calibration.
- **Token budget.** `typed-decisions` reads 1,024 tokens per question row and reserves 272 for the question, leaving 752 for the state (`english`: 240). States are about 150 tokens; error samples are dropped first if a state would not fit.
- **Calibration direction.** Laya's README says the checkpoints ship over-confident; on this task the zero-shot model is the opposite (near-flat probabilities), so fitted temperatures sharpen (`choice:3-5` 1.76 → 0.67) and two buckets hit Laya's 0.5 floor.
- **Calibration granularity.** Laya fits one temperature per (question type, option-count bucket), not per question: `severity` and `user_impact` share `choice:3-5`. A bucket needs ≥ 2,000 records for its own temperature.
- **laya-serve** applies the checkpoint's own temperatures and cannot load a local checkpoint or a calibration file; the client re-tempers its probabilities, and fine-tuned weights run in-process only.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `google-genai` | Gemini 2.5 Flash |
| `mcp` | Model Context Protocol (1.x) |
| `laya[serve]`, `torch`, `transformers` | Laya triage model and laya-serve |
| `fastapi` + `uvicorn` | Mock web service |
| `psycopg2-binary` | PostgreSQL |
| `httpx` | Dynatrace, Slack and laya-serve HTTP calls |
| `chromadb` | Local vector store (RAG) |
| `python-dotenv` | `.env` support |
| `pytest` (dev) | Tests |
| `react` + `vite` | Dashboard |
