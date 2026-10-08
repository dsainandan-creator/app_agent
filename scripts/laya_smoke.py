"""
scripts/laya_smoke.py – Check that Laya loads and answers.

Runs the 3-question example from Laya's README (department / urgency /
churn_risk) through laya.Router and prints the answers plus cold and warm
latency.

The README calls Router.predict without a model, which routes English text to
the `english` checkpoint (an extra ~840 MB download). This script uses
LAYA_MODEL (default typed-decisions) so the smoke test downloads only the
checkpoint the triage layer actually uses. Pass --model to override.

    python scripts/laya_smoke.py
    python scripts/laya_smoke.py --model english
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("USE_TF", "0")   # laya.load() can hang when TensorFlow is installed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from laya_config import load_config   # noqa: E402

STATE = ("Hi, we were billed twice for March. Please refund the duplicate today "
         "or we will cancel our plan.")
QUESTIONS = {
    "department": {"type": "choice", "instructions": "Which department should handle this?",
                   "criteria": {"billing": "invoices, payments, refunds",
                                "technical": "bugs, outages, system errors",
                                "other": "everything else"}},
    "urgency": {"type": "score", "instructions": "How urgent is this?",
                "criteria": ["not urgent", "soon", "blocking"]},
    "churn_risk": {"type": "noul", "instructions": "Does the user threaten to cancel or leave?"},
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default=load_config().model,
                        help="english | multilingual | typed-decisions (default: LAYA_MODEL)")
    args = parser.parse_args()

    import laya
    from laya import Router

    print(f"laya {laya.__version__} | model={args.model}")

    t0 = time.perf_counter()
    router = Router(max_loaded=1)
    router.load(args.model)               # download (first run) + build
    load_s = time.perf_counter() - t0
    agent = router.load(args.model)
    print(f"load: {load_s:.1f}s | device={getattr(agent, 'device', '?')} "
          f"| max_len={agent.cfg.get('max_len')} head_max_len={agent.cfg.get('head_max_len')}")

    t1 = time.perf_counter()
    result = router.predict(STATE, QUESTIONS, model=args.model)
    cold_ms = (time.perf_counter() - t1) * 1000

    t2 = time.perf_counter()
    router.predict(STATE, QUESTIONS, model=args.model)
    warm_ms = (time.perf_counter() - t2) * 1000

    a = result["answers"]
    print(f"department : {a['department']['choice']}  p={a['department']['probabilities']}")
    print(f"urgency    : {a['urgency']['score']:.2f} / 2.0  p={a['urgency']['probabilities']}")
    print(f"churn_risk : P(yes)={a['churn_risk']['noul']:.3f}")
    print(f"routing    : {result['routing']['model']} ({result['routing']['reason']})")
    print(f"latency    : first predict {cold_ms:.0f} ms | warm predict {warm_ms:.0f} ms")
    print("usage      :", json.dumps(result.get("usage", {})))


if __name__ == "__main__":
    main()
