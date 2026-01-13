"""Prometheus metrics for TwinOps observability."""

from prometheus_client import Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response

# === Counters ===

TOOL_CALLS_TOTAL = Counter(
    "twinops_tool_calls_total",
    "Total number of tool calls",
    ["tool", "risk_level", "outcome"],  # outcome: success, denied, error, simulated
)

SAFETY_DECISIONS_TOTAL = Counter(
    "twinops_safety_decisions_total",
    "Total safety kernel decisions",
    ["decision", "reason"],  # decision: allowed, denied, forced_sim, approval_required
)

MQTT_EVENTS_TOTAL = Counter(
    "twinops_mqtt_events_total",
    "Total MQTT events processed",
    ["event_type"],  # event_type: property_update, element_create, element_delete
)

HTTP_REQUESTS_TOTAL = Counter(
    "twinops_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)

CIRCUIT_BREAKER_TRANSITIONS = Counter(
    "twinops_circuit_breaker_transitions_total",
    "Circuit breaker state transitions",
    ["from_state", "to_state"],
)

# === Histograms ===

TOOL_LATENCY = Histogram(
    "twinops_tool_latency_seconds",
    "Tool execution latency in seconds",
    ["tool"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

LLM_LATENCY = Histogram(
    "twinops_llm_latency_seconds",
    "LLM response latency in seconds",
    ["provider"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

HTTP_REQUEST_LATENCY = Histogram(
    "twinops_http_request_latency_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

TWIN_CLIENT_LATENCY = Histogram(
    "twinops_twin_client_latency_seconds",
    "Twin client HTTP operation latency",
    ["operation"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# === Gauges ===

SHADOW_TWIN_FRESHNESS = Gauge(
    "twinops_shadow_twin_freshness_seconds",
    "Seconds since last shadow twin update",
)

ACTIVE_REQUESTS = Gauge(
    "twinops_active_requests",
    "Number of currently active requests",
)

MQTT_CONNECTION_STATUS = Gauge(
    "twinops_mqtt_connected",
    "MQTT connection status (1=connected, 0=disconnected)",
)

CIRCUIT_BREAKER_STATE = Gauge(
    "twinops_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=half_open, 2=open)",
)

PENDING_APPROVALS = Gauge(
    "twinops_pending_approvals",
    "Number of operations pending human approval",
)


# === Helper Functions ===

def record_tool_call(
    tool: str,
    risk_level: str,
    outcome: str,
    latency: float,
) -> None:
    """Record a tool call with metrics."""
    TOOL_CALLS_TOTAL.labels(
        tool=tool,
        risk_level=risk_level,
        outcome=outcome,
    ).inc()
    TOOL_LATENCY.labels(tool=tool).observe(latency)


def record_safety_decision(decision: str, reason: str) -> None:
    """Record a safety kernel decision."""
    SAFETY_DECISIONS_TOTAL.labels(
        decision=decision,
        reason=reason,
    ).inc()


def record_llm_call(provider: str, latency: float) -> None:
    """Record an LLM call with latency."""
    LLM_LATENCY.labels(provider=provider).observe(latency)


def record_mqtt_event(event_type: str) -> None:
    """Record an MQTT event."""
    MQTT_EVENTS_TOTAL.labels(event_type=event_type).inc()


def record_http_request(
    method: str,
    endpoint: str,
    status: int,
    latency: float,
) -> None:
    """Record an HTTP request."""
    HTTP_REQUESTS_TOTAL.labels(
        method=method,
        endpoint=endpoint,
        status=str(status),
    ).inc()
    HTTP_REQUEST_LATENCY.labels(
        method=method,
        endpoint=endpoint,
    ).observe(latency)


def update_shadow_freshness(seconds_since_update: float) -> None:
    """Update shadow twin freshness gauge."""
    SHADOW_TWIN_FRESHNESS.set(seconds_since_update)


def update_mqtt_status(connected: bool) -> None:
    """Update MQTT connection status gauge."""
    MQTT_CONNECTION_STATUS.set(1 if connected else 0)


def update_circuit_breaker_state(state: str) -> None:
    """Update circuit breaker state gauge."""
    state_map = {"closed": 0, "half_open": 1, "open": 2}
    CIRCUIT_BREAKER_STATE.set(state_map.get(state, -1))


def update_active_requests(count: int) -> None:
    """Update active requests gauge."""
    ACTIVE_REQUESTS.set(count)


def update_pending_approvals(count: int) -> None:
    """Update pending approvals gauge."""
    PENDING_APPROVALS.set(count)


# === HTTP Endpoint ===

async def metrics_endpoint(_request: Request) -> Response:
    """
    Prometheus metrics endpoint.

    Returns metrics in Prometheus text format.
    """
    return Response(
        generate_latest(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
