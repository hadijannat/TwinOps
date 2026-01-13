"""Shared error helpers and codes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse


class ErrorCode:
    INVALID_JSON = "invalid_json"
    MISSING_FIELD = "missing_field"
    NOT_FOUND = "not_found"
    SERVER_NOT_READY = "server_not_ready"
    SERVER_SHUTTING_DOWN = "server_shutting_down"
    OP_FAILED = "operation_failed"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    BAD_REQUEST = "bad_request"


def error_response(
    code: str,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    payload = {
        "error": {
            "code": code,
            "message": message,
        }
    }
    if details:
        payload["error"]["details"] = details
    return JSONResponse(payload, status_code=status_code)
