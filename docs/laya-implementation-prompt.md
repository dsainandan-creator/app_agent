# Task: add a Laya severity-triage layer to app_agent

## Context: what exists today (verify by reading the code)

This repo (`app_agent`, branch `RAG`) is an observability agent:

- `agent.py` runs Gemini 2.5 Flash in a ReAct loop (max 25 iterations). It is a dual MCP client that spawns two stdio servers:
  - `mcp_server.py` (OBS-MCP → `tools.py`): `get_logs`, `raise_alert`, `send_email_notification` (mock), `save_metrics`, `call_on_call_engineer` (posts a SEV-1 page to Slack via `slack_notify.py` Incoming Webhook).
  - `dynatrace_mcp_server.py` (DT-MCP → `dynatrace_client.py`): `dt_search_logs` (Grail DQL), `dt_ingest_log`.
- SEV-1 to SEV-4 is decided **entirely by Gemini** from `SYSTEM_PROMPT`. No code enforces the paging policy: `call_on_call_engineer` never checks severity, and `raise_alert` accepts any integer.
- Postgres tables `logs`, `metrics`, `alerts` (types RAISE_ALERT / EMAIL / ONCALL) and `agent_runs` are created in `mock_app/database.py`. The DB config is hardcoded there (localhost, user postgres, no password).
- Mock FastAPI app on :8000 (`mock_app/`) with a traffic simulator; dashboard on :5173.
- RAG mode: `python agent.py --rag`, built by `embed_docs.py` + ChromaDB.
- `run_all_severities.py` TRUNCATEs `logs` and seeds 4 fixed scenarios.

**Laya** is an open-source (Apache-2.0) typed decision model from Convai Innovations: Hugging Face `convaiinnovations/laya`, pip package `laya`.

- You describe a `state` plus typed `questions` (`choice` / `score` / `noul`).
- One forward pass returns, per question: `choice`, `probabilities`, `confidence`, `answer_confidence`, `action.act_probability`.
- `laya.Router().predict(state, questions, model=...)` routes to a checkpoint: `english` (512 tokens), `multilingual` or `typed-decisions` (1,024 tokens).
- `laya-serve` (from `laya[serve]`) exposes the Jev-compatible `POST /v1/systemone`.

Documented limits you must respect:

- Zero-shot accuracy is near chance on typed decisions.
- It ships over-confident: fit temperatures before trusting any threshold.
- Gate on `confidence`, never on `act_probability` (it reads ~1.0 for every input).
- `noul` can get stuck on "no" on the English checkpoint.
- `score` is the weakest question type.
- Long option lists degrade accuracy.
- Set `USE_TF=0` if `laya.load()` hangs.
- The `laya-serve` default port 8000 collides with our mock app.

**Verify every Laya API detail against the installed package** (read its source and `--help`) instead of trusting this summary. Tell me wherever it differs.

## Goal

1. Laya does the first-pass per-service SEV-1..4 classification from a compact evidence pack.
2. The evidence plus Laya's calibrated verdicts are handed to Gemini as a prior. Gemini confirms or overrides each verdict, with an evidence-based reason, and still writes the report.
3. A deterministic paging policy in code decides whether a Slack page actually goes out, using Laya and Gemini together, plus a hard safety floor.

## Ground rules

