import pytest

import policy
from laya_config import load_config
from policy import NO_PAGE, PAGE, PAGE_FORCED, PAGE_HELD

CFG = load_config({"LAYA_ENABLED": "true"})   # HIGH .80, LOW .40, floor 50%, min 20, payment-service


def pack(service="order-service", total=200, err_pct=10.0, resolution="agreement"):
    return {"service": service,
            "raw": {"pg": {"total_requests": total, "error_rate_pct": err_pct}},
            "source": {"resolution": resolution}}


def laya(p_sev1):
    return {"p": {"sev1": p_sev1, "sev2": 0.0, "sev3": 0.0, "sev4": 1 - p_sev1}}


# ---------------------------------------------------------------- the spec's table, row by row

@pytest.mark.parametrize("p,gemini,outcome,rule,reason_bit", [
    (0.85, True,  PAGE,        "laya_high",        "models agree"),
    (0.85, False, PAGE_FORCED, "laya_high",        "Gemini did not page"),
    (0.60, True,  PAGE,        "laya_mid",         "model disagreement"),
    (0.20, True,  PAGE_HELD,   "laya_low",         "held page, needs human review"),
    (None, True,  PAGE,        "laya_unavailable", "Laya unavailable"),
])
def test_policy_table_rows(p, gemini, outcome, rule, reason_bit):
    d = policy.decide(gemini, None if p is None else laya(p), pack(), CFG)
    assert (d.outcome, d.rule) == (outcome, rule)
    assert reason_bit in d.reason
    assert d.laya_p_sev1 == p and d.gemini_paged is gemini


@pytest.mark.parametrize("p,outcome", [(0.60, NO_PAGE), (0.20, NO_PAGE), (None, NO_PAGE)])
def test_no_page_when_gemini_does_not_page_and_laya_is_not_high(p, outcome):
    assert policy.decide(False, None if p is None else laya(p), pack(), CFG).outcome == outcome


@pytest.mark.parametrize("p,gemini,outcome", [
    (0.80, True, PAGE), (0.80, False, PAGE_FORCED),        # HIGH is inclusive
    (0.7999, False, NO_PAGE),
    (0.40, True, PAGE),                                     # LOW is inclusive (disagreement)
    (0.3999, True, PAGE_HELD),
])
def test_threshold_boundaries(p, gemini, outcome):
    assert policy.decide(gemini, laya(p), pack(), CFG).outcome == outcome


# ---------------------------------------------------------------- hard floor

@pytest.mark.parametrize("resolution", ["agreement", "DT-higher", "DT-unavailable"])
@pytest.mark.parametrize("gemini,outcome", [(True, PAGE), (False, PAGE_FORCED)])
def test_hard_floor_always_pages(resolution, gemini, outcome):
    p = pack("payment-service", err_pct=60.0, resolution=resolution)
    for laya_result in (laya(0.01), laya(0.99), None):        # whatever Laya says
        d = policy.decide(gemini, laya_result, p, CFG)
        assert d.outcome == outcome and d.rule == "hard_floor" and d.hard_floor


@pytest.mark.parametrize("p", [
    pack("payment-service", err_pct=60.0, resolution="DT-lag"),   # Dynatrace disagrees
    pack("order-service", err_pct=60.0),                           # not critical
    pack("payment-service", err_pct=49.9),                         # below the floor
])
def test_hard_floor_does_not_apply(p):
    d = policy.decide(True, laya(0.1), p, CFG)
    assert not d.hard_floor and d.outcome == PAGE_HELD


def test_hard_floor_overrides_min_volume():
    d = policy.decide(False, laya(0.0), pack("payment-service", total=3, err_pct=66.7), CFG)
    assert d.outcome == PAGE_FORCED and d.below_min_volume and d.hard_floor


# ---------------------------------------------------------------- minimum volume

def test_the_false_page_case_is_held():
    """SEV-1 page on 1 error out of 2 requests (order-service, 2026-10-07)."""
    d = policy.decide(True, laya(0.95), pack("order-service", total=2, err_pct=50.0), CFG)
    assert d.outcome == PAGE_HELD and d.rule == "min_volume"
    assert "2 requests < PAGE_MIN_REQUESTS=20" in d.reason


@pytest.mark.parametrize("total,outcome", [(0, PAGE_HELD), (19, PAGE_HELD), (20, PAGE)])
def test_min_volume_boundary(total, outcome):
    assert policy.decide(True, laya(0.95), pack(total=total), CFG).outcome == outcome


def test_min_volume_blocks_forced_page():
    assert policy.decide(False, laya(0.99), pack(total=5), CFG).outcome == NO_PAGE


def test_min_volume_threshold_is_configurable():
    cfg = load_config({"LAYA_ENABLED": "true", "PAGE_MIN_REQUESTS": "3"})
    assert policy.decide(True, laya(0.95), pack(total=5), cfg).outcome == PAGE


# ---------------------------------------------------------------- calibration before enforcement

def test_enforce_without_calibration_runs_as_advisory(capsys):
    cfg = load_config({"LAYA_ENABLED": "true", "LAYA_MODE": "enforce",
                       "LAYA_CALIBRATION_FILE": "/nonexistent/calibration.json"})
    assert cfg.effective_mode() == "advisory"
    assert "Running as advisory" in capsys.readouterr().err


def test_enforce_with_calibration_stays_enforce(tmp_path):
    cal = tmp_path / "calibration.json"
    cal.write_text("{}")
    cfg = load_config({"LAYA_ENABLED": "true", "LAYA_MODE": "enforce", "LAYA_CALIBRATION_FILE": str(cal)})
    assert cfg.effective_mode() == "enforce"
