from tools import summarize_logs


def row(endpoint, status, ms=100.0, detail=None):
    return {"endpoint": endpoint, "status_code": status, "response_time_ms": ms, "error_detail": detail}


ROWS = [  # newest first, as fetch_logs returns them
    row("/api/payments", 503, 900, "Payment gateway timeout"),
    row("/api/payments", 500, 800, "DB pool exhausted"),
    row("/api/payments", 500, 700, "Payment gateway timeout"),     # duplicate detail
    row("/api/payments", 422, 50, "Bad request"),                  # 4xx: not a sample
    row("/api/payments", 200, 100),
    row("/health", 200, 1),
]


def test_original_fields_unchanged():
    out = summarize_logs(ROWS, 30)
    assert out["source"] == "postgresql" and out["status"] == "ok" and out["window_minutes"] == 30
    assert out["overall"]["total_requests"] == 6
    assert out["overall"]["error_count"] == 3
    assert out["overall"]["success_count"] == 2
    assert out["overall"]["error_rate_pct"] == 50.0
    pay = next(s for s in out["services"] if s["service"] == "payment-service")
    assert pay["total_requests"] == 5 and pay["error_count"] == 3 and pay["error_rate_pct"] == 60.0
    assert pay["avg_response_time_ms"] == 510.0


def test_additive_client_error_fields():
    out = summarize_logs(ROWS, 30)
    assert out["overall"]["client_error_count"] == 1
    assert out["overall"]["client_error_rate_pct"] == 16.7
    pay = next(s for s in out["services"] if s["service"] == "payment-service")
    assert pay["client_error_count"] == 1 and pay["client_error_rate_pct"] == 20.0


def test_error_samples_distinct_recent_5xx_only():
    out = summarize_logs(ROWS, 30)
    pay = next(s for s in out["services"] if s["service"] == "payment-service")
    assert pay["error_samples"] == ["Payment gateway timeout", "DB pool exhausted"]
    gw = next(s for s in out["services"] if s["service"] == "api-gateway")
    assert gw["error_samples"] == []


def test_error_samples_capped_at_five():
    rows = [row("/api/orders", 500, detail=f"err {i}") for i in range(9)]
    out = summarize_logs(rows, 30)
    assert out["services"][0]["error_samples"] == [f"err {i}" for i in range(5)]


def test_empty_window():
    out = summarize_logs([], 30)
    assert out["overall"]["total_requests"] == 0 and out["overall"]["client_error_rate_pct"] == 0.0
    assert out["services"] == []
