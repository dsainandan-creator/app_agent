"""call_on_call_engineer, send_email_notification and raise_alert with the paging policy.
Slack and the database are mocked: notify_slack is replaced, save_alert records calls."""

import json

import pytest

import tools
from tests.test_policy import laya, pack

RUN = "run-1"


@pytest.fixture
def env(monkeypatch):
    state = {"slack": [], "alerts": [], "rows": {}}

    def fake_slack(service, message, context=None):
        state["slack"].append({"service": service, "message": message, "context": context})
        return {"incident_id": "INC-test", "status": "DRY_RUN", "service": service, "message": message}

    monkeypatch.setattr(tools, "notify_slack", fake_slack)
    monkeypatch.setattr(tools, "save_alert", lambda **kw: state["alerts"].append(kw))
    monkeypatch.setattr(tools, "fetch_laya_triage", lambda run_id: state["rows"] if run_id == RUN else {})
    monkeypatch.setattr(tools, "fetch_run_alerts", lambda run_id, types: [
        a for a in state["alerts"] if a.get("run_id") == run_id and a["alert_type"] in types])
    monkeypatch.setenv("LAYA_ENABLED", "true")
    return state


def triage_row(mode, p_sev1, **pack_kw):
    return {"mode": mode, "model": "typed-decisions", "evidence": pack(**pack_kw),
            "answers": {"status": "ok", "laya": laya(p_sev1)} if p_sev1 is not None
            else {"status": "unavailable"}}


def types_of(state):
    return [a["alert_type"] for a in state["alerts"]]


# ---------------------------------------------------------------- no Laya: unchanged

def test_without_run_id_pages_as_before(env):
    out = json.loads(tools.call_on_call_engineer("order-service", "down"))
    assert out["status"] == "DRY_RUN" and "policy" not in out
    assert len(env["slack"]) == 1 and env["slack"][0]["context"] is None
    assert types_of(env) == ["ONCALL"]


def test_run_without_triage_rows_pages_as_before(env):
    out = json.loads(tools.call_on_call_engineer("order-service", "down", run_id="other-run"))
    assert "policy" not in out and types_of(env) == ["ONCALL"]


# ---------------------------------------------------------------- the three modes, PAGE_HELD case

def test_shadow_records_decision_and_still_pages_unchanged(env):
    env["rows"]["order-service"] = triage_row("shadow", 0.1)
    out = json.loads(tools.call_on_call_engineer("order-service", "down", run_id=RUN))
    assert "policy" not in out                              # Gemini sees today's result
    assert len(env["slack"]) == 1 and env["slack"][0]["context"] is None
    shadow = [a for a in env["alerts"] if a["alert_type"] == "POLICY_SHADOW"]
    assert len(shadow) == 1 and shadow[0]["status"] == "PAGE_HELD" and shadow[0]["run_id"] == RUN
    assert "ONCALL" in types_of(env)


def test_advisory_records_decision_pages_and_shows_it(env):
    env["rows"]["order-service"] = triage_row("advisory", 0.1)
    out = json.loads(tools.call_on_call_engineer("order-service", "down", run_id=RUN))
    assert out["policy"]["outcome"] == "PAGE_HELD" and out["policy"]["enforced"] is False
    assert len(env["slack"]) == 1 and env["slack"][0]["context"]["laya_p_sev1"] == 0.1
    assert "POLICY_SHADOW" in types_of(env) and "PAGE_HELD" not in types_of(env)
    oncall = next(a for a in env["alerts"] if a["alert_type"] == "ONCALL")
    assert "not enforced" in oncall["message"]


def test_enforce_holds_the_page(env):
    env["rows"]["order-service"] = triage_row("enforce", 0.1)
    out = json.loads(tools.call_on_call_engineer("order-service", "down", run_id=RUN))
    assert out["status"] == "PAGE_HELD" and out["policy"]["enforced"] is True
    assert env["slack"] == []                                # no Slack page
    held = next(a for a in env["alerts"] if a["alert_type"] == "PAGE_HELD")
    assert held["severity"] == 2 and "held page, needs human review" in held["message"]
    assert "POLICY_ENFORCED" in types_of(env) and "ONCALL" not in types_of(env)


