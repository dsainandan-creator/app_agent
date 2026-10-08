import json
import random

import numpy as np
import pytest

from laya_triage import evidence as ev
from laya_triage.training import evaluate as evl
from laya_triage.training import generate_scenarios as gen
from laya_triage.training import shadow_report
from laya_triage.training.calibrate import one_hot_targets

QUESTIONS = ev.load_triage_config()["questions"]


def _pack(service="order-service", total=200, e_pg=0.0, latency=100.0, client=0.0,
          resolution="agreement", e_dt=None, critical=False):
    return {"service": service, "critical": critical,
            "raw": {"pg": {"total_requests": total, "error_rate_pct": e_pg, "avg_response_time_ms": latency,
                           "client_error_rate_pct": client},
                    "dt": None if e_dt is None else {"error_rate_pct": e_dt}},
            "source": {"resolution": resolution}}


# ---------------------------------------------------------------- rubric

@pytest.mark.parametrize("kw,severity", [
    (dict(e_pg=0.5), "sev4"),
    (dict(e_pg=6), "sev3"),
    (dict(latency=400), "sev3"),
    (dict(client=35), "sev3"),
    (dict(e_pg=16), "sev2"),
    (dict(latency=1200), "sev2"),
    (dict(e_pg=6, critical=True, service="payment-service"), "sev2"),
    (dict(e_pg=31), "sev1"),
    (dict(e_pg=6, latency=2500), "sev1"),
    (dict(e_pg=16, critical=True, service="payment-service"), "sev1"),
    (dict(e_pg=31, total=10), "sev2"),                                            # low volume: no sev1
    (dict(e_pg=60, total=10, critical=True, service="payment-service"), "sev1"),  # hard floor
    (dict(e_pg=10, e_dt=35, resolution="DT-higher"), "sev1"),                     # more alarming signal
    (dict(e_pg=10, e_dt=0, resolution="DT-lag"), "sev3"),                         # lag: trust PG
])
def test_rubric_severity(kw, severity):
    labels = gen.label(_pack(**kw), "db_pool")
    assert labels["severity"] == severity
    assert labels["page_now"] == ("A" if severity == "sev1" else "B")


def test_rubric_impact_and_failure_mode():
    assert gen.label(_pack(e_pg=40), "auth")["user_impact"] == "widespread"
    assert gen.label(_pack(e_pg=8), "auth")["user_impact"] == "partial"
    assert gen.label(_pack(e_pg=0.2), "auth")["user_impact"] == "minimal"
    assert gen.label(_pack(e_pg=8), "auth")["failure_mode"] == "auth"
    assert gen.label(_pack(e_pg=0.2), "auth")["failure_mode"] == "other"     # no meaningful 5xx


def test_rubric_labels_are_valid_options():
    for item in gen.generate({"x": 200}, seed=1)["x"]:
        for qid, key in item["labels"].items():
            assert key in QUESTIONS[qid]["criteria"]


# ---------------------------------------------------------------- generator

def test_generator_is_seeded_and_splits_differ():
    a = gen.generate({"train": 30, "test": 30}, seed=3)
    b = gen.generate({"train": 30, "test": 30}, seed=3)
    c = gen.generate({"train": 30, "test": 30}, seed=4)
    assert json.dumps(a, default=str) == json.dumps(b, default=str)
    assert json.dumps(a, default=str) != json.dumps(c, default=str)
    assert a["train"][0]["pack"] != a["test"][0]["pack"]


def test_generator_covers_cases():
    items = gen.generate({"x": 600}, seed=7)["x"]
    sev = {i["labels"]["severity"] for i in items}
    dt = {i["scenario"]["dt_case"] for i in items}
    res = {i["pack"]["source"]["resolution"] for i in items}
    assert sev == {"sev1", "sev2", "sev3", "sev4"}
    assert dt == {"agreement", "lag", "higher", "unavailable"}
    assert {"agreement", "DT-lag", "DT-higher", "DT-unavailable"} <= res
    assert all(i["pack"]["service"] == "payment-service"
               for i in items if i["scenario"]["failure_mode"] == "payment_gateway")


