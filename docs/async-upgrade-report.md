# Async Upgrade Report

**Project:** AI Study Assistant API v3.0.0
**Date:** 14 August 2026
**Stack:** Python 3.14.6, FastAPI 0.141.1, uvicorn, pytest + httpx

Every number in this report was measured against a running uvicorn server on
`127.0.0.1:8032`, not estimated.

---

## 1. The upgraded endpoint

`POST /summarise` was the endpoint converted from sync to async.

### Before

```python
@router.post("/summarise")
def post_summarise(request: SummariseRequest):
    bullets = summarise_text(request.text, request.max_bullets)   # blocking call
    return SummariseResponse(bullets=bullets, ...)
```

### After

```python
@router.post("/summarise")
async def post_summarise(request: SummariseRequest) -> SummariseResponse:
    try:
        bullets = await asyncio.wait_for(
            generate_summary(request.text, request.max_bullets, delay_seconds=delay),
            timeout=config.AI_TIMEOUT_SECONDS,      # 3 seconds
        )
    except (asyncio.TimeoutError, TimeoutError):
        raise HTTPException(504, "The AI service did not answer within 3 seconds...")
    except UpstreamAIError as error:
        raise HTTPException(503, str(error), headers={"Retry-After": "5"})
    except Exception as error:
        raise HTTPException(500, f"Unexpected error while summarising ({type(error).__name__}).")

    return SummariseResponse(bullets=bullets, ...)
```

`async def` is justified because the body genuinely awaits something.

### Why `/health` was deliberately left as `def`

The brief says to convert an endpoint "only if it awaits an async operation", and this is not a
formality. FastAPI treats the two differently:

| | Where it runs | Blocking work inside it |
|---|---|---|
| `def` | a worker thread from the threadpool | slows only that request |
| `async def` | directly on the event loop | **stalls every other request** |

So an `async def` that never awaits is *worse* than the sync version — it moves blocking work onto
the one thread that everything else shares. `/health` awaits nothing, so it stays `def`, and a test
asserts that (`test_health_stays_a_plain_def`).

---

## 2. The async flow

```
   client
     │  POST /summarise
     ▼
   async def post_summarise                 ← runs on the event loop
     │
     │  await asyncio.wait_for(  ────────────┐
     │      generate_summary(...),           │  deadline: 3s
     │      timeout=3.0)                     │
     ▼                                       │
   async def generate_summary                │
     │                                       │
     │  await asyncio.sleep(0.6)   ← the coroutine SUSPENDS here.
     │                                the event loop is free to serve
     │                                other requests during this wait
     ▼                                       │
   returns list[str]  ─────────────────────┘
     │
     ▼
   SummariseResponse   ← a list, not a coroutine
```

### `asyncio.sleep`, not `time.sleep`

The fake AI service awaits `asyncio.sleep` to imitate network latency. `time.sleep` would block
the thread, and inside a coroutine that means freezing every other request for the duration —
precisely the problem async exists to avoid. `await asyncio.sleep(...)` suspends only this
coroutine.

Connecting a real model means replacing that one line with
`await client.chat.completions.create(...)`. Nothing in the routers changes.

### The coroutine-versus-result trap

Calling an async function does not run it — it returns a coroutine object:

```python
coroutine = generate_summary(text, 2)      # nothing has happened yet
bullets   = await generate_summary(text, 2)  # now it has, and this is a list
```

Returning the un-awaited call from an endpoint hands FastAPI a coroutine and serialisation fails.
Three tests pin this down: one asserts the un-awaited call *is* a coroutine, one asserts the
awaited call is a list, and one asserts the HTTP response contains real JSON with no `"coroutine"`
anywhere in the body.

---

## 3. The timeout test

`tests/test_async_summarise.py::test_a_slow_ai_call_returns_504_instead_of_hanging`

```python
async def test_a_slow_ai_call_returns_504_instead_of_hanging(client):
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
```

The AI call is told to take 10 seconds; the endpoint's ceiling is 3. The test asserts the deadline
from **both** sides — the request must not hang for 10s, and must not bail out early either, which
would pass a naive "it was fast" assertion even if the timeout were misconfigured to 0.1s.

