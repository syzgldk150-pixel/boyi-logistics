"""FastAPI route helpers for the versioned internal contract."""

from __future__ import annotations

import json

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from shared.contracts import api_failure, api_success
from shared.redaction import redact_text


def validation_failure(exc: RequestValidationError) -> dict:
    """Return validation metadata without echoing request values."""
    fields = [
        {
            "location": [str(part) for part in item.get("loc", ())],
            "type": str(item.get("type") or "validation_error"),
        }
        for item in exc.errors()
    ]
    return api_failure("validation_error", "Request validation failed", data={"fields": fields})


class EnvelopedRoute(APIRoute):
    """Wrap JSON route responses in the shared ``ok/data/error`` envelope."""

    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def enveloped_handler(request: Request):
            try:
                response = await original_handler(request)
            except RequestValidationError as exc:
                if not request.url.path.startswith("/internal/v1/"):
                    raise
                return JSONResponse(status_code=422, content=validation_failure(exc))
            except HTTPException as exc:
                if not request.url.path.startswith("/internal/v1/"):
                    raise
                return JSONResponse(
                    status_code=exc.status_code,
                    content=api_failure(f"http_{exc.status_code}", redact_text(exc.detail)),
                    headers=exc.headers,
                )
            if not request.url.path.startswith("/internal/v1/"):
                return response
            body = getattr(response, "body", b"")
            try:
                payload = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                return JSONResponse(
                    status_code=502,
                    content=api_failure("invalid_internal_response", "Internal route returned non-JSON data"),
                )
            if isinstance(payload, dict) and set(payload) >= {"ok", "data", "error"}:
                envelope = payload
            elif response.status_code >= 400:
                message = "Internal request failed"
                if isinstance(payload, dict):
                    message = str(payload.get("message") or payload.get("detail") or payload.get("error") or message)
                envelope = api_failure(f"http_{response.status_code}", message, data=payload)
            else:
                envelope = api_success(payload)
            headers = {
                name: value
                for name, value in response.headers.items()
                if name.lower() not in {"content-length", "content-type"}
            }
            return JSONResponse(status_code=response.status_code, content=envelope, headers=headers)

        return enveloped_handler
