"""agent._apply_policy_after_loop: decisions for services Gemini did not page."""

import pytest

import agent
import tools
from tests.test_paging_tools import RUN, triage_row


@pytest.fixture
def env(monkeypatch):
    state = {"slack": [], "alerts": [], "rows": {}, "events": []}
    monkeypatch.setattr(tools, "notify_slack", lambda service, message, context=None: state["slack"].append(
        (service, message, context)) or {"incident_id": "INC-x", "status": "DRY_RUN"})
    monkeypatch.setattr(tools, "save_alert", lambda **kw: state["alerts"].append(kw))
    monkeypatch.setattr(tools, "fetch_laya_triage", lambda run_id: state["rows"])
    fetch = lambda run_id, types: [a for a in state["alerts"] if a["alert_type"] in types]
    monkeypatch.setattr(agent, "fetch_run_alerts", fetch)
    monkeypatch.setattr(agent, "log_agent_event", lambda **kw: state["events"].append(kw))
    monkeypatch.setenv("LAYA_ENABLED", "true")
    return state


def _packs(state):
    return [{"service": s, **state["rows"][s]["evidence"]} for s in state["rows"]]


def test_enforce_forces_page_for_unpaged_high_laya_service(env):
    env["rows"]["order-service"] = triage_row("enforce", 0.95)
    env["rows"]["api-gateway"] = triage_row("enforce", 0.05, service="api-gateway")
    table = agent._apply_policy_after_loop(RUN, "enforce", _packs(env), set(), {"order-service": "SEV-2"})
    assert [s[0] for s in env["slack"]] == ["order-service"]
    assert "Gemini did not page" in env["slack"][0][1]
    assert env["events"][0]["event_type"] == "POLICY_PAGE_FORCED"
    assert "| order-service | SEV-2 | PAGE_FORCED |" in table
    assert "| api-gateway | n/a | NO_PAGE |" in table


@pytest.mark.parametrize("mode", ["shadow", "advisory"])
def test_shadow_and_advisory_only_record(env, mode):
    env["rows"]["order-service"] = triage_row(mode, 0.95)
    table = agent._apply_policy_after_loop(RUN, mode, _packs(env), set(), {})
    assert env["slack"] == []
    assert [a["alert_type"] for a in env["alerts"]] == ["POLICY_SHADOW"]
    assert env["alerts"][0]["status"] == "PAGE_FORCED"
    assert "not enforced" in table


def test_services_gemini_paged_are_skipped(env):
    env["rows"]["order-service"] = triage_row("enforce", 0.95)
    agent._apply_policy_after_loop(RUN, "enforce", _packs(env), {"order-service"}, {})
    assert env["slack"] == [] and env["alerts"] == []