Confirmed live:

```
POST /summarise  {"simulate_delay_seconds": 10}
  returned after: 3.001862s   status: 504
  {"detail":"The AI service did not answer within 3 seconds. Please try again."}
```

3.00 seconds, not 10.

### Cancellation, not just abandonment

`asyncio.wait_for` cancels the coroutine when the deadline passes; it does not leave it running.
A separate test proves it by catching `CancelledError` inside the slow task:

```python
async def slow_task():
    try:
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        cancelled = True
        raise
```

This matters because an abandoned-but-still-running task would keep holding its connection or
memory long after the client gave up.

---

## 4. Error handling

| Situation | Status | Retryable | Behaviour |
|---|---|---|---|
| AI call exceeds the timeout | **504** | yes | Message names the limit; the task is cancelled |
| Temporary upstream failure | **503** | yes | `Retry-After: 5` header included |
| Anything unexpected | **500** | no | Exception *type* shown, message withheld |
| Invalid request body | **422** | no | Field-by-field detail |

Measured:

```
POST /summarise  {"simulate_failure": true}
  status: 503
  {"detail":"The AI service is temporarily unavailable (simulated)."}
  retry-after: 5
```

The 500 path deliberately reveals the exception type but not its message. A test enforces this: it
patches the service to raise `RuntimeError("secret internal detail that must not be exposed")`,
then asserts `"RuntimeError"` appears in the response and `"secret internal detail"` does not.
Internal messages can carry file paths, queries, or connection strings.

---

## 5. Sequential versus concurrent

`POST /study-pack` generates three independent outputs — summary, quiz, flashcards — with
`asyncio.gather`. `POST /study-pack/sequential` does the same work one `await` at a time and is
kept only for comparison.

### Measured, three tasks at 0.6s each

| Mode | Server-measured | Client wall clock |
|---|---|---|
| Sequential (`await` ×3 in a row) | **1.801s** | 1.802s |
| Concurrent (`asyncio.gather`) | **0.601s** | 0.602s |

And from `GET /study-pack/compare?delay_seconds=0.6`, which runs both in one request:

```json
{
  "per_task_delay_seconds": 0.6,
  "task_count": 3,
  "sequential_seconds": 1.8,
  "concurrent_seconds": 0.6,
  "speedup": 3.0,
  "time_saved_seconds": 1.2
}
```

### Why the difference exists

```
sequential:  [── summary 0.6s ──][── quiz 0.6s ──][── flashcards 0.6s ──]   = 1.8s
                                                                    

concurrent:  [── summary 0.6s ──]
             [── quiz 0.6s ─────]      all three started together          = 0.6s
             [── flashcards 0.6s]
```

Each task spends its time **waiting** on something else — here `asyncio.sleep`, in production a
network round-trip. A coroutine that is waiting uses no CPU, so the event loop is free to start the
others and then resume whichever finishes first. Three tasks that each wait 0.6s can overlap those
waits almost perfectly.

The gain comes from *starting them together*, not from `async` itself. `/study-pack/sequential` is
also `async def` and also uses `await`, and it is still 3× slower — awaiting three things one
after another costs exactly as much as three blocking calls. `await` marks a suspension point;
`gather` is what creates the overlap.

### When async does NOT help

This only works because the work is **I/O-bound**. Three CPU-heavy functions — parsing a large
file, resizing images, crunching numbers — would not speed up this way. They need the CPU, not a
network reply, so they would take turns on one core and `gather` would add overhead for nothing.
CPU-bound work needs `ProcessPoolExecutor`, or `run_in_threadpool` to at least keep it off the
event loop.

The rule of thumb: **async helps when your code is waiting, not when it is working.**

### `return_exceptions=True`

```python
results = await asyncio.wait_for(
    asyncio.gather(summary_task, quiz_task, flashcards_task, return_exceptions=True),
    timeout=config.STUDY_PACK_TIMEOUT_SECONDS,
)
```