def test_enforce_page_carries_policy_context(env):
    env["rows"]["order-service"] = triage_row("enforce", 0.9)
    out = json.loads(tools.call_on_call_engineer("order-service", "down", run_id=RUN))
    ctx = env["slack"][0]["context"]
    assert ctx["laya_p_sev1"] == 0.9 and ctx["gemini_severity"].startswith("SEV-1")
    assert ctx["policy"].startswith("PAGE (models agree")
    assert out["policy"]["outcome"] == "PAGE"


def test_enforce_hard_floor_pages_despite_low_laya(env):
    env["rows"]["payment-service"] = triage_row("enforce", 0.01, service="payment-service", err_pct=70.0)
    out = json.loads(tools.call_on_call_engineer("payment-service", "down", run_id=RUN))
    assert out["policy"]["rule"] == "hard_floor" and len(env["slack"]) == 1


def test_enforce_min_volume_holds_false_page(env):
    env["rows"]["order-service"] = triage_row("enforce", 0.95, total=2, err_pct=50.0)
    out = json.loads(tools.call_on_call_engineer("order-service", "1 of 2 failed", run_id=RUN))
    assert out["status"] == "PAGE_HELD" and env["slack"] == []


def test_enforce_laya_unavailable_fails_open(env):
    env["rows"]["order-service"] = triage_row("enforce", None)
    out = json.loads(tools.call_on_call_engineer("order-service", "down", run_id=RUN))
    assert out["policy"]["reason"] == "Laya unavailable" and len(env["slack"]) == 1


def test_unknown_service_pages(env):
    env["rows"]["order-service"] = triage_row("enforce", 0.1)
    out = json.loads(tools.call_on_call_engineer("orders-svc", "down", run_id=RUN))
    assert out["policy"]["rule"] == "unknown_service" and len(env["slack"]) == 1


# ---------------------------------------------------------------- email and forced pages

def test_email_lists_held_pages(env, capsys):
    env["rows"]["order-service"] = triage_row("enforce", 0.1)
    tools.call_on_call_engineer("order-service", "down", run_id=RUN)
    tools.send_email_notification("oncall@example.com", 2, "subj", "body", run_id=RUN)
    email = next(a for a in env["alerts"] if a["alert_type"] == "EMAIL")
    printed = capsys.readouterr().out
    assert "HELD PAGES" in printed and "order-service" in printed
    assert email["message"].startswith("To: oncall@example.com")


def test_email_without_held_pages_is_unchanged(env, capsys):
    tools.send_email_notification("a@b.c", 3, "subj", "body text", run_id=RUN)
    assert "HELD PAGES" not in capsys.readouterr().out


def test_send_forced_page_says_gemini_did_not_page(env):
    import policy
    d = policy.decide(False, laya(0.95), pack(), tools.load_laya_config())
    tools.send_forced_page(RUN, "order-service", d, "SEV-2")
    sent = env["slack"][0]
    assert "Gemini did not page" in sent["message"] and sent["context"]["gemini_severity"] == "SEV-2"
    assert "PAGE_FORCED" in env["alerts"][0]["message"]


# ---------------------------------------------------------------- raise_alert validation

@pytest.mark.parametrize("bad", [0, 5, -1, 2.5, "2", None, True])
def test_raise_alert_rejects_bad_severity(env, bad):
    with pytest.raises(ValueError, match="severity must be an integer from 1 to 4"):
        tools.raise_alert("s", bad, "m")
    assert env["alerts"] == []


@pytest.mark.parametrize("good,stored", [(1, 1), (4, 4), (2.0, 2)])
def test_raise_alert_accepts_valid_severity(env, good, stored):
    assert "SEV-" in tools.raise_alert("s", good, "m")
    assert env["alerts"][0]["severity"] == stored
