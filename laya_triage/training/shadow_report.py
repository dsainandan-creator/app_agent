"""
laya_triage/training/shadow_report.py – How Laya compares with Gemini in real runs.

Read-only. For agent runs in the last N days that have Laya triage:
  - Laya's severity (laya_triage.answers) vs Gemini's (the severity it passed to
    raise_alert, read from agent_runs, so runs from before alerts.run_id existed count too)
  - agreement rate, exact and within one level, and the disagreements
  - paging-policy decisions (POLICY_SHADOW / POLICY_ENFORCED alerts): the would-be
    PAGE_HELD and PAGE_FORCED cases

    python -m laya_triage.training.shadow_report
    python -m laya_triage.training.shadow_report --days 30 --out docs/laya_shadow_report.md
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2.extras                                   # noqa: E402

from mock_app.database import get_connection             # noqa: E402

SEV_NUM = {"sev1": 1, "sev2": 2, "sev3": 3, "sev4": 4}


def _query(sql: str, params=()) -> list:
    conn = get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def load(days: int) -> tuple:
    triage = _query(
        """SELECT run_id, service, mode, created_at,
                  answers->>'status'                      AS status,
                  answers->'laya'->>'severity'            AS laya_sev,
                  (answers->'laya'->'p'->>'sev1')::float  AS p_sev1
           FROM laya_triage
           WHERE created_at >= NOW() - INTERVAL '1 day' * %s
           ORDER BY created_at""", (days,))
    run_ids = sorted({t["run_id"] for t in triage})
    gemini = {}
    if run_ids:
        for r in _query(
                """SELECT run_id, tool_input FROM agent_runs
                   WHERE event_type = 'TOOL_CALL' AND tool_name = 'raise_alert' AND run_id = ANY(%s)
                   ORDER BY id""", (run_ids,)):
            try:
                args = json.loads(r["tool_input"])
                gemini[(r["run_id"], args["service"])] = int(args["severity"])
            except (ValueError, KeyError, TypeError):
                continue
    decisions = _query(
        """SELECT run_id, service, alert_type, status, message, timestamp FROM alerts
           WHERE alert_type IN ('POLICY_SHADOW', 'POLICY_ENFORCED')
             AND timestamp >= NOW() - INTERVAL '1 day' * %s
           ORDER BY id""", (days,))
    return triage, gemini, decisions


def _mode_of(decision: dict) -> str:
    """Decision rows are written as '[<mode>] ...' (POLICY_SHADOW covers shadow and advisory)."""
    msg = decision.get("message") or ""
    if msg.startswith("[") and "]" in msg:
        return msg[1:msg.index("]")]
    return "enforce" if decision["alert_type"] == "POLICY_ENFORCED" else "shadow/advisory"


def build(days: int, triage: list, gemini: dict, decisions: list) -> str:
    pairs = []
    for t in triage:
        g = gemini.get((t["run_id"], t["service"]))
        if t["status"] == "ok" and t["laya_sev"] and g:
            pairs.append({**t, "laya": SEV_NUM[t["laya_sev"]], "gemini": g})
    n = len(pairs)
    exact = sum(p["laya"] == p["gemini"] for p in pairs)
    within = sum(abs(p["laya"] - p["gemini"]) <= 1 for p in pairs)
    runs = len({t["run_id"] for t in triage})
    unavailable = sum(1 for t in triage if t["status"] != "ok")

    lines = [f"# Laya shadow report — last {days} days", "",
             f"- Runs with Laya triage: {runs} ({len(triage)} service triages, {unavailable} unavailable)",
             f"- Services with both a Laya and a Gemini severity: {n}"]
    if n:
        lines += [f"- Agreement: exact {exact / n:.1%} ({exact}/{n}), within one level {within / n:.1%}"]
        conf = Counter((p["gemini"], p["laya"]) for p in pairs)
        lines += ["", "## Gemini (rows) vs Laya (columns)", "",
                  "| Gemini \\ Laya | SEV-1 | SEV-2 | SEV-3 | SEV-4 |", "|---|---|---|---|---|"]
        lines += [f"| SEV-{g} | " + " | ".join(str(conf[(g, l)]) for l in range(1, 5)) + " |" for g in range(1, 5)]
        dis = [p for p in pairs if p["laya"] != p["gemini"]]
        lines += ["", f"## Disagreements ({len(dis)})", "",
                  "| When | Run | Service | Mode | Gemini | Laya | Laya P(sev1) |", "|---|---|---|---|---|---|---|"]
        lines += [f"| {p['created_at']:%Y-%m-%d %H:%M} | {p['run_id']} | {p['service']} | {p['mode']} "
                  f"| SEV-{p['gemini']} | SEV-{p['laya']} | {p['p_sev1']:.2f} |" for p in dis]

    outcome = Counter(d["status"] for d in decisions)
    lines += ["", "## Paging-policy decisions", "",
              "| Outcome | Count |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(outcome.items())] or ["| (none) | 0 |"]
    for kind, label in (("PAGE_HELD", "would-be PAGE_HELD (Gemini paged; policy would hold)"),
                        ("PAGE_FORCED", "would-be PAGE_FORCED (Gemini did not page; policy would page)")):
        rows = [d for d in decisions if d["status"] == kind]
        lines += ["", f"### {label}: {len(rows)}", ""]
        if rows:
            lines += ["| When | Run | Service | Mode | Detail |", "|---|---|---|---|---|"]
            lines += [f"| {d['timestamp']:%Y-%m-%d %H:%M} | {d['run_id']} | {d['service']} "
                      f"| {_mode_of(d)} "
                      f"| {d['message'].split('] ', 1)[-1].replace('|', '/')} |" for d in rows]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Compare Laya triage with Gemini's severities.")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    md = build(args.days, *load(args.days))
    print(md)
    if args.out:
        args.out.write_text(md)
        print(f"written: {args.out}")


if __name__ == "__main__":
    main()
