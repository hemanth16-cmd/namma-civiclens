"""Consistent error responses.

Every error uses the shape from docs/api-contract.md:
    {"error": {"code": "...", "message": "..."}}
Validation errors add a "details" list of field problems. Unexpected errors
never leak exception text or stack traces to the client.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .lifecycle import RuleViolation
from .store import StoreError

logger = logging.getLogger("civiclens.api")

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    408: "request_timeout",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    507: "storage_quota_exceeded",
}


class ApiProblem(StarletteHTTPException):
    """An HTTP error with a specific `error.code` (for example "evidence_exists")."""

    def __init__(self, status_code: int, code: str, message: str, headers: dict | None = None, extra: dict | None = None):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.extra = extra or {}  # additional public fields for error{}, e.g. {"complaintId": ...}


def error_body(code: str, message: str, details: list | None = None, extra: dict | None = None) -> dict:
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    if extra:
        body["error"].update({k: v for k, v in extra.items() if k not in ("code", "message", "details")})
    return body


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = getattr(exc, "code", None) or _HTTP_CODES.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed."
    return JSONResponse(error_body(code, message, extra=getattr(exc, "extra", None)), status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"field": ".".join(str(part) for part in err.get("loc", ())), "message": err.get("msg", "Invalid value.")}
        for err in exc.errors()
    ]
    return JSONResponse(error_body("validation_error", "The request is not valid.", details), status_code=422)


async def _storage_error(request: Request, exc: StoreError) -> JSONResponse:
    logger.error("Storage error on %s %s: %s", request.method, request.url.path, exc, exc_info=exc.__cause__)
    return JSONResponse(
        error_body("storage_unavailable", "The complaint could not be saved or read right now. Try again shortly."),
        status_code=503,
    )


async def _rule_violation(request: Request, exc: RuleViolation) -> JSONResponse:
    """A complaint-management rule refused the action (lifecycle.py). The message is written for the user."""
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status, headers=headers)


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(error_body("internal_error", "Something went wrong on the server."), status_code=500)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StoreError, _storage_error)
    app.add_exception_handler(RuleViolation, _rule_violation)
    app.add_exception_handler(Exception, _unexpected_error)
