"""
laya_triage/training/generate_scenarios.py – Seeded synthetic triage scenarios.

Each scenario is one service in one analysis window. The generator samples raw
log rows (volume, 5xx and 4xx rates, latency, error_detail texts for an injected
failure mode), aggregates them with the real tools.summarize_logs, builds a
matching Dynatrace view (agreement / lag / higher / unavailable), turns both into
an evidence pack with the real laya_triage.evidence code, and labels it with the
rubric in RUBRIC.md. Nothing touches the database or the network.

Outputs (in --out, default laya_triage/training/data/):
  train.jsonl, val.jsonl, test.jsonl   laya.evals rows: state, questions, expected,
                                       tags, plus id, pack and labels
  notebook_{split}.jsonl               the LocalLLaMA/typed-decisions row format the
                                       Laya fine-tuning notebook reads (id, workflow,
                                       and state / questions / gold as JSON strings)

    python -m laya_triage.training.generate_scenarios
    python -m laya_triage.training.generate_scenarios --train 3000 --val 2500 --test 300 --seed 7
"""

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from laya_triage import evidence as ev   # noqa: E402

DEFAULT_OUT = Path(__file__).parent / "data"
WORKFLOW = "observability_triage"

SERVICES = {
    # service -> an endpoint that tools._infer_service maps back to it
    "payment-service": "/api/payments",
    "order-service": "/api/orders",
    "user-service": "/api/users",
    "product-service": "/api/products",
    "api-gateway": "/health",
}

FAILURE_TEXTS = {
    "db_pool": [
        "Database connection pool exhausted - all threads blocked",
        "Timeout acquiring connection from pool after 5000 ms",
        "psycopg2.OperationalError: too many connections for role 'app'",
        "could not obtain a database connection within 3 s",
        "HikariPool-1 - Connection is not available, request timed out",
    ],
    "upstream_timeout": [
        "Upstream inventory-service timed out after 3000 ms",
        "504 Gateway Timeout from upstream shipping-api",
        "ReadTimeout calling user-profile service",
        "Circuit breaker OPEN for recommendations-service",
        "connect ECONNREFUSED pricing-api:8080",
    ],
    "payment_gateway": [
        "Payment provider returned 502 Bad Gateway",
        "Connection to payment provider timed out after 3 s",
        "Card processor unavailable (HTTP 503 from gateway)",
        "Payment gateway declined: processor timeout",
        "TLS handshake to payment provider failed",
    ],
    "auth": [
        "JWT signature verification failed",
        "OAuth token introspection endpoint unreachable",
        "401 from identity provider: invalid client credentials",
        "Session store lookup failed: token expired",
        "LDAP bind failed for service account",
    ],
    "resource_exhaustion": [
        "java.lang.OutOfMemoryError: Java heap space",
        "Thread pool exhausted: 200/200 workers busy",
        "No space left on device while writing temp file",
        "Container CPU throttled at 100% of limit",
        "Too many open files (EMFILE)",
    ],
    "other": [
        "NullPointerException in OrderMapper.toDto",
        "KeyError: 'currency' in response serializer",
        "Unexpected EOF while parsing request body",
        "Feature-flag service returned malformed JSON",
    ],
}

ERROR_BANDS = [  # (weight, low %, high %)
    (0.30, 0.0, 0.99),
    (0.15, 1.0, 4.9),
    (0.20, 5.0, 14.9),
    (0.15, 15.0, 29.9),
    (0.20, 30.0, 100.0),
]
LATENCY_REGIMES = [(60, 290), (300, 990), (1000, 1990), (2000, 6000)]
DT_CASES = [(0.50, "agreement"), (0.20, "lag"), (0.15, "higher"), (0.15, "unavailable")]


def _weighted(rng, pairs):
    r, acc = rng.random(), 0.0
    for weight, value in pairs:
        acc += weight
        if r < acc:
            return value
    return pairs[-1][1]


# ---------------------------------------------------------------------------
# Rubric (RUBRIC.md)
# ---------------------------------------------------------------------------

