"""Integration: the real Laya checkpoint classifies two synthetic services end to end.

Marked slow: loads ~840 MB of weights (downloaded on first use). Run with
    pytest -m slow
and skip with
    pytest -m "not slow"
"""

import pytest

from laya_config import load_config
from laya_triage import client
from laya_triage import evidence as ev
from tests.helpers import dt_payload, dt_service, pg_payload, pg_service

pytestmark = pytest.mark.slow


def test_real_checkpoint_classifies_two_services(monkeypatch):
    monkeypatch.setattr(client, "_router", None)
    monkeypatch.setattr(client, "_load_future", None)
    cfg = load_config({"LAYA_ENABLED": "true", "LAYA_TIMEOUT_S": "60",
                       "LAYA_CALIBRATION_FILE": "/nonexistent/calibration.json"})
    pg = pg_payload([
        pg_service("payment-service", total=400, errors=240, avg_ms=2600, client_errors=4,
                   samples=["Database connection pool exhausted - all threads blocked",
                            "Timeout acquiring connection from pool after 5000 ms"]),
        pg_service("api-gateway", total=400, errors=0, avg_ms=3),
    ])
    dt = dt_payload([dt_service("payment-service", total=380, err_pct=58.0, avg_ms=2500),
                     dt_service("api-gateway", total=390, err_pct=0.0, avg_ms=2)])
    packs = ev.build_evidence_packs(pg, dt, cfg.critical_services)

    out = client.classify(packs, cfg)

    assert out["status"] == "ok", out.get("error")
    assert out["backend"] == "in-process" and out["model"] == "typed-decisions"
    sick, healthy = (r["laya"] for r in out["results"])
    for laya in (sick, healthy):
        assert set(laya["p"]) == {"sev1", "sev2", "sev3", "sev4"}
        assert sum(laya["p"].values()) == pytest.approx(1.0, abs=1e-3)
        assert 1.0 <= laya["expected_sev"] <= 4.0
        assert set(laya["page_now"]) == {"yes", "no"}
        assert laya["truncated"] is False and laya["state_tokens"] <= ev.state_token_budget("typed-decisions")
    # Zero-shot Laya is near chance on exact labels, but the ordering should hold.
    assert sick["p"]["sev1"] > healthy["p"]["sev1"]
    assert sick["expected_sev"] < healthy["expected_sev"]
