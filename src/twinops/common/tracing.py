"""OpenTelemetry tracing for distributed observability."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Span, Status, StatusCode

from twinops.common.logging import get_logger

logger = get_logger(__name__)

# Type variable for preserving function signatures in decorators
F = TypeVar("F", bound=Callable[..., Any])

# Global tracer instance
_tracer: trace.Tracer | None = None


def setup_tracing(
    service_name: str = "twinops-agent",
    otlp_endpoint: str | None = None,
    enable_console: bool = False,
) -> trace.Tracer:
    """
    Configure OpenTelemetry tracing.

    Args:
        service_name: Name of the service for traces
        otlp_endpoint: OTLP collector endpoint (e.g., "localhost:4317")
        enable_console: Enable console span exporter for debugging

    Returns:
        Configured tracer instance
    """
    global _tracer

    # Create resource with service info
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "1.0.0",
        }
    )

    # Create tracer provider
    provider = TracerProvider(resource=resource)

    # Add OTLP exporter if endpoint provided
    if otlp_endpoint:
        otlp_exporter = OTLPSpanExporter(
            endpoint=otlp_endpoint,
            insecure=True,  # TODO: Configure TLS in production
        )
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info("OTLP tracing enabled", endpoint=otlp_endpoint)

    # Add console exporter for debugging
    if enable_console:
        console_exporter = ConsoleSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(console_exporter))
        logger.info("Console tracing enabled")

    # Set global tracer provider
    trace.set_tracer_provider(provider)

    # Get tracer
    _tracer = trace.get_tracer(service_name)

    return _tracer


def get_tracer() -> trace.Tracer:
    """Get the configured tracer, or a no-op tracer if not configured."""
    global _tracer
    if _tracer is None:
        # Return no-op tracer if not configured
        return trace.get_tracer("twinops-agent")
    return _tracer


@contextmanager
def span(
    name: str,
    attributes: dict[str, Any] | None = None,
) -> Generator[Span, None, None]:
    """
    Create a traced span context manager.

    Args:
        name: Name of the span
        attributes: Optional span attributes

    Yields:
        The span object for adding events/attributes
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as current_span:
        if attributes:
            for key, value in attributes.items():
                current_span.set_attribute(key, value)
        try:
            yield current_span
        except Exception as e:
            current_span.set_status(Status(StatusCode.ERROR, str(e)))
            current_span.record_exception(e)
            raise


def trace_tool_execution(
    tool_name: str,
    risk_level: str,
    roles: tuple[str, ...],
    simulated: bool = False,
) -> Callable[[F], F]:
    """
    Decorator for tracing tool execution.

    Args:
        tool_name: Name of the tool being executed
        risk_level: Risk level (LOW, MEDIUM, HIGH, CRITICAL)
        roles: User roles executing the tool
        simulated: Whether this is a simulated execution
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            with tracer.start_as_current_span("execute_tool") as current_span:
                current_span.set_attribute("tool.name", tool_name)
                current_span.set_attribute("tool.risk_level", risk_level)
                current_span.set_attribute("tool.simulated", simulated)
                current_span.set_attribute("user.roles", ",".join(roles))

                try:
                    result = await func(*args, **kwargs)
                    current_span.set_attribute("tool.success", True)
                    return result
                except Exception as e:
                    current_span.set_attribute("tool.success", False)
                    current_span.set_attribute("tool.error", str(e))
                    current_span.set_status(Status(StatusCode.ERROR, str(e)))
                    current_span.record_exception(e)
                    raise

        return wrapper  # type: ignore[return-value]

    return decorator


def trace_llm_call(provider: str) -> Callable[[F], F]:
    """
    Decorator for tracing LLM API calls.

    Args:
        provider: LLM provider name (anthropic, openai, rules)
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            with tracer.start_as_current_span("llm_call") as current_span:
                current_span.set_attribute("llm.provider", provider)

                try:
                    result = await func(*args, **kwargs)
                    current_span.set_attribute("llm.success", True)
                    return result
                except Exception as e:
                    current_span.set_attribute("llm.success", False)
                    current_span.set_status(Status(StatusCode.ERROR, str(e)))
                    current_span.record_exception(e)
                    raise

        return wrapper  # type: ignore[return-value]

    return decorator


def trace_safety_evaluation() -> Callable[[F], F]:
    """Decorator for tracing safety kernel evaluations."""

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            with tracer.start_as_current_span("safety_evaluate") as current_span:
                try:
                    result = await func(*args, **kwargs)
                    current_span.set_attribute("safety.allowed", result.allowed)
                    current_span.set_attribute("safety.force_simulation", result.force_simulation)
                    current_span.set_attribute("safety.require_approval", result.require_approval)
                    if result.reason:
                        current_span.set_attribute("safety.reason", result.reason)
                    return result
                except Exception as e:
                    current_span.set_status(Status(StatusCode.ERROR, str(e)))
                    current_span.record_exception(e)
                    raise

        return wrapper  # type: ignore[return-value]

    return decorator


def trace_http_request(operation: str) -> Callable[[F], F]:
    """
    Decorator for tracing HTTP client requests.

    Args:
        operation: Name of the HTTP operation
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            with tracer.start_as_current_span(f"http_{operation}") as current_span:
                current_span.set_attribute("http.operation", operation)

                try:
                    result = await func(*args, **kwargs)
                    current_span.set_attribute("http.success", True)
                    return result
                except Exception as e:
                    current_span.set_attribute("http.success", False)
                    current_span.set_status(Status(StatusCode.ERROR, str(e)))
                    current_span.record_exception(e)
                    raise

        return wrapper  # type: ignore[return-value]

    return decorator


def add_span_event(name: str, attributes: dict[str, Any] | None = None) -> None:
    """
    Add an event to the current span.

    Args:
        name: Event name
        attributes: Optional event attributes
    """
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        current_span.add_event(name, attributes=attributes or {})


def set_span_attribute(key: str, value: Any) -> None:
    """
    Set an attribute on the current span.

    Args:
        key: Attribute key
        value: Attribute value
    """
    current_span = trace.get_current_span()
    if current_span and current_span.is_recording():
        current_span.set_attribute(key, value)
