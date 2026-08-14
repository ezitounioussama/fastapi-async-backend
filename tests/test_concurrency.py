"""Tests for /study-pack: that gather really runs the three tasks concurrently."""

import asyncio
import time

from tests.conftest import SAMPLE_TEXT


async def test_study_pack_returns_all_three_outputs(client):
    body = (
        await client.post(
            "/study-pack",
            json={"text": SAMPLE_TEXT, "simulate_delay_seconds": 0},
        )
    ).json()

    assert body["summary"] is not None
    assert body["quiz"] is not None
    assert body["flashcards"] is not None
    assert body["mode"] == "concurrent"


async def test_every_part_reports_ok(client):
    body = (
        await client.post("/study-pack", json={"text": SAMPLE_TEXT, "simulate_delay_seconds": 0})
    ).json()

    assert {part["name"] for part in body["parts"]} == {"summary", "quiz", "flashcards"}
    assert all(part["status"] == "ok" for part in body["parts"])


async def test_counts_follow_the_request(client):
    body = (
        await client.post(
            "/study-pack",
            json={
                "text": SAMPLE_TEXT,
                "max_bullets": 2,
                "num_questions": 4,
                "num_cards": 5,
                "simulate_delay_seconds": 0,
            },
        )
    ).json()

    assert len(body["summary"]) <= 2
    assert len(body["quiz"]) == 4
    assert len(body["flashcards"]) == 5


# ---------------------------------------------------------------------------
# The measurement that justifies gather
# ---------------------------------------------------------------------------


async def test_three_concurrent_tasks_take_about_as_long_as_one(client):
    """The core claim, measured.

    Each task waits 0.5s. Sequentially that is ~1.5s; concurrently it should be
    ~0.5s. The assertion allows generous headroom so the test is not flaky on a
    loaded machine, while still failing outright if the calls are serialised.
    """
    delay = 0.5

    started = time.perf_counter()
    response = await client.post(
        "/study-pack", json={"text": SAMPLE_TEXT, "simulate_delay_seconds": delay}
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200

    # Well under the 1.5s a sequential run would need.
    assert elapsed < delay * 2, f"took {elapsed:.2f}s — the tasks did not overlap"
    # And it did actually wait for the work.
    assert elapsed >= delay, f"finished in {elapsed:.2f}s, faster than one task should allow"


async def test_the_sequential_endpoint_is_measurably_slower(client):
    """Same work, one await at a time — the version gather replaces."""
    delay = 0.3
    payload = {"text": SAMPLE_TEXT, "simulate_delay_seconds": delay}

    concurrent = await client.post("/study-pack", json=payload)
    sequential = await client.post("/study-pack/sequential", json=payload)

    concurrent_time = concurrent.json()["elapsed_seconds"]
    sequential_time = sequential.json()["elapsed_seconds"]

    assert sequential.json()["mode"] == "sequential"
    assert sequential_time > concurrent_time

    # Three tasks at 0.3s each: sequential ~0.9s, concurrent ~0.3s.
    assert sequential_time >= delay * 3 * 0.8
    assert concurrent_time < delay * 2


async def test_the_compare_endpoint_reports_a_real_speedup(client):
    body = (await client.get("/study-pack/compare?delay_seconds=0.3")).json()

    assert body["sequential_seconds"] > body["concurrent_seconds"]
    assert body["speedup"] > 1.5
    assert body["time_saved_seconds"] > 0
    assert body["task_count"] == 3


async def test_gather_itself_overlaps_the_waiting():
    """The same property at the asyncio level, with no HTTP involved."""

    async def wait_a_moment(seconds: float) -> float:
        await asyncio.sleep(seconds)
        return seconds

    started = time.perf_counter()
    results = await asyncio.gather(
        wait_a_moment(0.3), wait_a_moment(0.3), wait_a_moment(0.3)
    )
    elapsed = time.perf_counter() - started

    assert results == [0.3, 0.3, 0.3]
    assert elapsed < 0.6, f"gather took {elapsed:.2f}s, so the sleeps did not overlap"


# ---------------------------------------------------------------------------
# Partial failure
# ---------------------------------------------------------------------------


async def test_one_failing_task_does_not_lose_the_other_two(client):
    """What return_exceptions=True buys.

    Without it, gather re-raises the first exception and the successful results
    are thrown away with it.
    """
    body = (
        await client.post(
            "/study-pack",
            json={"text": SAMPLE_TEXT, "simulate_delay_seconds": 0, "fail_part": "quiz"},
        )
    ).json()

    assert body["quiz"] is None
    assert body["summary"] is not None
    assert body["flashcards"] is not None

    statuses = {part["name"]: part["status"] for part in body["parts"]}
    assert statuses == {"summary": "ok", "quiz": "failed", "flashcards": "ok"}


async def test_the_response_is_still_200_on_a_partial_failure(client):
    """Two useful outputs out of three is a success with a caveat, not an error."""
    response = await client.post(
        "/study-pack",
        json={"text": SAMPLE_TEXT, "simulate_delay_seconds": 0, "fail_part": "summary"},
    )

    assert response.status_code == 200
    assert response.json()["summary"] is None


async def test_each_part_can_be_failed_independently(client):
    for part in ("summary", "quiz", "flashcards"):
        body = (
            await client.post(
                "/study-pack",
                json={"text": SAMPLE_TEXT, "simulate_delay_seconds": 0, "fail_part": part},
            )
        ).json()

        statuses = {item["name"]: item["status"] for item in body["parts"]}
        assert statuses[part] == "failed"
        assert sum(1 for value in statuses.values() if value == "ok") == 2


async def test_the_whole_pack_times_out_when_every_task_is_too_slow(client):
    """The group deadline, not a per-task one."""
    response = await client.post(
        "/study-pack", json={"text": SAMPLE_TEXT, "simulate_delay_seconds": 20}
    )

    assert response.status_code == 504
    assert "did not finish within" in response.json()["detail"]
