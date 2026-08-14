"""POST /study-pack — three independent AI calls, run concurrently.

The point of this endpoint: a summary, a quiz and a set of flashcards are three
separate jobs. None needs another's output, so waiting for them one at a time
wastes exactly the time the other two could have been running in.

    sequential:  summary(0.6s) → quiz(0.6s) → flashcards(0.6s)  = ~1.8s
    concurrent:  all three started together, all awaited at once = ~0.6s

The saving is not a trick of arithmetic. Each call spends its time *waiting* on
someone else (here `asyncio.sleep`, in production a network round-trip), and a
waiting coroutine occupies no CPU. `asyncio.gather` starts all three, and the
event loop simply resumes whichever finishes first.

Concurrency only helps because the work is I/O-bound. Three CPU-heavy functions
would not speed up this way — they would take turns on the same core, and would
need processes or threads instead.
"""

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from app import config
from app.ai_service import (
    UpstreamAIError,
    generate_flashcards,
    generate_quiz,
    generate_summary,
)
from app.schemas import (
    ComparisonResponse,
    ErrorResponse,
    PartStatus,
    StudyPackRequest,
    StudyPackResponse,
)

router = APIRouter(tags=["study-pack"])


def _classify(error: BaseException) -> tuple[str, str]:
    """Map one task's exception to (status, detail) for its PartStatus entry."""
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "timeout", "This part did not finish in time."
    if isinstance(error, UpstreamAIError):
        return "failed", str(error)
    return "failed", f"Unexpected error ({type(error).__name__})."


@router.post(
    "/study-pack",
    response_model=StudyPackResponse,
    responses={
        504: {"model": ErrorResponse, "description": "All three tasks timed out"},
        500: {"model": ErrorResponse, "description": "Unexpected error"},
    },
    summary="Summary + quiz + flashcards, generated concurrently",
    description=(
        "Runs three independent AI calls with `asyncio.gather`, so the total time "
        "is roughly one call rather than three. `fail_part` forces one task to "
        "fail, which shows the other two still return."
    ),
)
async def post_study_pack(request: StudyPackRequest) -> StudyPackResponse:
    delay = (
        request.simulate_delay_seconds
        if request.simulate_delay_seconds is not None
        else config.DEFAULT_AI_DELAY
    )

    # Each task is told whether IT should fail, so a partial failure can be
    # demonstrated without taking the whole request down.
    def should_fail(part: str) -> bool:
        return request.simulate_failure or request.fail_part == part

    started = time.perf_counter()

    try:
        # gather schedules all three coroutines on the event loop at once.
        #
        # return_exceptions=True is the important flag: by default gather
        # re-raises the first exception and the caller loses the results of the
        # tasks that DID succeed. With it, each slot in the result list is either
        # a value or an exception object, so a failed quiz still leaves the
        # summary and flashcards usable.
        #
        # The whole group shares one deadline. Because the calls overlap, that
        # budget is sized for the slowest single call, not the sum of all three.
        results = await asyncio.wait_for(
            asyncio.gather(
                generate_summary(
                    request.text,
                    request.max_bullets,
                    delay_seconds=delay,
                    fail=should_fail("summary"),
                ),
                generate_quiz(
                    request.text,
                    request.num_questions,
                    delay_seconds=delay,
                    fail=should_fail("quiz"),
                ),
                generate_flashcards(
                    request.text,
                    request.num_cards,
                    delay_seconds=delay,
                    fail=should_fail("flashcards"),
                ),
                return_exceptions=True,
            ),
            timeout=config.STUDY_PACK_TIMEOUT_SECONDS,
        )

    except (asyncio.TimeoutError, TimeoutError):
        # The group as a whole blew the deadline, so there is nothing partial to
        # hand back.
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                f"The study pack did not finish within "
                f"{config.STUDY_PACK_TIMEOUT_SECONDS:g} seconds. Please try again."
            ),
        )

    except Exception as error:  # noqa: BLE001 — nothing should escape as a raw 500
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error building the study pack ({type(error).__name__}).",
        )

    elapsed = time.perf_counter() - started

    summary_result, quiz_result, flashcards_result = results

    parts: list[PartStatus] = []
    payload: dict = {"summary": None, "quiz": None, "flashcards": None}

    for name, result in (
        ("summary", summary_result),
        ("quiz", quiz_result),
        ("flashcards", flashcards_result),
    ):
        if isinstance(result, BaseException):
            state, detail = _classify(result)
            parts.append(PartStatus(name=name, status=state, detail=detail))
        else:
            payload[name] = result
            parts.append(PartStatus(name=name, status="ok"))

    return StudyPackResponse(
        summary=payload["summary"],
        quiz=payload["quiz"],
        flashcards=payload["flashcards"],
        parts=parts,
        mode="concurrent",
        elapsed_seconds=round(elapsed, 3),
        timestamp=datetime.now(timezone.utc),
    )


