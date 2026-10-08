"""
laya_triage/client.py – Ask Laya the triage questions for a list of evidence packs.

Two backends, chosen by LAYA_BASE_URL:
  empty  -> in-process laya.Router; all services in one Router.predict_batch call
  a URL  -> laya-serve over HTTP: POST /v1/systemone/batch (one request for all
            services), falling back to one POST /v1/systemone per service

Calibration: when LAYA_CALIBRATION_FILE exists it is applied.
  in-process: loaded into the Agent (Router agent_kwargs={"calibration": path}),
              so Laya's own probabilities are already calibrated.
  HTTP:       laya-serve cannot load a calibration file, so probabilities are
              re-tempered here: p_i ** (T_shipped / T_fitted), renormalised. That is
              exactly softmax(z / T_fitted), given the server returned
              softmax(z / T_shipped). calibrate.py records T_shipped in the file.

classify() never raises: on any failure, timeout included, it returns
status "unavailable" and the agent carries on without Laya.
"""

import concurrent.futures as cf
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

os.environ.setdefault("USE_TF", "0")    # laya.load() can hang when TensorFlow is installed

from laya_triage import evidence as ev

# Building the checkpoint on first use takes seconds (minutes on a first download).
# It is bounded separately from LAYA_TIMEOUT_S, which covers inference only.
LOAD_TIMEOUT_S = 180

SEVERITY_LEVELS = ("sev1", "sev2", "sev3", "sev4")

_router = None
_router_lock = threading.Lock()
_load_future: Optional[cf.Future] = None
_executor = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="laya")


# ---------------------------------------------------------------------------
# Calibration file
# ---------------------------------------------------------------------------

