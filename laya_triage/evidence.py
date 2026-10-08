"""
laya_triage/evidence.py – Build per-service evidence packs for Laya.

Input: the parsed JSON from get_logs (PostgreSQL) and dt_search_logs (Dynatrace).
Output: one evidence pack per PostgreSQL service.

Laya is an encoder and weak at arithmetic, so it never sees raw numbers: every
metric is turned into a text band (thresholds and wording in triage_config.json).
The raw numbers stay in the pack for Gemini and for the paging policy's hard floor.

Everything here is pure: no database, network or model access.
"""

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

CONFIG_PATH = Path(__file__).parent / "triage_config.json"

RESOLUTIONS = ("agreement", "DT-lag", "DT-higher", "DT-unavailable")


@lru_cache(maxsize=None)
def _load_cached(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_triage_config(path: Optional[Path] = None) -> dict:
    return _load_cached(str(path or CONFIG_PATH))


def _parse(payload):
    """Accept a tool's JSON string or an already-parsed dict."""
    if payload is None:
        return None
    if isinstance(payload, str):
        return json.loads(payload)
    return payload


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------

def band(value: float, levels: list) -> dict:
    """Return the first level whose 'below' the value is under ('below': null = top)."""
    for level in levels:
        if level["below"] is None or value < level["below"]:
            return level
    return levels[-1]


def band_label(name: str, value: float, cfg: Optional[dict] = None) -> str:
    cfg = cfg or load_triage_config()
    return band(value, cfg["bands"][name]["levels"])["label"]


def band_text(name: str, value: float, cfg: Optional[dict] = None) -> str:
    cfg = cfg or load_triage_config()
    return band(value, cfg["bands"][name]["levels"])["text"]


# ---------------------------------------------------------------------------
# Source resolution (PostgreSQL vs Dynatrace)
# ---------------------------------------------------------------------------

def resolve_sources(pg_err_pct: float, dt_service: Optional[dict], dt_status: str,
                    cfg: Optional[dict] = None) -> dict:
    """
    Compare a service's 5xx rate across the two sources, using the same rules as
    the agent's SYSTEM_PROMPT:
      < agreement_below_pp difference  -> agreement
      Dynatrace lower                  -> DT-lag
      Dynatrace higher                 -> DT-higher
      no usable Dynatrace data         -> DT-unavailable
    large_gap flags a difference of large_gap_pp or more.
    """
    cfg = cfg or load_triage_config()
    rules = cfg["resolution"]
    if (dt_status != "ok" or not dt_service
            or not dt_service.get("total_requests")):
        return {"resolution": "DT-unavailable", "delta_err_pp": None, "large_gap": False}

    delta = round(float(dt_service.get("error_rate_pct", 0.0)) - float(pg_err_pct), 1)
    if abs(delta) < rules["agreement_below_pp"]:
        resolution = "agreement"
    elif delta < 0:
        resolution = "DT-lag"
    else:
        resolution = "DT-higher"
    return {
        "resolution": resolution,
        "delta_err_pp": delta,
        "large_gap": abs(delta) >= rules["large_gap_pp"],
    }


# ---------------------------------------------------------------------------
# Evidence packs
# ---------------------------------------------------------------------------

def build_evidence_packs(pg, dt, critical_services=(), cfg: Optional[dict] = None) -> list:
    """
    One pack per PostgreSQL service. PostgreSQL is authoritative for which
    services exist; a service seen only in Dynatrace is not triaged.
    """
    cfg = cfg or load_triage_config()
    pg = _parse(pg) or {}
    dt = _parse(dt)
    dt_status = (dt or {}).get("status", "not_configured")
    dt_by_service = {s["service"]: s for s in (dt or {}).get("services") or []}
    max_samples = cfg["max_error_samples"]
    max_chars = cfg["max_sample_chars"]

    packs = []
    for svc in pg.get("services") or []:
        name = svc["service"]
        dt_svc = dt_by_service.get(name)
        source = resolve_sources(svc.get("error_rate_pct", 0.0), dt_svc, dt_status, cfg)
        dt_available = source["resolution"] != "DT-unavailable"

        raw_pg = {k: svc.get(k) for k in (
            "total_requests", "error_count", "error_rate_pct", "avg_response_time_ms",
            "client_error_count", "client_error_rate_pct",
        )}
        raw_dt = ({k: dt_svc.get(k) for k in (
            "total_requests", "error_count", "error_rate_pct", "avg_response_time_ms",
        )} if dt_available else None)

        samples = [s[:max_chars] for s in (svc.get("error_samples") or [])][:max_samples]

        packs.append({
            "service": name,
            "window_minutes": pg.get("window_minutes"),
            "critical": name in critical_services,
            "bands": {
                "server_error_rate": band_label("server_error_rate", svc.get("error_rate_pct") or 0.0, cfg),
                "latency": band_label("latency", svc.get("avg_response_time_ms") or 0.0, cfg),
                "request_volume": band_label("request_volume", svc.get("total_requests") or 0, cfg),
                "client_error_rate": band_label("client_error_rate", svc.get("client_error_rate_pct") or 0.0, cfg),
                "dt_server_error_rate": (band_label("server_error_rate", raw_dt["error_rate_pct"] or 0.0, cfg)
                                         if dt_available else None),
                "dt_latency": (band_label("latency", raw_dt["avg_response_time_ms"] or 0.0, cfg)
                               if dt_available else None),
            },
            "source": {**source, "dt_status": dt_status},
            "error_samples": samples,
            "raw": {"pg": raw_pg, "dt": raw_dt},
        })
    return packs


def laya_state(pack: dict, cfg: Optional[dict] = None) -> dict:
    """The state Laya reads: text bands only, no raw numbers."""
    cfg = cfg or load_triage_config()
    raw_pg = pack["raw"]["pg"]
    src = pack["source"]

    sources = cfg["resolution"]["text"][src["resolution"]]
    if src["large_gap"]:
        sources += cfg["resolution"]["large_gap_text"]

    state = {
        "service": pack["service"],
        "criticality": cfg["criticality_text"]["critical" if pack["critical"] else "standard"],
        "server_errors": band_text("server_error_rate", raw_pg.get("error_rate_pct") or 0.0, cfg),
        "latency": band_text("latency", raw_pg.get("avg_response_time_ms") or 0.0, cfg),
        "request_volume": band_text("request_volume", raw_pg.get("total_requests") or 0, cfg),
        "client_errors": band_text("client_error_rate", raw_pg.get("client_error_rate_pct") or 0.0, cfg),
        "monitoring_sources": sources,
    }
    if pack["raw"]["dt"] is not None:
        state["dynatrace_server_errors"] = band_text(
            "server_error_rate", pack["raw"]["dt"].get("error_rate_pct") or 0.0, cfg)
    state["recent_errors"] = list(pack["error_samples"]) or ["none recorded"]
    return state


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------

def serialize_state(state) -> str:
    """Same serialisation Laya applies to dict states (laya.common.serialize_state)."""
    return state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)


