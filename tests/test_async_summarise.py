"""Tests for the async /summarise endpoint: awaiting, timeout, error handling."""

import asyncio
import inspect
import time

import pytest

from app import config
from app.ai_service import UpstreamAIError, generate_summary
from app.routers.summarise import post_summarise
from tests.conftest import SAMPLE_TEXT


# ---------------------------------------------------------------------------
# The endpoint is genuinely async, and returns a result rather than a coroutine
# ---------------------------------------------------------------------------


def test_the_endpoint_is_a_coroutine_function():
    """`async def`, not `def`."""
    assert inspect.iscoroutinefunction(post_summarise)


def test_the_ai_service_functions_are_coroutine_functions():
    assert inspect.iscoroutinefunction(generate_summary)


def test_calling_the_service_without_await_gives_a_coroutine():
    """The bug this checkpoint is about.

    Calling an async function returns a coroutine object; the work has not run.
    Only `await` produces the value. Returning the un-awaited call from an
    endpoint is the classic mistake, so this test documents the difference.
    """
    coroutine = generate_summary(SAMPLE_TEXT, 2, delay_seconds=0)

    assert inspect.iscoroutine(coroutine)
    assert not isinstance(coroutine, list)

    # Close it explicitly, otherwise Python warns that it was never awaited.
    coroutine.close()


async def test_awaiting_the_service_gives_the_real_value():
    bullets = await generate_summary(SAMPLE_TEXT, 2, delay_seconds=0)

    assert isinstance(bullets, list)
    assert all(isinstance(bullet, str) for bullet in bullets)


async def test_the_endpoint_returns_json_not_a_coroutine(client):
    """A coroutine would fail serialisation; a list serialises cleanly."""
    response = await client.post(
        "/summarise", json={"text": SAMPLE_TEXT, "max_bullets": 2, "simulate_delay_seconds": 0}
    )

    body = response.json()
    assert response.status_code == 200
    assert isinstance(body["bullets"], list)
    assert body["bullet_count"] == len(body["bullets"])
    assert "coroutine" not in response.text


# ---------------------------------------------------------------------------
# Normal behaviour
# ---------------------------------------------------------------------------


async def test_summarise_respects_max_bullets(client):
    for maximum in (1, 2, 3):
        response = await client.post(
            "/summarise",
            json={"text": SAMPLE_TEXT, "max_bullets": maximum, "simulate_delay_seconds": 0},
        )
        assert response.json()["bullet_count"] <= maximum


async def test_bullets_are_taken_from_the_source_text(client):
    """Extractive: nothing may be invented."""
    body = (
        await client.post(
            "/summarise",
            json={"text": SAMPLE_TEXT, "max_bullets": 3, "simulate_delay_seconds": 0},
        )
    ).json()

    for bullet in body["bullets"]:
        assert bullet in SAMPLE_TEXT


async def test_elapsed_time_reflects_the_simulated_delay(client):
    """Proves the endpoint really waited for the awaited call."""
    body = (
        await client.post(
            "/summarise",
            json={"text": SAMPLE_TEXT, "max_bullets": 2, "simulate_delay_seconds": 0.3},
        )
    ).json()

    assert body["elapsed_seconds"] >= 0.3


async def test_validation_still_applies(client):
    for payload in (
        {"text": "too short", "max_bullets": 2},
        {"text": SAMPLE_TEXT, "max_bullets": 0},
        {"text": SAMPLE_TEXT, "max_bullets": 99},
        {"max_bullets": 2},
    ):
        response = await client.post("/summarise", json=payload)
        assert response.status_code == 422, payload


# ---------------------------------------------------------------------------
# THE TIMEOUT TEST (required by the brief)
# ---------------------------------------------------------------------------


async def test_a_slow_ai_call_returns_504_instead_of_hanging(client):
    """The timeout test.

    The AI call is told to take 10 seconds while the endpoint's ceiling is 3, so
    the request must come back as 504 in about 3 seconds — not hang for 10, and
    not raise.
    """
    started = time.perf_counter()

    response = await client.post(
        "/summarise",
        json={"text": SAMPLE_TEXT, "max_bullets": 2, "simulate_delay_seconds": 10},
    )

    elapsed = time.perf_counter() - started

    assert response.status_code == 504
    assert "did not answer within" in response.json()["detail"]

    # It gave up near the deadline rather than waiting for the full 10 seconds.
    assert elapsed < config.AI_TIMEOUT_SECONDS + 2, f"took {elapsed:.2f}s"
    assert elapsed >= config.AI_TIMEOUT_SECONDS - 0.5, f"gave up too early at {elapsed:.2f}s"


async def test_a_call_just_under_the_timeout_still_succeeds(client):
    """The boundary in the other direction: a slow-but-acceptable call works."""
    response = await client.post(
        "/summarise",
        json={
            "text": SAMPLE_TEXT,
            "max_bullets": 2,
            "simulate_delay_seconds": config.AI_TIMEOUT_SECONDS - 1.5,
        },
    )

    assert response.status_code == 200


async def test_wait_for_cancels_the_slow_task():
    """asyncio.wait_for does not just stop waiting — it cancels the coroutine.

    Without cancellation the abandoned task would keep running in the background
    and keep holding whatever it was using.
    """
    cancelled = False

    async def slow_task():
        nonlocal cancelled
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled = True
            raise

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(slow_task(), timeout=0.1)

    assert cancelled, "the slow task should have been cancelled"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_a_temporary_upstream_failure_returns_503(client):
    response = await client.post(
        "/summarise",
        json={"text": SAMPLE_TEXT, "max_bullets": 2, "simulate_failure": True,
              "simulate_delay_seconds": 0},
    )

    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]


async def test_the_503_tells_the_client_to_retry(client):
    """Retry-After marks this as worth retrying, unlike a 4xx."""
    response = await client.post(
        "/summarise",
        json={"text": SAMPLE_TEXT, "max_bullets": 2, "simulate_failure": True,
              "simulate_delay_seconds": 0},
    )

    assert "retry-after" in {key.lower() for key in response.headers}


async def test_an_unexpected_error_returns_500_without_leaking_internals(client, monkeypatch):
    """Anything unforeseen becomes a clean 500, not a traceback."""

    async def exploding_summary(*args, **kwargs):
        raise RuntimeError("secret internal detail that must not be exposed")

    monkeypatch.setattr("app.routers.summarise.generate_summary", exploding_summary)

    response = await client.post(
        "/summarise", json={"text": SAMPLE_TEXT, "max_bullets": 2, "simulate_delay_seconds": 0}
    )

    assert response.status_code == 500
    assert "RuntimeError" in response.json()["detail"]  # the type is useful
    assert "secret internal detail" not in response.text  # the message is not


async def test_the_service_raises_the_upstream_error_type_directly():
    with pytest.raises(UpstreamAIError):
        await generate_summary(SAMPLE_TEXT, 2, delay_seconds=0, fail=True)
