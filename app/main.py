"""Application entry point."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import config
from app.routers import extras, health, study_pack, summarise

DESCRIPTION = """
An AI backend where the slow calls are awaited rather than blocking.

**What to try**

| Endpoint | Shows |
|---|---|
| `POST /summarise` | one awaited AI call, with a timeout |
| `POST /summarise` with `simulate_delay_seconds: 10` | the 504 timeout path |
| `POST /summarise` with `simulate_failure: true` | the 503 upstream-failure path |
| `POST /study-pack` | three calls at once with `asyncio.gather` |
| `POST /study-pack/sequential` | the same work one at a time, for contrast |
| `GET /study-pack/compare` | both timings measured side by side |
| `POST /chat/stream` | words sent as they are produced |
| `POST /jobs/summarise` | 202 now, result later |

There is no API key: the AI layer is a fake async service that awaits
`asyncio.sleep` to imitate real latency, which makes timeouts and failures
reproducible.
"""

app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, description=DESCRIPTION)

app.include_router(health.router)
app.include_router(summarise.router)
app.include_router(study_pack.router)
app.include_router(extras.router)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, error: RequestValidationError):
    """Validation failures as an object, matching the other error shapes."""
    fields = ", ".join(".".join(str(part) for part in item["loc"]) for item in error.errors())

    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "detail": f"The request was rejected. Check: {fields or 'request body'}.",
            "retryable": False,
        },
    )


@app.get("/", tags=["health"], summary="API index")
def read_root() -> dict:
    return {
        "name": config.APP_NAME,
        "version": config.APP_VERSION,
        "docs": "/docs",
        "async_endpoints": [
            "/summarise",
            "/study-pack",
            "/study-pack/sequential",
            "/study-pack/compare",
            "/chat/stream",
            "/jobs/summarise",
        ],
        "sync_endpoints": ["/", "/health"],
    }
