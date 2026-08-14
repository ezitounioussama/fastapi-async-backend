"""Optional extras: streaming, and a background task.

Both show async doing something a plain synchronous handler cannot:

  * streaming sends the first words before the answer is finished
  * a background task returns immediately and finishes the work afterwards
"""

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.responses import StreamingResponse

from app import config
from app.ai_service import generate_summary, stream_answer
from app.schemas import (
    JobStartedResponse,
    JobStatusResponse,
    SAMPLE_TEXT,
    StreamRequest,
)

router = APIRouter(tags=["extras"])


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@router.post(
    "/chat/stream",
    summary="Stream an answer word by word",
    description=(
        "Returns text/event-stream. The client receives the first words "
        "immediately instead of waiting for the whole reply. Swagger UI shows the "
        "assembled body at the end, so `curl -N` demonstrates it better."
    ),
    response_class=StreamingResponse,
)
async def chat_stream(request: StreamRequest) -> StreamingResponse:
    """Hand each chunk to the client as it is produced.

    StreamingResponse consumes an async generator. Because the generator awaits
    between chunks, the event loop stays free to serve other requests while this
    response is still open — a blocking generator would hold the whole server.
    """

    async def event_stream():
        # Server-Sent Events format: "data: <payload>\n\n" per event.
        async for chunk in stream_answer(request.question):
            yield f"data: {chunk}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Stops nginx and similar proxies from buffering the whole response,
            # which would defeat the point of streaming.
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

# A dictionary is enough to demonstrate the pattern. It is per-process and
# disappears on restart, so a real deployment would use Redis or a database — the
# same distinction as any cache versus permanent storage.
_jobs: dict[str, dict] = {}


async def _run_summary_job(job_id: str, text: str, max_bullets: int, delay: float) -> None:
    """The work that happens after the response has already been sent."""
    _jobs[job_id]["status"] = "running"

    try:
        bullets = await asyncio.wait_for(
            generate_summary(text, max_bullets, delay_seconds=delay),
            timeout=config.AI_TIMEOUT_SECONDS + delay,
        )
        _jobs[job_id].update(
            status="done",
            result=bullets,
            finished_at=datetime.now(timezone.utc),
        )

    except (asyncio.TimeoutError, TimeoutError):
        _jobs[job_id].update(
            status="failed",
            detail="The job timed out.",
            finished_at=datetime.now(timezone.utc),
        )

    except Exception as error:  # noqa: BLE001 - a background failure must be recorded, not lost
        # Nothing is listening for an exception here: the response has already
        # gone out. Unless the failure is stored, the job would silently hang in
        # "running" forever.
        _jobs[job_id].update(
            status="failed",
            detail=f"Unexpected error ({type(error).__name__}).",
            finished_at=datetime.now(timezone.utc),
        )


@router.post(
    "/jobs/summarise",
    response_model=JobStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a summary in the background",
    description=(
        "Returns 202 immediately with a job id, then finishes the work afterwards. "
        "Poll `/jobs/{job_id}` for the result."
    ),
)
async def start_summary_job(
    background_tasks: BackgroundTasks,
    text: str = SAMPLE_TEXT,
    max_bullets: int = 3,
    delay_seconds: float = 2.0,
) -> JobStartedResponse:
    """Queue the work and reply straight away.

    202 Accepted rather than 200: the request was accepted, but the result does
    not exist yet. This is the right shape for work too slow to wait on — the
    client is not left holding an open connection.
    """
    if len(text) < config.TEXT_MIN:
        raise HTTPException(
            status_code=422,
            detail=f"text must be at least {config.TEXT_MIN} characters.",
        )

    job_id = uuid.uuid4().hex[:12]

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "created_at": datetime.now(timezone.utc),
        "finished_at": None,
        "result": None,
        "detail": None,
    }

    # FastAPI runs this after the response has been sent.
    background_tasks.add_task(
        _run_summary_job,
        job_id,
        text,
        max_bullets,
        min(max(delay_seconds, 0.0), config.MAX_SIMULATED_DELAY),
    )

    return JobStartedResponse(
        job_id=job_id,
        status="queued",
        poll_url=f"/jobs/{job_id}",
        note="The response arrived before the work finished. Poll poll_url for the result.",
    )


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    responses={404: {"description": "Unknown job id"}},
    summary="Check a background job",
)
async def get_job(job_id: str) -> JobStatusResponse:
    job = _jobs.get(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id.")

    return JobStatusResponse(**job)