By default `gather` re-raises the first exception and the results of the tasks that *succeeded* are
discarded with it. With `return_exceptions=True` each slot is either a value or an exception, so a
failed quiz still leaves a usable summary and flashcards. Confirmed live:

```
POST /study-pack  {"fail_part": "quiz"}
  parts: [('summary','ok'), ('quiz','failed'), ('flashcards','ok')]
  summary present: True | quiz present: False | flashcards present: True
```

The response is still `200`: two of three outputs is a success with a caveat, and the caveat is
reported in the `parts` array rather than hidden.

Note also that the group's timeout is sized for the **slowest single call**, not the sum of all
three — which is only safe because they overlap.

---

## 6. Optional extras

### Streaming (`POST /chat/stream`)

Server-Sent Events driven by an async generator. Measured chunk arrival times:

```
+0.18s  data: Here
+0.33s  data: is
+0.48s  data: a
+0.63s  data: placeholder
...
+4.54s  data: [DONE]      ← last chunk
```

The first word reached the client at 0.18s while the full response took 4.54s. The `await` between
chunks is what allows this: the event loop stays free while the response is still open, so a
streaming request does not monopolise the server.

Reproduce with `curl -N` (shell pipelines buffer, so `curl | head` will make it look instant —
the timings above were taken with an httpx client reading chunk by chunk).

### Background task (`POST /jobs/summarise`)

Returns `202 Accepted` with a job id, then finishes the work afterwards. Measured against uvicorn:

```
POST /jobs/summarise?delay_seconds=2.0
  response time: 0.004464s   status: 202
```

**4.5 milliseconds** for a job that takes 2 seconds. Polling afterwards:

```json
{
  "job_id": "01cb355bee84",
  "status": "done",
  "created_at": "2026-08-14T18:31:48.963510Z",
  "finished_at": "2026-08-14T18:31:50.964145Z",
  "result": ["Python is a high-level programming language.", "..."]
}
```

The timestamps differ by exactly 2 seconds, confirming the work ran after the response was sent.

`202` rather than `200` is the correct code: the request was accepted, but the result does not
exist yet.

---

## 7. A testing pitfall worth recording

The first version of the background-task test asserted that the 202 came back faster than the job
took. **It failed** — the response blocked for the full 1.00s.

That was not a bug in the endpoint. httpx's `ASGITransport` calls the app in-process with no socket
involved, so "response sent" and "app finished" happen at the same moment, and Starlette runs the
background task before the test client sees anything. Timing that measures the test transport, not
the endpoint.

Checking against a real uvicorn server showed the endpoint was correct all along (4.5ms, above).
So the test now asserts what the in-process transport can actually observe — the 202, the job id
and the poll URL — and the real-server timing lives in this report where it is reproducible with
curl. The alternative, loosening the assertion until it passed, would have hidden the distinction
instead of documenting it.

---

## 8. Test results

```
$ pytest -q
.....................................                                    [100%]
37 passed in 22.31s
```

| File | Tests | Covers |
|---|---|---|
| `tests/test_async_summarise.py` | 16 | coroutine vs result, the timeout test, cancellation, 503/500 paths, validation |
| `tests/test_concurrency.py` | 12 | gather overlap measured, sequential comparison, partial failure, group timeout |
| `tests/test_extras.py` | 9 | streaming progressiveness, background job lifecycle, `/health` staying sync |

The suite takes ~22 seconds because several tests wait on real deadlines — the timeout test alone
spends 3 seconds proving the endpoint gives up on schedule.

---

## Summary of what changed

| Item | Before | After |
|---|---|---|
| `/summarise` | `def`, blocking | `async def`, awaited, 3s timeout |
| Slow AI call | waits forever | 504 after 3s, task cancelled |
| Upstream failure | traceback / 500 | 503 with `Retry-After` |
| Three outputs | not available | `/study-pack`, 3 calls in 0.6s not 1.8s |
| Partial failure | all-or-nothing | per-part status, successes still returned |
| Long jobs | client waits | `202` + poll, response in 4.5ms |
| Long answers | wait for the whole thing | streamed, first word at 0.18s |
