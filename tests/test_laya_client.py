import concurrent.futures as cf
import json
import math
import time

import pytest

from laya_config import load_config
from laya_triage import client
from laya_triage import evidence as ev
from tests.helpers import pg_payload, pg_service

TRIAGE = ev.load_triage_config()


def _answers(sev=(0.7, 0.2, 0.05, 0.05), page=(0.8, 0.2)):
    return {
        "severity": {"type": "choice", "confidence": 0.5,
                     "probabilities": dict(zip(("sev1", "sev2", "sev3", "sev4"), sev))},
        "user_impact": {"type": "choice", "probabilities": {"widespread": 0.6, "partial": 0.3, "minimal": 0.1}},
        "failure_mode": {"type": "choice", "probabilities": {
            "db_pool": 0.5, "upstream_timeout": 0.2, "payment_gateway": 0.1,
            "auth": 0.1, "resource_exhaustion": 0.05, "other": 0.05}},
        "page_now": {"type": "choice", "probabilities": {"A": page[0], "B": page[1]}},
    }


def _packs():
    pg = pg_payload([pg_service("payment-service", errors=60), pg_service("api-gateway")])
    return ev.build_evidence_packs(pg, None, ("payment-service",))


def _cfg(**env):
    return load_config({"LAYA_ENABLED": "true", "LAYA_CALIBRATION_FILE": "/nonexistent.json", **env})


# ---------------------------------------------------------------- normalisation

def test_normalise_answers_derives_expected_sev_and_margin():
    out = client.normalise_answers(_answers(), TRIAGE)
    assert out["severity"] == "sev1"
    assert out["expected_sev"] == pytest.approx(1 * 0.7 + 2 * 0.2 + 3 * 0.05 + 4 * 0.05)
    assert out["margin"] == pytest.approx(0.5)
    assert out["answer_confidence"] == pytest.approx(0.7)
    assert out["confidence"] == 0.5                       # Laya's own field, passed through
    assert out["page_now"] == {"yes": 0.8, "no": 0.2}     # A/B mapped to yes/no
    assert out["failure_mode"] == {"choice": "db_pool", "p": 0.5}
    assert out["user_impact"] == {"choice": "widespread", "p": 0.6}


# ---------------------------------------------------------------- calibration maths

def _softmax(z, t):
    e = [math.exp(v / t) for v in z]
    return [v / sum(e) for v in e]


def test_retemper_matches_softmax_at_fitted_temperature():
    z = [2.0, 0.5, -1.0, 0.1]
    t_ship, t_fit = 1.76, 2.5
    served = dict(zip("abcd", _softmax(z, t_ship)))
    cal = {"temperature": [1.0, 1.0, 1.0], "temperature_by_options": {"choice:3-5": t_fit},
           "shipped": {"temperature": [1.0, 1.0, 1.0], "temperature_by_options": {"choice:3-5": t_ship}}}
    out = client.retemper(served, "choice", cal)
    for got, want in zip(out.values(), _softmax(z, t_fit)):
        assert got == pytest.approx(want, rel=1e-6)


def test_retemper_falls_back_to_type_temperature_and_noop_without_shipped():
    z = [1.0, -1.0]
    served = dict(zip("AB", _softmax(z, 1.9)))
    cal = {"temperature": [3.0, 1.0, 1.0], "temperature_by_options": {},
           "shipped": {"temperature": [1.0, 1.0, 1.0], "temperature_by_options": {"choice:2": 1.9}}}
    out = client.retemper(served, "choice", cal)
    assert list(out.values()) == pytest.approx(_softmax(z, 3.0))
    assert client.retemper(served, "choice", {"temperature": [3, 1, 1]}) == served


def test_bucket_names_match_laya():
    from laya.common import temp_bucket
    for k in (2, 3, 4, 5, 6, 10, 11, 20):
        assert client._bucket("choice", k) == temp_bucket(0, k)


# ---------------------------------------------------------------- fallbacks

def test_unavailable_when_backend_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("weights missing")
    monkeypatch.setattr(client, "warmup", lambda cfg: _done_future(None))
    monkeypatch.setattr(client, "_predict_in_process", boom)
    out = client.classify(_packs(), _cfg())
    assert out["status"] == "unavailable"
    assert "weights missing" in out["error"]
    assert [r["status"] for r in out["results"]] == ["unavailable", "unavailable"]
    assert [r["service"] for r in out["results"]] == ["payment-service", "api-gateway"]


def test_unavailable_on_inference_timeout(monkeypatch):
    class SlowRouter:
        def predict_batch(self, requests):
            time.sleep(1.0)
    monkeypatch.setattr(client, "warmup", lambda cfg: _done_future(SlowRouter()))
    out = client.classify(_packs(), _cfg(LAYA_TIMEOUT_S="0.05"))
    assert out["status"] == "unavailable" and "timed out" in out["error"]


def test_unavailable_when_http_server_down():
    out = client.classify(_packs(), _cfg(LAYA_BASE_URL="http://127.0.0.1:9"))
    assert out["status"] == "unavailable" and out["backend"] == "http"


def test_ok_path_with_fake_router(monkeypatch):
    class FakeRouter:
        def predict_batch(self, requests):
            assert len(requests) == 2 and all(r["model"] == "typed-decisions" for r in requests)
            assert requests[0]["questions"] is TRIAGE["questions"]
            return [{"answers": _answers(), "usage": {"truncated": False}},
                    {"answers": _answers(sev=(0.05, 0.05, 0.2, 0.7), page=(0.1, 0.9))}]
    monkeypatch.setattr(client, "warmup", lambda cfg: _done_future(FakeRouter()))
    monkeypatch.setattr(client, "_router", None)
    out = client.classify(_packs(), _cfg())
    assert out["status"] == "ok" and out["calibrated"] is False
    pay, gw = out["results"]
    assert pay["laya"]["severity"] == "sev1" and gw["laya"]["severity"] == "sev4"
    assert gw["laya"]["page_now"]["yes"] == 0.1
    assert pay["state"]["criticality"].startswith("critical")


def test_empty_packs_is_ok():
    assert client.classify([], _cfg())["status"] == "ok"


def _done_future(value):
    f = cf.Future()
    f.set_result(value)
    return f
