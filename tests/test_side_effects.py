"""External side effects are dry-run by default: no HTTP call unless explicitly switched off."""

import json

import httpx
import pytest

import dynatrace_client
import slack_notify

REAL_LOOKING_WEBHOOK = "https://hooks.slack.com/services/T000/B000/xxxx"


@pytest.fixture
def no_http(monkeypatch):
    """Any outbound POST fails the test."""
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError(f"unexpected HTTP POST: {args[:1]}")
    monkeypatch.setattr(httpx, "post", forbidden)
    return calls


@pytest.fixture
def fake_post(monkeypatch):
    calls = []

    class Resp:
        status_code = 204
        def raise_for_status(self):
            pass

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return Resp()
    monkeypatch.setattr(httpx, "post", post)
    return calls


@pytest.fixture
def dry_log(monkeypatch, tmp_path):
    path = tmp_path / "slack_dry_run.jsonl"
    monkeypatch.setattr(slack_notify, "DRY_RUN_LOG", path)
    return path


# ---------------------------------------------------------------- Slack

@pytest.mark.parametrize("env", [None, "true", "TRUE", "1", "yes", "anything"])
def test_slack_dry_run_makes_no_http_call_even_with_webhook(monkeypatch, no_http, dry_log, env, capsys):
    if env is None:
        monkeypatch.delenv("SLACK_DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("SLACK_DRY_RUN", env)
    monkeypatch.setattr(slack_notify, "SLACK_WEBHOOK_URL", REAL_LOOKING_WEBHOOK)

    result = slack_notify.notify_slack("payment-service", "DB pool exhausted")

    assert no_http == []
    assert result["status"] == "DRY_RUN"
    assert slack_notify.slack_mode() == "DRY_RUN"
    record = json.loads(dry_log.read_text().splitlines()[-1])
    assert record["service"] == "payment-service"
    assert record["payload"]["blocks"][0]["text"]["text"].startswith("🚨")
    assert "[Slack DRY_RUN]" in capsys.readouterr().err


def test_slack_dry_run_works_without_webhook(no_http, dry_log):
    assert slack_notify.SLACK_WEBHOOK_URL == ""
    assert slack_notify.notify_slack("s", "m")["status"] == "DRY_RUN"


@pytest.mark.parametrize("env", ["false", "FALSE", "0", "no", "off"])
def test_slack_live_only_when_explicitly_false(monkeypatch, fake_post, env):
    monkeypatch.setenv("SLACK_DRY_RUN", env)
    monkeypatch.setattr(slack_notify, "SLACK_WEBHOOK_URL", REAL_LOOKING_WEBHOOK)
    result = slack_notify.notify_slack("s", "m")
    assert result["status"] == "SENT"
    assert len(fake_post) == 1 and fake_post[0][0][0] == REAL_LOOKING_WEBHOOK


def test_slack_live_without_webhook_raises(monkeypatch, no_http):
    monkeypatch.setenv("SLACK_DRY_RUN", "false")
    with pytest.raises(RuntimeError, match="SLACK_WEBHOOK_URL is not set"):
        slack_notify.notify_slack("s", "m")


def test_oncall_tool_records_dry_run_status(monkeypatch, no_http, dry_log):
    import tools
    saved = []
    monkeypatch.setattr(tools, "save_alert", lambda **kw: saved.append(kw))
    out = json.loads(tools.call_on_call_engineer("payment-service", "down"))
    assert out["status"] == "DRY_RUN"
    assert saved[0]["alert_type"] == "ONCALL" and saved[0]["status"] == "DRY_RUN"


# ---------------------------------------------------------------- Dynatrace dt_ingest_log tool

INGEST_ARGS = dict(endpoint="/api/payments", method="POST", status_code=500,
                   response_time_ms=900.0, message="boom", request_id="r1")


def test_dt_ingest_tool_dry_run_by_default(monkeypatch, no_http, capsys):
    monkeypatch.delenv("DT_INGEST_TOOL_DRY_RUN", raising=False)
    monkeypatch.setattr(dynatrace_client, "DT_INGEST_KEY", "dt0c01.fake")
    monkeypatch.setattr(dynatrace_client, "DT_ENV_URL", "https://abc123.apps.dynatrace.com")
    out = dynatrace_client.ingest_log_sync(**INGEST_ARGS)
    assert out.startswith("DRY_RUN") and no_http == []
    assert "[DT DRY_RUN]" in capsys.readouterr().err


def test_dt_ingest_tool_live_only_when_false(monkeypatch, fake_post):
    monkeypatch.setenv("DT_INGEST_TOOL_DRY_RUN", "false")
    monkeypatch.setattr(dynatrace_client, "DT_INGEST_KEY", "dt0c01.fake")
    monkeypatch.setattr(dynatrace_client, "DT_ENV_URL", "https://abc123.apps.dynatrace.com")
    out = dynatrace_client.ingest_log_sync(**INGEST_ARGS)
    assert out.startswith("Log ingested") and len(fake_post) == 1
    assert fake_post[0][0][0] == "https://abc123.live.dynatrace.com/api/v2/logs/ingest"
