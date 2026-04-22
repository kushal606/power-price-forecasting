"""
monitoring.py — PowerCast Prometheus Metrics
=============================================
All metric objects are defined here and imported wherever needed.
Keeping metrics in a separate module avoids circular imports and
makes it trivial to disable monitoring by removing one import line.

Usage (in app.py):
    from monitoring import (
        metrics_blueprint,
        record_request,
        record_forecast,
        record_error,
        update_mcp_gauge,
    )

Metrics exposed at /metrics:
  powercast_http_requests_total          Counter
  powercast_http_request_duration_seconds Histogram
  powercast_forecast_requests_total      Counter
  powercast_forecast_duration_seconds    Histogram
  powercast_errors_total                 Counter
  powercast_last_mcp_predicted           Gauge
  powercast_avg_mcp_predicted            Gauge
  powercast_model_ready                  Gauge
"""

import time
import logging

from flask import Blueprint, Response, request, g

try:
    from prometheus_client import (
        Counter,
        Histogram,
        Gauge,
        generate_latest,
        CONTENT_TYPE_LATEST,
        REGISTRY,
    )
    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "[monitoring] prometheus_client not installed — metrics disabled.\n"
        "  Run:  pip install prometheus_client"
    )

log = logging.getLogger(__name__)

# =============================================================================
# 1. METRIC DEFINITIONS
#    Each metric is wrapped in a try/except so a duplicate-registration error
#    during Flask debug-mode double-import never crashes the app.
# =============================================================================

def _safe_metric(factory, *args, **kwargs):
    """Create a Prometheus metric, returning None if unavailable or duplicate."""
    if not _PROM_AVAILABLE:
        return None
    try:
        return factory(*args, **kwargs)
    except ValueError:
        # Already registered (can happen with Flask reloader)
        name = args[0] if args else kwargs.get('name', '?')
        return REGISTRY._names_to_collectors.get(name)


# ── HTTP layer ────────────────────────────────────────────────────────────────

HTTP_REQUESTS = _safe_metric(
    Counter,
    'powercast_http_requests_total',
    'Total HTTP requests received',
    ['method', 'endpoint', 'http_status'],
)

HTTP_LATENCY = _safe_metric(
    Histogram,
    'powercast_http_request_duration_seconds',
    'HTTP request latency in seconds',
    ['method', 'endpoint'],
    # Buckets tuned for a forecasting API: most requests < 30 s
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

# ── Forecast layer ────────────────────────────────────────────────────────────

FORECAST_REQUESTS = _safe_metric(
    Counter,
    'powercast_forecast_requests_total',
    'Total forecast API calls',
    ['horizon'],        # daily / weekly / monthly / seasonal / custom
)

FORECAST_DURATION = _safe_metric(
    Histogram,
    'powercast_forecast_duration_seconds',
    'Time taken to generate a forecast (seconds)',
    ['horizon'],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 60.0, 120.0],
)

# ── Error tracking ────────────────────────────────────────────────────────────

ERRORS = _safe_metric(
    Counter,
    'powercast_errors_total',
    'Total application errors',
    ['endpoint', 'error_type'],
)

# ── MCP prediction gauges (bonus) ─────────────────────────────────────────────

LAST_MCP = _safe_metric(
    Gauge,
    'powercast_last_mcp_predicted',
    'Last MCP value predicted by the model (₹/MWh)',
)

AVG_MCP = _safe_metric(
    Gauge,
    'powercast_avg_mcp_predicted',
    'Rolling average of predicted MCP values (₹/MWh)',
)

MODEL_READY = _safe_metric(
    Gauge,
    'powercast_model_ready',
    '1 if the ML model is loaded and ready, 0 otherwise',
)


# =============================================================================
# 2. HELPER FUNCTIONS
#    Called from app.py hooks and route handlers.
#    All functions are safe no-ops when prometheus_client is not installed.
# =============================================================================

def record_request(method: str, endpoint: str, status_code: int, duration: float):
    """
    Called from after_request hook.
    Increments request counter and records latency.

    :param method:     HTTP method (GET / POST)
    :param endpoint:   Flask endpoint name (e.g. 'api_forecast')
    :param status_code: HTTP status code returned
    :param duration:   Elapsed time in seconds (time.perf_counter())
    """
    if HTTP_REQUESTS:
        HTTP_REQUESTS.labels(
            method=method,
            endpoint=endpoint,
            http_status=str(status_code),
        ).inc()
    if HTTP_LATENCY:
        HTTP_LATENCY.labels(
            method=method,
            endpoint=endpoint,
        ).observe(duration)


def record_forecast(horizon: str, duration: float):
    """
    Call this immediately after a forecast completes successfully.

    :param horizon:  'daily' | 'weekly' | 'monthly' | 'seasonal' | 'custom'
    :param duration: How long the forecast took in seconds
    """
    if FORECAST_REQUESTS:
        FORECAST_REQUESTS.labels(horizon=horizon).inc()
    if FORECAST_DURATION:
        FORECAST_DURATION.labels(horizon=horizon).observe(duration)


def record_error(endpoint: str, error_type: str):
    """
    Call this in except blocks to count errors by endpoint and type.

    :param endpoint:   Flask endpoint name or URL path
    :param error_type: Short class name e.g. 'ValueError', 'RuntimeError'
    """
    if ERRORS:
        ERRORS.labels(endpoint=endpoint, error_type=error_type).inc()


def update_mcp_gauge(prices: list):
    """
    Update the MCP prediction gauges after a forecast.

    :param prices: List of predicted MCP values (₹/MWh)
    """
    if not prices:
        return
    if LAST_MCP:
        LAST_MCP.set(prices[-1])
    if AVG_MCP:
        avg = sum(prices) / len(prices)
        AVG_MCP.set(round(avg, 2))


def set_model_ready(ready: bool):
    """Update the model-ready gauge. Call from warm_up() completion."""
    if MODEL_READY:
        MODEL_READY.set(1 if ready else 0)


# =============================================================================
# 3. /metrics BLUEPRINT
#    Registered in app.py with app.register_blueprint(metrics_blueprint)
# =============================================================================

metrics_blueprint = Blueprint('metrics', __name__)


@metrics_blueprint.route('/metrics')
def prometheus_metrics():
    """
    Expose all Prometheus metrics in text format.
    Prometheus scrapes this endpoint on its configured interval.
    """
    if not _PROM_AVAILABLE:
        return (
            "# prometheus_client not installed\n"
            "# Run: pip install prometheus_client\n",
            503,
            {'Content-Type': 'text/plain'},
        )
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
