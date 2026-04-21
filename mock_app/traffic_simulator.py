"""
traffic_simulator.py – Background traffic generator.

Continuously fires HTTP requests against the local FastAPI service to produce
realistic, mixed traffic without any manual curling. Cycles through four phases:

  NORMAL    → baseline healthy traffic, low error rate
  DEGRADED  → elevated errors, slower responses (e.g. upstream issues)
  SPIKE     → sudden burst of requests, many failures (e.g. deploy gone wrong)
  RECOVERY  → errors subsiding, latency returning to normal

Each phase lasts a random duration, then the cycle repeats.
"""

import asyncio
import logging
import random

import httpx

log = logging.getLogger("traffic_simulator")

BASE_URL = "http://localhost:8000"

# ---------------------------------------------------------------------------
# Traffic phase definitions
# ---------------------------------------------------------------------------

# Each phase specifies:
#   requests_per_second : float  – average throughput
#   endpoints           : list   – weighted pool of (method, path) to hit
#   extra_error_rate    : float  – additional forced-error probability on top
#                                  of each endpoint's own random failure
#   duration_range      : tuple  – (min_seconds, max_seconds) for this phase

PHASES = {
    "NORMAL": {
        "requests_per_second": 0.3,   # ~1 req every 3s — minimal DB writes
        "duration_range": (60, 120),
        "extra_error_rate": 0.0,
        "endpoints": [
            # (weight, method, path)
            (10, "GET",  "/health"),
            (8,  "GET",  "/api/users"),
            (6,  "GET",  "/api/users/1"),
            (6,  "GET",  "/api/users/2"),
            (5,  "POST", "/api/orders"),
            (5,  "GET",  "/api/orders/100"),
            (4,  "GET",  "/api/payments/1"),
            (4,  "POST", "/api/payments"),
            (8,  "GET",  "/api/products"),
            (4,  "GET",  "/api/inventory/1"),
        ],
    },
    "DEGRADED": {
        "requests_per_second": 0.3,
        "duration_range": (45, 90),
        "extra_error_rate": 0.20,   # push up error rate to clearly show degradation
        "endpoints": [
            (5,  "GET",  "/api/users"),
            (5,  "POST", "/api/orders"),
            (8,  "GET",  "/api/payments/1"),
            (8,  "POST", "/api/payments"),
            (4,  "GET",  "/api/inventory/1"),
            (3,  "GET",  "/api/orders/99"),
        ],
    },
    "SPIKE": {
        "requests_per_second": 0.8,   # ~1 req/s burst — enough to show spike without flooding DB
        "duration_range": (30, 60),
        "extra_error_rate": 0.35,     # high error rate to trigger SEV-1 or SEV-2
        "endpoints": [
            (4,  "POST", "/api/payments"),
            (4,  "GET",  "/api/payments/1"),
            (4,  "POST", "/api/orders"),
            (4,  "GET",  "/api/orders/200"),
            (3,  "GET",  "/api/users"),
            (2,  "GET",  "/api/inventory/5"),
        ],
    },
    "RECOVERY": {
        "requests_per_second": 0.2,
        "duration_range": (60, 120),
        "extra_error_rate": 0.05,
        "endpoints": [
            (10, "GET",  "/health"),
            (6,  "GET",  "/api/products"),
            (5,  "GET",  "/api/users"),
            (4,  "GET",  "/api/orders/100"),
            (3,  "POST", "/api/orders"),
            (3,  "GET",  "/api/payments/1"),
        ],
    },
}

PHASE_ORDER = ["NORMAL", "NORMAL", "DEGRADED", "SPIKE", "RECOVERY"]


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

def _pick_endpoint(phase_cfg: dict) -> tuple[str, str]:
    """Weighted random pick of (method, path) from the phase endpoint pool."""
    pool = phase_cfg["endpoints"]
    weights = [w for w, *_ in pool]
    chosen = random.choices(pool, weights=weights, k=1)[0]
    return chosen[1], chosen[2]   # method, path


async def _fire_request(client: httpx.AsyncClient, method: str, path: str):
    """Send one request; swallow errors so the loop never crashes."""
    try:
        url = BASE_URL + path
        if method == "POST":
            await client.post(url, json={}, timeout=5.0)
        else:
            await client.get(url, timeout=5.0)
    except Exception:
        pass   # connection errors during startup are expected – ignore


async def _run_phase(client: httpx.AsyncClient, phase_name: str):
    cfg = PHASES[phase_name]
    duration = random.uniform(*cfg["duration_range"])
    rps = cfg["requests_per_second"]
    interval = 1.0 / rps

    log.info(f"[traffic] Phase: {phase_name:10s} | {rps:.1f} req/s | {duration:.0f}s")

    deadline = asyncio.get_event_loop().time() + duration

    while asyncio.get_event_loop().time() < deadline:
        method, path = _pick_endpoint(cfg)

        # Optionally force an extra error by routing to a bad path
        if random.random() < cfg["extra_error_rate"]:
            path = random.choice([
                "/api/payments/9999",
                "/api/orders/9999",
                "/api/users/9999",
            ])

        asyncio.create_task(_fire_request(client, method, path))

        # Small jitter so requests don't arrive perfectly metered
        jitter = random.uniform(-interval * 0.2, interval * 0.2)
        await asyncio.sleep(max(0.05, interval + jitter))


async def run_simulator():
    """
    Main loop: cycles through phases indefinitely.
    Waits 3 seconds on startup to let the server fully bind.
    """
    await asyncio.sleep(3)
    log.info("[traffic] Simulator started – generating continuous traffic.")

    async with httpx.AsyncClient() as client:
        cycle = 0
        while True:
            for phase_name in PHASE_ORDER:
                # Occasionally inject an extra random phase
                if random.random() < 0.2:
                    phase_name = random.choice(list(PHASES.keys()))
                await _run_phase(client, phase_name)
            cycle += 1
            log.info(f"[traffic] Cycle {cycle} complete, restarting phase rotation.")