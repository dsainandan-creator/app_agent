"""
laya_triage/prompting.py – What Gemini is told about Laya in advisory/enforce mode.

Shadow mode uses none of this: Gemini's prompt and flow stay exactly as before.
"""

import json

LAYA_PROMPT_ADDENDUM = """
━━━ LAYA TRIAGE PRIOR (advisory/enforce mode) ━━━
Before this conversation, code already ran steps 1 and 2: the get_logs and dt_search_logs
results are in the first message. You may call those tools again, but you do not need to.

A separate classifier, Laya, read a banded summary of each service (text bands, not raw
numbers) and returned a severity distribution. Treat it as a calibrated prior and a second
opinion, not ground truth: zero-shot Laya is close to chance on this task.

For EACH service:
  - Confirm Laya's verdict or override it, with an evidence-based reason that cites the
    PostgreSQL / Dynatrace numbers.
  - Quote Laya's probabilities, e.g. "Laya: sev2 (P(sev1)=0.25, P(sev2)=0.39)".
  - If Laya's status is "unavailable", say so and assess from the data alone.

Use this correlation table format instead of the one in step 3 (two extra columns):

   | Service | PG err% | PG avg_ms | DT err% | DT avg_ms | Δ err% | Resolution | Laya SEV (p) | Agent SEV |
   |---------|---------|-----------|---------|-----------|--------|------------|--------------|-----------|

Paging: call call_on_call_engineer only for services you assess as SEV-1, exactly as before.
A deterministic paging policy in code also decides; its outcome comes back in the tool result.
  - If the result has "enforced": true, that outcome is what happened (e.g. PAGE_HELD means no
    page was sent). Report it as-is.
  - If it has "enforced": false (advisory mode), the page WAS sent and the outcome is only what
    the policy would have done. Say "page sent; policy would <outcome>", never "held".
"""


def laya_verdicts(packs: list, triage: dict) -> list:
    """One compact row per service: the evidence bands plus Laya's answer."""
    by_service = {r["service"]: r for r in triage.get("results") or []}
    rows = []
    for pack in packs:
        r = by_service.get(pack["service"], {"status": "unavailable"})
        row = {
            "service": pack["service"],
            "critical": pack["critical"],
            "bands": {k: v for k, v in pack["bands"].items() if v is not None},
            "source_resolution": pack["source"]["resolution"],
            "delta_err_pp": pack["source"]["delta_err_pp"],
            "recent_errors": pack["error_samples"],
            "laya_status": r.get("status", "unavailable"),
        }
        if r.get("status") == "ok":
            laya = r["laya"]
            row["laya"] = {k: laya[k] for k in (
                "severity", "p", "expected_sev", "margin", "page_now",
                "failure_mode", "user_impact", "calibrated")}
        rows.append(row)
    return rows


def advisory_user_message(window_minutes: int, pg: dict, dt: dict, packs: list, triage: dict) -> str:
    """First user message for advisory/enforce mode: data plus Laya's verdicts."""
    payload = {
        "get_logs": pg,
        "dt_search_logs": dt,
        "laya_triage": {
            "status": triage.get("status"),
            "model": triage.get("model"),
            "calibrated": triage.get("calibrated"),
            "error": triage.get("error"),
            "services": laya_verdicts(packs, triage),
        },
    }
    return (
        f"Analyse the application logs from the last {window_minutes} minutes. "
        "Steps 1 and 2 are done: the PostgreSQL (get_logs) and Dynatrace (dt_search_logs) "
        "results and Laya's triage are below. For each service, state the PostgreSQL value, the "
        "Dynatrace value and Laya's verdict with its probabilities, then confirm or override Laya "
        "with an evidence-based reason before assigning severity. Do not estimate any metric — "
        "only use values present in this data. Then take the appropriate actions (SEV-1 → Slack "
        "page, all services → alerts + email), save metrics, and write a concise incident report "
        "whose correlation table includes the 'Laya SEV (p)' and 'Agent SEV' columns.\n\n"
        + json.dumps(payload, default=str)
    )


def unavailable_triage(packs: list, model: str, error: str) -> dict:
    """The triage result used when LAYA-MCP itself cannot be reached."""
    return {"status": "unavailable", "model": model, "calibrated": False, "error": error,
            "results": [{"service": p["service"], "status": "unavailable"} for p in packs]}


def triage_table(triage: dict) -> str:
    """Terminal summary of Laya's verdicts."""
    if triage.get("status") != "ok":
        return f"[Laya] unavailable: {triage.get('error')}"
    lines = [f"[Laya] {triage.get('model')} | calibrated={triage.get('calibrated')} "
             f"| {triage.get('latency_ms')} ms"]
    for r in triage.get("results") or []:
        if r.get("status") != "ok":
            lines.append(f"  {r['service']:18s} unavailable")
            continue
        l = r["laya"]
        lines.append(
            f"  {r['service']:18s} {l['severity']}  P(sev1)={l['p']['sev1']:.2f}  "
            f"E[sev]={l['expected_sev']:.2f}  page_now={l['page_now'].get('yes', 0):.2f}  "
            f"{l['failure_mode']['choice']}")
    return "\n".join(lines)
