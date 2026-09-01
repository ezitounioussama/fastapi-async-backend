# AI Study Assistant API — Async Edition

The same study-assistant backend, rewritten so slow AI calls are awaited instead of blocking. A
request that needs a summary, a quiz and flashcards fires all three at once with `asyncio.gather`
and finishes in 0.6s instead of 1.8s; a call that hangs is cut off by a timeout and returns 504
after 3s rather than 10; a long job can answer `202` in 4.5ms and finish in the background.

There is no API key. The AI layer is a fake async service that awaits `asyncio.sleep` to imitate
real latency — which is what makes timeouts and partial failures reproducible instead of dependent
on a real service misbehaving at the right moment.

```bash
uv venv
uv pip install -r requirements.txt

uv run python main.py     # http://127.0.0.1:8000/docs
uv run pytest -q          # 37 passed
```

## Also in this repo

- **[NOTES.md](NOTES.md)** — the endpoint list, curl commands for every error path, the measured
  timings, and why `/health` is deliberately *not* async
- **[docs/async-upgrade-report.md](docs/async-upgrade-report.md)** — before/after code, the async
  flow diagram, the timeout test in full, and one testing pitfall worth reading: a background-task
  test that failed for a reason that turned out not to be a bug

---

Author: **Oussama Ezitouni**