def _severity_ignoring_volume(e, latency, client, crit):
    if e >= 30 or (latency >= 2000 and e >= 5) or (crit and e >= 15):
        return "sev1"
    if e >= 15 or latency >= 1000 or (crit and e >= 5):
        return "sev2"
    if e >= 5 or latency >= 300 or client >= 30:
        return "sev3"
    return "sev4"


def label(pack: dict, failure_mode: str) -> dict:
    """Labels for one evidence pack, per RUBRIC.md."""
    pg = pack["raw"]["pg"]
    dt = pack["raw"]["dt"]
    e_pg = float(pg["error_rate_pct"])
    e = max(e_pg, float(dt["error_rate_pct"])) if pack["source"]["resolution"] == "DT-higher" else e_pg
    latency = float(pg["avg_response_time_ms"])
    client = float(pg["client_error_rate_pct"])
    n = int(pg["total_requests"])
    crit = bool(pack["critical"])

    if crit and e >= 50:
        severity = "sev1"
    else:
        severity = _severity_ignoring_volume(e, latency, client, crit)
        if n < 20 and severity == "sev1":
            severity = "sev2"

    if e >= 30 or latency >= 2000:
        impact = "widespread"
    elif e >= 5 or latency >= 1000 or client >= 30:
        impact = "partial"
    else:
        impact = "minimal"

    return {
        "severity": severity,
        "page_now": "A" if severity == "sev1" else "B",
        "user_impact": impact,
        "failure_mode": failure_mode if e_pg >= 1 else "other",
    }


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def sample_scenario(rng: random.Random, critical_services=("payment-service",)) -> dict:
    service = rng.choice(list(SERVICES))
    endpoint = SERVICES[service]
    n = max(2, int(round(10 ** rng.uniform(0.3, 3.7))))            # 2 .. ~5000 requests

    lo, hi = _weighted(rng, [(w, (a, b)) for w, a, b in ERROR_BANDS])
    errors = int(round(n * rng.uniform(lo, hi) / 100))
    client_rate = rng.uniform(0, 8) if rng.random() < 0.8 else rng.uniform(10, 45)
    client_errors = min(n - errors, int(round(n * client_rate / 100)))

    err_pct = errors / n * 100
    regime_weights = ([0.75, 0.2, 0.04, 0.01] if err_pct < 5 else
                      [0.35, 0.35, 0.2, 0.1] if err_pct < 30 else [0.15, 0.3, 0.3, 0.25])
    lat_lo, lat_hi = _weighted(rng, list(zip(regime_weights, LATENCY_REGIMES)))
    mean_latency = rng.uniform(lat_lo, lat_hi)

    modes = [m for m in FAILURE_TEXTS if m != "payment_gateway" or service == "payment-service"]
    failure_mode = rng.choice(modes)
    texts = FAILURE_TEXTS[failure_mode]

    # Raw rows, newest first, exactly as fetch_logs returns them.
    rows = []
    statuses = [500] * errors + [404] * client_errors + [200] * (n - errors - client_errors)
    rng.shuffle(statuses)
    for status in statuses:
        detail = rng.choice(texts) if status >= 500 else None
        rows.append({"endpoint": endpoint, "status_code": status,
                     "response_time_ms": max(1.0, rng.gauss(mean_latency, mean_latency * 0.15)),
                     "error_detail": detail})

    from tools import summarize_logs           # imported late: tools pulls in the DB module
    pg = summarize_logs(rows, 30)

    dt_case = _weighted(rng, DT_CASES)
    svc = pg["services"][0]
    if dt_case == "unavailable":
        dt = rng.choice([
            {"source": "dynatrace", "status": "error", "services": []},
            {"source": "dynatrace", "status": "not_configured", "services": []},
            {"source": "dynatrace", "status": "ok", "services": []},     # service missing
        ])
    else:
        shift = {"agreement": rng.uniform(-4, 4),
                 "lag": -rng.uniform(5, 30),
                 "higher": rng.uniform(5, 25)}[dt_case]
        dt_err = min(100.0, max(0.0, svc["error_rate_pct"] + shift))
        dt_total = max(1, int(round(n * rng.uniform(0.85, 1.0))))
        dt = {"source": "dynatrace", "status": "ok", "services": [{
            "service": service, "total_requests": dt_total,
            "error_count": int(round(dt_total * dt_err / 100)),
            "error_rate_pct": round(dt_err, 1),
            "avg_response_time_ms": round(svc["avg_response_time_ms"] * rng.uniform(0.7, 1.1), 1),
        }]}

    (pack,) = ev.build_evidence_packs(pg, dt, critical_services)
    return {"pack": pack, "labels": label(pack, failure_mode),
            "scenario": {"failure_mode": failure_mode, "dt_case": dt_case}}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def eval_row(example_id: str, item: dict, questions: dict) -> dict:
    labels = item["labels"]
    return {
        "id": example_id,
        "state": ev.laya_state(item["pack"]),
        "questions": questions,
        "expected": dict(labels),
        "tags": [f"sev:{labels['severity']}", f"dt:{item['scenario']['dt_case']}",
                 f"mode:{item['scenario']['failure_mode']}", f"service:{item['pack']['service']}"],
        "labels": labels,
        "pack": item["pack"],
    }


