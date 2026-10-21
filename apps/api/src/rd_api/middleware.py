"""HTTP middleware: request IDs, security headers, Prometheus request metrics."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from prometheus_client import Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUESTS = Counter("rd_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram(
    "rd_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0),
)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

Handler = Callable[[Request], Awaitable[Response]]


def _route_template(request: Request) -> str:
    """Route path template set by the router (``/v1/x/{id}``), so metrics never carry raw user paths."""
    route = request.scope.get("route")
    return str(getattr(route, "path", "unmatched"))


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        elapsed = time.perf_counter() - start
        route = _route_template(request)
        REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        LATENCY.labels(request.method, route).observe(elapsed)
        response.headers["x-request-id"] = request_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """The API serves JSON only, so the CSP denies everything."""

    def __init__(self, app: object, *, hsts: bool) -> None:
        super().__init__(app)  # type: ignore[arg-type]  # starlette types app as ASGIApp; FastAPI passes it through
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        response = await call_next(request)
        h = response.headers
        h["content-security-policy"] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        h["x-content-type-options"] = "nosniff"
        h["x-frame-options"] = "DENY"
        h["referrer-policy"] = "no-referrer"
        h["permissions-policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        h["cross-origin-opener-policy"] = "same-origin"
        h["cache-control"] = h.get("cache-control", "no-store")
        if self._hsts:
            h["strict-transport-security"] = "max-age=63072000; includeSubDomains; preload"
        return response
