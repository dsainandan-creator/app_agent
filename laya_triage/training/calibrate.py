"""
laya_triage/training/calibrate.py – Fit Laya temperatures on the validation split.

Uses Laya's own fitter instead of a hand-rolled one:
  laya.calibrate.records_from_labeled(agent, pairs)  raw logits per question, one
                                                     forward pass per state
  laya.calibrate.fit_temperature_map(records)        NLL + LBFGS on log T, per
                                                     (question type, option-count bucket)

Laya fits one temperature per bucket, not per question. Buckets are option-count
ranges (2, 3-5, 6-10, 11+), so for our questions:
  choice:3-5   severity (4 options) and user_impact (3)  -> shared temperature
  choice:6-10  failure_mode (6)
  choice:2     page_now (2)
A bucket gets its own temperature only with >= 2000 records (laya.calibrate.MIN_BUCKET_N);
smaller buckets fall back to the fitted type-level 'choice' scalar.

The output is Laya's calibration payload (loadable with Agent(calibration=...)) plus
a "shipped" block recording the checkpoint's own temperatures, which
laya_triage/client.py needs to re-temper laya-serve's probabilities exactly.

By default this writes laya_triage/training/out/calibration.json, NOT the path the
agent reads (LAYA_CALIBRATION_FILE, laya_triage/calibration.json): that file
unlocks LAYA_MODE=enforce, so install it deliberately with --out.

    python -m laya_triage.training.calibrate
    python -m laya_triage.training.calibrate --limit 500                 # quick check
    python -m laya_triage.training.calibrate --out laya_triage/calibration.json
"""

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("USE_TF", "0")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from laya_config import load_config                 # noqa: E402
from laya_triage.client import build_router         # noqa: E402

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_OUT = Path(__file__).parent / "out" / "calibration.json"


def read_split(path: Path, limit: int = None) -> list:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
                if limit and len(rows) >= limit:
                    break
    return rows


def one_hot_targets(row: dict) -> dict:
    """{qid: [1.0 for the labelled option, 0.0 otherwise]} in criteria order."""
    return {qid: [1.0 if option == row["expected"][qid] else 0.0
                  for option in row["questions"][qid]["criteria"]]
            for qid in row["expected"]}


def fit(rows: list, config, device: str = None, chunk: int = 250, log=print) -> dict:
    """Run the forwards, fit the map and return the calibration payload."""
    from laya.calibrate import calibration_payload, fit_temperature_map, records_from_labeled

    agent = build_router(config, device=device).load(config.model)    # shipped temperatures
    t0 = time.perf_counter()
    import torch
    records = []
    for start in range(0, len(rows), chunk):
        part = rows[start:start + chunk]
        # laya 0.3.22's records_from_labeled calls agent._forward outside no_grad, which
        # fails converting the logits to numpy ("requires grad"); run it under no_grad.
        with torch.no_grad():
            records += records_from_labeled(agent, [(r["state"], r["questions"], one_hot_targets(r))
                                                    for r in part])
        log(f"  forwards {min(start + chunk, len(rows))}/{len(rows)} "
            f"({time.perf_counter() - t0:.0f}s)")
    forward_s = time.perf_counter() - t0

    result = fit_temperature_map(records, compute_ece=True, seed=0)
    payload = calibration_payload(result["temperature"], result["temperature_by_options"],
                                  model_id_or_path=agent.model_id_or_path,
                                  subfolder=agent.subfolder, config=agent.cfg)
    import laya
    payload["shipped"] = {
        "temperature": [float(t) for t in agent.temperature],
        "temperature_by_options": {k: float(v) for k, v in agent.temperature_by_options.items()},
    }
    payload["fitted_on"] = {
        "laya_version": laya.__version__,
        "model": config.model,
        "model_path": config.model_path or None,
        "n_states": len(rows),
        "n_records": len(records),
        "n_by_bucket": result["n_by_bucket"],
        "forward_seconds": round(forward_s, 1),
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    payload["report"] = result.get("report")
    return payload


def main():
    parser = argparse.ArgumentParser(description="Fit Laya calibration temperatures on the val split.")
    parser.add_argument("--split", default="val")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--limit", type=int, default=None, help="use only the first N states")
    parser.add_argument("--device", default=None, help="cpu | mps | cuda (default: Laya's choice)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    config = load_config({**os.environ, "LAYA_ENABLED": "true"})
    rows = read_split(args.data_dir / f"{args.split}.jsonl", args.limit)
    print(f"calibrating {config.model} on {len(rows)} {args.split} states")
    payload = fit(rows, config, device=args.device)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2))
    print(f"shipped : {payload['shipped']}")
    print(f"fitted  : temperature={payload['temperature']} by_options={payload['temperature_by_options']}")
    print(f"buckets : {payload['fitted_on']['n_by_bucket']}")
    if payload["report"]:
        r = payload["report"]
        print(f"held-out ECE: {json.dumps({k: v for k, v in r.items() if 'ece' in k})}")
    print(f"written : {args.out}")


if __name__ == "__main__":
    main()