def notebook_row(example_id: str, item: dict, questions: dict) -> dict:
    """Row shape of LocalLLaMA/typed-decisions, as the fine-tuning notebook reads it:
    state / questions / gold are JSON strings; gold[qid] = {label, probabilities}."""
    gold = {}
    for qid, key in item["labels"].items():
        options = list(questions[qid]["criteria"])
        gold[qid] = {"label": key, "probabilities": {o: (1.0 if o == key else 0.0) for o in options}}
    return {
        "id": example_id,
        "workflow": WORKFLOW,
        "state": json.dumps(ev.laya_state(item["pack"]), ensure_ascii=False),
        "questions": json.dumps(questions, ensure_ascii=False),
        "gold": json.dumps(gold, ensure_ascii=False),
    }


def generate(counts: dict, seed: int, critical_services=("payment-service",)) -> dict:
    """{split: [item, ...]} with a separate deterministic stream per split."""
    out = {}
    for offset, (split, n) in enumerate(counts.items()):
        rng = random.Random(f"{seed}:{split}:{offset}")
        out[split] = [sample_scenario(rng, critical_services) for _ in range(n)]
    return out


def write(splits: dict, out_dir: Path) -> dict:
    questions = ev.load_triage_config()["questions"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {}
    for split, items in splits.items():
        with open(out_dir / f"{split}.jsonl", "w", encoding="utf-8") as f, \
             open(out_dir / f"notebook_{split}.jsonl", "w", encoding="utf-8") as nb:
            for i, item in enumerate(items):
                example_id = f"{WORKFLOW}-{split}-{i:05d}"
                f.write(json.dumps(eval_row(example_id, item, questions), ensure_ascii=False) + "\n")
                nb.write(json.dumps(notebook_row(example_id, item, questions), ensure_ascii=False) + "\n")
        dist = {}
        for item in items:
            dist[item["labels"]["severity"]] = dist.get(item["labels"]["severity"], 0) + 1
        stats[split] = {"n": len(items), "severity": dict(sorted(dist.items()))}
    return stats


def main():
    parser = argparse.ArgumentParser(description="Generate seeded synthetic triage scenarios.")
    parser.add_argument("--train", type=int, default=3000)
    parser.add_argument("--val", type=int, default=2500,
                        help="2000+ gives every question bucket its own fitted temperature")
    parser.add_argument("--test", type=int, default=300)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--critical", default="payment-service",
                        help="comma-separated CRITICAL_SERVICES used for the labels")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    critical = tuple(s.strip() for s in args.critical.split(",") if s.strip())
    splits = generate({"train": args.train, "val": args.val, "test": args.test}, args.seed, critical)
    stats = write(splits, args.out)
    for split, s in stats.items():
        print(f"{split:5s} {s['n']:5d}  severity {s['severity']}")
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
