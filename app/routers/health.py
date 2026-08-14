"""GET /health — plain `def` on purpose.

This endpoint awaits nothing, so it stays synchronous. FastAPI runs plain `def`
handlers in a worker thread, which is the right choice for work that does not
wait on I/O. Marking it `async def` for consistency would be a mistake: the
instruction is to convert an endpoint to async *only if* it awaits something.
"""

from datetime import datetime, timezone

from fastapi import APIRouter

from app import config
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service health check")
def get_health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=config.APP_VERSION,
        ai_timeout_seconds=config.AI_TIMEOUT_SECONDS,
        default_ai_delay_seconds=config.DEFAULT_AI_DELAY,
        timestamp=datetime.now(timezone.utc),
    )
