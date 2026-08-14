"""POST /summarise — the endpoint converted from sync to async.

Before (sync):

    @router.post("/summarise")
    def post_summarise(request: SummariseRequest):
        bullets = summarise_text(request.text, request.max_bullets)   # blocking
        return ...

After (async), which is what this file does:

    @router.post("/summarise")
    async def post_summarise(request: SummariseRequest):
        bullets = await asyncio.wait_for(generate_summary(...), timeout=...)
        return ...

`async def` is justified here because the body genuinely awaits something. An
`async def` that never awaits would be worse than the sync version: FastAPI runs
plain `def` endpoints in a worker thread, but runs `async def` endpoints directly
on the event loop — so blocking work inside `async def` stalls every other
request instead of just its own.
"""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from app import config
from app.ai_service import UpstreamAIError, generate_summary
from app.schemas import ErrorResponse, SummariseRequest, SummariseResponse

router = APIRouter(tags=["summarise"])


@router.post(
    "/summarise",
    response_model=SummariseResponse,
    responses={
        503: {"model": ErrorResponse, "description": "AI service temporarily unavailable"},
        504: {"model": ErrorResponse, "description": "AI call timed out"},
        500: {"model": ErrorResponse, "description": "Unexpected error"},
    },
    summary="Summarise text (async, with timeout)",
    description=(
        "Awaits the async AI service and returns bullets. Set "
        "`simulate_delay_seconds` above the timeout to see the 504 path, or "
        "`simulate_failure: true` to see the 503 path."
    ),
)
async def post_summarise(request: SummariseRequest) -> SummariseResponse:
    delay = (
        request.simulate_delay_seconds
        if request.simulate_delay_seconds is not None
        else config.DEFAULT_AI_DELAY
    )

    started = time.perf_counter()

    try:
        # asyncio.wait_for cancels the coroutine when the deadline passes and
        # raises TimeoutError. Without it a hung upstream call would hold this
        # request open indefinitely.
        #
        # Python 3.11+ also offers `async with asyncio.timeout(...)`, which reads
        # better when several awaits share one deadline; wait_for is clearer for
        # a single call like this.
        bullets = await asyncio.wait_for(
            generate_summary(
                request.text,
                request.max_bullets,
                delay_seconds=delay,
                fail=request.simulate_failure,
            ),
            timeout=config.AI_TIMEOUT_SECONDS,
        )

    except (asyncio.TimeoutError, TimeoutError):
        # In Python 3.11+ asyncio.TimeoutError IS the builtin TimeoutError; both
        # are listed so this still behaves on older versions.
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"The AI service did not answer within "
                f"{config.AI_TIMEOUT_SECONDS:g} seconds. Please try again."
            ),
        )

    except UpstreamAIError as error:
        # A transient upstream problem. 503 with a Retry-After header tells the
        # client this is worth retrying, unlike a 400-class error.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            # The exception's own message already says it is unavailable, so it is
            # passed through rather than wrapped in a second copy of the same words.
            detail=str(error),
            headers={"Retry-After": "5"},
        )

    except Exception as error:  # noqa: BLE001 — last resort, so nothing escapes as a 500 traceback
        # The exception type is included because it is useful, but the message is
        # kept generic: internal details do not belong in a client response.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error while summarising ({type(error).__name__}).",
        )

    elapsed = time.perf_counter() - started

    # `bullets` here is a list, not a coroutine — that is what `await` bought us.
    # Returning `generate_summary(...)` without awaiting would hand FastAPI a
    # coroutine object and fail serialisation, which is the classic beginner bug.
    return SummariseResponse(
        bullets=bullets,
        bullet_count=len(bullets),
        original_characters=len(request.text),
        elapsed_seconds=round(elapsed, 3),
        timestamp=datetime.now(timezone.utc),
    )
