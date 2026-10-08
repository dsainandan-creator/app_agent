import json

import pytest

from laya_triage import evidence as ev
from tests.helpers import dt_payload, dt_service, pg_payload, pg_service

CFG = ev.load_triage_config()


# ---------------------------------------------------------------- bands

@pytest.mark.parametrize("pct,label", [
    (0.0, "none"), (0.99, "none"), (1.0, "low"), (4.9, "low"), (5.0, "moderate"),
    (14.9, "moderate"), (15.0, "high"), (29.9, "high"), (30.0, "severe"), (100.0, "severe"),
])
def test_server_error_bands(pct, label):
    assert ev.band_label("server_error_rate", pct, CFG) == label


@pytest.mark.parametrize("ms,label", [
    (0, "normal"), (299.9, "normal"), (300, "elevated"), (999, "elevated"),
    (1000, "high"), (1999, "high"), (2000, "severe"), (9000, "severe"),
])
def test_latency_bands(ms, label):
    assert ev.band_label("latency", ms, CFG) == label


@pytest.mark.parametrize("n,label", [
    (0, "very_low"), (19, "very_low"), (20, "low"), (99, "low"),
    (100, "normal"), (999, "normal"), (1000, "high"),
])
def test_volume_bands(n, label):
    assert ev.band_label("request_volume", n, CFG) == label


def test_client_error_bands_match_server_thresholds():
    assert ev.band_label("client_error_rate", 4.0, CFG) == "low"
    assert ev.band_label("client_error_rate", 31.0, CFG) == "severe"


# ---------------------------------------------------------------- resolution

@pytest.mark.parametrize("pg,dt,label,gap", [
    (20.0, 20.0, "agreement", False),
    (20.0, 24.9, "agreement", False),     # < 5 pp
    (20.0, 15.1, "agreement", False),
    (20.0, 25.0, "DT-higher", False),     # exactly 5 pp is not agreement
    (20.0, 15.0, "DT-lag", False),
    (40.0, 25.0, "DT-lag", True),         # 15 pp gap
    (10.0, 30.0, "DT-higher", True),
])
def test_resolution_labels(pg, dt, label, gap):
    r = ev.resolve_sources(pg, dt_service("x", err_pct=dt), "ok", CFG)
    assert r["resolution"] == label
    assert r["large_gap"] is gap
    assert r["delta_err_pp"] == round(dt - pg, 1)


@pytest.mark.parametrize("dt_svc,status", [
    (None, "ok"),                                  # service absent from Dynatrace
    (dt_service("x", total=0), "ok"),              # no Dynatrace traffic
    (dt_service("x", err_pct=20.0), "error"),      # query failed
    (dt_service("x", err_pct=20.0), "not_configured"),
])
def test_resolution_unavailable(dt_svc, status):
    r = ev.resolve_sources(20.0, dt_svc, status, CFG)
    assert r == {"resolution": "DT-unavailable", "delta_err_pp": None, "large_gap": False}


# ---------------------------------------------------------------- packs

def _packs(dt=None):
    pg = pg_payload([
        pg_service("payment-service", total=200, errors=80, avg_ms=2400, client_errors=6,
                   samples=["Database connection pool exhausted", "Payment gateway timeout"]),
        pg_service("api-gateway", total=50, errors=0, avg_ms=2),
    ])
    return ev.build_evidence_packs(pg, dt, critical_services=("payment-service",), cfg=CFG)


def test_pack_bands_raw_and_criticality():
    dt = dt_payload([dt_service("payment-service", total=190, err_pct=38.0, avg_ms=2300)])
    pay, gw = _packs(dt)
    assert pay["critical"] is True and gw["critical"] is False
    assert pay["bands"]["server_error_rate"] == "severe"
    assert pay["bands"]["latency"] == "severe"
    assert pay["bands"]["request_volume"] == "normal"
    assert pay["bands"]["client_error_rate"] == "low"
    assert pay["bands"]["dt_server_error_rate"] == "severe"
    assert pay["source"]["resolution"] == "agreement"       # 38% vs 40%: -2 pp
    assert pay["raw"]["pg"]["error_rate_pct"] == 40.0
    assert pay["raw"]["dt"]["error_rate_pct"] == 38.0
    # api-gateway is missing from Dynatrace
    assert gw["source"]["resolution"] == "DT-unavailable"
    assert gw["raw"]["dt"] is None and gw["bands"]["dt_latency"] is None


def test_packs_accept_json_strings_and_missing_dt():
    pg = json.dumps(pg_payload([pg_service("user-service", errors=3)]))
    (pack,) = ev.build_evidence_packs(pg, None, cfg=CFG)
    assert pack["source"]["resolution"] == "DT-unavailable"
    assert pack["source"]["dt_status"] == "not_configured"


def test_samples_capped_and_trimmed():
    long = "x" * 500
    pg = pg_payload([pg_service("s", errors=5, samples=[long, "a", "b", "c", "d", "e"])])
    (pack,) = ev.build_evidence_packs(pg, None, cfg=CFG)
    assert len(pack["error_samples"]) == CFG["max_error_samples"]
    assert len(pack["error_samples"][0]) == CFG["max_sample_chars"]


def test_laya_state_has_bands_not_numbers():
    pay, _ = _packs(dt_payload([dt_service("payment-service", total=190, err_pct=40.0)]))
    state = ev.laya_state(pay, CFG)
    text = ev.serialize_state(state)
    assert state["criticality"].startswith("critical")
    assert state["server_errors"].startswith("severe")
    assert state["monitoring_sources"] == CFG["resolution"]["text"]["agreement"]
    assert state["recent_errors"] == ["Database connection pool exhausted", "Payment gateway timeout"]
    # raw counts and the exact rate never reach Laya
    assert "200" not in text and "40.0" not in text and "2400" not in text


def test_laya_state_large_gap_and_no_samples():
    pg = pg_payload([pg_service("s", total=100, errors=40)])
    dt = dt_payload([dt_service("s", err_pct=10.0)])
    (pack,) = ev.build_evidence_packs(pg, dt, cfg=CFG)
    state = ev.laya_state(pack, CFG)
    assert state["monitoring_sources"].endswith(CFG["resolution"]["large_gap_text"])
    assert state["recent_errors"] == ["none recorded"]


# ---------------------------------------------------------------- token budget

def test_state_budget_per_model():
    assert ev.state_token_budget("typed-decisions", CFG) == 1024 - 272
    assert ev.state_token_budget("english", CFG) == 512 - 272


def test_fit_state_keeps_small_state_unchanged():
    state = {"service": "s", "recent_errors": ["a", "b"]}
    out, info = ev.fit_state_to_budget(state, "typed-decisions", cfg=CFG)
    assert out == state and info["samples_dropped"] == 0


def test_fit_state_drops_samples_until_it_fits():
    state = {"service": "s", "recent_errors": ["e1", "e2", "e3", "e4", "e5"]}
    # 1 token per character; budget 240 for english
    count = lambda text: len(text) + (300 if "e3" in text else 0)
    out, info = ev.fit_state_to_budget(state, "english", count_tokens=count, cfg=CFG)
    assert out["recent_errors"] == ["e1", "e2"]
    assert info["samples_dropped"] == 3
    assert info["state_tokens"] <= info["budget"]
    assert state["recent_errors"] == ["e1", "e2", "e3", "e4", "e5"]   # input not mutated


def test_fit_state_asserts_when_base_state_is_too_big():
    with pytest.raises(AssertionError):
        ev.fit_state_to_budget({"service": "s" * 5000, "recent_errors": []}, "english", cfg=CFG)