def estimate_tokens(text: str, cfg: Optional[dict] = None) -> int:
    """Conservative token estimate for when no tokenizer is available (HTTP mode)."""
    cfg = cfg or load_triage_config()
    return math.ceil(len(text) / cfg["token_budget"]["chars_per_token_estimate"])


def state_token_budget(model: str, cfg: Optional[dict] = None) -> int:
    """Tokens left for the state after the question head, for one checkpoint."""
    cfg = cfg or load_triage_config()
    tb = cfg["token_budget"]
    return tb["max_len"][model] - tb["head_reserve_tokens"]


def fit_state_to_budget(state: dict, model: str, count_tokens: Optional[Callable[[str], int]] = None,
                        cfg: Optional[dict] = None) -> tuple:
    """
    Drop error samples (oldest last in the list first) until the serialised state
    fits the checkpoint's state budget. Returns (state, info). Raises
    AssertionError if even a state with no samples does not fit.
    """
    cfg = cfg or load_triage_config()
    count = count_tokens or (lambda text: estimate_tokens(text, cfg))
    budget = state_token_budget(model, cfg)
    state = dict(state)
    samples = list(state.get("recent_errors") or [])
    dropped = 0

    tokens = count(serialize_state(state))
    while tokens > budget and samples and samples != ["none recorded"]:
        samples.pop()
        dropped += 1
        state["recent_errors"] = samples or ["none recorded"]
        tokens = count(serialize_state(state))

    assert tokens <= budget, (
        f"evidence state for {state.get('service')!r} needs {tokens} tokens; "
        f"the {model} state budget is {budget}"
    )
    return state, {"state_tokens": tokens, "budget": budget, "samples_dropped": dropped}
