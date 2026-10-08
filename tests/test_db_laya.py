"""laya_triage table against the separate observability_test database (never 'observability')."""

import psycopg2
import pytest

from mock_app import database as db

TEST_DB = "observability_test"


@pytest.fixture
def test_db(monkeypatch):
    cfg = dict(db.DB_CONFIG, dbname=TEST_DB)
    try:
        psycopg2.connect(**cfg).close()
    except psycopg2.OperationalError:
        pytest.skip(f"{TEST_DB} database not available")
    monkeypatch.setattr(db, "DB_CONFIG", cfg)
    monkeypatch.setattr(db, "_laya_table_ready", False)
    assert db.get_connection().info.dbname == TEST_DB
    return cfg


def test_init_db_is_idempotent_and_creates_laya_triage(test_db):
    db.init_db()
    db.init_db()
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'laya_triage'")
    cols = {r[0] for r in cur.fetchall()}
    conn.close()
    assert {"run_id", "service", "mode", "model", "evidence", "answers",
            "expected_sev", "confidence", "latency_ms", "created_at"} <= cols


def test_save_and_fetch_round_trip(test_db):
    import uuid
    run_id = "test-" + uuid.uuid4().hex[:8]
    result = {"service": "payment-service", "status": "ok",
              "laya": {"severity": "sev1", "p": {"sev1": 0.9}, "expected_sev": 1.2,
                       "confidence": 0.6, "latency_ms": 40.0}}
    db.save_laya_triage(run_id, "shadow", "typed-decisions", {"service": "payment-service"}, result)
    db.save_laya_triage(run_id, "shadow", "typed-decisions", {"service": "api-gateway"},
                        {"service": "api-gateway", "status": "unavailable"})
    rows = db.fetch_laya_triage(run_id)
    assert set(rows) == {"payment-service", "api-gateway"}
    assert rows["payment-service"]["answers"]["laya"]["p"]["sev1"] == 0.9
    assert rows["api-gateway"]["answers"]["status"] == "unavailable"
    assert rows["payment-service"]["mode"] == "shadow"


def test_alerts_run_id_and_fetch_run_alerts(test_db):
    import uuid
    db.init_db()
    run_id = "test-" + uuid.uuid4().hex[:8]
    db.save_alert(2, "order-service", "held", "PAGE_HELD", status="HELD", run_id=run_id)
    db.save_alert(None, "order-service", "decision", "POLICY_SHADOW", status="PAGE_HELD", run_id=run_id)
    db.save_alert(4, "api-gateway", "no run id", "RAISE_ALERT")        # old call shape still works
    rows = db.fetch_run_alerts(run_id, ("PAGE_HELD", "POLICY_SHADOW"))
    assert [(r["alert_type"], r["status"]) for r in rows] == [("PAGE_HELD", "HELD"), ("POLICY_SHADOW", "PAGE_HELD")]
    assert db.fetch_run_alerts(run_id, ("PAGE_HELD",))[0]["severity"] == 2
