"""Standard error envelope (Phase Gate Protocol item 2 requires this for
every endpoint). Every error response — validation, auth, rate limit, or
unhandled — takes the same shape so API consumers (LeadBoost) never have to
special-case error parsing per endpoint.

    {"error": {"code": "...", "message": "...", "request_id": "..."}}
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class AuthError(ApiError):
    def __init__(self, message: str = "Authentication required or invalid.") -> None:
        super().__init__(code="unauthorized", message=message, status_code=status.HTTP_401_UNAUTHORIZED)


class RateLimitError(ApiError):
    def __init__(self, message: str = "Rate limit exceeded.") -> None:
        super().__init__(code="rate_limited", message=message, status_code=status.HTTP_429_TOO_MANY_REQUESTS)


def _envelope(code: str, message: str, request_id: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def _attach_request_id(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        logger.warning("api_error", code=exc.code, message=exc.message, request_id=request_id, path=request.url.path)
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, request_id),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_envelope("validation_error", str(exc.errors()), request_id),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        logger.error("unhandled_exception", error=str(exc), request_id=request_id, path=request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("internal_error", "An unexpected error occurred.", request_id),
        )