def test_output_rows(tmp_path):
    stats = gen.write(gen.generate({"train": 5, "test": 3}, seed=1), tmp_path)
    assert stats["train"]["n"] == 5
    from laya.evals import Dataset
    ds = Dataset.from_jsonl(str(tmp_path / "test.jsonl"))
    assert len(ds) == 3 and set(ds.examples[0].expected) == set(QUESTIONS)
    nb = json.loads((tmp_path / "notebook_train.jsonl").read_text().splitlines()[0])
    assert set(nb) == {"id", "workflow", "state", "questions", "gold"}
    gold, questions = json.loads(nb["gold"]), json.loads(nb["questions"])
    for qid, g in gold.items():
        assert list(g["probabilities"]) == list(questions[qid]["criteria"])
        assert g["probabilities"][g["label"]] == 1.0 and sum(g["probabilities"].values()) == 1.0
    assert isinstance(json.loads(nb["state"]), dict)


def test_one_hot_targets_follow_criteria_order():
    row = {"questions": QUESTIONS, "expected": {"severity": "sev3", "page_now": "A"}}
    t = one_hot_targets(row)
    assert t["severity"] == [0.0, 0.0, 1.0, 0.0] and t["page_now"] == [1.0, 0.0]


# ---------------------------------------------------------------- calibration maths (Laya's fitter)

def test_fit_temperature_map_recovers_known_temperature():
    from laya.calibrate import fit_temperature_map
    rng = np.random.default_rng(0)
    t_true, records = 2.5, []
    for _ in range(3000):                      # >= MIN_BUCKET_N for choice:3-5
        z = rng.normal(0, 3, size=4)
        p = np.exp(z / t_true); p /= p.sum()
        y = rng.choice(4, p=p)
        records.append((0, z, np.eye(4)[y], 4))
    fitted = fit_temperature_map(records)
    assert fitted["temperature_by_options"]["choice:3-5"] == pytest.approx(t_true, rel=0.12)
    assert fitted["n_by_bucket"]["choice:3-5"] == 3000


def test_small_buckets_fall_back_to_type_scalar():
    from laya.calibrate import fit_temperature_map
    rng = np.random.default_rng(1)
    recs = [(0, rng.normal(size=6), np.eye(6)[0], 6) for _ in range(50)]
    fitted = fit_temperature_map(recs)
    assert "choice:6-10" not in fitted["temperature_by_options"]
    assert fitted["temperature"][0] != 1.0


# ---------------------------------------------------------------- evaluation helpers

def test_paging_rates_and_suggestions():
    p = [0.9, 0.7, 0.3, 0.85, 0.2, 0.1]
    y = [True, True, True, False, False, False]
    r = evl.paging_rates(p, y, high=0.8, low=0.4)
    assert r["sev1_recall_at_high"] == pytest.approx(1 / 3)
    assert r["missed_sev1_rate_at_low"] == pytest.approx(1 / 3)
    assert r["false_page_rate_at_high"] == pytest.approx(1 / 3)
    rows = evl.sweep(p, y)
    s = evl.suggest(rows)
    assert s["high"]["t"] == 0.9 and s["high"]["false_page"] == 0.0
    assert s["low"]["t"] == 0.3 and s["low"]["miss"] == 0.0


# ---------------------------------------------------------------- shadow report

def test_shadow_report_build():
    import datetime as dt
    now = dt.datetime(2026, 10, 8, 9, 0)
    triage = [
        {"run_id": "r1", "service": "a", "mode": "shadow", "created_at": now, "status": "ok", "laya_sev": "sev2", "p_sev1": 0.2},
        {"run_id": "r1", "service": "b", "mode": "shadow", "created_at": now, "status": "ok", "laya_sev": "sev1", "p_sev1": 0.9},
        {"run_id": "r1", "service": "c", "mode": "shadow", "created_at": now, "status": "unavailable", "laya_sev": None, "p_sev1": None},
    ]
    gemini = {("r1", "a"): 2, ("r1", "b"): 3}
    decisions = [{"run_id": "r1", "service": "b", "alert_type": "POLICY_SHADOW", "status": "PAGE_FORCED",
                  "message": "[advisory] PAGE_FORCED (x)", "timestamp": now}]
    md = shadow_report.build(7, triage, gemini, decisions)
    assert "exact 50.0% (1/2), within one level 50.0%" in md
    assert "| r1 | b | shadow | SEV-3 | SEV-1 | 0.90 |" in md
    assert "would-be PAGE_FORCED" in md and "| r1 | b | advisory |" in md
    assert "1 unavailable" in md
