"""Request latency observability: best-effort SQLite recording plus a
stats query endpoint.

Every API request (everything except ``GET /health``) is recorded with its
request id, method, path, status code, UTC start/end timestamps and
duration. Recording failures are swallowed -- observability bookkeeping must
never change the status or body of the tracked response.
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.middleware.base import BaseHTTPMiddleware

from . import database

logger = logging.getLogger("app.observability")

router = APIRouter()


def _utc_now_iso() -> str:
    """UTC ISO-8601 timestamp; the fixed +00:00 offset keeps the text
    representation lexicographically ordered by wall-clock time."""
    return datetime.now(timezone.utc).isoformat()


def _normalize_iso(value: str) -> str:
    """Parse a user-supplied ISO-8601 timestamp and normalize it to UTC.

    Raises ``ValueError`` on anything ``datetime.fromisoformat`` cannot
    parse; a timestamp without timezone info is assumed to be UTC.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class RequestLatencyMiddleware(BaseHTTPMiddleware):
    """Record latency for every API request except ``GET /health``.

    Only the successful health probe itself is skipped; a wrong method on
    ``/health`` (e.g. a 405) is still recorded. The record write is best
    effort: any failure (database locked, disk full, ...) is logged and
    dropped, so the original response is returned untouched. The
    generated request id is also surfaced on the request state and echoed
    back in the ``X-Request-ID`` response header.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        started_perf = time.perf_counter()
        started_at = _utc_now_iso()

        status_code = 500
        response = None
        skip_recording = request.method == "GET" and request.url.path == "/health"
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            ended_at = _utc_now_iso()
            duration_ms = (time.perf_counter() - started_perf) * 1000.0
            if not skip_recording:
                try:
                    database.record_request_latency(
                        request_id=request_id,
                        method=request.method,
                        path=request.url.path,
                        status_code=status_code,
                        started_at=started_at,
                        ended_at=ended_at,
                        duration_ms=duration_ms,
                    )
                except Exception:
                    # Best-effort bookkeeping: never propagate to the response.
                    logger.warning("failed to record request latency", exc_info=True)
            # Correlate the response with its request id even when persistence
            # failed; a raised exception leaves no response to tag.
            if response is not None:
                response.headers["X-Request-ID"] = request_id


@router.get("/observability/latency")
def get_latency(
    start: Optional[str] = Query(
        default=None,
        description="Include requests started at/after this ISO-8601 timestamp (inclusive).",
    ),
    end: Optional[str] = Query(
        default=None,
        description="Include requests started before this ISO-8601 timestamp (exclusive).",
    ),
    path_prefix: Optional[str] = Query(
        default=None,
        description="Only include requests whose path starts with this prefix.",
    ),
):
    """Latency statistics over a half-open ``[start, end)`` time interval.

    Returns the total request count, the count of error responses
    (status >= 400), p50/p95 latency in milliseconds (nearest-rank), and the
    count grouped by status code. A malformed timestamp yields 422.
    """
    normalized = {}
    for name, value in (("start", start), ("end", end)):
        if value is not None:
            if not value:
                raise HTTPException(
                    status_code=422,
                    detail=f"{name} must be an ISO-8601 timestamp",
                )
            try:
                normalized[name] = _normalize_iso(value)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"{name} is not a valid ISO-8601 timestamp: {value!r}",
                )

    stats = database.latency_stats(
        start=normalized.get("start"),
        end=normalized.get("end"),
        path_prefix=path_prefix,
    )
    return {
        "start": normalized.get("start"),
        "end": normalized.get("end"),
        "path_prefix": path_prefix,
        **stats,
    }
