"""Payload builders shared by the tests."""


def pg_payload(services, window=30):
    """A get_logs-shaped payload from a list of per-service dicts."""
    return {"source": "postgresql", "status": "ok", "window_minutes": window,
            "overall": {}, "services": services}


def pg_service(name, total=100, errors=0, avg_ms=100.0, client_errors=0, samples=()):
    return {
        "service": name, "total_requests": total, "error_count": errors,
        "error_rate_pct": round(errors / total * 100, 1) if total else 0.0,
        "avg_response_time_ms": avg_ms,
        "client_error_count": client_errors,
        "client_error_rate_pct": round(client_errors / total * 100, 1) if total else 0.0,
        "error_samples": list(samples),
    }


def dt_payload(services, status="ok"):
    return {"source": "dynatrace", "status": status, "services": services}


def dt_service(name, total=100, err_pct=0.0, avg_ms=100.0):
    return {"service": name, "total_requests": total,
            "error_count": round(total * err_pct / 100),
            "error_rate_pct": err_pct, "avg_response_time_ms": avg_ms}