def load_calibration(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _bucket(qtype: str, k: int) -> str:
    """laya.common.temp_bucket, without importing torch."""
    size = "2" if k <= 2 else "3-5" if k <= 5 else "6-10" if k <= 10 else "11+"
    return f"{qtype}:{size}"


_QTYPE_INDEX = {"choice": 0, "score": 1, "noul": 2}


def _temperature(temps: dict, qtype: str, k: int) -> float:
    by_options = temps.get("temperature_by_options") or {}
    return float(by_options.get(_bucket(qtype, k), temps["temperature"][_QTYPE_INDEX[qtype]]))


def retemper(probs: dict, qtype: str, calibration: dict) -> dict:
    """Re-temper probabilities produced at the shipped temperature to the fitted one."""
    shipped = calibration.get("shipped")
    if not shipped:
        return probs
    k = len(probs)
    ratio = _temperature(shipped, qtype, k) / _temperature(calibration, qtype, k)
    powered = {key: max(float(p), 1e-12) ** ratio for key, p in probs.items()}
    total = sum(powered.values())
    return {key: v / total for key, v in powered.items()}


# ---------------------------------------------------------------------------
# Result normalisation
# ---------------------------------------------------------------------------

def _top(probs: dict) -> tuple:
    ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    return ranked[0][0], ranked[0][1], ranked[0][1] - second


def normalise_answers(answers: dict, triage_cfg: dict, calibration: Optional[dict] = None) -> dict:
    """Turn Laya's per-question answers into the triage result the agent and policy read."""
    def probs_of(qid):
        p = {k: float(v) for k, v in answers[qid]["probabilities"].items()}
        return retemper(p, "choice", calibration) if calibration else p

    sev_p = probs_of("severity")
    sev_choice, sev_top, sev_margin = _top(sev_p)
    expected_sev = sum((i + 1) * sev_p.get(level, 0.0) for i, level in enumerate(SEVERITY_LEVELS))

    page_labels = triage_cfg["page_now_labels"]
    page_p = {page_labels[key]: p for key, p in probs_of("page_now").items()}

    def choice_of(qid):
        choice, p, _ = _top(probs_of(qid))
        return {"choice": choice, "p": round(p, 4)}

    return {
        "severity": sev_choice,
        "p": {level: round(sev_p.get(level, 0.0), 4) for level in SEVERITY_LEVELS},
        "expected_sev": round(expected_sev, 3),
        "margin": round(sev_margin, 4),
        # Laya reports two confidences: `confidence` (normalised entropy, the one its README
        # says to gate on) and `answer_confidence` (max p, the quantity calibration fits).
        "confidence": answers["severity"].get("confidence"),
        "answer_confidence": round(sev_top, 4),
        "page_now": {label: round(p, 4) for label, p in page_p.items()},
        "failure_mode": choice_of("failure_mode"),
        "user_impact": choice_of("user_impact"),
    }


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def build_router(config, calibration_path: Optional[Path] = None, device: Optional[str] = None):
    """A Router for config.model: a local checkpoint when LAYA_MODEL_PATH is set, and the
    calibration file applied to the Agent when one is given."""
    from laya import Router
    kwargs = {"max_loaded": 1}
    if config.model_path:
        kwargs["models"] = {config.model: config.model_path}
    if calibration_path:
        kwargs["agent_kwargs"] = {"calibration": str(calibration_path)}
    if device:
        kwargs["device"] = device
    return Router(**kwargs)


def _get_router(config, calibration_path: Optional[Path]):
    global _router
    with _router_lock:
        if _router is None:
            _router = build_router(config, calibration_path)
        _router.load(config.model)
        return _router


def warmup(config) -> cf.Future:
    """Start building the checkpoint in the background (in-process mode only)."""
    global _load_future
    if _load_future is None and not config.base_url:
        cal = config.calibration_file if config.calibrated else None
        _load_future = _executor.submit(_get_router, config, cal)
    return _load_future


def _predict_in_process(states: list, questions: dict, config) -> list:
    router = warmup(config).result(timeout=LOAD_TIMEOUT_S)
    requests = [{"state": s, "questions": questions, "model": config.model} for s in states]
    fut = _executor.submit(router.predict_batch, requests)
    return fut.result(timeout=config.timeout_s)


def _predict_http(states: list, questions: dict, config) -> list:
    import httpx
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    with httpx.Client(base_url=config.base_url, timeout=config.timeout_s, headers=headers) as http:
        resp = http.post("/v1/systemone/batch",
                         json={"model": config.model, "states": states, "questions": questions})
        if resp.status_code == 404:          # older laya-serve without the batch route
            out = []
            for state in states:
                r = http.post("/v1/systemone",
                              json={"model": config.model, "state": state, "questions": questions})
                r.raise_for_status()
                out.append(r.json())
            return out
        resp.raise_for_status()
        return resp.json()["results"]


def _token_counter(config):
    """Real tokenizer in-process (once the checkpoint is loaded), estimate otherwise."""
    if not config.base_url and _router is not None:
        try:
            tok = _router.load(config.model).tok
            return lambda text: len(tok(text, add_special_tokens=False)["input_ids"])
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def classify(packs: list, config, triage_cfg: Optional[dict] = None) -> dict:
    """
    Classify every evidence pack in one batched Laya call.

    Returns {"status": "ok" | "unavailable", "model", "backend", "calibrated",
             "latency_ms", "error"?, "results": [per-service result, in pack order]}.
    """
    triage_cfg = triage_cfg or ev.load_triage_config()
    backend = "http" if config.base_url else "in-process"
    calibration = load_calibration(config.calibration_file) if config.calibrated else None
    base = {"model": config.model, "backend": backend, "calibrated": calibration is not None}

    def unavailable(error: str, latency_ms: float = 0.0) -> dict:
        return {**base, "status": "unavailable", "error": error, "latency_ms": round(latency_ms, 1),
                "results": [{"service": p["service"], "status": "unavailable"} for p in packs]}

    if not packs:
        return {**base, "status": "ok", "latency_ms": 0.0, "results": []}

    t0 = time.perf_counter()
    try:
        if backend == "in-process":
            warmup(config).result(timeout=LOAD_TIMEOUT_S)
        counter = _token_counter(config)
        states, budgets = [], []
        for pack in packs:
            state, info = ev.fit_state_to_budget(ev.laya_state(pack, triage_cfg), config.model,
                                                 count_tokens=counter, cfg=triage_cfg)
            states.append(state)
            budgets.append(info)

        questions = triage_cfg["questions"]
        t_infer = time.perf_counter()
        if backend == "http":
            raw = _predict_http(states, questions, config)
        else:
            raw = _predict_in_process(states, questions, config)
        infer_ms = (time.perf_counter() - t_infer) * 1000.0
    except cf.TimeoutError:
        return unavailable(f"timed out (inference limit {config.timeout_s}s, load limit {LOAD_TIMEOUT_S}s)",
                           (time.perf_counter() - t0) * 1000.0)
    except Exception as exc:   # noqa: BLE001 – never raise into the agent
        return unavailable(f"{type(exc).__name__}: {exc}", (time.perf_counter() - t0) * 1000.0)

    # In-process probabilities are already calibrated by the Agent; only HTTP needs re-tempering.
    retemper_with = calibration if backend == "http" else None
    per_service_ms = infer_ms / len(packs)
    results = []
    for pack, state, info, r in zip(packs, states, budgets, raw):
        try:
            laya = normalise_answers(r["answers"], triage_cfg, retemper_with)
        except Exception as exc:   # noqa: BLE001
            results.append({"service": pack["service"], "status": "unavailable",
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
        usage = r.get("usage") or {}
        laya.update({
            "calibrated": calibration is not None,
            "latency_ms": round(per_service_ms, 1),
            "truncated": bool(usage.get("truncated")),
            "state_tokens": info["state_tokens"],
            "samples_dropped": info["samples_dropped"],
        })
        results.append({"service": pack["service"], "status": "ok", "state": state, "laya": laya})

    return {**base, "status": "ok", "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "results": results}
