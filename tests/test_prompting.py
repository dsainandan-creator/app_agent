import json

from laya_triage import evidence as ev
from laya_triage import prompting
from tests.helpers import dt_payload, dt_service, pg_payload, pg_service

PG = pg_payload([pg_service("payment-service", total=100, errors=60, samples=["DB pool exhausted"]),
                 pg_service("api-gateway", total=50)])
DT = dt_payload([dt_service("payment-service", err_pct=58.0)])
PACKS = ev.build_evidence_packs(PG, DT, ("payment-service",))

LAYA_OK = {"severity": "sev1", "p": {"sev1": 0.82, "sev2": 0.1, "sev3": 0.05, "sev4": 0.03},
           "expected_sev": 1.29, "margin": 0.72, "page_now": {"yes": 0.9, "no": 0.1},
           "failure_mode": {"choice": "db_pool", "p": 0.7}, "user_impact": {"choice": "widespread", "p": 0.6},
           "calibrated": False, "confidence": 0.4, "latency_ms": 30}
TRIAGE = {"status": "ok", "model": "typed-decisions", "calibrated": False, "latency_ms": 120,
          "results": [{"service": "payment-service", "status": "ok", "laya": LAYA_OK},
                      {"service": "api-gateway", "status": "unavailable"}]}


def test_verdicts_join_evidence_and_laya():
    pay, gw = prompting.laya_verdicts(PACKS, TRIAGE)
    assert pay["laya"]["severity"] == "sev1" and pay["laya"]["p"]["sev1"] == 0.82
    assert pay["source_resolution"] == "agreement" and pay["critical"] is True
    assert pay["recent_errors"] == ["DB pool exhausted"]
    assert gw["laya_status"] == "unavailable" and "laya" not in gw
    assert "dt_server_error_rate" not in gw["bands"]          # None bands dropped


def test_advisory_message_carries_data_and_columns():
    msg = prompting.advisory_user_message(30, PG, DT, PACKS, TRIAGE)
    assert "Laya SEV (p)" in msg and "Agent SEV" in msg
    payload = json.loads(msg[msg.index("{"):])
    assert payload["get_logs"] == PG and payload["dt_search_logs"] == DT
    assert payload["laya_triage"]["services"][0]["laya"]["severity"] == "sev1"


def test_addendum_has_the_rules():
    a = prompting.LAYA_PROMPT_ADDENDUM
    for needle in ("calibrated prior", "Confirm Laya's verdict or override it",
                   "Quote Laya's probabilities", "Laya SEV (p)", "Agent SEV", "unavailable",
                   '"enforced": false', "page sent; policy would"):
        assert needle in a


def test_unavailable_triage_covers_every_pack():
    t = prompting.unavailable_triage(PACKS, "typed-decisions", "boom")
    assert t["status"] == "unavailable" and t["error"] == "boom"
    assert [r["service"] for r in t["results"]] == ["payment-service", "api-gateway"]


def test_triage_table_text():
    assert "unavailable" in prompting.triage_table({"status": "unavailable", "error": "x"})
    text = prompting.triage_table(TRIAGE)
    assert "payment-service" in text and "P(sev1)=0.82" in text and "api-gateway" in text
