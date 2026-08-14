"""Streaming, background jobs, health and the index route."""

import asyncio
import time

from tests.conftest import SAMPLE_TEXT


# --------------------------------------------------------------------------
# Health / index
# --------------------------------------------------------------------------


async def test_health_is_sync_and_reports_the_timing_settings(client):
    body = (await client.get("/health")).json()

    assert body["status"] == "ok"
    assert body["ai_timeout_seconds"] > 0
    assert body["default_ai_delay_seconds"] >= 0


def test_health_stays_a_plain_def():
    """It awaits nothing, so it should not be async — the brief's instruction 2."""
    import inspect

    from app.routers.health import get_health

    assert not inspect.iscoroutinefunction(get_health)


async def test_index_separates_async_from_sync_endpoints(client):
    body = (await client.get("/")).json()

    assert "/study-pack" in body["async_endpoints"]
    assert "/health" in body["sync_endpoints"]


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


async def test_streaming_returns_an_event_stream(client):
    async with client.stream(
        "POST", "/chat/stream", json={"question": "What does async do?"}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        chunks = [chunk async for chunk in response.aiter_text()]

    joined = "".join(chunks)
    assert "data:" in joined
    assert "[DONE]" in joined


async def test_the_first_chunk_arrives_before_the_stream_finishes(client):
    """The point of streaming: output starts before the work is complete."""
    first_chunk_at = None
    started = time.perf_counter()

    async with client.stream(
        "POST", "/chat/stream", json={"question": "Why stream?"}
    ) as response:
        async for chunk in response.aiter_text():
            if chunk.strip() and first_chunk_at is None:
                first_chunk_at = time.perf_counter() - started

    total = time.perf_counter() - started

    assert first_chunk_at is not None
    # The first words showed up well before the whole answer was assembled.
    assert first_chunk_at < total, (
        f"first chunk at {first_chunk_at:.2f}s, total {total:.2f}s"
    )


async def test_streaming_validates_its_input(client):
    response = await client.post("/chat/stream", json={"question": ""})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------


async def test_starting_a_job_returns_202_with_a_job_id(client):
    """The job is accepted and identified, so the caller can poll for it.

    A note on what this test does NOT assert: that the response beats the work.

    Over a real socket it does — measured against uvicorn, the 202 came back in
    4.5ms while the job took 2s. But httpx's ASGITransport calls the app
    in-process with no socket involved, so "response sent" and "app finished"
    happen at the same moment and Starlette's background task runs before the
    client sees anything. Timing the response here would measure the test
    transport, not the endpoint.

    The real-server timing is recorded in docs/async-upgrade-report.md instead,
    where it can be reproduced with curl.
    """
    response = await client.post(
        "/jobs/summarise", params={"text": SAMPLE_TEXT, "delay_seconds": 0.2}
    )

    body = response.json()

    assert response.status_code == 202
    assert body["status"] == "queued"
    assert body["job_id"]
    assert body["poll_url"] == f"/jobs/{body['job_id']}"


async def test_a_job_finishes_and_its_result_can_be_polled(client):
    start = await client.post(
        "/jobs/summarise", params={"text": SAMPLE_TEXT, "delay_seconds": 0.2}
    )
    job_id = start.json()["job_id"]

    # Poll until it settles, with a ceiling so a stuck job fails the test.
    for _ in range(50):
        status_body = (await client.get(f"/jobs/{job_id}")).json()
        if status_body["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.1)

    assert status_body["status"] == "done", status_body
    assert status_body["result"]
    assert status_body["finished_at"] is not None


async def test_polling_an_unknown_job_is_404(client):
    response = await client.get("/jobs/does-not-exist")
    assert response.status_code == 404


async def test_a_job_rejects_text_that_is_too_short(client):
    response = await client.post("/jobs/summarise", params={"text": "short"})
    assert response.status_code == 422