@router.post(
    "/study-pack/sequential",
    response_model=StudyPackResponse,
    summary="The same work, awaited one task at a time (for comparison)",
    description=(
        "Deliberately slower. Kept so the difference against `/study-pack` can be "
        "measured rather than taken on trust."
    ),
)
async def post_study_pack_sequential(request: StudyPackRequest) -> StudyPackResponse:
    """Await each task in turn — the version asyncio.gather replaces.

    Note that this is still `async def` and still uses `await`. That alone buys
    nothing: awaiting three things one after another takes just as long as three
    blocking calls. The gain comes from starting them together.
    """
    delay = (
        request.simulate_delay_seconds
        if request.simulate_delay_seconds is not None
        else config.DEFAULT_AI_DELAY
    )

    started = time.perf_counter()

    summary = await generate_summary(request.text, request.max_bullets, delay_seconds=delay)
    quiz = await generate_quiz(request.text, request.num_questions, delay_seconds=delay)
    flashcards = await generate_flashcards(request.text, request.num_cards, delay_seconds=delay)

    elapsed = time.perf_counter() - started

    return StudyPackResponse(
        summary=summary,
        quiz=quiz,
        flashcards=flashcards,
        parts=[
            PartStatus(name="summary", status="ok"),
            PartStatus(name="quiz", status="ok"),
            PartStatus(name="flashcards", status="ok"),
        ],
        mode="sequential",
        elapsed_seconds=round(elapsed, 3),
        timestamp=datetime.now(timezone.utc),
    )


@router.get(
    "/study-pack/compare",
    response_model=ComparisonResponse,
    summary="Measure sequential against concurrent",
    description=(
        "Runs the same three tasks both ways and reports the two timings, so the "
        "difference is a measurement rather than a claim."
    ),
)
async def compare_modes(delay_seconds: float = 0.4) -> ComparisonResponse:
    delay = min(max(delay_seconds, 0.0), config.MAX_SIMULATED_DELAY)
    text = (
        "Python is a high-level programming language. It is known for readable "
        "syntax. It is widely used for automation."
    )

    # Sequential: three awaits in a row.
    sequential_start = time.perf_counter()
    await generate_summary(text, 2, delay_seconds=delay)
    await generate_quiz(text, 2, delay_seconds=delay)
    await generate_flashcards(text, 2, delay_seconds=delay)
    sequential = time.perf_counter() - sequential_start

    # Concurrent: the same three, started together.
    concurrent_start = time.perf_counter()
    await asyncio.gather(
        generate_summary(text, 2, delay_seconds=delay),
        generate_quiz(text, 2, delay_seconds=delay),
        generate_flashcards(text, 2, delay_seconds=delay),
    )
    concurrent = time.perf_counter() - concurrent_start

    return ComparisonResponse(
        per_task_delay_seconds=delay,
        task_count=3,
        sequential_seconds=round(sequential, 3),
        concurrent_seconds=round(concurrent, 3),
        # Guard against a divide-by-zero when delay is 0.
        speedup=round(sequential / concurrent, 2) if concurrent > 0 else 0.0,
        time_saved_seconds=round(sequential - concurrent, 3),
        explanation=(
            f"Sequential waits {delay:g}s three times over. Concurrent starts all "
            f"three and waits once, because a coroutine that is waiting on I/O "
            f"uses no CPU and the event loop is free to run the others."
        ),
    )
