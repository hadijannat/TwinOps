"""Authentication helpers and middleware."""

from __future__ import annotations

import hashlib
import re
import ssl
from dataclasses import dataclass
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from twinops.common.logging import get_logger
from twinops.common.settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuthContext:
    """Authenticated request context."""

    subject: str
    roles: tuple[str, ...]
    method: str
    fingerprint: str | None = None


class AuthError(Exception):
    """Authentication error with HTTP status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _parse_roles(value: str) -> tuple[str, ...]:
    return tuple(role.strip() for role in value.split(",") if role.strip())


def _format_subject(subject: Iterable[Iterable[tuple[str, str]]]) -> str:
    parts: list[str] = []
    for rdn in subject:
        for key, value in rdn:
            parts.append(f"{key}={value}")
    return ",".join(parts)


def _parse_xfcc_subject(value: str) -> tuple[str | None, str | None]:
    """Parse subject and hash from X-Forwarded-Client-Cert header."""
    first = value.split(",", 1)[0]
    subject = None
    fingerprint = None

    subject_match = re.search(r'Subject="([^"]+)"', first)
    if subject_match:
        subject = subject_match.group(1)
    else:
        subject_match = re.search(r"Subject=([^;]+)", first)
        if subject_match:
            subject = subject_match.group(1)

    hash_match = re.search(r"Hash=([^;]+)", first)
    if hash_match:
        fingerprint = hash_match.group(1)

    return subject, fingerprint


def _extract_mtls_identity(request: Request, settings: Settings) -> tuple[str | None, str | None]:
    ssl_object = request.scope.get("ssl_object")
    if ssl_object:
        try:
            cert = ssl_object.getpeercert()  # type: ignore[call-arg]
        except ssl.SSLError as exc:
            logger.warning("Failed to read peer certificate", error=str(exc))
            cert = None

        if cert and cert.get("subject"):
            subject = _format_subject(cert.get("subject", ()))
        else:
            subject = None

        fingerprint = None
        try:
            cert_bytes = ssl_object.getpeercert(binary_form=True)  # type: ignore[call-arg]
            if cert_bytes:
                fingerprint = hashlib.sha256(cert_bytes).hexdigest()
        except ssl.SSLError:
            fingerprint = None

        if subject or fingerprint:
            return subject, fingerprint

    if settings.mtls_trust_proxy_headers:
        subject = None
        fingerprint = None
        if settings.mtls_subject_header:
            subject = request.headers.get(settings.mtls_subject_header)
        if not subject and settings.mtls_forwarded_cert_header:
            header_value = request.headers.get(settings.mtls_forwarded_cert_header)
            if header_value:
                subject, fingerprint = _parse_xfcc_subject(header_value)
        return subject, fingerprint

    return None, None


def authenticate_request(request: Request, settings: Settings) -> AuthContext:
    """Authenticate a request and return an AuthContext."""
    if settings.auth_mode == "none":
        roles_header = request.headers.get("X-Roles", "")
        roles = _parse_roles(roles_header) or settings.default_roles
        subject = request.headers.get("X-Subject", "anonymous")
        return AuthContext(subject=subject, roles=roles, method="header")

    subject, fingerprint = _extract_mtls_identity(request, settings)
    if not subject:
        raise AuthError(401, "Client certificate required")

    roles = settings.mtls_role_map.get(subject, [])
    if roles:
        return AuthContext(subject=subject, roles=tuple(roles), method="mtls", fingerprint=fingerprint)

    if settings.mtls_allow_unmapped:
        return AuthContext(
            subject=subject,
            roles=settings.default_roles,
            method="mtls",
            fingerprint=fingerprint,
        )

    raise AuthError(403, "Client certificate not authorized")


class AuthMiddleware(BaseHTTPMiddleware):
    """Authentication middleware for the API."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings
        self._exempt_paths = set(settings.auth_exempt_paths)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        try:
            auth = authenticate_request(request, self._settings)
        except AuthError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status_code)

        request.state.auth = auth
        return await call_next(request)
