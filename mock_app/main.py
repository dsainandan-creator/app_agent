"""
Mock FastAPI application that simulates a real service.
Exposes various endpoints, randomly produces 2xx and error responses,
and forwards every request log to PostgreSQL (observability DB).
"""

import asyncio
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from mock_app.database import init_db, log_request
from mock_app.traffic_simulator import run_simulator
from mock_app.dashboard import router as dashboard_router

logging.basicConfig(level=logging.INFO, format="%(message)s")

# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Launch the background traffic simulator
    task = asyncio.create_task(run_simulator())
    print("[APP] Mock service is ready. Traffic simulator running.")
    yield
    task.cancel()
    print("[APP] Mock service shut down.")


app = FastAPI(title="Mock Observability Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(dashboard_router)


# ---------------------------------------------------------------------------
# Middleware – log every request
# ---------------------------------------------------------------------------

@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    start = time.time()

    response = await call_next(request)

    elapsed_ms = (time.time() - start) * 1000
    status = response.status_code
    endpoint = str(request.url.path)
    method = request.method

    if status >= 500:
        msg = f"[{method}] {endpoint} → {status} Internal Server Error"
        err = "Unhandled exception in service layer"
    elif status >= 400:
        msg = f"[{method}] {endpoint} → {status} Client Error"
        err = "Bad request or resource not found"
    else:
        msg = f"[{method}] {endpoint} → {status} OK  ({elapsed_ms:.1f} ms)"
        err = None

    log_request(
        endpoint=endpoint,
        method=method,
        status_code=status,
        response_time_ms=round(elapsed_ms, 2),
        message=msg,
        request_id=request_id,
        error_detail=err,
    )

    return response


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def maybe_fail(error_rate: float = 0.25, codes: list[int] = None):
    """Randomly raise an HTTP error based on error_rate (0–1)."""
    if codes is None:
        codes = [500, 503]
    if random.random() < error_rate:
        raise HTTPException(status_code=random.choice(codes))


def random_latency(min_ms: int = 20, max_ms: int = 400):
    time.sleep(random.uniform(min_ms, max_ms) / 1000)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Always healthy."""
    return {"status": "ok", "service": "mock-app"}


@app.get("/api/users")
async def list_users():
    """List users – 20% chance of 500."""
    random_latency(30, 200)
    maybe_fail(error_rate=0.20, codes=[500])
    users = [{"id": i, "name": f"User {i}", "active": True} for i in range(1, 6)]
    return {"users": users, "count": len(users)}


@app.get("/api/users/{user_id}")
async def get_user(user_id: int):
    """Get a single user – 15% 404, 10% 500."""
    random_latency(20, 150)
    maybe_fail(error_rate=0.10, codes=[500])
    if random.random() < 0.15:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    return {"id": user_id, "name": f"User {user_id}", "email": f"user{user_id}@example.com"}


@app.post("/api/orders")
async def create_order(request: Request):
    """Create order – 25% 400/500 mix."""
    random_latency(50, 350)
    maybe_fail(error_rate=0.25, codes=[400, 500, 503])
    order_id = random.randint(10000, 99999)
    return {"order_id": order_id, "status": "created", "total": round(random.uniform(10, 500), 2)}


@app.get("/api/orders/{order_id}")
async def get_order(order_id: int):
    """Fetch order – 12% 404, 18% 500."""
    random_latency(30, 250)
    maybe_fail(error_rate=0.18, codes=[500, 503])
    if random.random() < 0.12:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return {"order_id": order_id, "status": "shipped", "total": round(random.uniform(10, 500), 2)}


@app.get("/api/payments/{payment_id}")
async def get_payment(payment_id: int):
    """Payment lookup – 30% error rate (critical path)."""
    random_latency(100, 500)
    maybe_fail(error_rate=0.30, codes=[500, 503, 502])
    if random.random() < 0.10:
        raise HTTPException(status_code=404, detail=f"Payment {payment_id} not found")
    return {
        "payment_id": payment_id,
        "status": random.choice(["completed", "pending"]),
        "amount": round(random.uniform(5, 1000), 2),
    }


@app.post("/api/payments")
async def process_payment(request: Request):
    """Process payment – 35% error rate."""
    random_latency(200, 800)
    maybe_fail(error_rate=0.35, codes=[500, 503, 422])
    return {"payment_id": random.randint(1, 9999), "status": "processing"}


@app.get("/api/products")
async def list_products():
    """Products – low 8% error rate."""
    random_latency(20, 100)
    maybe_fail(error_rate=0.08, codes=[500])
    products = [
        {"id": i, "name": f"Product {i}", "price": round(random.uniform(5, 200), 2)}
        for i in range(1, 11)
    ]
    return {"products": products}


@app.get("/api/inventory/{product_id}")
async def get_inventory(product_id: int):
    """Inventory – 22% error."""
    random_latency(40, 200)
    maybe_fail(error_rate=0.22, codes=[500, 503])
    return {"product_id": product_id, "in_stock": random.randint(0, 100)}


# ---------------------------------------------------------------------------
# Simulate endpoints (for testing)
# ---------------------------------------------------------------------------

@app.post("/simulate/spike-errors")
async def simulate_spike():
    """Injects 10 rapid error logs to simulate a failure spike."""
    import asyncio
    from mock_app.database import log_request as _log

    for i in range(10):
        code = random.choice([500, 503, 502])
        _log(
            endpoint="/api/payments",
            method="POST",
            status_code=code,
            response_time_ms=round(random.uniform(800, 3000), 2),
            message=f"[POST] /api/payments → {code} Payment gateway timeout",
            request_id=str(uuid.uuid4())[:8],
            error_detail="Connection to payment provider timed out after 3s",
        )
    return {"injected": 10, "type": "error_spike"}


@app.post("/simulate/critical")
async def simulate_critical():
    """Injects 20 critical 500 errors to trigger Sev-1."""
    from mock_app.database import log_request as _log

    for i in range(20):
        _log(
            endpoint=random.choice(["/api/payments", "/api/orders", "/api/users"]),
            method=random.choice(["GET", "POST"]),
            status_code=500,
            response_time_ms=round(random.uniform(1000, 5000), 2),
            message="CRITICAL: Service completely unresponsive",
            request_id=str(uuid.uuid4())[:8],
            error_detail="Database connection pool exhausted – all threads blocked",
        )
    return {"injected": 20, "type": "critical_failure"}


@app.get("/api/status")
async def app_status():
    return {
        "service": "mock-app",
        "version": "1.0.0",
        "endpoints": [
            "GET /health",
            "GET /api/users",
            "GET /api/users/{id}",
            "POST /api/orders",
            "GET /api/orders/{id}",
            "GET /api/payments/{id}",
            "POST /api/payments",
            "GET /api/products",
            "GET /api/inventory/{id}",
            "POST /simulate/spike-errors",
            "POST /simulate/critical",
        ],
    }
