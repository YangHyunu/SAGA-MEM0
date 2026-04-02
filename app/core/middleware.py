import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.metrics import http_request_duration_seconds, http_requests_total
from app.core.tracing import tracer

logger: structlog.stdlib.BoundLogger = structlog.get_logger()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        session_id = request.headers.get("x-session-id", "")
        user_id = request.headers.get("x-user-id", "")

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
        )

        with tracer.start_as_current_span("http.request") as span:
            span.set_attributes({
                "http.method": request.method,
                "http.url": str(request.url),
                "http.request_id": request_id,
            })

            t0 = time.monotonic()

            try:
                response = await call_next(request)
            except Exception:
                elapsed = time.monotonic() - t0
                http_request_duration_seconds.labels(
                    method=request.method,
                    endpoint=request.url.path,
                    status="500",
                ).observe(elapsed)
                http_requests_total.labels(
                    method=request.method,
                    endpoint=request.url.path,
                    status="500",
                ).inc()
                logger.exception(
                    "http_request_error",
                    method=request.method,
                    path=request.url.path,
                    duration=round(elapsed, 3),
                )
                raise

            elapsed = time.monotonic() - t0
            status = str(response.status_code)

            http_request_duration_seconds.labels(
                method=request.method,
                endpoint=request.url.path,
                status=status,
            ).observe(elapsed)
            http_requests_total.labels(
                method=request.method,
                endpoint=request.url.path,
                status=status,
            ).inc()

            span.set_attributes({
                "http.status_code": response.status_code,
                "http.duration_ms": round(elapsed * 1000, 1),
            })

            response.headers["X-Request-ID"] = request_id

            logger.info(
                "http_request_completed",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration=round(elapsed, 3),
            )

            return response
