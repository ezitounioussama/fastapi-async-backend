"""Request and response models.

Several requests carry `simulate_delay_seconds` and `simulate_failure`. Those are
deliberate demo controls: they let a grader trigger a timeout or an upstream
failure on purpose, and let the tests assert those paths deterministically
instead of hoping a real service misbehaves at the right moment. In a production
build they would be removed or restricted to a debug environment.
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from app import config

SAMPLE_TEXT = (
    "Python is a high-level programming language. It is known for readable syntax. "
    "Many beginners start with Python because the code looks close to plain English. "
    "It is widely used for web development, data analysis and automation."
)


class SimulationControls(BaseModel):
    """Shared demo knobs for forcing slow or failing AI calls."""

    simulate_delay_seconds: Optional[float] = Field(
        default=None,
        ge=0,
        le=config.MAX_SIMULATED_DELAY,
        description=(
            "Override how long the fake AI call takes. Set this above the "
            "endpoint's timeout to see the timeout path."
        ),
    )
    simulate_failure: bool = Field(
        default=False,
        description="Force the AI call to raise a temporary upstream error.",
    )


# ---------------------------------------------------------------------------
# /summarise
# ---------------------------------------------------------------------------


class SummariseRequest(SimulationControls):
    text: str = Field(
        min_length=config.TEXT_MIN,
        max_length=config.TEXT_MAX,
        examples=[SAMPLE_TEXT],
    )
    max_bullets: int = Field(
        default=3, ge=config.BULLETS_MIN, le=config.BULLETS_MAX, examples=[3]
    )


class SummariseResponse(BaseModel):
    bullets: List[str]
    bullet_count: int
    original_characters: int
    elapsed_seconds: float = Field(description="How long the awaited AI call took.")
    timestamp: datetime


# ---------------------------------------------------------------------------
# /study-pack
# ---------------------------------------------------------------------------


class StudyPackRequest(SimulationControls):
    text: str = Field(
        min_length=config.TEXT_MIN,
        max_length=config.TEXT_MAX,
        examples=[SAMPLE_TEXT],
    )
    max_bullets: int = Field(default=3, ge=config.BULLETS_MIN, le=config.BULLETS_MAX)
    num_questions: int = Field(default=3, ge=config.QUESTIONS_MIN, le=config.QUESTIONS_MAX)
    num_cards: int = Field(default=3, ge=config.CARDS_MIN, le=config.CARDS_MAX)

    fail_part: Optional[Literal["summary", "quiz", "flashcards"]] = Field(
        default=None,
        description=(
            "Force just one of the three tasks to fail, to show that the other "
            "two still return."
        ),
    )


class QuizQuestion(BaseModel):
    number: int
    question: str
    options: List[str]
    answer_index: int = Field(ge=0, le=3)


class Flashcard(BaseModel):
    number: int
    front: str
    back: str


class PartStatus(BaseModel):
    """Per-task outcome, so one failure does not hide the successes."""

    name: Literal["summary", "quiz", "flashcards"]
    status: Literal["ok", "failed", "timeout"]
    detail: Optional[str] = None


class StudyPackResponse(BaseModel):
    summary: Optional[List[str]] = None
    quiz: Optional[List[QuizQuestion]] = None
    flashcards: Optional[List[Flashcard]] = None

    parts: List[PartStatus] = Field(description="Outcome of each of the three tasks.")
    mode: Literal["concurrent", "sequential"] = Field(
        description="How the three tasks were run."
    )
    elapsed_seconds: float = Field(description="Wall-clock time for all three tasks.")
    timestamp: datetime


class ComparisonResponse(BaseModel):
    """Sequential and concurrent runs of the same work, measured side by side."""

    per_task_delay_seconds: float
    task_count: int
    sequential_seconds: float = Field(description="Awaited one after another.")
    concurrent_seconds: float = Field(description="Awaited together with asyncio.gather.")
    speedup: float = Field(description="sequential ÷ concurrent.")
    time_saved_seconds: float
    explanation: str


# ---------------------------------------------------------------------------
# Streaming and background work
# ---------------------------------------------------------------------------


class StreamRequest(BaseModel):
    question: str = Field(
        min_length=config.QUESTION_MIN,
        max_length=config.QUESTION_MAX,
        examples=["What does async do?"],
    )


class JobStartedResponse(BaseModel):
    job_id: str
    status: Literal["queued"]
    poll_url: str
    note: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "done", "failed"]
    created_at: datetime
    finished_at: Optional[datetime] = None
    result: Optional[List[str]] = None
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Health and errors
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    ai_timeout_seconds: float
    default_ai_delay_seconds: float
    timestamp: datetime


class ErrorResponse(BaseModel):
    error: str
    detail: str
    retryable: bool = Field(description="True when trying again may succeed.")
