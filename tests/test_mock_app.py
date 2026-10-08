"""Mock app: failure-specific error_detail texts, and the app still serves requests.
Database writes are stubbed; the traffic simulator is not started."""

import pytest

from mock_app import main


@pytest.mark.parametrize("endpoint,status,needle", [
    ("/api/payments", 502, "502"),
    ("/api/payments/9999", 503, "payment"),
    ("/api/payments", 500, "pool"),
    ("/api/orders", 503, "Upstream"),
    ("/api/orders/9999", 500, "connection"),
    ("/api/users", 500, ""),
    ("/api/inventory/1", 503, "warehouse-api"),
    ("/api/products", 500, ""),
])
def test_server_errors_are_specific(endpoint, status, needle):
    for _ in range(20):
        detail = main.failure_detail(endpoint, status)
        assert detail != "Unhandled exception in service layer"
        assert needle.lower() in detail.lower()


def test_unknown_endpoint_and_client_errors():
    assert main.failure_detail("/health", 500) == "Unhandled 500 in /health"
    assert main.failure_detail("/api/orders", 400).startswith("Invalid request payload")
    assert main.failure_detail("/api/payments", 422).startswith("Card validation failed")
    assert main.failure_detail("/api/x", 418) == "Client error 418"


def test_middleware_logs_specific_detail(monkeypatch):
    from fastapi.testclient import TestClient
    logged = []
    monkeypatch.setattr(main, "log_request", lambda **kw: logged.append(kw))
    monkeypatch.setattr(main.random, "random", lambda: 0.0)        # every maybe_fail fails
    monkeypatch.setattr(main, "random_latency", lambda *a: None)
    client = TestClient(main.app)                                   # no lifespan: no DB init, no simulator
    assert client.get("/health").status_code == 200
    r = client.get("/api/orders/5")
    assert r.status_code in (500, 503)
    entry = logged[-1]
    assert entry["status_code"] == r.status_code
    assert entry["error_detail"] in sum(main._SERVER_ERRORS["/api/orders"].values(), [])
