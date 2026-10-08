"""dt_search_logs aggregation: summarised DQL first, raw-record DQL as the fallback,
same output schema either way. DQL calls are mocked."""

import pytest

import dynatrace_client as dc

SUMMARY = [
    {"service.name": "payment-service", "total": "200", "errors": "50", "latency_sum": "80000.0", "latency_n": "200"},
    {"service.name": "api-gateway", "total": "100", "errors": "0", "latency_sum": "100.0", "latency_n": "100"},
]
RECORDS = [
    {"service.name": "payment-service", "http.status_code": "500", "response_time_ms": "900"},
    {"service.name": "payment-service", "http.status_code": "200", "response_time_ms": "100"},
    {"service.name": "api-gateway", "http.status_code": "200", "response_time_ms": "1"},
]


@pytest.fixture
def tenant(monkeypatch):
    monkeypatch.setattr(dc, "DT_API_KEY", "dt0s16.fake")
    monkeypatch.setattr(dc, "DT_ENV_URL", "https://abc123.apps.dynatrace.com")
    monkeypatch.setattr(dc, "_query_classic", lambda w: None)      # Grail-only tenant
    calls = []

    def run_dql(dql):
        calls.append(dql)
        return state["summary"] if "summarize" in dql else state["records"]
    state = {"summary": SUMMARY, "records": RECORDS}
    monkeypatch.setattr(dc, "_run_dql", run_dql)
    return state, calls


def test_summary_path(tenant):
    state, calls = tenant
    out = dc.query_logs(30)
    assert out["status"] == "ok" and "summarize" in out["api_used"]
    assert len(calls) == 1 and "by: {`service.name`}" in calls[0] and "limit" not in calls[0]
    assert out["overall"] == {"total_requests": 300, "error_count": 50, "success_count": 250,
                              "error_rate_pct": 16.7, "avg_response_time_ms": round(80100 / 300, 1)}
    pay = next(s for s in out["services"] if s["service"] == "payment-service")
    assert pay == {"service": "payment-service", "total_requests": 200, "error_count": 50,
                   "error_rate_pct": 25.0, "avg_response_time_ms": 400.0}


@pytest.mark.parametrize("summary", [None, [{"unexpected": 1}]])
def test_falls_back_to_records(tenant, summary):
    state, calls = tenant
    state["summary"] = summary
    out = dc.query_logs(30)
    assert out["status"] == "ok" and "fallback" in out["api_used"]
    assert len(calls) == 2 and "limit 1000" in calls[1]
    assert out["overall"]["total_requests"] == 3 and out["overall"]["error_count"] == 1


def test_same_schema_both_paths(tenant):
    state, _ = tenant
    a = dc.query_logs(30)
    state["summary"] = None
    b = dc.query_logs(30)
    assert sorted(a) == sorted(b)
    assert sorted(a["overall"]) == sorted(b["overall"])
    assert sorted(a["services"][0]) == sorted(b["services"][0])


def test_both_paths_fail_is_error(tenant):
    state, _ = tenant
    state["summary"] = None
    state["records"] = None
    out = dc.query_logs(30)
    assert out["status"] == "error" and out["services"] == []


def test_zero_latency_samples(tenant):
    state, _ = tenant
    state["summary"] = [{"service.name": "s", "total": 4, "errors": 1, "latency_sum": None, "latency_n": 0}]
    out = dc.query_logs(30)
    assert out["services"][0]["avg_response_time_ms"] == 0.0 and out["overall"]["avg_response_time_ms"] == 0.0
