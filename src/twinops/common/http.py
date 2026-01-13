"""HTTP client and request utilities."""

from __future__ import annotations

from dataclasses import dataclass
import contextvars
import uuid

import structlog

from starlette.requests import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp


@dataclass(frozen=True)
class RequestIdentity:
    """Identity context for outbound requests."""

    subject: str | None


def get_request_identity(request: Request) -> RequestIdentity:
    """Extract identity from request state."""
    auth = getattr(request.state, "auth", None)
    subject = getattr(auth, "subject", None) if auth else None
    return RequestIdentity(subject=subject)


_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "twinops_request_id",
    default=None,
)
_subject_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "twinops_subject",
    default=None,
)


def set_request_id(value: str | None) -> None:
    """Set request id in context."""
    _request_id_var.set(value)
    if value is not None:
        structlog.contextvars.bind_contextvars(request_id=value)


def get_request_id() -> str | None:
    """Get current request id."""
    return _request_id_var.get()


def set_subject(value: str | None) -> None:
    """Set current subject in context."""
    _subject_var.set(value)
    if value is not None:
        structlog.contextvars.bind_contextvars(subject=value)


def get_subject() -> str | None:
    """Get current subject."""
    return _subject_var.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request id to each request/response and context."""

    def __init__(self, app: ASGIApp, header_name: str = "X-Request-ID") -> None:
        super().__init__(app)
        self._header_name = header_name

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(self._header_name) or uuid.uuid4().hex
        request.state.request_id = request_id
        set_request_id(request_id)

        try:
            response = await call_next(request)
            response.headers.setdefault(self._header_name, request_id)
            return response
        finally:
            structlog.contextvars.clear_contextvars()
