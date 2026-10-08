"""
laya_triage/training/evaluate.py – Score Laya on the synthetic test split.

Built on laya.evals: Dataset / Example for the rows, evaluate() to run the
checkpoint and aggregate, ChoiceAccuracy + MeanConfidence as evaluators, ece()
for calibration error, and assert_regression() to gate against a baseline report.

Reports:
  - per-question accuracy, and the confusion matrix for severity
  - ECE as served (the checkpoint's shipped temperatures) and after the fitted
    calibration (probabilities re-tempered exactly; temperature scaling never
    changes which answer wins, so accuracy is identical)
  - paging at the current LAYA_PAGE_HIGH / LAYA_PAGE_LOW: SEV-1 recall and
    missed-SEV-1 rate, false-page rate
  - a threshold sweep with suggested HIGH / LOW values

    python -m laya_triage.training.evaluate
    python -m laya_triage.training.evaluate --limit 100 --device cpu
    python -m laya_triage.training.evaluate --baseline docs/laya_eval_zero_shot.json \\
        --tolerance choice_accuracy=0.02
"""

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

os.environ.setdefault("USE_TF", "0")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from laya_config import load_config                          # noqa: E402
from laya_triage.client import build_router, retemper         # noqa: E402
from laya_triage.training.calibrate import read_split         # noqa: E402

DATA_DIR = Path(__file__).parent / "data"
OUT_DIR = Path(__file__).parent / "out"
SEVERITIES = ("sev1", "sev2", "sev3", "sev4")
QUESTIONS = ("severity", "user_impact", "failure_mode", "page_now")
SWEEP = [round(0.05 * i, 2) for i in range(1, 20)]
TARGET_RATE = 0.05


def default_calibration(config) -> Path:
    for path in (OUT_DIR / "calibration.json", config.calibration_file):
        if Path(path).is_file():
            return Path(path)
    return None


def paging_rates(p_sev1: list, is_sev1: list, high: float, low: float) -> dict:
    pos = [p for p, y in zip(p_sev1, is_sev1) if y]
    neg = [p for p, y in zip(p_sev1, is_sev1) if not y]
    frac = lambda xs, cond: (sum(1 for x in xs if cond(x)) / len(xs)) if xs else float("nan")
    return {
        "n_sev1": len(pos), "n_other": len(neg),
        "sev1_recall_at_high": frac(pos, lambda p: p >= high),        # Laya alone would force-page
        "missed_sev1_rate_at_low": frac(pos, lambda p: p < low),      # a Gemini page would be held
        "false_page_rate_at_high": frac(neg, lambda p: p >= high),    # forced page on a non-SEV-1
        "held_non_sev1_rate_at_low": frac(neg, lambda p: p < low),
    }


def sweep(p_sev1: list, is_sev1: list) -> list:
    out = []
    for t in SWEEP:
        r = paging_rates(p_sev1, is_sev1, t, t)
        out.append({"t": t, "recall": r["sev1_recall_at_high"], "miss": r["missed_sev1_rate_at_low"],
                    "false_page": r["false_page_rate_at_high"]})
    return out


def suggest(rows: list) -> dict:
    high = next((r for r in rows if r["false_page"] <= TARGET_RATE), None)
    low = next((r for r in reversed(rows) if r["miss"] <= TARGET_RATE), None)
    return {"high": high, "low": low}


