"""Uniform error responses: {"error": {"code", "message", "fields"?}}.

Request validation errors are rebuilt from scratch so that FastAPI's default
behaviour of echoing the offending `input` back to the client never applies:
a malformed `api_key` must not be reflected in a 422 (I4).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_found(what: str = "Not found") -> ApiError:
    return ApiError(404, "not_found", what)


def forbidden(message: str = "You don't have permission to do that") -> ApiError:
    return ApiError(403, "forbidden", message)


def _body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {
                "loc": [str(p) for p in err.get("loc", ())],
                "type": str(err.get("type", "")),
                # `msg` for our own validators is written by us and never contains input;
                # pydantic's built-in messages don't include the value either.
                "msg": str(err.get("msg", "")),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            _body("validation_error", "Invalid request", fields=fields), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}
        return JSONResponse(
            _body(code.get(exc.status_code, "http_error"), str(exc.detail)),
            status_code=exc.status_code,
        )
