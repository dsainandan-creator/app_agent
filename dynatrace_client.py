"""
dynatrace_client.py – Dynatrace observability integration.

Two capabilities:
  1. Log ingestion  – POST /api/v2/logs/ingest  (per-request, daemon thread)
  2. Log querying   – Grail DQL via /platform/storage/query/v1/query:execute
                      (called by the Dynatrace MCP tool)

Token split:
  Dynatrace uses two different token authority models on Grail tenants:
    DYNATRACE_API_KEY    – OAuth/platform token (dt0s16.*)
                           Used for: DQL log queries
                           Required scope: logs.read (or storage:logs:read)
    DYNATRACE_INGEST_KEY – Classic API token (dt0c01.*)
                           Used for: /api/v2/logs/ingest writes
                           Required scope: logs.ingest
                           Create at: Dynatrace → Access Tokens → New token

  If DYNATRACE_INGEST_KEY is not set, ingest is silently skipped.

URL routing:
  /api/v2/logs/ingest              → live.dynatrace.com  (classic API)
  /platform/storage/query/…        → apps.dynatrace.com  (Grail DQL)
  Auto-derived from DYNATRACE_ENV_URL regardless of which subdomain is set.
"""

import json
import os
import sys
import time
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

DT_API_KEY: str    = os.environ.get("DYNATRACE_API_KEY", "")      # OAuth – read/DQL
DT_INGEST_KEY: str = os.environ.get("DYNATRACE_INGEST_KEY", "")   # Classic – write/ingest
DT_ENV_URL: str    = os.environ.get("DYNATRACE_ENV_URL", "").rstrip("/")


# ---------------------------------------------------------------------------
# URL helpers  –  always derive from the env ID, ignore the subdomain the
# user happened to put in .env so both *.apps and *.live configs work.
# ---------------------------------------------------------------------------

def _env_id() -> str:
    """Extract the environment ID (first subdomain component) from DT_ENV_URL."""
    host = DT_ENV_URL.split("://")[-1].split("/")[0]   # strip scheme + path
    return host.split(".")[0]                           # e.g. "cjq99583"


def _live_url() -> str:
    """Classic API base — /api/v2/* must be called here, not on *.apps."""
    return f"https://{_env_id()}.live.dynatrace.com"


def _apps_url() -> str:
    """New platform base — DQL /platform/storage/* is served here."""
    return f"https://{_env_id()}.apps.dynatrace.com"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    """True if DQL querying is available (OAuth token + env URL)."""
    return bool(DT_API_KEY and DT_ENV_URL)


def dt_ingest_tool_dry_run() -> bool:
    """True unless DT_INGEST_TOOL_DRY_RUN is explicitly false. Read on every call."""
    return os.environ.get("DT_INGEST_TOOL_DRY_RUN", "true").strip().lower() not in ("false", "0", "no", "off")


def ingest_configured() -> bool:
    """True if log ingestion is available.

    Uses DYNATRACE_INGEST_KEY when set (classic dt0c01.* token).
    Falls back to DYNATRACE_API_KEY so a single platform token with
    the logs.ingest scope can handle both reading and writing.
    """
    return bool((DT_INGEST_KEY or DT_API_KEY) and DT_ENV_URL)


def _infer_service(endpoint: str) -> str:
    if not endpoint:
        return "unknown"
    if "/payments" in endpoint:
        return "payment-service"
    if "/orders" in endpoint:
        return "order-service"
    if "/users" in endpoint:
        return "user-service"
    if "/products" in endpoint or "/inventory" in endpoint:
        return "product-service"
    return "api-gateway"


def _read_header() -> dict:
    """OAuth token header — used for DQL queries."""
    return {"Authorization": f"Api-Token {DT_API_KEY}"}


def _ingest_header() -> dict:
    """Token header for log ingestion — prefers DYNATRACE_INGEST_KEY,
    falls back to DYNATRACE_API_KEY when a single platform token covers both."""
    token = DT_INGEST_KEY or DT_API_KEY
    return {"Authorization": f"Api-Token {token}"}


# Keep a single alias so existing call-sites don't break
def _auth_header() -> dict:
    return _read_header()