def main():
    from laya.evals import ChoiceAccuracy, Dataset, Example, MeanConfidence, assert_regression, ece, evaluate

    parser = argparse.ArgumentParser(description="Evaluate Laya on the synthetic test split.")
    parser.add_argument("--split", default="test")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--device", default=None, help="cpu | mps | cuda (default: Laya's choice)")
    parser.add_argument("--calibration", type=Path, default=None,
                        help="calibration JSON for the 'after' numbers (default: training/out, then LAYA_CALIBRATION_FILE)")
    parser.add_argument("--out-md", type=Path, default=OUT_DIR / "laya_eval.md")
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--title", default="Laya triage evaluation")
    parser.add_argument("--baseline", type=Path, default=None, help="earlier --out-json report to gate against")
    parser.add_argument("--tolerance", action="append", default=[], help="metric=max_abs_diff")
    args = parser.parse_args()

    config = load_config({**os.environ, "LAYA_ENABLED": "true"})
    rows = read_split(args.data_dir / f"{args.split}.jsonl", args.limit)
    dataset = Dataset([Example(state=r["state"], questions=r["questions"], expected=r["expected"],
                               tags=tuple(r.get("tags") or ()), model=config.model) for r in rows])
    cal_path = args.calibration or default_calibration(config)
    calibration = json.loads(cal_path.read_text()) if cal_path else None

    runner = build_router(config, device=args.device)       # shipped temperatures, no calibration
    t_load = time.perf_counter()
    agent = runner.load(config.model)
    load_s = time.perf_counter() - t_load
    device = str(getattr(agent, "device", "?"))

    t0 = time.perf_counter()
    report = evaluate(runner, dataset, evaluators=[ChoiceAccuracy(), MeanConfidence()],
                      batch_size=args.batch_size,
                      config={"model": config.model, "model_path": config.model_path or None,
                              "split": args.split, "device": device})
    eval_s = time.perf_counter() - t0

    # ---- per question, ECE before / after
    by_q = {q: [c for c in report.cases if c["qid"] == q] for q in QUESTIONS}
    per_q = {}
    for q, cases in by_q.items():
        conf_b = [c["confidence"] for c in cases]
        correct = [bool(c["correct"]) for c in cases]
        row = {"n": len(cases),
               "accuracy": report.slices["qid"][q]["choice_accuracy"],
               "ece_shipped": ece(conf_b, correct)}
        if calibration:
            conf_a = [max(retemper(c["answer"]["probabilities"], "choice", calibration).values())
                      for c in cases]
            row["ece_calibrated"] = ece(conf_a, correct)
        per_q[q] = row

    # ---- severity confusion matrix
    confusion = {e: {p: 0 for p in SEVERITIES} for e in SEVERITIES}
    for c in by_q["severity"]:
        confusion[c["expected"]][c["answer"]["choice"]] += 1

    # ---- paging metrics on P(sev1)
    is_sev1 = [c["expected"] == "sev1" for c in by_q["severity"]]
    p_shipped = [c["answer"]["probabilities"]["sev1"] for c in by_q["severity"]]
    paging = {"shipped": paging_rates(p_shipped, is_sev1, config.page_high, config.page_low)}
    sweeps = {"shipped": sweep(p_shipped, is_sev1)}
    if calibration:
        p_cal = [retemper(c["answer"]["probabilities"], "choice", calibration)["sev1"]
                 for c in by_q["severity"]]
        paging["calibrated"] = paging_rates(p_cal, is_sev1, config.page_high, config.page_low)
        sweeps["calibrated"] = sweep(p_cal, is_sev1)

    gate = None
    if args.baseline:
        tolerances = {k: float(v) for k, v in (t.split("=", 1) for t in args.tolerance)}
        try:
            deltas = assert_regression(report, json.loads(args.baseline.read_text()), tolerances)
            gate = ("PASS", deltas)
        except AssertionError as exc:
            gate = ("FAIL", str(exc))

    md = render(args, config, device, len(rows), load_s, eval_s, report, per_q, confusion,
                paging, sweeps, cal_path, calibration, gate)
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(md)
    out_json = args.out_json or args.out_md.with_suffix(".json")
    out_json.write_text(json.dumps({**report.to_json(), "per_question": per_q, "confusion": confusion,
                                    "paging": paging, "sweep": sweeps,
                                    "timing": {"load_s": load_s, "eval_s": eval_s}}, default=str))
    print(md)
    print(f"written: {args.out_md} and {out_json}")
    if gate and gate[0] == "FAIL":
        sys.exit(1)


def _f(x, pct=False):
    if x is None or x != x:
        return "n/a"
    return f"{x * 100:.1f}%" if pct else f"{x:.3f}"


def render(args, config, device, n, load_s, eval_s, report, per_q, confusion, paging, sweeps,
           cal_path, calibration, gate) -> str:
    import laya
    o = report.overall
    lines = [
        f"# {args.title}",
        "",
        f"- Checkpoint: `{config.model}`" + (f" from `{config.model_path}`" if config.model_path else "")
        + f" (laya {laya.__version__}), device `{device}`",
        f"- Data: synthetic `{args.split}` split, {n} services x {len(QUESTIONS)} questions "
        f"({len(report.cases)} decisions); labels from `laya_triage/training/RUBRIC.md`",
        f"- Time: checkpoint load {load_s:.1f} s, evaluation {eval_s:.1f} s "
        f"({eval_s / max(n, 1) * 1000:.0f} ms per service, batch size {args.batch_size}); "
        f"host {platform.machine()} / {platform.system()}",
        f"- Calibration for the 'after' numbers: "
        + (f"`{cal_path}` (fitted on {calibration.get('fitted_on', {}).get('n_states', '?')} val states)"
           if calibration else "none"),
        "",
        "## Accuracy and calibration",
        "",
        "| Question | Options | Accuracy | Chance | ECE (shipped temps) | ECE (calibrated) |",
        "|----------|---------|----------|--------|---------------------|------------------|",
    ]
    options = {"severity": 4, "user_impact": 3, "failure_mode": 6, "page_now": 2}
    for q, r in per_q.items():
        lines.append(f"| {q} | {options[q]} | {_f(r['accuracy'], True)} | {100 / options[q]:.0f}% "
                     f"| {_f(r['ece_shipped'])} | {_f(r.get('ece_calibrated'))} |")
    lines += ["",
              f"Overall: choice accuracy {_f(o.get('choice_accuracy'), True)}, mean confidence "
              f"{_f(o.get('mean_confidence'))}, ECE {_f(o.get('ece'))} (shipped temperatures). "
              "Temperature scaling does not change which option wins, so accuracy is the same "
              "before and after calibration.",
              "",
              "## Severity confusion matrix (rows: label, columns: Laya)",
              "",
              "| label \\ Laya | " + " | ".join(SEVERITIES) + " |",
              "|---|" + "---|" * len(SEVERITIES)]
    for e in SEVERITIES:
        lines.append(f"| **{e}** | " + " | ".join(str(confusion[e][p]) for p in SEVERITIES) + " |")

    lines += ["",
              f"## Paging at LAYA_PAGE_HIGH={config.page_high:g} / LAYA_PAGE_LOW={config.page_low:g}",
              "",
              "Laya's P(sev1) alone, before the hard floor, minimum volume or Gemini are applied.",
              "",
              "| Probabilities | SEV-1 recall (P >= HIGH) | Missed SEV-1 (P < LOW) | False-page rate (non-SEV-1, P >= HIGH) | Non-SEV-1 held (P < LOW) |",
              "|---|---|---|---|---|"]
    for name, r in paging.items():
        lines.append(f"| {name} | {_f(r['sev1_recall_at_high'], True)} | {_f(r['missed_sev1_rate_at_low'], True)} "
                     f"| {_f(r['false_page_rate_at_high'], True)} | {_f(r['held_non_sev1_rate_at_low'], True)} |")
    first = next(iter(paging.values()))
    lines += ["", f"({first['n_sev1']} SEV-1 and {first['n_other']} other services in the test split.)"]

    for name, rows in sweeps.items():
        s = suggest(rows)
        lines += ["", f"## Threshold sweep ({name} probabilities)", "",
                  "| t | SEV-1 recall (P >= t) | Missed SEV-1 (P < t) | False-page rate (P >= t) |",
                  "|---|---|---|---|"]
        lines += [f"| {r['t']:.2f} | {_f(r['recall'], True)} | {_f(r['miss'], True)} | {_f(r['false_page'], True)} |"
                  for r in rows]
        hi, lo = s["high"], s["low"]
        lines += ["",
                  f"Suggested LAYA_PAGE_HIGH (lowest t with false-page rate <= {TARGET_RATE:.0%}): "
                  + (f"**{hi['t']:.2f}** (SEV-1 recall {_f(hi['recall'], True)})" if hi else "**none meets it**"),
                  "",
                  f"Suggested LAYA_PAGE_LOW (highest t with missed SEV-1 <= {TARGET_RATE:.0%}): "
                  + (f"**{lo['t']:.2f}** (holds {_f(1 - lo['false_page'], True)} of non-SEV-1 services below it)"
                     if lo else "**none meets it**")]
    if gate:
        lines += ["", f"## Regression gate: {gate[0]}", "", f"```\n{gate[1]}\n```"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