- Create branch `feature/laya-triage` from the current `RAG` branch. Commit once per phase with clear messages. Do not push.
- Never modify or commit `.env`. Add every new key to a new `.env.example` (no secret values).
- Never run TRUNCATE or other destructive SQL against the `observability` DB. Tests use mocks or a separate `observability_test` DB (create it if missing).
- Never send real Slack messages from tests: mock `notify_slack`. For live verification, run with `SLACK_WEBHOOK_URL` empty, or ask me first.
- With `LAYA_ENABLED=false` the agent must behave exactly as it does today.
- Do not remove existing dependencies. (`openai` looks unused: flag it, don't delete it.)
- **Before writing code:** read `agent.py`, `mcp_server.py`, `tools.py`, `dynatrace_mcp_server.py`, `dynatrace_client.py`, `slack_notify.py`, `mock_app/*`, `run_all_severities.py`, `README.md` and the three runbook `.txt` files. Then post a short plan that lists any conflict with this spec, and wait for my OK.

## Phase 1: install and configure Laya

- Install `laya` and `laya[serve]` into the project `.venv`. Check compatibility with `.python-version`, and tell me the install and download size (torch plus the checkpoint).
- Pin exact versions of the new packages in `requirements.txt`; keep the existing pins.
- Add `scripts/laya_smoke.py`: load the Router, run the 3-question example from Laya's README (department / urgency / churn_risk), and print the answers and latency.
- Add `laya_config.py`, which reads these settings from `.env` and supplies defaults:

  ```
  LAYA_ENABLED=true
  LAYA_MODE=shadow                 # shadow | advisory | enforce
  LAYA_MODEL=typed-decisions       # english | multilingual | typed-decisions
  LAYA_BASE_URL=                   # empty = in-process Router; else e.g. http://localhost:8100 (laya-serve)
  LAYA_API_KEY=
  LAYA_TIMEOUT_S=5
  LAYA_PAGE_HIGH=0.80
  LAYA_PAGE_LOW=0.40
  LAYA_CALIBRATION_FILE=laya/calibration.json
  HARD_FLOOR_ERR_PCT=50
  CRITICAL_SERVICES=payment-service
  ```

- Document how to run `laya-serve` on port 8100. Find the real port flag or env var; don't guess.

## Phase 2: evidence pack (`laya/evidence.py`)

Write pure, unit-tested functions. Input is the parsed JSON from `get_logs` and `dt_search_logs`. Output is one evidence pack per service.

- **Laya sees text bands, not raw numbers.** Encoders are weak at arithmetic.
  - 5xx error-rate bands: none <1%, low 1–5%, moderate 5–15%, high 15–30%, severe ≥30%.
  - Latency bands: normal <300 ms, elevated 300–1000 ms, high 1000–2000 ms, severe ≥2000 ms.
  - Request-volume band.
  - Δ error-rate in percentage points between Postgres and Dynatrace.
  - Source resolution label: agreement / DT-lag / DT-higher / DT-unavailable, using the existing prompt rules (<5 pp = agreement, ≥15 pp = large gap).
  - Criticality from `CRITICAL_SERVICES`.
  - 4xx rate band.
  - Up to 5 distinct recent `error_detail` samples. This needs an additive change to `get_logs` (new fields only; don't break the existing schema).
- Keep the raw numbers in the pack too, for Gemini and the hard floor.
- Put all band thresholds and question text in `laya/triage_config.json`, not in code.
- Assert the serialized state stays within the chosen checkpoint's token budget; truncate the samples if it doesn't.

## Phase 3: Laya client and LAYA-MCP server

- `laya/client.py`:
  - Wraps either the in-process `Router` or HTTP `POST /v1/systemone`, depending on `LAYA_BASE_URL`.
  - Applies the calibration temperatures if the calibration file exists.
  - Returns a normalized result.
  - Enforces a timeout, and never raises into the agent: on any failure it returns `status: "unavailable"`.
- Questions, defined in `laya/triage_config.json`:
  - `severity`: choice `sev1`/`sev2`/`sev3`/`sev4`. Write the option descriptions from `dynatrace-alerts.txt`.
  - `user_impact`: choice `widespread`/`partial`/`minimal`.
  - `failure_mode`: choice `db_pool`/`upstream_timeout`/`payment_gateway`/`auth`/`resource_exhaustion`/`other`.
  - `page_now`: two-option choice, `A` = "yes, wake an on-call engineer now…", `B` = "no…". Use choice, not `noul`. The eval script may compare `noul` on the typed-decisions checkpoint.
- Derive in code, not with Laya's `score` type:
  - `expected_sev = Σ i·p(sev_i)`
  - the top-2 probability margin
- New server `laya_mcp_server.py` (stdio, same style as the other two), with tool `classify_services(evidence_packs)` returning per-service results. Answer all questions for a service in one forward pass; batch services if the API allows it.
- New table `laya_triage`, created idempotently in `init_db`: `run_id, service, mode, model, evidence jsonb, answers jsonb, expected_sev, confidence, latency_ms, created_at`.

Example per-service output:

```json
{"service": "payment-service", "status": "ok",
 "laya": {"severity": "sev1", "p": {"sev1": 0.71, "sev2": 0.22, "sev3": 0.05, "sev4": 0.02},
          "expected_sev": 1.4, "confidence": 0.64, "page_now": {"yes": 0.83},
          "failure_mode": {"choice": "db_pool", "p": 0.77}, "user_impact": {"choice": "widespread", "p": 0.69},
          "calibrated": true, "latency_ms": 41}}
```

## Phase 4: agent integration (`agent.py`)

- When `LAYA_ENABLED`, spawn LAYA-MCP as a third stdio server.
- Before the Gemini chat starts, **code (not Gemini)**:
  1. calls `get_logs` and `dt_search_logs`;
  2. builds the evidence packs;
  3. calls `classify_services`.

  Log each call as TOOL_CALL / TOOL_RESULT in `agent_runs`, the same way existing tool calls are logged.
- `shadow` mode: the Gemini prompt and flow are unchanged. Laya results are only logged.
- `advisory` and `enforce` modes:
  - Put the evidence packs and Laya verdicts in the first user message.
  - Update `SYSTEM_PROMPT`: Laya's verdict is a calibrated prior. For each service, confirm it or override it with an evidence-based reason, and quote Laya's probabilities.
  - Gemini still follows the remaining steps: `raise_alert` per service, one email, `save_metrics`, and the final report.
  - The report's correlation table gets "Laya SEV (p)" and "Agent SEV" columns.
  - Gemini may re-call the read tools but doesn't need to.
- Inside `agent.py`'s tool routing, inject `run_id` into `call_on_call_engineer` and `raise_alert` arguments, so the tools can look up this run's triage. Gemini must not need to supply it. Keep the parameter optional in the tool schemas.

## Phase 5: paging policy (`policy.py`, enforced inside `tools.call_on_call_engineer`)

Implement this table exactly, and unit-test every row:

| Laya P(sev1) | Gemini calls page? | Outcome |
|---|---|---|
| ≥ LAYA_PAGE_HIGH | yes | PAGE |
| ≥ LAYA_PAGE_HIGH | no | PAGE_FORCED: after the Gemini loop ends, agent.py pages services that weren't paged; the message says Gemini did not page |
| LAYA_PAGE_LOW–HIGH | yes | PAGE, labelled "model disagreement" |
| < LAYA_PAGE_LOW | yes | PAGE_HELD: no Slack page; record an alert and include it in the email as SEV-2 "held page, needs human review" |
| Laya unavailable | yes | PAGE (fail open to today's behaviour), labelled "Laya unavailable" |

- **Hard floor:** the service is in `CRITICAL_SERVICES`, AND its Postgres 5xx rate is ≥ `HARD_FLOOR_ERR_PCT`, AND Dynatrace agrees or is unavailable → always PAGE, whatever either model says.
- Mode behaviour:
  - `shadow`: compute and log the decision as alert_type `POLICY_SHADOW`; actual behaviour is unchanged.
  - `advisory`: the same logging, plus the decision is shown in the report.
  - `enforce`: the policy outcome is what actually happens.
- **Calibration before enforcement:** if `LAYA_MODE=enforce` and the calibration file is missing, log a warning and run as `advisory`.
- `raise_alert` must reject severities outside 1–4 with a clear error the model can see.
- The Slack page must include Laya's P(sev1), Gemini's severity, and the policy reason. Make the incident ID collision-safe (add milliseconds or a short random suffix).

## Phase 6: training data, calibration, evaluation (`laya/training/`)

- `generate_scenarios.py`: a parametric, seeded generator that replaces the hand-written rows.
  - Vary: services, request volume, 5xx rate, 4xx rate, latency distribution, `error_detail` texts per failure mode, and Dynatrace agreement / lag / unavailable.
  - Output: evidence packs plus per-service labels (`severity`, `page_now`, `failure_mode`, `user_impact`), following a written labelling rubric in `laya/training/RUBRIC.md` derived from the SYSTEM_PROMPT definitions and service criticality.
  - Write JSONL train/val/test splits. **No DB writes.**
- Export to the format expected by Laya's fine-tune notebook (`notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb` in Laya's GitHub repo). Read the notebook to confirm the schema. Document how I run it on Kaggle and load the resulting checkpoint. Don't try to train locally.
- `calibrate.py`: fit one temperature per question on the val split by minimising NLL (temperature on probabilities: `p_i^(1/T)`, renormalised). Write `laya/calibration.json`.
- `evaluate.py`: on the test split, report:
  - per-question accuracy and a confusion matrix for severity;
  - ECE before and after calibration;
  - SEV-1 recall (the missed-SEV-1 rate) and the false-page rate at the current LAYA_PAGE_HIGH / LAYA_PAGE_LOW;
  - a suggested threshold table.

  Run it now against the zero-shot checkpoint and save the output as `docs/laya_eval_zero_shot.md`. Expect weak numbers: this is the baseline.
- `shadow_report.py`: for the last N days, compare `laya_triage` against Gemini's severities (from `alerts` / `agent_runs`). Report the agreement rate, list the disagreements, and list the would-be PAGE_HELD and PAGE_FORCED cases.

## Phase 7: input-quality fixes (separate commit)

These are needed for `failure_mode` and the source comparison to mean anything.

- `mock_app/main.py`: give each endpoint realistic, failure-specific `error_detail` text, replacing the generic "Unhandled exception in service layer". Keep the simulator working.
- `get_logs`: add `client_error_count` and `client_error_rate_pct` (4xx) as additive fields.
- **Ask me first:** `dynatrace_client.py`'s DQL has `limit 1000`, which undercounts Dynatrace at volume. Propose aggregating in DQL (`summarize` by `service.name`) instead.

## Phase 8: tests (pytest)

- Unit tests:
  - evidence banding and resolution labels;
  - every row of the policy table, plus the hard floor and all three modes;
  - calibration maths;
  - client fallback when Laya is unavailable or times out;
  - the MCP tool schemas;
  - `raise_alert` severity validation.
- One integration test, marked `slow`: load the real Laya checkpoint and classify 2 synthetic services.
- Mock Slack and Gemini everywhere. All tests must pass.

## Phase 9: README

Update `README.md`:

- The architecture text diagram, now showing LAYA-MCP. Also reference `docs/architecture-laya.excalidraw`.
- What Laya does and does not do here.
- Setup: install, model download size, `USE_TF=0`, `laya-serve` on port 8100.
- An env-var table.
- The modes and the recommended rollout: shadow → advisory → enforce, and enforce only after fine-tuning, calibration and an eval that meets agreed thresholds.
- Commands: smoke test, generate, calibrate, evaluate, shadow report, run the agent.
- The paging policy table.
- Limitations: near-chance zero-shot accuracy, synthetic labels, numbers banded in code, token budget.

## Definition of done

- With `LAYA_ENABLED=false`, the agent behaves as before. Show me one run.
- After `POST /simulate/critical`, with the Slack webhook disabled, a `LAYA_MODE=shadow` run writes `laya_triage` and `POLICY_SHADOW` rows, and the report is unchanged.
- An `advisory` run shows the Laya-vs-Agent columns and the policy decisions in the report.
- `pytest` passes, `requirements.txt` is pinned, `.env.example` and the README are updated, and `docs/laya_eval_zero_shot.md` exists.
- Finish with a summary covering:
  - changes per file;
  - how to run each mode;
  - every Laya API detail that differed from this spec;
  - open questions for me.