def _safe_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _aggregate(results: list) -> dict:
    """Aggregate a flat list of log entry dicts into per-service stats."""
    svc_buckets: dict = defaultdict(lambda: {"total": 0, "errors": 0, "latencies": []})

    for entry in results:
        # DT may return attributes flat or nested under an "attributes" key
        nested = entry.get("attributes") or {}

        def _get(key: str):
            v = entry.get(key)
            return v if v is not None else nested.get(key)

        svc    = _get("service.name") or _infer_service(_get("http.url") or "")
        status = _safe_int(_get("http.status_code"))
        rt     = _safe_float(_get("response_time_ms"))

        svc_buckets[svc]["total"] += 1
        if rt is not None:
            svc_buckets[svc]["latencies"].append(rt)
        if status is not None and status >= 500:
            svc_buckets[svc]["errors"] += 1

    total        = len(results)
    total_errors = sum(b["errors"] for b in svc_buckets.values())
    all_lat      = [lt for b in svc_buckets.values() for lt in b["latencies"]]
    avg_rt       = sum(all_lat) / len(all_lat) if all_lat else 0.0

    services = []
    for svc, b in sorted(svc_buckets.items()):
        t   = b["total"]
        e   = b["errors"]
        avg = sum(b["latencies"]) / len(b["latencies"]) if b["latencies"] else 0.0
        services.append({
            "service":              svc,
            "total_requests":       t,
            "error_count":          e,
            "error_rate_pct":       round(e / t * 100, 1) if t else 0.0,
            "avg_response_time_ms": round(avg, 1),
        })

    return {"total": total, "total_errors": total_errors, "avg_rt": avg_rt, "services": services}


# ---------------------------------------------------------------------------
# Log ingestion  (fire-and-forget via daemon thread)
# ---------------------------------------------------------------------------

def _build_payload(
    endpoint, method, status_code, response_time_ms, message, request_id, error_detail
) -> list:
    severity = "ERROR" if status_code >= 500 else ("WARN" if status_code >= 400 else "INFO")
    now = datetime.now(timezone.utc)
    return [{
        "content":           message,
        "severity":          severity,
        "service.name":      _infer_service(endpoint),
        "http.status_code":  status_code,
        "http.method":       method,
        "http.url":          endpoint,
        "response_time_ms":  response_time_ms,
        "request_id":        request_id,
        "error_detail":      error_detail or "",
        "timestamp":         now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
    }]


def _do_ingest(payload: list) -> None:
    """Blocking POST using the classic ingest token — always called in a daemon thread."""
    try:
        resp = httpx.post(
            f"{_live_url()}/api/v2/logs/ingest",
            headers={**_ingest_header(), "Content-Type": "application/json; charset=utf-8"},
            json=payload,
            timeout=5.0,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"[DT] Ingest warning: {exc}")


def ingest_log(
    endpoint: str,
    method: str,
    status_code: int,
    response_time_ms: float,
    message: str,
    request_id: str,
    error_detail: str = None,
) -> None:
    """
    Fire-and-forget: send one log entry to Dynatrace without blocking the caller.
    Uses DYNATRACE_INGEST_KEY (classic dt0c01.* token, logs.ingest scope).
    Silently skips if DYNATRACE_INGEST_KEY is not set.
    """
    if not ingest_configured():
        return
    payload = _build_payload(
        endpoint, method, status_code, response_time_ms, message, request_id, error_detail
    )
    threading.Thread(target=_do_ingest, args=(payload,), daemon=True).start()


def ingest_log_sync(
    endpoint: str,
    method: str,
    status_code: int,
    response_time_ms: float,
    message: str,
    request_id: str,
    error_detail: str = None,
) -> str:
    """
    Synchronous log ingest for the Dynatrace MCP tool.
    Uses DYNATRACE_INGEST_KEY (classic dt0c01.* token, logs.ingest scope).

    Dry run by default: unless DT_INGEST_TOOL_DRY_RUN=false, the payload is logged
    to stderr and returned without any HTTP call. This covers only the dt_ingest_log
    tool; the mock app's automatic per-request forwarding (ingest_log) stays live.
    """
    payload = _build_payload(
        endpoint, method, status_code, response_time_ms, message, request_id, error_detail
    )
    if dt_ingest_tool_dry_run():
        body = json.dumps(payload, ensure_ascii=False)
        print(f"[DT DRY_RUN] dt_ingest_log would POST /api/v2/logs/ingest: {body}", file=sys.stderr)
        return f"DRY_RUN: not sent to Dynatrace (set DT_INGEST_TOOL_DRY_RUN=false to send). Payload: {body}"
    if not ingest_configured():
        return (
            "Dynatrace ingest not configured — DYNATRACE_INGEST_KEY is missing. "
            "Create a classic API token (dt0c01.*) at Dynatrace → Access Tokens "
            "with the 'Ingest logs (logs.ingest)' scope and set it as DYNATRACE_INGEST_KEY in .env."
        )
    try:
        resp = httpx.post(
            f"{_live_url()}/api/v2/logs/ingest",
            headers={**_ingest_header(), "Content-Type": "application/json; charset=utf-8"},
            json=payload,
            timeout=5.0,
        )
        resp.raise_for_status()
        svc = _infer_service(endpoint)
        return (
            f"Log ingested to Dynatrace (HTTP {resp.status_code}): "
            f"service={svc}, status_code={status_code}, endpoint={endpoint}."
        )
    except Exception as exc:
        return f"Dynatrace ingest failed: {exc}"


