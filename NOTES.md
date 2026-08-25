# Notes — endpoints, timings, and the async reasoning

## Measured numbers

| | Measured |
|---|---|
| Three tasks, sequential | **1.801s** |
| The same three with `asyncio.gather` | **0.601s** (3.0× faster) |
| Slow AI call (10s) against a 3s timeout | **504 after 3.00s**, not 10 |
| Background job: 202 response vs 2s of work | **4.5ms** |
| Streaming: first word vs full answer | **0.18s** vs 4.54s |

## Endpoints

| Method | Path | Async? | Shows |
|---|---|---|---|
| GET | `/health` | `def` | Sync on purpose — it awaits nothing |
| POST | `/summarise` | `async` | One awaited AI call with a 3s timeout |
| POST | `/study-pack` | `async` | Three calls at once via `asyncio.gather` |
| POST | `/study-pack/sequential` | `async` | The same work one at a time, for contrast |
| GET | `/study-pack/compare` | `async` | Both timings measured in one request |
| POST | `/chat/stream` | `async` | Words sent as they are produced (SSE) |
| POST | `/jobs/summarise` | `async` | `202` now, result later |
| GET | `/jobs/{job_id}` | `async` | Poll a background job |

## The interesting paths

```bash
# Normal call
curl -X POST localhost:8000/summarise -H 'Content-Type: application/json' \
  -d '{"text":"Python is a high-level programming language. It is known for readable syntax. It is widely used for automation.","max_bullets":2}'

# Force the timeout: the AI is told to take 10s, the ceiling is 3s → 504 in 3s
curl -X POST localhost:8000/summarise -H 'Content-Type: application/json' \
  -d '{"text":"Python is a high-level programming language. It is known for readable syntax.","simulate_delay_seconds":10}'

# Force a temporary upstream failure → 503 with Retry-After
curl -X POST localhost:8000/summarise -H 'Content-Type: application/json' \
  -d '{"text":"Python is a high-level programming language. It is known for readable syntax.","simulate_failure":true}'

# Sequential vs concurrent, measured
curl 'localhost:8000/study-pack/compare?delay_seconds=0.6'

# Partial failure: the quiz fails, summary and flashcards still return
curl -X POST localhost:8000/study-pack -H 'Content-Type: application/json' \
  -d '{"text":"Python is a high-level programming language. It is known for readable syntax.","fail_part":"quiz"}'

# Streaming — the -N matters, otherwise curl buffers and it looks instant
curl -N -X POST localhost:8000/chat/stream -H 'Content-Type: application/json' \
  -d '{"question":"What does async do?"}'
```

`simulate_delay_seconds`, `simulate_failure` and `fail_part` are deliberate demo controls so the
error paths can be triggered on purpose. A production build would remove them or gate them behind
a debug flag.

## Why `/health` is not async

FastAPI runs the two kinds of handler in different places:

| | Runs on | Blocking work inside |
|---|---|---|
| `def` | a worker thread | slows only that request |
| `async def` | the event loop | **stalls every other request** |

So `async def` without an `await` is worse than sync — it moves blocking work onto the shared
thread. The brief says to convert an endpoint *only if* it awaits something, and a test asserts
`/health` stays a plain `def`.

## Sequential vs concurrent

```
sequential:  [── summary 0.6s ──][── quiz 0.6s ──][── flashcards 0.6s ──]  = 1.8s

concurrent:  [── summary 0.6s ──]
             [── quiz 0.6s ─────]   started together                       = 0.6s
             [── flashcards 0.6s]
```

Each task spends its time *waiting*, and a waiting coroutine uses no CPU, so the event loop starts
the others and resumes whichever finishes first.

The gain comes from **starting them together**, not from `async` itself — `/study-pack/sequential`
is also `async def` with `await` and is still 3× slower. `await` marks a suspension point;
`gather` creates the overlap.

**When async does not help:** CPU-bound work. Three functions that need the CPU rather than a
network reply would just take turns on one core. Those need `ProcessPoolExecutor`, or
`run_in_threadpool` to at least keep them off the event loop. Async helps when your code is
*waiting*, not when it is *working*.

## Error handling

| Situation | Status | Behaviour |
|---|---|---|
| Timeout | 504 | Names the limit; the coroutine is cancelled, not abandoned |
| Temporary upstream failure | 503 | `Retry-After: 5` |
| Unexpected | 500 | Exception *type* shown, message withheld |
| Invalid body | 422 | Field-by-field detail |

The 500 path shows the exception type but never its message — a test patches the service to raise
`RuntimeError("secret internal detail...")` and asserts the type appears in the response while the
message does not. Internal messages leak paths, queries and connection strings.

`/study-pack` uses `gather(..., return_exceptions=True)`, so one failing task does not discard the
two that succeeded. Each result carries its own status in the `parts` array.
