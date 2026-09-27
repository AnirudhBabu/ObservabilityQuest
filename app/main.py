"""
order-service
=============
A small checkout service used as the "system under observation" for the
reliability stack. It deliberately models three realistic sub-operations
(payment validation, inventory reservation, card charge) as separate spans
so traces show meaningful internal structure, and exposes RED metrics
(Rate, Errors, Duration) plus one business metric (checkout value) for
Prometheus.

Failure/latency injection is exposed via /admin/chaos so the stack can be
driven hard enough to trip the SLO burn-rate alerts on purpose -- this is
what makes the demo credible instead of just "green dashboards forever".
"""
import os
import random
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# ---------------------------------------------------------------------
# OpenTelemetry tracing setup
# ---------------------------------------------------------------------
resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "order-service")})
provider = TracerProvider(resource=resource)
otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("order-service")

# ---------------------------------------------------------------------
# Prometheus RED metrics
#   Rate     -> http_requests_total
#   Errors   -> http_requests_total{status="error"} / http_requests_total
#   Duration -> http_request_duration_seconds (histogram -> latency SLOs)
# plus one business metric: checkout_value_total
# ---------------------------------------------------------------------
REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "route", "status"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "route"],
    buckets=(0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 2, 5),
)
CHECKOUT_VALUE = Counter("checkout_value_total", "Total value processed through checkout ($)")
CHECKOUT_ERRORS = Counter("checkout_errors_total", "Checkout failures", ["stage"])

# Chaos knobs -- mutable at runtime via /admin/chaos to demo alert firing
STATE = {
    "error_rate": float(os.getenv("BASE_ERROR_RATE", "0.01")),
    "latency_ms": float(os.getenv("BASE_LATENCY_MS", "80")),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    FastAPIInstrumentor.instrument_app(app)
    yield


app = FastAPI(title="order-service", lifespan=lifespan)


class ChaosConfig(BaseModel):
    error_rate: float | None = None
    latency_ms: float | None = None


class CheckoutRequest(BaseModel):
    cart_value: float = 42.00


def _instrumented(route: str):
    def decorator(fn):
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            status = "ok"
            try:
                return await fn(*args, **kwargs)
            except HTTPException:
                status = "error"
                raise
            finally:
                REQUEST_LATENCY.labels("POST", route).observe(time.perf_counter() - start)
                REQUEST_COUNT.labels("POST", route, status).inc()
        return wrapper
    return decorator


def _simulate_stage(span_name: str, base_ms: float, jitter_ms: float, fail_prob: float):
    """Runs one sub-operation as its own span; raises on injected failure."""
    with tracer.start_as_current_span(span_name) as span:
        delay = max(0.0, random.gauss(base_ms, jitter_ms)) / 1000.0
        time.sleep(delay)
        span.set_attribute("delay_ms", delay * 1000)
        if random.random() < fail_prob:
            span.set_attribute("error", True)
            CHECKOUT_ERRORS.labels(stage=span_name).inc()
            raise RuntimeError(f"{span_name} failed")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/checkout")
@_instrumented("/checkout")
async def checkout(req: CheckoutRequest):
    base_latency = STATE["latency_ms"]
    err = STATE["error_rate"]
    with tracer.start_as_current_span("checkout") as span:
        span.set_attribute("cart.value", req.cart_value)
        try:
            _simulate_stage("validate_payment", base_latency * 0.4, 15, err / 3)
            _simulate_stage("reserve_inventory", base_latency * 0.3, 10, err / 3)
            _simulate_stage("charge_card", base_latency * 0.3, 20, err / 3)
        except RuntimeError as exc:
            span.record_exception(exc)
            raise HTTPException(status_code=502, detail=str(exc))

        CHECKOUT_VALUE.inc(req.cart_value)
        return {"status": "confirmed", "cart_value": req.cart_value}


@app.post("/admin/chaos")
async def set_chaos(cfg: ChaosConfig):
    """Dial error rate / latency up or down to demonstrate SLO burn-rate
    alerts firing and clearing in the Grafana / Alertmanager UIs."""
    if cfg.error_rate is not None:
        STATE["error_rate"] = max(0.0, min(1.0, cfg.error_rate))
    if cfg.latency_ms is not None:
        STATE["latency_ms"] = max(0.0, cfg.latency_ms)
    return STATE