# ---------------------------------------------------------------------------
# Log querying  –  classic API with Grail DQL fallback
# ---------------------------------------------------------------------------

def _query_classic(window_minutes: int) -> list | None:
    """
    GET /api/v2/logs/search on *.live.dynatrace.com (classic Log Monitoring).
    Returns list of log entries, or None if the endpoint is unavailable (404).
    Raises PermissionError on 401/403 so the caller can surface a clear message.
    """
    now     = datetime.now(timezone.utc)
    from_dt = now - timedelta(minutes=window_minutes)
    try:
        resp = httpx.get(
            f"{_live_url()}/api/v2/logs/search",
            headers=_auth_header(),
            params={
                "from":  from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to":    now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 1000,
            },
            timeout=15.0,
        )
        if resp.status_code == 404:
            return None     # Tenant uses Grail — caller will try DQL
        if resp.status_code in (401, 403):
            raise PermissionError(
                f"HTTP {resp.status_code} on /api/v2/logs/search. "
                "Your API token is missing the 'logs.read' scope. "
                "Go to Dynatrace → Settings → Access Tokens, find your token, "
                "and add the 'Read log data (logs.read)' permission."
            )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 404:
            return None
        if code in (401, 403):
            raise PermissionError(str(exc)) from exc
        raise


def _run_dql(dql: str) -> list | None:
    """
    POST /platform/storage/query/v1/query:execute on *.apps.dynatrace.com (Grail DQL).
    Polls until SUCCEEDED / FAILED.  Returns list of records or None.
    """
    try:
        resp = httpx.post(
            f"{_apps_url()}/platform/storage/query/v1/query:execute",
            headers={**_auth_header(), "Content-Type": "application/json"},
            json={"query": dql, "requestTimeoutMilliseconds": 30000},
            timeout=35.0,
        )
        resp.raise_for_status()
        data = resp.json()

        # Poll if the query hasn't finished yet
        request_token = data.get("requestToken")
        for _ in range(10):
            state = data.get("state")
            if state == "SUCCEEDED":
                return data.get("result", {}).get("records", [])
            if state in ("FAILED", "CANCELLED"):
                print(f"[DT] DQL query {state}", file=sys.stderr)
                return None
            if state == "RUNNING" and request_token:
                time.sleep(2)
                poll = httpx.get(
                    f"{_apps_url()}/platform/storage/query/v1/query:poll",
                    headers=_auth_header(),
                    params={"requestToken": request_token},
                    timeout=15.0,
                )
                poll.raise_for_status()
                data = poll.json()
            else:
                # Immediate result (no state field) or unknown state
                return data.get("result", {}).get("records", [])

        return None
    except Exception as exc:
        print(f"[DT] DQL query warning: {exc}", file=sys.stderr)
        return None


def _query_dql(window_minutes: int) -> list | None:
    """Raw log records (newest 1000 only). Fallback when the summarised query fails."""
    return _run_dql(
        f"fetch logs, from: now()-{window_minutes}m, to: now() "
        "| filter isNotNull(`service.name`) "
        "| fields timestamp, content, severity, `service.name`, "
        "         `http.status_code`, `response_time_ms`, `http.url` "
        "| sort timestamp desc "
        "| limit 1000"
    )


def _query_dql_summary(window_minutes: int) -> dict | None:
    """
    Per-service totals computed by Grail (summarize ... by service.name), so every log
    in the window counts instead of the newest 1000 rows. Returns the same aggregate
    shape as _aggregate(), or None if the query fails or returns something unexpected.
    """
    records = _run_dql(
        f"fetch logs, from: now()-{window_minutes}m, to: now() "
        "| filter isNotNull(`service.name`) "
        "| summarize total = count(), "
        "            errors = countIf(toLong(`http.status_code`) >= 500), "
        "            latency_sum = sum(toDouble(response_time_ms)), "
        "            latency_n = countIf(isNotNull(toDouble(response_time_ms))), "
        "            by: {`service.name`}"
    )
    if records is None:
        return None
    try:
        services, total, total_errors, lat_sum, lat_n = [], 0, 0, 0.0, 0
        for r in records:
            t = int(r["total"])
            e = int(r.get("errors") or 0)
            ls = float(r.get("latency_sum") or 0.0)
            ln = int(r.get("latency_n") or 0)
            total, total_errors, lat_sum, lat_n = total + t, total_errors + e, lat_sum + ls, lat_n + ln
            services.append({
                "service":              r["service.name"],
                "total_requests":       t,
                "error_count":          e,
                "error_rate_pct":       round(e / t * 100, 1) if t else 0.0,
                "avg_response_time_ms": round(ls / ln, 1) if ln else 0.0,
            })
    except (KeyError, TypeError, ValueError) as exc:
        print(f"[DT] DQL summary had an unexpected shape: {exc}", file=sys.stderr)
        return None
    return {"total": total, "total_errors": total_errors,
            "avg_rt": lat_sum / lat_n if lat_n else 0.0,
            "services": sorted(services, key=lambda s: s["service"])}


# ---------------------------------------------------------------------------
# Public query entry point
# ---------------------------------------------------------------------------

def query_logs(window_minutes: int = 30) -> dict:
    """
    Fetch and aggregate logs from Dynatrace for the last N minutes.

    Tries the classic Log Monitoring API first; if the tenant uses Grail-only
    log storage (returns 404 on classic search), falls back to the DQL query API.

    Returns a dict with the same field names as get_logs() so the agent can
    compare both sources side-by-side without ambiguity:

    {
      "source":         "dynatrace",
      "status":         "ok" | "not_configured" | "error",
      "api_used":       "classic" | "grail-dql",
      "window_minutes": N,
      "note":           "...",
      "overall": { total_requests, error_count, success_count,
                   error_rate_pct, avg_response_time_ms },
      "services": [ { service, total_requests, error_count,
                      error_rate_pct, avg_response_time_ms }, ... ]
    }
    """
    if not is_configured():
        return {
            "source": "dynatrace",
            "status": "not_configured",
            "note": (
                "DYNATRACE_API_KEY or DYNATRACE_ENV_URL not set in .env. "
                "Dynatrace data unavailable — base analysis solely on PostgreSQL."
            ),
            "window_minutes": window_minutes,
            "overall": None,
            "services": [],
        }

    agg = None
    try:
        results  = _query_classic(window_minutes)
        api_used = "classic (/api/v2/logs/search on live.dynatrace.com)"

        if results is None:         # classic endpoint not available → try Grail DQL
            agg = _query_dql_summary(window_minutes)
            api_used = "grail-dql summarize (/platform/storage/query/v1/query:execute on apps.dynatrace.com)"
            if agg is None:         # summarised query failed → raw records, newest 1000
                results  = _query_dql(window_minutes)
                api_used = ("grail-dql records, limit 1000 (fallback) "
                            "(/platform/storage/query/v1/query:execute on apps.dynatrace.com)")
            else:
                results = []

        if results is None:
            return {
                "source": "dynatrace",
                "status": "error",
                "note": (
                    "Grail DQL query failed (401 — this endpoint requires an OAuth 2.0 token, "
                    "not a classic API token). To enable log querying, add the 'logs.read' scope "
                    "to your API token in Dynatrace → Settings → Access Tokens. "
                    "Log ingestion is working correctly. Base analysis on PostgreSQL for now."
                ),
                "window_minutes": window_minutes,
                "overall": None,
                "services": [],
            }

    except PermissionError as exc:
        return {
            "source": "dynatrace",
            "status": "permission_error",
            "note": str(exc),
            "window_minutes": window_minutes,
            "overall": None,
            "services": [],
        }
    except Exception as exc:
        return {
            "source": "dynatrace",
            "status": "error",
            "note": (
                f"Dynatrace query failed: {exc}. "
                "Base analysis on PostgreSQL and note DT data is unavailable."
            ),
            "window_minutes": window_minutes,
            "overall": None,
            "services": [],
        }

    if agg is None:
        agg = _aggregate(results)
    total = agg["total"]
    errs  = agg["total_errors"]

    return {
        "source":         "dynatrace",
        "status":         "ok",
        "api_used":       api_used,
        "window_minutes": window_minutes,
        "note": (
            "Dynatrace data may lag 30–60 s behind real-time due to the ingestion pipeline. "
            "If DT error rates are lower than PostgreSQL, recent spikes may not yet be visible."
        ),
        "overall": {
            "total_requests":       total,
            "error_count":          errs,
            "success_count":        total - errs,
            "error_rate_pct":       round(errs / total * 100, 1) if total else 0.0,
            "avg_response_time_ms": round(agg["avg_rt"], 1),
        },
        "services": agg["services"],
    }
